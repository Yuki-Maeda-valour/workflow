//! 旧 bash の出力の再現。bash 5.2.21 は stdout・stderr を行ごとに書き出す(`sh_setlinebuf`)。
//! glibc 2.39 の stdio(`_IO_new_file_xsputn`・`_IO_new_file_overflow`・`new_do_write`)と同じ規則で、
//! 溜め場所に溜め、改行と満杯で `write` する。`write` の失敗(`EINTR` を含む)で、その時の溜めた物は
//! 捨てられ、誤りの印が立つ。印は bash の `echo`・`printf` が確かめる。

use dev_workflow_platform::implement_guard as os;
use std::io;

/// 書き込み先(検査では偽に差し替える)。
pub trait Io {
    /// 1 回の `write`。
    fn write(&mut self, fd: i32, data: &[u8]) -> io::Result<usize>;
    /// その fd の `st_blksize`。
    fn blksize(&mut self, fd: i32) -> Option<i64>;
}

/// 本物の fd へ書く。
pub struct RealIo;

impl Io for RealIo {
    fn write(&mut self, fd: i32, data: &[u8]) -> io::Result<usize> {
        match os::borrow_fd(fd) {
            Some(borrowed) => os::write_once(borrowed, data),
            None => Err(io::Error::from_raw_os_error(os::errno::EBADF)),
        }
    }

    fn blksize(&mut self, fd: i32) -> Option<i64> {
        os::fd_blksize(fd)
    }
}

/// glibc の `BUFSIZ`。
const BUFSIZ: usize = 8192;
/// glibc 2.39 の `PRINTF_BUFFER_SIZE_TO_FILE_STAGE`。
const STAGE: usize = 128;

/// 行ごとに書き出す 1 つの FILE(bash の stdout か stderr)。
#[derive(Debug, Default)]
pub struct Stream {
    buffer: Vec<u8>,
    /// 溜め場所の大きさ(0 は未確保)。
    capacity: usize,
    /// `_IO_CURRENTLY_PUTTING`。
    putting: bool,
    /// `ferror`。
    pub error: bool,
}

/// 書き込みの結果を受ける側(誤りの番号を覚える)。
pub struct Writer<'a> {
    pub io: &'a mut dyn Io,
    /// 最後に失敗した `write` の番号(bash の `errno`)。
    pub errno: &'a mut i32,
}

impl Writer<'_> {
    /// `_IO_new_file_write`: 全部書けるか失敗するまで書く。書けたバイト数を返す。
    fn write_all(&mut self, fd: i32, data: &[u8]) -> (usize, bool) {
        let mut done = 0usize;
        while done < data.len() {
            match self.io.write(fd, data.get(done..).unwrap_or_default()) {
                Ok(0) => {
                    // write は 0 を返さない(返ったら同じ所でまた書く。glibc と同じ)。
                    continue;
                }
                Ok(n) => done += n,
                Err(error) => {
                    *self.errno = error.raw_os_error().unwrap_or(os::errno::EIO);
                    return (done, false);
                }
            }
        }
        (done, true)
    }
}

impl Stream {
    /// `_IO_file_doallocate`: `st_blksize` が 0 より大きく `BUFSIZ` より小さければその値。
    fn allocate(&mut self, writer: &mut Writer<'_>, fd: i32) {
        if self.capacity == 0 {
            self.capacity = match writer.io.blksize(fd) {
                Some(size) if size > 0 && (size as u64) < BUFSIZ as u64 => size as usize,
                _ => BUFSIZ,
            };
        }
    }

    /// `new_do_write` と `_IO_do_write`: 溜めた物を書き、成否にかかわらず溜め場所を空にする。
    fn do_write(&mut self, writer: &mut Writer<'_>, fd: i32) -> bool {
        if self.buffer.is_empty() {
            return true;
        }
        let data = std::mem::take(&mut self.buffer);
        let (_, ok) = writer.write_all(fd, &data);
        if !ok {
            self.error = true;
        }
        ok
    }

    /// `_IO_new_file_overflow(f, EOF)`(溜めた物を書く)か、1 文字を足す。
    fn overflow(&mut self, writer: &mut Writer<'_>, fd: i32, ch: Option<u8>) -> bool {
        if !self.putting {
            self.allocate(writer, fd);
            self.putting = true;
        }
        let Some(ch) = ch else {
            return self.do_write(writer, fd);
        };
        if self.buffer.len() >= self.capacity && !self.do_write(writer, fd) {
            return false;
        }
        self.buffer.push(ch);
        if ch == b'\n' {
            return self.do_write(writer, fd);
        }
        true
    }

    /// `putc`(行ごとの書き出しでは、いつも `__overflow` を通る)。
    pub fn putc(&mut self, writer: &mut Writer<'_>, fd: i32, ch: u8) -> bool {
        self.overflow(writer, fd, Some(ch))
    }

    /// `_IO_new_file_xsputn`(`fputs`・`printf("%s")`)。書けたバイト数を返す。
    pub fn xsputn(&mut self, writer: &mut Writer<'_>, fd: i32, data: &[u8]) -> usize {
        let n = data.len();
        if n == 0 {
            return 0;
        }
        let mut rest = data;
        let mut must_flush = false;
        let mut count = 0usize;
        if self.putting {
            count = self.capacity.saturating_sub(self.buffer.len());
            if count >= n
                && let Some(last) = rest.iter().rposition(|b| *b == b'\n')
            {
                count = last + 1;
                must_flush = true;
            }
        }
        if count > 0 {
            let take = count.min(rest.len());
            self.buffer
                .extend_from_slice(rest.get(..take).unwrap_or_default());
            rest = rest.get(take..).unwrap_or_default();
        }
        if !rest.is_empty() || must_flush {
            if !self.overflow(writer, fd, None) {
                return n - rest.len();
            }
            let block = self.capacity;
            let whole = if block >= 128 {
                rest.len() - rest.len() % block
            } else {
                rest.len()
            };
            if whole > 0 {
                let (written, ok) = writer.write_all(fd, rest.get(..whole).unwrap_or_default());
                if !ok {
                    self.error = true;
                }
                if written < whole {
                    return n - (rest.len() - written);
                }
                rest = rest.get(whole..).unwrap_or_default();
            }
            // `_IO_default_xsputn`: 行ごとの書き出しでは 1 文字ずつ `__overflow` を通る。
            while let Some((&ch, tail)) = rest.split_first() {
                if !self.overflow(writer, fd, Some(ch)) {
                    return n - rest.len();
                }
                rest = tail;
            }
        }
        n - rest.len()
    }

    /// glibc 2.39 の `vfprintf`(`printf("%s")`・`fprintf`)。行ごとの書き出しの FILE には溜め場所の
    /// 空きが無い扱いなので、128 バイトの中継の溜め場所を通して `_IO_sputn` する。
    pub fn vfprintf(&mut self, writer: &mut Writer<'_>, fd: i32, data: &[u8]) -> usize {
        let mut written = 0usize;
        for piece in data.chunks(STAGE) {
            let n = self.xsputn(writer, fd, piece);
            written += n;
            if n < piece.len() {
                break;
            }
        }
        written
    }

    /// `fflush`。
    pub fn flush(&mut self, writer: &mut Writer<'_>, fd: i32) -> bool {
        if !self.putting {
            return true;
        }
        self.do_write(writer, fd)
    }

    /// `fpurge`。
    pub fn purge(&mut self) {
        self.buffer.clear();
    }

    /// `clearerr`。
    pub fn clear_error(&mut self) {
        self.error = false;
    }
}
