//! 旧版の標準出力・標準エラー(`io.TextIOWrapper` の上の `BufferedWriter`)の再現と、
//! 起動時の環境(`PYTHONUNBUFFERED`・`PYTHONINTMAXSTRDIGITS`・`COLUMNS`)の読み方(U1・U5)。
use super::error::{PyException, from_io};
use super::pystr::{decode_fs, encode_backslashreplace, encode_error, encode_fs};
use std::io::{self, Write};

/// `TextIOWrapper` が書込みを溜める上限(`_CHUNK_SIZE`)。
const CHUNK_SIZE: usize = 8192;
/// `st_blksize` が分からないときの `BufferedWriter` の大きさ(`io.DEFAULT_BUFFER_SIZE`)。
pub const DEFAULT_BUFFER_SIZE: usize = 8192;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StreamKind {
    /// 標準出力。UTF-8 と surrogateescape(U1)。
    Stdout,
    /// 標準エラー。UTF-8 と backslashreplace。バッファを使うときは行ごとに flush する。
    Stderr,
}

/// 旧版の標準入出力 1 本。`PYTHONUNBUFFERED` が無効なら `BufferedWriter` を挟み、有効なら
/// `FileIO` へそのまま書く(`write_through`)。
pub struct PyStream<'a> {
    raw: &'a mut dyn Write,
    kind: StreamKind,
    /// `BufferedWriter`。無ければ(`PYTHONUNBUFFERED`)`FileIO` へ直接書く。
    buffered: Option<Buffered>,
    /// `TextIOWrapper` が溜めた、まだ下へ渡していないバイト。`None` は旧版の `pending_bytes == NULL`。
    pending: Option<Vec<u8>>,
    line_buffering: bool,
    write_through: bool,
}

struct Buffered {
    /// 書いていない内容(`write_pos` から `write_end` まで)。
    buffer: Vec<u8>,
    /// 一部だけ書けた後に残る、書き終えた先頭の長さ(`write_pos`)。全部書けると 0 に戻る。
    consumed: usize,
    size: usize,
}

/// `FileIO.write` の結果。
enum RawWrite {
    Wrote(usize),
    /// 非 block の出力先が満杯(`None`)。値はそのときの errno(旧版は C の errno をそのまま使う)。
    Blocked(i32),
}

/// `flush` の失敗。非 block の満杯は、`write` の中で別に扱う。
enum Flush {
    Blocked(i32),
    Error(PyException),
}

/// `BlockingIOError: [Errno N] write could not complete without blocking`。
/// 番号は、そのときの C の errno(実際の出力先では EAGAIN。Linux は 11、Mac は 35)。
fn blocking_error(errno: i32) -> PyException {
    PyException::new(
        "BlockingIOError",
        &format!("[Errno {errno}] write could not complete without blocking"),
    )
}

impl Buffered {
    /// 次に溜められる空き(`buffer_size - pos`)。一部だけ書けた後も、`pos` は戻らない。
    fn available(&self) -> usize {
        self.size - (self.consumed + self.buffer.len())
    }
}

impl<'a> PyStream<'a> {
    /// `buffer_size` は `st_blksize`(1 以下なら既定の 8192)。`unbuffered` は `PYTHONUNBUFFERED`。
    /// `tty` は出力先が端末か(端末の標準出力は、バッファを使うとき改行ごとに flush する)。
    pub fn new(
        raw: &'a mut dyn Write,
        kind: StreamKind,
        unbuffered: bool,
        buffer_size: usize,
        tty: bool,
    ) -> Self {
        let size = if buffer_size > 1 {
            buffer_size
        } else {
            DEFAULT_BUFFER_SIZE
        };
        Self {
            raw,
            kind,
            buffered: (!unbuffered).then(|| Buffered {
                buffer: Vec::new(),
                consumed: 0,
                size,
            }),
            pending: None,
            // 標準エラーは(バッファを使うとき)端末でなくても行ごとに flush する。標準出力は端末のときだけ。
            line_buffering: !unbuffered && (kind == StreamKind::Stderr || tty),
            write_through: unbuffered,
        }
    }

    /// `FileIO.write` 1 回分。非 block で書けないとき(EAGAIN)は、旧版が返す `None`。
    fn raw_write(&mut self, data: &[u8]) -> Result<RawWrite, PyException> {
        loop {
            match self.raw.write(data) {
                Ok(0) if !data.is_empty() => {
                    return Err(from_io(&io::Error::from(io::ErrorKind::WriteZero), None));
                }
                Ok(count) => return Ok(RawWrite::Wrote(count)),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    return Ok(RawWrite::Blocked(error.raw_os_error().unwrap_or(0)));
                }
                Err(error) => return Err(from_io(&error, None)),
            }
        }
    }

    /// `BufferedWriter._bufferedwriter_flush_unlocked`。失敗しても、書けた分は取り除く。
    fn flush_inner(&mut self) -> Result<(), Flush> {
        while self.buffered.as_ref().is_some_and(|b| !b.buffer.is_empty()) {
            let data = self.buffered.as_ref().expect("バッファ").buffer.clone();
            let count = match self.raw_write(&data).map_err(Flush::Error)? {
                RawWrite::Wrote(count) => count,
                RawWrite::Blocked(errno) => return Err(Flush::Blocked(errno)),
            };
            let buffered = self.buffered.as_mut().expect("バッファ");
            buffered.buffer.drain(..count);
            buffered.consumed += count;
        }
        if let Some(buffered) = self.buffered.as_mut() {
            buffered.consumed = 0;
        }
        Ok(())
    }

    fn flush_buffer(&mut self) -> Result<(), PyException> {
        self.flush_inner().map_err(|failure| match failure {
            Flush::Blocked(errno) => blocking_error(errno),
            Flush::Error(error) => error,
        })
    }

    /// `sys.stdout.buffer.write(data)`。バッファが無いときは `FileIO.write`(1 回だけ書き、戻りの長さは見ない。
    /// 非 block で書けないときは `None` が返り、データは黙って捨てられる)。
    pub fn write_buffer(&mut self, data: &[u8]) -> Result<(), PyException> {
        let Some(buffered) = self.buffered.as_ref() else {
            self.raw_write(data)?;
            return Ok(());
        };
        let size = buffered.size;
        if data.len() <= buffered.available() {
            self.buffered
                .as_mut()
                .expect("バッファ")
                .buffer
                .extend_from_slice(data);
            return Ok(());
        }
        match self.flush_inner() {
            Ok(()) => {}
            Err(Flush::Error(error)) => return Err(error),
            Err(Flush::Blocked(errno)) => {
                // 先頭の書けた分を詰め、入る分だけ溜める。全部入れば成功、入らなければ入る分だけ溜めて失敗。
                let buffered = self.buffered.as_mut().expect("バッファ");
                buffered.consumed = 0;
                let available = size - buffered.buffer.len();
                if data.len() <= available {
                    buffered.buffer.extend_from_slice(data);
                    return Ok(());
                }
                buffered.buffer.extend_from_slice(&data[..available]);
                return Err(blocking_error(errno));
            }
        }
        let mut written = 0;
        let mut remaining = data.len();
        while remaining > size {
            match self.raw_write(&data[written..])? {
                RawWrite::Wrote(count) => {
                    written += count;
                    remaining -= count;
                }
                RawWrite::Blocked(errno) => {
                    // 全部は溜められないが、入る分(バッファの大きさ)だけ溜める。
                    let buffered = self.buffered.as_mut().expect("バッファ");
                    buffered.buffer = data[written..written + size].to_vec();
                    buffered.consumed = 0;
                    return Err(blocking_error(errno));
                }
            }
        }
        if remaining > 0 {
            self.buffered
                .as_mut()
                .expect("バッファ")
                .buffer
                .extend_from_slice(&data[written..written + remaining]);
        }
        Ok(())
    }

    /// `TextIOWrapper._textiowrapper_writeflush`。溜めた内容を取り出してから下へ渡す(失敗しても戻さない)。
    fn write_flush(&mut self) -> Result<(), PyException> {
        let Some(pending) = self.pending.take() else {
            return Ok(());
        };
        self.write_buffer(&pending)
    }

    fn encode(&self, text: &[u32]) -> Result<Vec<u8>, PyException> {
        match self.kind {
            StreamKind::Stdout => encode_fs(text).map_err(|failure| encode_error(&failure)),
            StreamKind::Stderr => Ok(encode_backslashreplace(text)),
        }
    }

    /// `sys.stdout.write(text)`。
    pub fn write_text(&mut self, text: &[u32]) -> Result<(), PyException> {
        let bytes = self.encode(text)?;
        let needflush = self.line_buffering && text.iter().any(|&c| c == 0x0a || c == 0x0d);
        let count = self.pending.as_ref().map_or(0, Vec::len);
        if self.pending.is_none() {
            self.pending = Some(bytes);
        } else if count + bytes.len() > CHUNK_SIZE {
            // 溜める量を上限の中に保つため、先に下へ渡す。
            self.write_flush()?;
            self.pending = Some(bytes);
        } else if let Some(pending) = self.pending.as_mut() {
            pending.extend_from_slice(&bytes);
        }
        let total = self.pending.as_ref().map_or(0, Vec::len);
        if total >= CHUNK_SIZE || needflush || self.write_through {
            self.write_flush()?;
        }
        if needflush {
            self.flush_buffer()?;
        }
        Ok(())
    }

    /// `print(text)`(`file` を指定した場合も同じ)。本文と改行を別々に書く。
    pub fn print(&mut self, text: &[u32]) -> Result<(), PyException> {
        self.write_text(text)?;
        self.write_text(&[0x0a])
    }

    /// `sys.stdout.flush()`。
    pub fn flush(&mut self) -> Result<(), PyException> {
        self.write_flush()?;
        self.flush_buffer()
    }
}

// ---------------------------------------------------------------- 起動時の環境

/// `PYTHONUNBUFFERED`。未設定・空と、符号と先頭の空白を許した 0 だけが無効。
pub fn unbuffered_from(value: Option<&[u8]>) -> bool {
    let Some(bytes) = value else { return false };
    if bytes.is_empty() {
        return false;
    }
    let mut rest = bytes;
    while rest
        .first()
        .is_some_and(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
    {
        rest = &rest[1..];
    }
    if rest.first().is_some_and(|b| matches!(b, b'+' | b'-')) {
        rest = &rest[1..];
    }
    rest.is_empty() || !rest.iter().all(|b| *b == b'0')
}

/// 起動を止める `PYTHONINTMAXSTRDIGITS` の誤りの出力(標準エラーへ)。終了コードは 1。
pub const INT_MAX_STR_DIGITS_FATAL: &str = "Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\nPython runtime state: preinitialized\n\n";

/// `PYTHONINTMAXSTRDIGITS` の値。未設定と空は既定(4300)。0 は無制限。誤りは `Err`。
pub fn int_max_str_digits_from(value: Option<&[u8]>) -> Result<usize, ()> {
    let Some(bytes) = value else { return Ok(4300) };
    if bytes.is_empty() {
        return Ok(4300);
    }
    // strtol: 先頭の空白、符号、数字(1 つ以上)。末尾に余りがあれば誤り。範囲は int。
    let mut rest = bytes;
    while rest
        .first()
        .is_some_and(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
    {
        rest = &rest[1..];
    }
    let negative = rest.first() == Some(&b'-');
    if rest.first().is_some_and(|b| matches!(b, b'+' | b'-')) {
        rest = &rest[1..];
    }
    if rest.is_empty() || !rest.iter().all(u8::is_ascii_digit) {
        return Err(());
    }
    let digits = std::str::from_utf8(rest)
        .expect("ASCII")
        .trim_start_matches('0');
    let magnitude: i64 = if digits.is_empty() {
        0
    } else if digits.len() > 18 {
        return Err(());
    } else {
        digits.parse().expect("数字")
    };
    let value = if negative { -magnitude } else { magnitude };
    if value > i64::from(i32::MAX) || value < i64::from(i32::MIN) {
        return Err(());
    }
    if value == 0 || value >= 640 {
        Ok(value as usize)
    } else {
        Err(())
    }
}

/// 旧版が起動時に読む環境。`terminal_columns` は標準出力の端末の幅(端末でなければ 80)。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Env {
    pub columns: Option<Vec<u32>>,
    pub terminal_columns: usize,
    pub unbuffered: bool,
    pub max_str_digits: usize,
}

impl Env {
    /// 環境変数の生バイトから作る。`PYTHONINTMAXSTRDIGITS` の誤りは `Err`(起動を止める)。
    pub fn from_values(
        columns: Option<&[u8]>,
        terminal_columns: usize,
        unbuffered: Option<&[u8]>,
        max_str_digits: Option<&[u8]>,
    ) -> Result<Self, ()> {
        Ok(Self {
            columns: columns.map(decode_fs),
            terminal_columns,
            unbuffered: unbuffered_from(unbuffered),
            max_str_digits: int_max_str_digits_from(max_str_digits)?,
        })
    }
}

/// `Py_FinalizeEx` の標準入出力の flush。標準出力、標準エラーの順で、失敗した本数を数える。
/// 失敗は標準エラーへ `Exception ignored …` として書く(その書込みの失敗は無視する)。
/// 戻り値は、どちらかが失敗したときの終了コード 120。
pub fn finalize(stdout: &mut PyStream<'_>, stderr: &mut PyStream<'_>) -> Option<i32> {
    let mut failed = false;
    if let Err(error) = stdout.flush() {
        failed = true;
        let _ = stderr.write_text(&unraisable("stdout", &error));
    }
    // 標準エラーの失敗は `PyErr_Clear` で捨てられ、何も書かれない(終了コードだけ 120)。
    if stderr.flush().is_err() {
        failed = true;
    }
    failed.then_some(120)
}

fn unraisable(name: &str, error: &PyException) -> Vec<u32> {
    let mut text = format!(
        "Exception ignored in: <_io.TextIOWrapper name='<{name}>' mode='w' encoding='utf-8'>\n{}",
        error.type_name
    )
    .chars()
    .map(u32::from)
    .collect::<Vec<u32>>();
    if !error.message.as_codepoints().is_empty() {
        text.extend(": ".chars().map(u32::from));
        text.extend_from_slice(error.message.as_codepoints());
    }
    text.push(0x0a);
    text
}

#[cfg(test)]
mod tests {
    use super::super::pystr::to_string;
    use super::super::testing::{fixture, with_big_stack};
    use super::*;
    use sha2::{Digest, Sha256};

    /// 先頭から `capacity` バイトまで受け取り、それ以降は失敗する書込み先。
    struct Sink {
        capacity: Option<usize>,
        /// 0 のとき、満杯で `WouldBlock`(EAGAIN)にする。
        code: i32,
        writes: Vec<usize>,
        data: Vec<u8>,
    }

    impl Write for Sink {
        fn write(&mut self, chunk: &[u8]) -> io::Result<usize> {
            if let Some(capacity) = self.capacity
                && self.data.len() >= capacity
                && !chunk.is_empty()
            {
                return Err(if self.code == 0 {
                    io::Error::from(io::ErrorKind::WouldBlock)
                } else {
                    io::Error::from_raw_os_error(self.code)
                });
            }
            let count = match self.capacity {
                Some(capacity) => chunk.len().min(capacity - self.data.len()),
                None => chunk.len(),
            };
            self.data.extend_from_slice(&chunk[..count]);
            self.writes.push(count);
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn run_length(values: &[usize]) -> Vec<(usize, usize)> {
        let mut out: Vec<(usize, usize)> = Vec::new();
        for &value in values {
            match out.last_mut() {
                Some(last) if last.0 == value => last.1 += 1,
                _ => out.push((value, 1)),
            }
        }
        out
    }

    fn outcome(result: Result<(), PyException>) -> serde_json::Value {
        match result {
            Ok(()) => serde_json::Value::Null,
            Err(error) => serde_json::json!({
                "type": error.type_name,
                "msg": to_string(error.message.as_codepoints()),
            }),
        }
    }

    #[test]
    fn stream_cases_match_python_3_10() {
        with_big_stack(|| {
            let cases = fixture!("stream_cases.json");
            assert!(cases.as_array().unwrap().len() > 2000);
            for case in cases.as_array().unwrap() {
                let mut sink = Sink {
                    capacity: case["capacity"].as_u64().map(|c| c as usize),
                    code: case["errno"].as_i64().unwrap_or(0) as i32,
                    writes: Vec::new(),
                    data: Vec::new(),
                };
                let kind = if case["stream"] == "stdout" {
                    StreamKind::Stdout
                } else {
                    StreamKind::Stderr
                };
                let unbuffered = case["unbuffered"].as_bool().unwrap();
                let mut results = Vec::new();
                let mut finals = Vec::new();
                {
                    let mut stream = PyStream::new(
                        &mut sink,
                        kind,
                        unbuffered,
                        case["block"].as_u64().unwrap() as usize,
                        case["tty"].as_bool().unwrap(),
                    );
                    for op in case["ops"].as_array().unwrap() {
                        let kind_name = op[0].as_str().unwrap();
                        let result = match kind_name {
                            "w" | "p" => {
                                let unit: Vec<u32> = op[1]
                                    .as_array()
                                    .unwrap()
                                    .iter()
                                    .map(|v| v.as_u64().unwrap() as u32)
                                    .collect();
                                let text: Vec<u32> = unit.repeat(op[2].as_u64().unwrap() as usize);
                                if kind_name == "w" {
                                    stream.write_text(&text)
                                } else {
                                    stream
                                        .write_text(&text)
                                        .and_then(|()| stream.write_text(&[10]))
                                }
                            }
                            "bw" => stream.write_buffer(&vec![
                                op[1].as_u64().unwrap() as u8;
                                op[2].as_u64().unwrap() as usize
                            ]),
                            _ => stream.flush(),
                        };
                        results.push(outcome(result));
                    }
                    for _ in 0..2 {
                        finals.push(outcome(stream.flush()));
                    }
                }
                let label = format!("{}", case["ops"])
                    .chars()
                    .take(300)
                    .collect::<String>();
                assert_eq!(
                    serde_json::Value::Array(results),
                    case["results"],
                    "{label} {case}"
                );
                assert_eq!(serde_json::Value::Array(finals), case["finals"], "{label}");
                let expected: Vec<(usize, usize)> = case["writes"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|p| {
                        (
                            p[0].as_u64().unwrap() as usize,
                            p[1].as_u64().unwrap() as usize,
                        )
                    })
                    .collect();
                assert_eq!(run_length(&sink.writes), expected, "{label}");
                assert_eq!(
                    sink.data.len() as u64,
                    case["data_len"].as_u64().unwrap(),
                    "{label}"
                );
                assert_eq!(
                    format!("{:x}", Sha256::digest(&sink.data)),
                    case["data_sha256"].as_str().unwrap(),
                    "{label}"
                );
            }
        });
    }

    #[test]
    fn finalize_reports_a_failed_flush_with_exit_120() {
        for (code, name) in [
            (32, "BrokenPipeError: [Errno 32] Broken pipe"),
            (28, "OSError: [Errno 28] No space left on device"),
        ] {
            let mut out = Sink {
                capacity: Some(0),
                code,
                writes: vec![],
                data: vec![],
            };
            let mut err = Vec::new();
            let status;
            {
                let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
                let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
                stdout.print(&[0x78]).unwrap();
                status = finalize(&mut stdout, &mut stderr);
            }
            assert_eq!(status, Some(120));
            assert_eq!(
                String::from_utf8(err).unwrap(),
                format!(
                    "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n{name}\n"
                )
            );
        }
        let mut out = Vec::new();
        let mut err = Vec::new();
        {
            let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
            let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
            stdout.print(&[0x78]).unwrap();
            assert_eq!(finalize(&mut stdout, &mut stderr), None);
        }
        assert_eq!(out, b"x\n");
    }

    #[test]
    fn unbuffered_environment_flag_follows_python() {
        for (value, expect) in [
            (None, false),
            (Some(&b""[..]), false),
            (Some(b"0"), false),
            (Some(b" 0"), false),
            (Some(b"+0"), false),
            (Some(b"-0"), false),
            (Some(b"00"), false),
            (Some(b"\t\n 000"), false),
            (Some(b"1"), true),
            (Some(b"0 "), true),
            (Some(b"0abc"), true),
            (Some(b"abc"), true),
            (Some(b"+"), true),
            (Some(b" "), true),
            (Some(b"-1"), true),
            (Some(b"x0"), true),
        ] {
            assert_eq!(unbuffered_from(value), expect, "{value:?}");
        }
    }

    #[test]
    fn int_max_str_digits_environment_follows_python() {
        for (value, expect) in [
            (None, Ok(4300)),
            (Some(&b""[..]), Ok(4300)),
            (Some(b"0"), Ok(0)),
            (Some(b"+0"), Ok(0)),
            (Some(b"-0"), Ok(0)),
            (Some(b" 640"), Ok(640)),
            (Some(b"00700"), Ok(700)),
            (Some(b"2147483647"), Ok(2147483647)),
            (Some(b"639"), Err(())),
            (Some(b"100"), Err(())),
            (Some(b"-1"), Err(())),
            (Some(b"abc"), Err(())),
            (Some(b"640 "), Err(())),
            (Some(b"0x10"), Err(())),
            (Some(b"1_000"), Err(())),
            (Some(b"2147483648"), Err(())),
            (Some(b"99999999999999"), Err(())),
            (Some(b" "), Err(())),
            (Some(b"+"), Err(())),
            (Some(b"-640"), Err(())),
        ] {
            assert_eq!(int_max_str_digits_from(value), expect, "{value:?}");
        }
    }

    #[test]
    fn environment_cases_match_python_3_10() {
        let cases = fixture!("env_cases.json");
        let digits = cases["digits"].as_array().unwrap();
        assert!(digits.len() > 150);
        for case in digits {
            let value = super::super::testing::from_hex(case["value"].as_str().unwrap());
            let result = int_max_str_digits_from(Some(&value));
            match case["ok"].as_u64() {
                Some(expect) => assert_eq!(result, Ok(expect as usize), "{value:?}"),
                None => {
                    assert_eq!(result, Err(()), "{value:?}");
                    assert_eq!(case["code"], 1);
                    assert_eq!(
                        case["stderr"].as_str().unwrap(),
                        INT_MAX_STR_DIGITS_FATAL,
                        "{value:?}"
                    );
                }
            }
        }
        for case in cases["unbuffered"].as_array().unwrap() {
            let value = super::super::testing::from_hex(case["value"].as_str().unwrap());
            assert_eq!(
                unbuffered_from(Some(&value)),
                case["write_through"].as_bool().unwrap(),
                "{value:?}"
            );
        }
    }
}
