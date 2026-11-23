//! 旧版の sys.stdout・sys.stderr を写した書き手と、C の層で「設定された例外」の印。
//!
//! 正本は CPython v3.10.11 の `Modules/_io/textio.c`(TextIOWrapper の write・flush・close)・
//! `Modules/_io/bufferedio.c`(BufferedWriter の write・flush・close)・`Modules/_io/fileio.c`(FileIO.write)・
//! `Python/fileutils.c`(`_Py_write`)・`Objects/fileobject.c`(`PyFile_WriteObject`・`PyFile_WriteString`)・
//! `Objects/object.c`(`PyObject_Str`・`PyObject_Repr` の先頭の `PyErr_CheckSignals`)。
//! - 書込みは区切りも回数も Python と同じにする(溜める・書き出す時点・失敗したときに残る中身)。
//! - SIGINT の印は、Python が `PyErr_CheckSignals` を呼ぶ所だけで見る。
use dev_workflow_platform::check_links as os;

/// `TextIOWrapper` の `chunk_size`。
const CHUNK_SIZE: usize = 8192;
/// `DEFAULT_BUFFER_SIZE`(fstat の st_blksize が 1 以下のとき)。
const DEFAULT_BUFFER_SIZE: usize = 8192;
const EINTR: i32 = 4;
#[cfg(target_os = "macos")]
pub const EAGAIN: i32 = 35;
#[cfg(not(target_os = "macos"))]
pub const EAGAIN: i32 = 11;

/// 書込みの呼出しから起きる例外。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IoError {
    /// OSError(種類は errno から選ぶ。ファイル名は無い)。
    Os(i32),
    /// `BufferedWriter` の EAGAIN(`[Errno N] write could not complete without blocking`)。
    Blocking(i32),
    /// SIGINT による KeyboardInterrupt。
    Interrupt,
}

/// `FileIO.write` の 1 回(`_Py_write` の EINTR の繰り返しを含む)。
enum Raw {
    Written(usize),
    /// EAGAIN(FileIO.write は None を返す)。
    Again,
    Failed(IoError),
}

fn raw_write(fd: i32, data: &[u8]) -> Raw {
    loop {
        match os::write(fd, data) {
            Ok(count) => return Raw::Written(count),
            Err(EINTR) => {
                if os::take_interrupt() {
                    return Raw::Failed(IoError::Interrupt);
                }
            }
            Err(EAGAIN) => return Raw::Again,
            Err(errno) => return Raw::Failed(IoError::Os(errno)),
        }
    }
}

/// `BufferedWriter`(読取りの溜めは持たない)。`write_end` の None は C の -1。
#[derive(Debug)]
struct Buffered {
    buffer: Vec<u8>,
    pos: usize,
    write_pos: usize,
    write_end: Option<usize>,
}

impl Buffered {
    fn new(size: usize) -> Self {
        Self {
            buffer: vec![0; size],
            pos: 0,
            write_pos: 0,
            write_end: None,
        }
    }

    fn size(&self) -> usize {
        self.buffer.len()
    }

    fn reset(&mut self) {
        self.write_pos = 0;
        self.write_end = None;
    }

    /// `_bufferedwriter_flush_unlocked`。書けた分だけ write_pos を進め、失敗したら残りを溜めたまま返す。
    fn flush_unlocked(&mut self, fd: i32) -> Result<(), IoError> {
        let Some(end) = self.write_end else {
            self.reset();
            return Ok(());
        };
        // 書くだけの流れでは raw_pos と write_pos が常に等しいので、巻き戻しは起きない。
        while self.write_pos < end {
            match raw_write(fd, &self.buffer[self.write_pos..end]) {
                Raw::Written(count) => {
                    self.write_pos += count;
                    if os::take_interrupt() {
                        return Err(IoError::Interrupt);
                    }
                }
                Raw::Again => return Err(IoError::Blocking(EAGAIN)),
                Raw::Failed(error) => return Err(error),
            }
        }
        self.reset();
        Ok(())
    }

    /// `_io_BufferedWriter_write_impl`。
    fn write(&mut self, fd: i32, data: &[u8]) -> Result<(), IoError> {
        if self.write_end.is_none() {
            self.pos = 0;
        }
        let avail = self.size() - self.pos;
        if data.len() <= avail {
            self.buffer[self.pos..self.pos + data.len()].copy_from_slice(data);
            if self.write_end.is_none() || self.write_pos > self.pos {
                self.write_pos = self.pos;
            }
            self.pos += data.len();
            if self.write_end.is_none_or(|end| self.pos > end) {
                self.write_end = Some(self.pos);
            }
            return Ok(());
        }
        match self.flush_unlocked(fd) {
            Ok(()) => {}
            Err(IoError::Blocking(errno)) => {
                let end = self.write_end.expect("失敗した書出しは溜めを残す");
                let start = self.write_pos;
                self.buffer.copy_within(start..end, 0);
                let kept = end - start;
                self.write_end = Some(kept);
                self.pos -= start;
                self.write_pos = 0;
                let avail = self.size() - kept;
                if data.len() <= avail {
                    self.buffer[kept..kept + data.len()].copy_from_slice(data);
                    self.write_end = Some(kept + data.len());
                    self.pos += data.len();
                    return Ok(());
                }
                self.buffer[kept..kept + avail].copy_from_slice(&data[..avail]);
                self.write_end = Some(kept + avail);
                self.pos += avail;
                return Err(IoError::Blocking(errno));
            }
            Err(error) => return Err(error),
        }
        let mut remaining = data.len();
        let mut written = 0;
        while remaining > self.size() {
            match raw_write(fd, &data[written..]) {
                Raw::Written(count) => {
                    written += count;
                    remaining -= count;
                    if os::take_interrupt() {
                        return Err(IoError::Interrupt);
                    }
                }
                Raw::Again => {
                    let size = self.size();
                    self.buffer.copy_from_slice(&data[written..written + size]);
                    self.pos = size;
                    self.write_end = Some(size);
                    return Err(IoError::Blocking(EAGAIN));
                }
                Raw::Failed(error) => return Err(error),
            }
        }
        self.buffer[..remaining].copy_from_slice(&data[written..]);
        self.write_pos = 0;
        self.write_end = Some(remaining);
        self.pos = remaining;
        Ok(())
    }
}

/// sys.stdout・sys.stderr の `TextIOWrapper`。`buffered` が None なら FileIO へ直接書く(PYTHONUNBUFFERED)。
#[derive(Debug)]
pub struct Stream {
    fd: i32,
    pending: Option<Vec<u8>>,
    line_buffering: bool,
    write_through: bool,
    buffered: Option<Buffered>,
}

impl Stream {
    /// `create_stdio`。端末か fd 2 なら行ごとに書き出し、PYTHONUNBUFFERED なら FileIO へ直接書く。
    pub fn new(fd: i32, unbuffered: bool, isatty: bool, blksize: u64) -> Self {
        let size = usize::try_from(blksize)
            .ok()
            .filter(|&size| size > 1)
            .unwrap_or(DEFAULT_BUFFER_SIZE);
        Self {
            fd,
            pending: None,
            line_buffering: !unbuffered && (isatty || fd == 2),
            write_through: unbuffered,
            buffered: (!unbuffered).then(|| Buffered::new(size)),
        }
    }

    /// 起動の失敗の診断で使う、標準エラーの仮の書き手(stdprinter。1 回の write ごとに書く)。
    pub fn printer(fd: i32) -> Self {
        Self {
            fd,
            pending: None,
            line_buffering: false,
            write_through: true,
            buffered: None,
        }
    }

    fn buffer_write(&mut self, data: &[u8]) -> Result<(), IoError> {
        match &mut self.buffered {
            Some(buffered) => buffered.write(self.fd, data),
            // FileIO.write の戻り値(書けた長さ・EAGAIN の None)を TextIOWrapper は見ない。
            None => match raw_write(self.fd, data) {
                Raw::Written(_) | Raw::Again => Ok(()),
                Raw::Failed(error) => Err(error),
            },
        }
    }

    /// `_textiowrapper_writeflush`。溜めは書く前に空にする(失敗しても戻さない)。
    fn writeflush(&mut self) -> Result<(), IoError> {
        match self.pending.take() {
            Some(pending) => self.buffer_write(&pending),
            None => Ok(()),
        }
    }

    /// `TextIOWrapper.write(text)`。`text` は符号化した後のバイト列。
    pub fn write(&mut self, text: &[u8]) -> Result<(), IoError> {
        let haslf = self.line_buffering && text.contains(&b'\n');
        let needflush = self.line_buffering && (haslf || text.contains(&b'\r'));
        let pending = self.pending.as_ref().map(Vec::len);
        match pending {
            None => self.pending = Some(text.to_vec()),
            Some(count) if count + text.len() > CHUNK_SIZE => {
                self.writeflush()?;
                self.pending = Some(text.to_vec());
            }
            Some(_) => {
                if let Some(pending) = &mut self.pending {
                    pending.extend_from_slice(text);
                }
            }
        }
        let count = self.pending.as_ref().map_or(0, Vec::len);
        if count >= CHUNK_SIZE || needflush || self.write_through {
            self.writeflush()?;
        }
        if needflush {
            self.buffer_flush()?;
        }
        Ok(())
    }

    fn buffer_flush(&mut self) -> Result<(), IoError> {
        match &mut self.buffered {
            Some(buffered) => buffered.flush_unlocked(self.fd),
            None => Ok(()),
        }
    }

    /// `TextIOWrapper.flush()`。
    pub fn flush(&mut self) -> Result<(), IoError> {
        self.writeflush()?;
        self.buffer_flush()
    }

    /// `TextIOWrapper.close()`(終了の処理での破棄)。失敗はすべて黙って捨てる。
    pub fn close(&mut self) {
        let _ = self.flush();
        // BufferedWriter.close も、閉じる前に書き出す。
        let _ = self.buffer_flush();
    }
}

/// どちらの標準出力か。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Which {
    Stdout,
    Stderr,
}

/// 旧版の標準出力・標準エラーと、C の層で設定された例外。
#[derive(Debug)]
pub struct Py {
    pub stdout: Option<Stream>,
    pub stderr: Option<Stream>,
    /// `PyErr_Occurred()`(C の層の書込みが失敗して残った例外)。
    pub err: Option<IoError>,
}

impl Py {
    pub fn stream(&mut self, which: Which) -> Option<&mut Stream> {
        match which {
            Which::Stdout => self.stdout.as_mut(),
            Which::Stderr => self.stderr.as_mut(),
        }
    }

    pub fn has(&self, which: Which) -> bool {
        match which {
            Which::Stdout => self.stdout.is_some(),
            Which::Stderr => self.stderr.is_some(),
        }
    }

    /// `PyErr_CheckSignals()`。印が立っていれば KeyboardInterrupt を設定して偽を返す。
    pub fn check_signals(&mut self) -> bool {
        if os::take_interrupt() {
            self.err = Some(IoError::Interrupt);
            false
        } else {
            true
        }
    }

    /// `PyFile_WriteObject(text, f)`(先頭の `PyObject_Str`・`PyObject_Repr` が印を見る)。
    pub fn write_object(&mut self, which: Which, text: &[u8]) -> bool {
        if !self.check_signals() {
            return false;
        }
        let Some(stream) = self.stream(which) else {
            return true;
        };
        match stream.write(text) {
            Ok(()) => true,
            Err(error) => {
                self.err = Some(error);
                false
            }
        }
    }

    /// `PyFile_WriteString(text, f)`。例外が設定されていれば何もせずに失敗を返す。
    pub fn write_string(&mut self, which: Which, text: &[u8]) -> bool {
        if self.err.is_some() {
            return false;
        }
        self.write_object(which, text)
    }

    /// `f.flush()`(結果の例外は呼出元が扱う)。
    pub fn flush(&mut self, which: Which) -> Result<(), IoError> {
        match self.stream(which) {
            Some(stream) => stream.flush(),
            None => Ok(()),
        }
    }

    /// 本処理の `print(body)`(本文と改行を別々の write で渡し、呼出しの後に印を見る)。
    pub fn print(&mut self, body: &[u8]) -> Result<(), IoError> {
        if self.stdout.is_some()
            && (!self.write_object(Which::Stdout, body) || !self.write_string(Which::Stdout, b"\n"))
        {
            return Err(self.err.take().expect("失敗した書込みは例外を残す"));
        }
        self.eval_breaker()
    }

    /// Python の処理の区切りで立った印を見る(eval loop の CHECK_EVAL_BREAKER)。
    pub fn eval_breaker(&mut self) -> Result<(), IoError> {
        if os::take_interrupt() {
            Err(IoError::Interrupt)
        } else {
            Ok(())
        }
    }

    /// `flush_io`。stderr、stdout の順に書き出し、失敗は黙って捨てる。
    pub fn flush_io(&mut self) {
        let saved = self.err.take();
        let _ = self.flush(Which::Stderr);
        let _ = self.flush(Which::Stdout);
        self.err = saved;
    }

    /// 終了の処理で標準入出力を破棄する(stderr、stdout の順。旧版の実測)。
    pub fn close_all(&mut self) {
        for which in [Which::Stderr, Which::Stdout] {
            if let Some(stream) = self.stream(which) {
                stream.close();
            }
        }
        self.stdout = None;
        self.stderr = None;
        self.err = None;
    }
}
