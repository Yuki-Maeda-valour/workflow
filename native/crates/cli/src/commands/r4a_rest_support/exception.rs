//! Python の未処理例外の表示(traceback)と OSError の対応。
//!
//! 表示に出る旧版の実行環境のパスは固定のファイル名にする(設計の承認差)。
//! 行番号・関数名・ソースの行・例外の種類と本文は旧版のまま。

use super::chars::{self, PyStr};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Frame {
    pub file: &'static str,
    pub line: u32,
    pub func: &'static str,
    /// 空白を除いたソースの 1 行。`<string>` などでは `None`。
    pub source: Option<&'static str>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PyException {
    pub kind: String,
    pub message: PyStr,
    /// 外側(先に呼ばれた)から内側の順。
    pub frames: Vec<Frame>,
    /// `During handling of the above exception, another exception occurred`。
    pub context: Option<Box<PyException>>,
    /// `repr(exc)` の括弧の中。`None` なら `repr(message)` の 1 引数の形。
    pub repr_args: Option<PyStr>,
}

impl PyException {
    pub fn new(kind: &str, message: &str, frames: &[Frame]) -> Self {
        Self {
            kind: kind.to_owned(),
            message: chars::from_str(message),
            frames: frames.to_vec(),
            context: None,
            repr_args: None,
        }
    }

    pub fn with_context(mut self, context: Option<PyException>) -> Self {
        self.context = context.map(Box::new);
        self
    }

    /// `repr(exc)`(`補助の失敗: ` の本文に使う。引数 1 個の例外の形)。
    pub fn repr(&self) -> PyStr {
        // `repr` は、モジュール名を除いたクラス名(`JSONDecodeError(...)`)。
        let mut out = chars::from_str(self.kind.rsplit('.').next().unwrap_or(&self.kind));
        out.push(u32::from(b'('));
        match &self.repr_args {
            Some(args) => out.extend_from_slice(args),
            None => out.extend(chars::repr(&self.message)),
        }
        out.push(u32::from(b')'));
        out
    }

    /// stderr へ 1 回ずつ書く断片の列。CPython の `PyTraceBack_Print` の区切りに合わせる。
    pub fn render(&self) -> Vec<PyStr> {
        let mut pieces = Vec::new();
        self.render_into(&mut pieces);
        pieces
    }

    fn render_into(&self, pieces: &mut Vec<PyStr>) {
        if let Some(context) = &self.context {
            context.render_into(pieces);
            pieces.push(chars::from_str(
                "\nDuring handling of the above exception, another exception occurred:\n\n",
            ));
        }
        pieces.push(chars::from_str("Traceback (most recent call last):\n"));
        for frame in &self.frames {
            pieces.push(chars::from_str(&format!(
                "  File \"{}\", line {}, in {}\n",
                frame.file, frame.line, frame.func
            )));
            if let Some(source) = frame.source {
                pieces.push(chars::from_str("    "));
                pieces.push(chars::from_str(source));
                pieces.push(chars::from_str("\n"));
            }
        }
        self.push_exception_line(pieces);
    }

    /// 例外の行。`str(KeyError(x))` は `repr(x)` になる。
    fn push_exception_line(&self, pieces: &mut Vec<PyStr>) {
        pieces.push(chars::from_str(&self.kind));
        if !self.message.is_empty() {
            pieces.push(chars::from_str(": "));
            pieces.push(if self.kind == "KeyError" {
                chars::repr(&self.message)
            } else {
                self.message.clone()
            });
        }
        pieces.push(chars::from_str("\n"));
    }

    /// `PyErr_WriteUnraisable` が書く断片。
    pub fn render_unraisable(&self, object_repr: &str) -> Vec<PyStr> {
        let mut pieces = vec![chars::from_str(&format!(
            "Exception ignored in: {object_repr}\n"
        ))];
        self.push_exception_line(&mut pieces);
        pieces
    }
}

/// OS ごとの errno(Python の `errnomap` の対象)。Linux と macOS で値が違うものは、OS ごとの定数で書く。
mod errno {
    pub const EPERM: i32 = 1;
    pub const ENOENT: i32 = 2;
    pub const ESRCH: i32 = 3;
    pub const EINTR: i32 = 4;
    pub const EBADF: i32 = 9;
    pub const ECHILD: i32 = 10;
    pub const EACCES: i32 = 13;
    pub const EEXIST: i32 = 17;
    pub const ENOTDIR: i32 = 20;
    pub const EISDIR: i32 = 21;
    pub const EPIPE: i32 = 32;
    #[cfg(target_os = "linux")]
    pub const EAGAIN: i32 = 11;
    #[cfg(target_os = "linux")]
    pub const EALREADY: i32 = 114;
    #[cfg(target_os = "linux")]
    pub const EINPROGRESS: i32 = 115;
    #[cfg(target_os = "linux")]
    pub const ESHUTDOWN: i32 = 108;
    #[cfg(target_os = "linux")]
    pub const ECONNABORTED: i32 = 103;
    #[cfg(target_os = "linux")]
    pub const ECONNREFUSED: i32 = 111;
    #[cfg(target_os = "linux")]
    pub const ECONNRESET: i32 = 104;
    #[cfg(target_os = "linux")]
    pub const ETIMEDOUT: i32 = 110;
    #[cfg(target_os = "linux")]
    pub const ELOOP: i32 = 40;
    #[cfg(target_os = "macos")]
    pub const EAGAIN: i32 = 35;
    #[cfg(target_os = "macos")]
    pub const EALREADY: i32 = 37;
    #[cfg(target_os = "macos")]
    pub const EINPROGRESS: i32 = 36;
    #[cfg(target_os = "macos")]
    pub const ESHUTDOWN: i32 = 58;
    #[cfg(target_os = "macos")]
    pub const ECONNABORTED: i32 = 53;
    #[cfg(target_os = "macos")]
    pub const ECONNREFUSED: i32 = 61;
    #[cfg(target_os = "macos")]
    pub const ECONNRESET: i32 = 54;
    #[cfg(target_os = "macos")]
    pub const ETIMEDOUT: i32 = 60;
    #[cfg(target_os = "macos")]
    pub const ELOOP: i32 = 62;
}

pub use errno::{EBADF, ELOOP, ENOENT, ENOTDIR};

/// errno に対応する Python の OSError の種類名(`errnomap` と同じ集合)。
pub fn os_error_kind(code: i32) -> &'static str {
    use errno::*;
    match code {
        EEXIST => "FileExistsError",
        EAGAIN | EALREADY | EINPROGRESS => "BlockingIOError",
        ECHILD => "ChildProcessError",
        EPIPE | ESHUTDOWN => "BrokenPipeError",
        ECONNABORTED => "ConnectionAbortedError",
        ECONNREFUSED => "ConnectionRefusedError",
        ECONNRESET => "ConnectionResetError",
        ENOENT => "FileNotFoundError",
        EISDIR => "IsADirectoryError",
        ENOTDIR => "NotADirectoryError",
        EINTR => "InterruptedError",
        EACCES | EPERM => "PermissionError",
        ESRCH => "ProcessLookupError",
        ETIMEDOUT => "TimeoutError",
        _ => "OSError",
    }
}

#[cfg(target_os = "linux")]
mod glibc {
    include!("unicode/errno_linux.rs");
}

/// `strerror(errno)` の本文。Linux は旧 Python が使う glibc の表(musl の本文と違う errno がある)。
/// ほかの OS(Mac を除く)は、旧 Python と同じ OS の `strerror`(Rust の表示から ` (os error N)` を除く)。
#[cfg(target_os = "linux")]
pub fn strerror(errno: i32) -> String {
    match usize::try_from(errno)
        .ok()
        .and_then(|n| glibc::GLIBC_STRERROR.get(n))
    {
        Some(text) => (*text).to_owned(),
        None => format!("Unknown error {errno}"),
    }
}

#[cfg(target_os = "macos")]
mod apple {
    include!("unicode/errno_macos.rs");
}

/// macOS は Apple Libc(`Libc-1725.40.4`、CI の macos-15 の版)の表を使い、OS の `strerror` は使わない。
/// 範囲の外は `Unknown error: N`。
#[cfg(target_os = "macos")]
pub fn strerror(errno: i32) -> String {
    match usize::try_from(errno)
        .ok()
        .and_then(|n| apple::APPLE_STRERROR.get(n))
    {
        Some(text) => (*text).to_owned(),
        None => format!("Unknown error: {errno}"),
    }
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn strerror(errno: i32) -> String {
    let text = std::io::Error::from_raw_os_error(errno).to_string();
    match text.rfind(" (os error ") {
        Some(at) => text[..at].to_owned(),
        None => text,
    }
}

/// `OSError(errno, strerror)` の例外(`[Errno N] 本文`)。
pub fn os_error(errno: i32, frames: &[Frame]) -> PyException {
    let mut exception = PyException::new(
        os_error_kind(errno),
        &format!("[Errno {errno}] {}", strerror(errno)),
        frames,
    );
    exception.repr_args = Some(chars::from_str(&format!(
        "{errno}, {}",
        String::from_utf8_lossy(
            &chars::to_utf8_strict(&chars::repr(&chars::from_str(&strerror(errno))))
                .unwrap_or_default()
        )
    )));
    exception
}

#[cfg(test)]
mod tests {
    use super::*;

    const MODULE: Frame = Frame {
        file: "resolve-task-store.py",
        line: 41,
        func: "<module>",
        source: Some("sys.exit(main())"),
    };

    fn flat(pieces: &[PyStr]) -> String {
        pieces
            .iter()
            .flat_map(|p| p.iter().map(|&c| char::from_u32(c).unwrap_or('?')))
            .collect()
    }

    #[test]
    fn traceback_pieces_and_context() {
        let inner = PyException::new("OSError", "[Errno 28] No space left on device", &[MODULE]);
        assert_eq!(
            flat(&inner.render()),
            "Traceback (most recent call last):\n  File \"resolve-task-store.py\", line 41, in <module>\n    sys.exit(main())\nOSError: [Errno 28] No space left on device\n"
        );
        let outer = PyException::new("TypeError", "x", &[]).with_context(Some(inner));
        assert!(flat(&outer.render()).contains(
            "\nDuring handling of the above exception, another exception occurred:\n\nTraceback"
        ));
        assert_eq!(
            os_error(28, &[]).message,
            chars::from_str("[Errno 28] No space left on device")
        );
        assert_eq!(os_error_kind(32), "BrokenPipeError");
        // musl では本文が違う errno も、旧 Python(glibc)の本文になる。
        #[cfg(target_os = "linux")]
        {
            assert_eq!(strerror(40), "Too many levels of symbolic links");
            assert_eq!(strerror(9999), "Unknown error 9999");
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn apple_table_covers_the_whole_range() {
        assert_eq!(strerror(0), "Undefined error: 0");
        assert_eq!(
            strerror(47),
            "Address family not supported by protocol family"
        );
        assert_eq!(strerror(107), "Capabilities insufficient");
        assert_eq!(strerror(108), "Unknown error: 108");
        assert_eq!(strerror(-1), "Unknown error: -1");
    }

    #[test]
    fn repr_matches_python() {
        let e = PyException::new("OSError", "[Errno 28] No space left on device", &[]);
        assert_eq!(
            flat(&[e.repr()]),
            "OSError('[Errno 28] No space left on device')"
        );
        assert_eq!(
            flat(&[os_error(28, &[]).repr()]),
            "OSError(28, 'No space left on device')"
        );
    }
}
