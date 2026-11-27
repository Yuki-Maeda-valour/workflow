//! 旧Python 3.10の`sys.stdout`・`sys.stderr`(`TextIOWrapper`→`BufferedWriter`→`FileIO`)の書き方。
//!
//! CPython 3.10の`Modules/_io/textio.c`と`bufferedio.c`の書込みの処理を、同じ順と同じ状態で移した。
//! 書く時点・1回の`write(2)`の区切り・失敗の後に残るバイトが旧版と同じになる。
//! 文字コードはUTF-8に固定し、標準出力は`surrogateescape`、標準エラーは`backslashreplace`で書く
//! (2026-11-11 22:24の利用者決定)。非ブロッキングのfd(`BlockingIOError`)の経路は扱わない。

use super::text::{EncodeError, PyText, encode_backslashreplace, encode_surrogateescape};
use dev_workflow_platform::r4g::Errno;

/// 1回の`write(2)`の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RawError {
    Os(Errno),
    /// `EINTR`の後に`SIGINT`の印を見つけた(旧Pythonの`_Py_write`が`KeyboardInterrupt`を投げる)。
    Interrupt,
}

impl From<Errno> for RawError {
    fn from(errno: Errno) -> Self {
        Self::Os(errno)
    }
}

/// 1回の`write(2)`。`FileIO.write`と同じく、書けたバイト数か失敗を返す。
pub trait Raw {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, RawError>;

    /// 旧Pythonの`PyErr_CheckSignals`(`BufferedWriter`は書込みのたびに、次に止まる前に呼ぶ)。
    fn check_signals(&mut self) -> Result<(), RawError> {
        Ok(())
    }
}

/// 標準出力か標準エラーの、本物のfdへの書込み。
pub struct StandardRaw {
    pub fd: i32,
}

impl Raw for StandardRaw {
    /// `EINTR`のたびに`SIGINT`の印を確かめ、立っていれば`Interrupt`で止める(旧Pythonの`_Py_write`)。
    fn write(&mut self, bytes: &[u8]) -> Result<usize, RawError> {
        use dev_workflow_platform::r4g::fs::{self, WriteError};
        use dev_workflow_platform::r4g::startup::take_python_interrupt;
        use std::os::fd::AsFd;
        let mut interrupted = || take_python_interrupt();
        let result = if self.fd == 1 {
            fs::write(std::io::stdout().as_fd(), bytes, &mut interrupted)
        } else {
            fs::write(std::io::stderr().as_fd(), bytes, &mut interrupted)
        };
        result.map_err(|error| match error {
            WriteError::Os(errno) => RawError::Os(errno),
            WriteError::Interrupted => RawError::Interrupt,
        })
    }

    fn check_signals(&mut self) -> Result<(), RawError> {
        if dev_workflow_platform::r4g::startup::take_python_interrupt() {
            Err(RawError::Interrupt)
        } else {
            Ok(())
        }
    }
}

/// 検査専用の故障の規則(型による差し替え)。合計で`limit`バイトまで通し、その後は`errno`で失敗する。
/// 境界をまたぐ書込みは、通る分だけ書けたことにする(部分書込み)。
pub struct FaultRaw<R: Raw> {
    pub inner: R,
    pub limit: usize,
    pub errno: Errno,
    passed: usize,
}

impl<R: Raw> FaultRaw<R> {
    pub fn new(inner: R, limit: usize, errno: Errno) -> Self {
        Self {
            inner,
            limit,
            errno,
            passed: 0,
        }
    }
}

impl<R: Raw> Raw for FaultRaw<R> {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, RawError> {
        let room = self.limit - self.passed;
        if room == 0 && !bytes.is_empty() {
            return Err(RawError::Os(self.errno));
        }
        let count = self.inner.write(&bytes[..bytes.len().min(room)])?;
        self.passed += count;
        Ok(count)
    }

    fn check_signals(&mut self) -> Result<(), RawError> {
        self.inner.check_signals()
    }
}

/// 旧Pythonの書込みの例外。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PyError {
    /// `OSError`(`BrokenPipeError`などの派生を含む)。
    Os(Errno),
    /// `raw write() returned invalid length N (should have been between 0 and M)`の`OSError`。
    InvalidLength { returned: usize, requested: usize },
    /// `UnicodeEncodeError`(標準出力の`surrogateescape`で書けないsurrogate)。
    Encode(EncodeError),
    /// `KeyboardInterrupt`(書込みの`EINTR`の後の信号の処理)。
    Interrupt,
}

impl From<RawError> for PyError {
    fn from(error: RawError) -> Self {
        match error {
            RawError::Os(errno) => Self::Os(errno),
            RawError::Interrupt => Self::Interrupt,
        }
    }
}

/// CPythonの`BufferedWriter`(書込みだけの状態)。
struct Buffered {
    raw: Box<dyn Raw>,
    buffer: Vec<u8>,
    pos: usize,
    raw_pos: usize,
    write_pos: usize,
    /// `-1`は書込みのバッファが無い状態(`VALID_WRITE_BUFFER`が偽)。
    write_end: isize,
}

impl Buffered {
    fn new(raw: Box<dyn Raw>, size: usize) -> Self {
        Self {
            raw,
            buffer: vec![0; size],
            pos: 0,
            raw_pos: 0,
            write_pos: 0,
            write_end: -1,
        }
    }

    fn valid(&self) -> bool {
        self.write_end != -1
    }

    /// `_bufferedwriter_raw_write`。
    fn raw_write(
        &mut self,
        start: usize,
        end: usize,
        data: Option<&[u8]>,
    ) -> Result<usize, PyError> {
        let slice = match data {
            Some(data) => &data[start..end],
            None => &self.buffer[start..end],
        };
        let requested = slice.len();
        let count = self.raw.write(slice).map_err(PyError::from)?;
        if count > requested {
            return Err(PyError::InvalidLength {
                returned: count,
                requested,
            });
        }
        Ok(count)
    }

    /// `_bufferedwriter_flush_unlocked`。失敗しても、書けた分だけ`write_pos`を進めて残りを持つ。
    fn flush(&mut self) -> Result<(), PyError> {
        if self.valid() && self.write_pos as isize != self.write_end {
            while (self.write_pos as isize) < self.write_end {
                let count = self.raw_write(self.write_pos, self.write_end as usize, None)?;
                self.write_pos += count;
                self.raw_pos = self.write_pos;
                // 部分書込みは信号で戻りうるので、次に止まる前に信号を処理する(CPython の同じ所)。
                self.raw.check_signals().map_err(PyError::from)?;
            }
        }
        self.write_pos = 0;
        self.write_end = -1;
        Ok(())
    }

    /// `_io__Buffered_write_impl`(書込みだけ)。
    fn write(&mut self, data: &[u8]) -> Result<usize, PyError> {
        if !self.valid() {
            self.pos = 0;
            self.raw_pos = 0;
        }
        let size = self.buffer.len();
        let avail = size - self.pos;
        if data.len() <= avail {
            self.buffer[self.pos..self.pos + data.len()].copy_from_slice(data);
            if !self.valid() || self.write_pos > self.pos {
                self.write_pos = self.pos;
            }
            self.pos += data.len();
            if self.pos as isize > self.write_end {
                self.write_end = self.pos as isize;
            }
            return Ok(data.len());
        }
        // まず今のバッファを書く。
        self.flush()?;
        // 次にデータそのもの。バッファは空になっている。
        let mut remaining = data.len();
        let mut written = 0;
        while remaining > size {
            let count = self.raw_write(written, data.len(), Some(data))?;
            written += count;
            remaining -= count;
            self.raw.check_signals().map_err(PyError::from)?;
        }
        self.write_pos = 0;
        self.write_end = -1;
        if remaining > 0 {
            self.buffer[..remaining].copy_from_slice(&data[written..]);
        }
        self.write_pos = 0;
        self.write_end = remaining as isize;
        self.pos = remaining;
        self.raw_pos = 0;
        Ok(data.len())
    }
}

/// `TextIOWrapper.buffer`。標準のバッファ付きか、`PYTHONUNBUFFERED`のときの`FileIO`そのもの。
enum Binary {
    Buffered(Buffered),
    Unbuffered(Box<dyn Raw>),
}

impl Binary {
    fn write(&mut self, data: &[u8]) -> Result<usize, PyError> {
        match self {
            Self::Buffered(buffered) => buffered.write(data),
            Self::Unbuffered(raw) => raw.write(data).map_err(PyError::from),
        }
    }

    fn flush(&mut self) -> Result<(), PyError> {
        match self {
            Self::Buffered(buffered) => buffered.flush(),
            Self::Unbuffered(_) => Ok(()),
        }
    }
}

/// どちらの標準の流れか。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stream {
    Stdout,
    Stderr,
}

impl Stream {
    fn name(self) -> &'static str {
        match self {
            Self::Stdout => "<stdout>",
            Self::Stderr => "<stderr>",
        }
    }
}

/// `_io.TextIOWrapper`の1本(`newline='\n'`・UTF-8)。
pub struct TextStream {
    stream: Stream,
    binary: Binary,
    pending: Vec<u8>,
    pending_any: bool,
    line_buffering: bool,
    write_through: bool,
}

/// `TextIOWrapper._CHUNK_SIZE`。
const CHUNK_SIZE: usize = 8192;

/// `io.DEFAULT_BUFFER_SIZE`。
pub const DEFAULT_BUFFER_SIZE: usize = 8192;

/// 旧Pythonの`create_stdio`が決める1本の作り方。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StreamConfig {
    /// `PYTHONUNBUFFERED`が効いていない(`buffered_stdio`)。
    pub buffered: bool,
    /// 端末か(標準出力の行ごとのflush)。
    pub terminal: bool,
    /// `st_blksize`(1より大きければバッファの大きさ。ほかは8192)。
    pub blksize: i128,
}

impl TextStream {
    pub fn new(stream: Stream, raw: Box<dyn Raw>, config: StreamConfig) -> Self {
        let binary = if config.buffered {
            let size = if config.blksize > 1 {
                usize::try_from(config.blksize).unwrap_or(DEFAULT_BUFFER_SIZE)
            } else {
                DEFAULT_BUFFER_SIZE
            };
            Binary::Buffered(Buffered::new(raw, size))
        } else {
            Binary::Unbuffered(raw)
        };
        Self {
            stream,
            binary,
            pending: Vec::new(),
            pending_any: false,
            line_buffering: config.buffered && (config.terminal || stream == Stream::Stderr),
            write_through: !config.buffered,
        }
    }

    /// `repr(sys.stdout)`など。
    pub fn repr(&self) -> String {
        format!(
            "<_io.TextIOWrapper name='{}' mode='w' encoding='utf-8'>",
            self.stream.name()
        )
    }

    /// `_textiowrapper_writeflush`。保留をまとめて消してから`buffer.write`へ渡す。
    fn write_flush(&mut self) -> Result<(), PyError> {
        if !self.pending_any {
            return Ok(());
        }
        let data = std::mem::take(&mut self.pending);
        self.pending_any = false;
        self.binary.write(&data).map(|_| ())
    }

    /// `TextIOWrapper.write(text)`。
    pub fn write(&mut self, text: &[u32]) -> Result<usize, PyError> {
        let has_lf = self.line_buffering && text.contains(&10);
        let need_flush = self.line_buffering && (has_lf || text.contains(&13));
        let encoded = match self.stream {
            Stream::Stdout => encode_surrogateescape(text).map_err(PyError::Encode)?,
            Stream::Stderr => encode_backslashreplace(text),
        };
        if self.pending.len() + encoded.len() > CHUNK_SIZE {
            // 保留を chunk_size より大きく連結しない。
            self.write_flush()?;
        }
        self.pending.extend_from_slice(&encoded);
        self.pending_any = true;
        if self.pending.len() >= CHUNK_SIZE || need_flush || self.write_through {
            self.write_flush()?;
        }
        if need_flush {
            self.binary.flush()?;
        }
        Ok(text.len())
    }

    /// `TextIOWrapper.flush()`。
    pub fn flush(&mut self) -> Result<(), PyError> {
        self.write_flush()?;
        self.binary.flush()
    }

    /// `sys.stdout.buffer.write(data)`。テキストの保留は先に書かない(旧版と同じ)。
    pub fn write_buffer(&mut self, data: &[u8]) -> Result<usize, PyError> {
        self.binary.write(data)
    }
}

/// `print(*parts, sep=' ', end='\n', file=stream)`。各部分を別々の`write`で書く。
pub fn print(stream: &mut TextStream, parts: &[PyText]) -> Result<(), PyError> {
    for (index, part) in parts.iter().enumerate() {
        if index > 0 {
            stream.write(&[32])?;
        }
        stream.write(part)?;
    }
    stream.write(&[10]).map(|_| ())
}

/// 旧Pythonの`OSError`の派生の名前(`errno`から選ぶ)。
pub fn os_error_type(errno: i32) -> &'static str {
    use libc_numbers::*;
    match errno {
        EAGAIN | EALREADY | EINPROGRESS => "BlockingIOError",
        ECHILD => "ChildProcessError",
        EPIPE | ESHUTDOWN => "BrokenPipeError",
        ECONNABORTED => "ConnectionAbortedError",
        ECONNREFUSED => "ConnectionRefusedError",
        ECONNRESET => "ConnectionResetError",
        EEXIST => "FileExistsError",
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

/// `errno`の番号(Linuxはasm-generic、MacはDarwin)。cliはlibcへ直接依存しないので、ここで持つ。
#[allow(dead_code)]
mod libc_numbers {
    pub const EPERM: i32 = 1;
    pub const ENOENT: i32 = 2;
    pub const ESRCH: i32 = 3;
    pub const EINTR: i32 = 4;
    pub const ECHILD: i32 = 10;
    pub const EACCES: i32 = 13;
    pub const EEXIST: i32 = 17;
    pub const ENOTDIR: i32 = 20;
    pub const EISDIR: i32 = 21;
    pub const EPIPE: i32 = 32;
    #[cfg(target_os = "linux")]
    pub const EAGAIN: i32 = 11;
    #[cfg(not(target_os = "linux"))]
    pub const EAGAIN: i32 = 35;
    #[cfg(target_os = "linux")]
    pub const EALREADY: i32 = 114;
    #[cfg(not(target_os = "linux"))]
    pub const EALREADY: i32 = 37;
    #[cfg(target_os = "linux")]
    pub const EINPROGRESS: i32 = 115;
    #[cfg(not(target_os = "linux"))]
    pub const EINPROGRESS: i32 = 36;
    #[cfg(target_os = "linux")]
    pub const ESHUTDOWN: i32 = 108;
    #[cfg(not(target_os = "linux"))]
    pub const ESHUTDOWN: i32 = 58;
    #[cfg(target_os = "linux")]
    pub const ECONNABORTED: i32 = 103;
    #[cfg(not(target_os = "linux"))]
    pub const ECONNABORTED: i32 = 53;
    #[cfg(target_os = "linux")]
    pub const ECONNREFUSED: i32 = 111;
    #[cfg(not(target_os = "linux"))]
    pub const ECONNREFUSED: i32 = 61;
    #[cfg(target_os = "linux")]
    pub const ECONNRESET: i32 = 104;
    #[cfg(not(target_os = "linux"))]
    pub const ECONNRESET: i32 = 54;
    #[cfg(target_os = "linux")]
    pub const ETIMEDOUT: i32 = 110;
    #[cfg(not(target_os = "linux"))]
    pub const ETIMEDOUT: i32 = 60;
}

/// 例外の1行(`TypeName: message`)。旧Pythonの`str(OSError)`を使う。
pub fn exception_line(error: &PyError) -> PyText {
    let mut text = Vec::new();
    match error {
        PyError::Os(errno) => {
            let number = errno.raw_os_error();
            text.extend(super::text::from_str(os_error_type(number)));
            text.extend(super::text::from_str(": "));
            text.extend(super::strerror::os_error(number, None));
        }
        PyError::InvalidLength {
            returned,
            requested,
        } => text.extend(super::text::from_str(&format!(
            "OSError: raw write() returned invalid length {returned} (should have been between 0 and {requested})"
        ))),
        PyError::Encode(error) => text.extend(super::text::from_str(&format!(
            "UnicodeEncodeError: {}",
            error.message()
        ))),
        PyError::Interrupt => text.extend(super::text::from_str("KeyboardInterrupt")),
    }
    text
}

/// 旧Pythonの`sys.stdout`と`sys.stderr`。起動の時点で閉じていたfdは、Rustの標準ライブラリが`/dev/null`で
/// 開いたまま使う(2026-11-12 08:55の決定)ので、`None`になる場合は無い。
pub struct PythonStdio {
    pub stdout: TextStream,
    pub stderr: TextStream,
}

impl PythonStdio {
    /// 終了の処理(`flush_std_files`)。標準出力のflushの失敗は`Exception ignored in`の2行を
    /// 標準エラーへ書く。どちらかが失敗したら終了値は120になる。
    pub fn finalize(&mut self, code: u8) -> u8 {
        let mut failed = false;
        if let Err(error) = self.stdout.flush() {
            failed = true;
            let repr = self.stdout.repr();
            let mut text = super::text::from_str(&format!("Exception ignored in: {repr}\n"));
            text.extend(exception_line(&error));
            text.push(10);
            let _ = self.stderr.write(&text);
        }
        if self.stderr.flush().is_err() {
            failed = true;
        }
        if failed { 120 } else { code }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// 1回ごとの書込みを記録し、計画どおりに部分書込みや失敗を返す。
    #[derive(Clone)]
    struct Recorder {
        log: Rc<RefCell<Vec<String>>>,
        plan: Rc<RefCell<Vec<i64>>>,
    }

    impl Raw for Recorder {
        fn write(&mut self, bytes: &[u8]) -> Result<usize, RawError> {
            let step = self.plan.borrow_mut().pop();
            let result = match step {
                None => Ok(bytes.len()),
                Some(value) if value < 0 => {
                    Err(RawError::Os(Errno::from_raw_os_error(-value as i32)))
                }
                Some(value) => Ok((value as usize).min(bytes.len())),
            };
            self.log.borrow_mut().push(match &result {
                Ok(count) => format!("w{}:{}", bytes.len(), count),
                Err(RawError::Os(error)) => format!("w{}:e{}", bytes.len(), error.raw_os_error()),
                Err(RawError::Interrupt) => format!("w{}:interrupt", bytes.len()),
            });
            result
        }
    }

    /// 旧Pythonの同じ C の実装に、同じ記録用の raw をつないで、同じ操作の列を流す。
    const ORACLE: &str = r#"
import io, json, sys
spec = json.loads(sys.argv[1])
log = []
plan = list(spec['plan'])
class Rec(io.RawIOBase):
    def writable(self): return True
    def write(self, b):
        n = len(b)
        step = plan.pop() if plan else None
        if step is None:
            log.append('w%d:%d' % (n, n)); return n
        if step < 0:
            log.append('w%d:e%d' % (n, -step)); raise OSError(-step, 'x')
        c = min(step, n); log.append('w%d:%d' % (n, c)); return c
raw = Rec()
binary = io.BufferedWriter(raw, buffer_size=spec['size']) if spec['buffered'] else raw
stream = io.TextIOWrapper(binary, encoding='utf-8', errors=spec['errors'], newline='\n', line_buffering=spec['line'], write_through=spec['through'])
for op, value in spec['ops']:
    try:
        if op == 'text': stream.write(value)
        elif op == 'bytes': stream.buffer.write(value.encode())
        elif op == 'flush': stream.flush()
        log.append('ok')
    except OSError as e:
        log.append('err%d' % (e.errno or 0))
print(' '.join(log))
"#;

    fn run_ours(
        stream_kind: Stream,
        config: StreamConfig,
        plan: &[i64],
        ops: &[(&str, String)],
    ) -> String {
        let log = Rc::new(RefCell::new(Vec::new()));
        let mut reversed: Vec<i64> = plan.to_vec();
        reversed.reverse();
        let recorder = Recorder {
            log: log.clone(),
            plan: Rc::new(RefCell::new(reversed)),
        };
        let mut stream = TextStream::new(stream_kind, Box::new(recorder), config);
        for (op, value) in ops {
            let result = match *op {
                "text" => stream
                    .write(&super::super::text::from_str(value))
                    .map(|_| ()),
                "bytes" => stream.write_buffer(value.as_bytes()).map(|_| ()),
                _ => stream.flush(),
            };
            log.borrow_mut().push(match result {
                Ok(()) => "ok".to_string(),
                Err(PyError::Os(errno)) => format!("err{}", errno.raw_os_error()),
                Err(other) => panic!("{other:?}"),
            });
        }
        log.borrow().join(" ")
    }

    #[test]
    fn write_boundaries_and_failures_match_the_cpython_io_stack() {
        let big = "x".repeat(9000);
        let mid = "y".repeat(5000);
        let cases: Vec<(bool, bool, bool, usize, Vec<i64>, Vec<(&str, String)>)> = vec![
            // (buffered, line, through, size, plan, ops)
            (
                true,
                false,
                false,
                4096,
                vec![],
                vec![
                    ("text", "a\n".into()),
                    ("text", "b".into()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                true,
                false,
                4096,
                vec![],
                vec![
                    ("text", "no newline".into()),
                    ("text", "x\ny".into()),
                    ("text", "z\r".into()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                false,
                false,
                4096,
                vec![],
                vec![
                    ("text", mid.clone()),
                    ("text", mid.clone()),
                    ("text", big.clone()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                false,
                false,
                16,
                vec![],
                vec![
                    ("text", "0123456789".into()),
                    ("bytes", "ABCDEFGHIJ".into()),
                    ("text", "tail".into()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                false,
                false,
                16,
                vec![5, -32],
                vec![
                    ("bytes", "0123456789abcdefXYZ".into()),
                    ("bytes", "more".into()),
                    ("flush", String::new()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                false,
                false,
                16,
                vec![3, -28, 4],
                vec![
                    ("bytes", "0123456789".into()),
                    ("flush", String::new()),
                    ("bytes", "ab".into()),
                    ("flush", String::new()),
                ],
            ),
            (
                true,
                true,
                false,
                4096,
                vec![-32],
                vec![("text", "line\n".into()), ("text", "next\n".into())],
            ),
            (
                false,
                false,
                true,
                0,
                vec![2],
                vec![
                    ("text", "abcdef".into()),
                    ("bytes", "ghij".into()),
                    ("text", "k\n".into()),
                ],
            ),
            (
                true,
                false,
                false,
                8192,
                vec![100, 0, -32],
                vec![
                    ("text", big.clone()),
                    ("text", "after".into()),
                    ("flush", String::new()),
                ],
            ),
        ];
        for (buffered, line, through, size, plan, ops) in cases {
            let spec = serde_json::json!({
                "buffered": buffered, "line": line, "through": through, "size": size.max(1),
                "plan": plan.iter().rev().collect::<Vec<_>>(), "errors": "surrogateescape",
                "ops": ops.iter().map(|(op, value)| [op.to_string(), value.clone()]).collect::<Vec<_>>(),
            });
            let output = std::process::Command::new("python3")
                .args(["-I", "-c", ORACLE, &spec.to_string()])
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            let expected = String::from_utf8(output.stdout).unwrap();
            let config = StreamConfig {
                buffered,
                terminal: line,
                blksize: size as i128,
            };
            let ours = run_ours(Stream::Stdout, config, &plan, &ops);
            assert_eq!(ours, expected.trim_end(), "{spec}");
        }
    }

    #[test]
    fn stderr_is_line_buffered_and_escapes_surrogates() {
        let log = Rc::new(RefCell::new(Vec::new()));
        let recorder = Recorder {
            log: log.clone(),
            plan: Rc::new(RefCell::new(vec![])),
        };
        let config = StreamConfig {
            buffered: true,
            terminal: false,
            blksize: 4096,
        };
        let mut stream = TextStream::new(Stream::Stderr, Box::new(recorder), config);
        stream.write(&[0x61, 0xdcff]).unwrap();
        assert!(log.borrow().is_empty());
        stream.write(&[10]).unwrap();
        assert_eq!(*log.borrow(), ["w8:8"]);
        assert_eq!(
            stream.repr(),
            "<_io.TextIOWrapper name='<stderr>' mode='w' encoding='utf-8'>"
        );
    }

    #[test]
    fn finalize_reports_the_stdout_flush_failure_and_returns_120() {
        let out_log = Rc::new(RefCell::new(Vec::new()));
        struct Capture(Rc<RefCell<Vec<u8>>>);
        impl Raw for Capture {
            fn write(&mut self, bytes: &[u8]) -> Result<usize, RawError> {
                self.0.borrow_mut().extend_from_slice(bytes);
                Ok(bytes.len())
            }
        }
        let captured = Rc::new(RefCell::new(Vec::new()));
        let failing = Recorder {
            log: out_log,
            plan: Rc::new(RefCell::new(vec![-28])),
        };
        let config = StreamConfig {
            buffered: true,
            terminal: false,
            blksize: 4096,
        };
        let mut stdio = PythonStdio {
            stdout: TextStream::new(Stream::Stdout, Box::new(failing), config),
            stderr: TextStream::new(Stream::Stderr, Box::new(Capture(captured.clone())), config),
        };
        print(&mut stdio.stdout, &[super::super::text::from_str("{}")]).unwrap();
        assert_eq!(stdio.finalize(0), 120);
        let expected = format!(
            "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 28] {}\n",
            super::super::strerror::strerror(28)
        );
        assert_eq!(
            String::from_utf8(captured.borrow().clone()).unwrap(),
            expected
        );
        assert_eq!(os_error_type(32), "BrokenPipeError");
        assert_eq!(os_error_type(28), "OSError");
    }
}
