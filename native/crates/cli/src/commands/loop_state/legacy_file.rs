// CPython 3.10 Modules/_io/textio.c と bufferedio.c の書込みの順の写し(write_state 用)。
// CPYTHON-LICENSE を参照。Python の処理系は起動しない。
//! `os.fdopen(fd, "w", encoding="utf-8")` の TextIOWrapper(8192 の区切り)と、
//! `st_blksize` から大きさを決める BufferedWriter の、系呼出しの順と失敗の位置を保つ。
use dev_workflow_platform::loop_state::StateWriter;
use std::io;

const CHUNK_SIZE: usize = 8192;
const DEFAULT_BUFFER_SIZE: usize = 8192;

/// 旧 BufferedWriter の位置(bufferedio.c の pos・raw_pos・write_pos・write_end)。
struct Buffered {
    buffer: Vec<u8>,
    pos: i64,
    raw_pos: i64,
    write_pos: i64,
    /// -1 は書込みの区間が無いこと(VALID_WRITE_BUFFER が偽)。
    write_end: i64,
}
impl Buffered {
    fn valid_write(&self) -> bool {
        self.write_end != -1
    }
    fn reset(&mut self) {
        self.write_pos = 0;
        self.write_end = -1;
    }
    fn raw_offset(&self) -> i64 {
        if self.valid_write() && self.raw_pos >= 0 {
            self.raw_pos - self.pos
        } else {
            0
        }
    }
    fn flush(&mut self, raw: &mut StateWriter) -> io::Result<()> {
        if !self.valid_write() || self.write_pos == self.write_end {
            self.reset();
            return Ok(());
        }
        // 書込みだけの流れでは巻戻しは起きない(旧版でも 0 になる)。
        debug_assert_eq!(self.raw_offset() + (self.pos - self.write_pos), 0);
        while self.write_pos < self.write_end {
            let n =
                raw.write_once(&self.buffer[self.write_pos as usize..self.write_end as usize])?;
            self.write_pos += n as i64;
            self.raw_pos = self.write_pos;
            self.pos = self.write_pos;
        }
        self.reset();
        Ok(())
    }
    fn write(&mut self, raw: &mut StateWriter, data: &[u8]) -> io::Result<()> {
        let size = self.buffer.len() as i64;
        if !self.valid_write() {
            self.pos = 0;
            self.raw_pos = 0;
        }
        let avail = size - self.pos;
        let len = data.len() as i64;
        if len <= avail {
            let at = self.pos as usize;
            self.buffer[at..at + data.len()].copy_from_slice(data);
            if !self.valid_write() || self.write_pos > self.pos {
                self.write_pos = self.pos;
            }
            self.pos += len;
            if self.pos > self.write_end {
                self.write_end = self.pos;
            }
            return Ok(());
        }
        self.flush(raw)?;
        debug_assert_eq!(self.raw_offset(), 0);
        let mut remaining = len;
        let mut written = 0usize;
        while remaining > size {
            let n = raw.write_once(&data[written..])?;
            written += n;
            remaining -= n as i64;
        }
        if remaining > 0 {
            self.buffer[..remaining as usize].copy_from_slice(&data[written..]);
        }
        self.write_pos = 0;
        self.write_end = remaining;
        self.pos = remaining;
        self.raw_pos = 0;
        Ok(())
    }
}

/// 旧 TextIOWrapper(pending の区切り 8192)と BufferedWriter の組。
pub struct LegacyTextFile {
    raw: StateWriter,
    pending: Option<Vec<u8>>,
    pending_count: usize,
    buffered: Buffered,
    closed: bool,
}
impl LegacyTextFile {
    /// io.open と同じく、st_blksize が 1 より大きければそれを、そうでなければ 8192 を使う。
    pub fn new(raw: StateWriter) -> Self {
        let size = match raw.block_size() {
            Ok(size) if size > 1 => size as usize,
            _ => DEFAULT_BUFFER_SIZE,
        };
        Self {
            raw,
            pending: None,
            pending_count: 0,
            buffered: Buffered {
                buffer: vec![0; size],
                pos: 0,
                raw_pos: 0,
                write_pos: 0,
                write_end: -1,
            },
            closed: false,
        }
    }
    #[cfg(test)]
    pub fn buffer_size(&self) -> usize {
        self.buffered.buffer.len()
    }
    fn write_flush(&mut self) -> io::Result<()> {
        let Some(pending) = self.pending.take() else {
            return Ok(());
        };
        self.pending_count = 0;
        self.buffered.write(&mut self.raw, &pending)
    }
    /// TextIOWrapper.write。json の chunk は ensure_ascii で ASCII だけになる。
    pub fn write(&mut self, text: &[u8]) -> io::Result<()> {
        let len = text.len();
        if self.pending.is_none() {
            self.pending_count = 0;
            self.pending = Some(text.to_vec());
        } else if self.pending_count + len > CHUNK_SIZE {
            self.write_flush()?;
            self.pending = Some(text.to_vec());
        } else {
            self.pending.as_mut().unwrap().extend_from_slice(text);
        }
        self.pending_count += len;
        if self.pending_count >= CHUNK_SIZE {
            self.write_flush()?;
        }
        Ok(())
    }
    /// TextIOWrapper.flush(pending を渡してから BufferedWriter.flush)。
    pub fn flush(&mut self) -> io::Result<()> {
        self.write_flush()?;
        self.buffered.flush(&mut self.raw)
    }
    pub fn sync(&self) -> io::Result<()> {
        self.raw.sync()
    }
    /// TextIOWrapper.close の 2 回の flush。(TextIOWrapper.flush の失敗,
    /// BufferedWriter.close の中の flush の失敗)を返す。raw の close は失敗を出さない。
    pub fn close(mut self) -> (Option<io::Error>, Option<io::Error>) {
        if self.closed {
            return (None, None);
        }
        self.closed = true;
        let text = self.flush().err();
        let buffer = self.buffered.flush(&mut self.raw).err();
        (text, buffer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_platform::fs::HeldDir;
    use std::ffi::OsStr;
    fn temporary() -> (std::path::PathBuf, HeldDir) {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let base = std::fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!(
                "s2l-legacy-file-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            ));
        std::fs::create_dir_all(&base).unwrap();
        let held = HeldDir::open_absolute(&base).unwrap();
        (base, held)
    }
    #[test]
    fn writes_all_chunks_in_order_with_block_size_buffer() {
        let (base, held) = temporary();
        let raw = StateWriter::create(&held, OsStr::new("out")).unwrap();
        let mut file = LegacyTextFile::new(raw);
        let expected = std::fs::metadata(&base).map(|m| {
            use std::os::unix::fs::MetadataExt;
            m.blksize() as usize
        });
        assert!(expected.is_ok());
        let mut all = Vec::new();
        for n in 0..5000u32 {
            let chunk = format!("{n},").into_bytes();
            all.extend_from_slice(&chunk);
            file.write(&chunk).unwrap();
        }
        let long = vec![b'x'; 20000];
        all.extend_from_slice(&long);
        file.write(&long).unwrap();
        file.flush().unwrap();
        let (text, buffer) = file.close();
        assert!(text.is_none() && buffer.is_none());
        assert_eq!(std::fs::read(base.join("out")).unwrap(), all);
        std::fs::remove_dir_all(base).unwrap();
    }
}
