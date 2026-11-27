//! 応答の形(`response` :510-523・`path_field` :36-45)と、書き出し(Python の
//! `sys.stdout.buffer` と終了時の flush)の再現。
use dev_workflow_platform::restore_taskmd as os;
use std::io;
use std::os::fd::AsFd;

/// 引数の誤りの応答(`main` :610)。
pub const ARGUMENT_RESPONSE: &[u8] =
    b"RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=internal-argument\n";

/// 既存 shell と同じ C 風引用。非 UTF-8 のバイトもそのまま保つ。
pub fn path_field(raw: &[u8]) -> Vec<u8> {
    if !raw
        .iter()
        .any(|&c| c < 32 || c == 127 || c == 34 || c == 92)
    {
        return raw.to_vec();
    }
    let mut out = vec![b'"'];
    for &c in raw {
        match c {
            7 => out.extend_from_slice(b"\\a"),
            8 => out.extend_from_slice(b"\\b"),
            9 => out.extend_from_slice(b"\\t"),
            10 => out.extend_from_slice(b"\\n"),
            11 => out.extend_from_slice(b"\\v"),
            12 => out.extend_from_slice(b"\\f"),
            13 => out.extend_from_slice(b"\\r"),
            34 => out.extend_from_slice(b"\\\""),
            92 => out.extend_from_slice(b"\\\\"),
            c if c < 32 || c == 127 => out.extend_from_slice(format!("\\{c:03o}").as_bytes()),
            c => out.push(c),
        }
    }
    out.push(b'"');
    out
}

/// 1 回の書き込み(Python の `FileIO.write`)。途中まで書けたら書けた数を返す。
pub trait RawWrite {
    fn write_once(&mut self, data: &[u8]) -> io::Result<usize>;
}

/// 標準出力の fd 1 へ直接書く。`EINTR` は再試行する(`_Py_write`)。
pub struct Stdout;

impl RawWrite for Stdout {
    fn write_once(&mut self, data: &[u8]) -> io::Result<usize> {
        let stdout = io::stdout();
        loop {
            match os::write(stdout.as_fd(), data) {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                other => return other,
            }
        }
    }
}

/// Python の `BufferedWriter` と同じ溜め方をする出力。
pub struct Output<W: RawWrite> {
    raw: W,
    buffer: Vec<u8>,
    size: usize,
}

/// 溜め場所の大きさ。stdout の `st_blksize` が 1 より大きければその値、そうでなければ 8192。
pub fn stdout_buffer_size() -> usize {
    match os::fstat(io::stdout().as_fd()) {
        Ok(info) if info.blksize > 1 => usize::try_from(info.blksize).unwrap_or(8192),
        _ => 8192,
    }
}

impl Output<Stdout> {
    pub fn stdout() -> Self {
        Self::new(Stdout, stdout_buffer_size())
    }
}

impl<W: RawWrite> Output<W> {
    pub fn new(raw: W, size: usize) -> Self {
        Self {
            raw,
            buffer: Vec::new(),
            size,
        }
    }

    /// `BufferedWriter.write`。L が空きに収まれば溜める。収まらなければ溜めた物を書いてから、
    /// 残りが B 以下になるまで直接書き、残りを溜める。
    pub fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        let avail = self.size.saturating_sub(self.buffer.len());
        if data.len() <= avail {
            self.buffer.extend_from_slice(data);
            return Ok(data.len());
        }
        self.flush()?;
        let mut written = 0usize;
        while data.len() - written > self.size {
            let n = self
                .raw
                .write_once(data.get(written..).unwrap_or_default())?;
            written += n.min(data.len() - written);
        }
        self.buffer
            .extend_from_slice(data.get(written..).unwrap_or_default());
        Ok(data.len())
    }

    /// `BufferedWriter.flush`。溜めた物を全部書けるか失敗するまで書く(書けた分は除く)。
    pub fn flush(&mut self) -> io::Result<()> {
        while !self.buffer.is_empty() {
            let n = self.raw.write_once(&self.buffer)?;
            self.buffer.drain(..n.min(self.buffer.len()));
        }
        Ok(())
    }
}

/// Python が書き出しの失敗で stderr へ出す 2 行(`Exception ignored in: …` と例外の行)。
pub fn unraisable_lines(error: &io::Error) -> [Vec<u8>; 2] {
    let first =
        b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"
            .to_vec();
    let number = error.raw_os_error().unwrap_or(0);
    let (kind, text) = describe_errno(number);
    let second = format!("{kind}: [Errno {number}] {text}\n");
    [first, second.into_bytes()]
}

/// errno から Python の型と C ライブラリの文(S3I-A #474 で `errno_text` へ広げた。Linux は glibc 2.39 の
/// 生成した表、Mac は libSystem の `strerror_r`)。
fn describe_errno(number: i32) -> (&'static str, String) {
    (
        super::errno_text::exception_type(number),
        super::errno_text::strerror(number),
    )
}

/// stderr へ 1 行ずつ書く(Python の stderr は行ごとに書く)。失敗は無視する。
pub fn write_stderr_lines(lines: &[Vec<u8>]) {
    let stderr = io::stderr();
    for line in lines {
        let mut rest: &[u8] = line;
        while !rest.is_empty() {
            match os::write(stderr.as_fd(), rest) {
                Ok(0) => break,
                Ok(n) => rest = rest.get(n..).unwrap_or_default(),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_field_matches_python_escapes() {
        assert_eq!(path_field(b"/plain/\xff"), b"/plain/\xff");
        assert_eq!(
            path_field(b"/a\n\"\\\x01\x7f\x07\x08\t\x0b\x0c\r\xfe"),
            b"\"/a\\n\\\"\\\\\\001\\177\\a\\b\\t\\v\\f\\r\xfe\""
        );
    }

    #[test]
    fn unraisable_lines_match_the_fixed_table() {
        let lines = unraisable_lines(&io::Error::from_raw_os_error(os::errno::ENOSPC));
        assert_eq!(
            lines[0],
            b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n"
        );
        assert_eq!(
            lines[1],
            format!(
                "OSError: [Errno {}] No space left on device\n",
                os::errno::ENOSPC
            )
            .as_bytes()
        );
        let lines = unraisable_lines(&io::Error::from_raw_os_error(os::errno::EPIPE));
        assert_eq!(
            lines[1],
            format!(
                "BrokenPipeError: [Errno {}] Broken pipe\n",
                os::errno::EPIPE
            )
            .as_bytes()
        );
    }
}
