//! 旧版(CPython 3.10.11)の標準入出力・起動・終了処理の再現。
//!
//! - [`PyStr`]: Python の str(文字の番号の列。UTF-8 でないバイトを表すサロゲートも持つ)。
//! - [`Config`]: 起動を変える環境変数(`PYTHONUNBUFFERED`・`PYTHONOPTIMIZE`・`PYTHONHASHSEED`・
//!   `PYTHONINTMAXSTRDIGITS`)の読み方。
//! - [`Runtime`]: `sys.stdout`・`sys.stderr`(`TextIOWrapper` → `BufferedWriter` → `FileIO` の
//!   溜め方と書き方)・`print`・信号の確かめ・例外の表示・終了処理(`flush_io` → `PyErr_Print` →
//!   `Py_FinalizeEx`)。
//!
//! 文字コードは決定 22:24 のとおり UTF-8 に固定する。標準出力は surrogateescape、標準エラーは
//! backslashreplace で書く(`PYTHONUTF8=1` の旧版と同じ)。
//!
//! 手本は CPython 3.10.11 の `Modules/_io/textio.c`・`bufferedio.c`・`fileio.c`・
//! `Python/pylifecycle.c`・`pythonrun.c`・`traceback.c`・`errors.c`・`initconfig.c`。

use super::errors::{self, Filename, PyException, types};
use dev_workflow_core::ExitCode;
use dev_workflow_platform::r4e::{self, Disposition, FdKind, FdState, SigintSetup, StartupState};
use std::fmt;

// ---------------------------------------------------------------- 文字列

/// Python の str。文字の番号の列で、UTF-8 でないバイトを表すサロゲート(U+DC80〜U+DCFF)も持つ。
#[derive(Clone, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct PyStr(Vec<u32>);

impl PyStr {
    pub const fn new() -> Self {
        Self(Vec::new())
    }

    /// 文字の番号の列から作る。
    pub fn from_code_points(points: Vec<u32>) -> Self {
        Self(points)
    }

    /// OS から受けたバイト(引数・パス・環境変数)を、ファイルシステムの文字コード(UTF-8 と
    /// surrogateescape)で文字にする。UTF-8 でない各バイトは U+DC80〜U+DCFF になる。
    pub fn from_os_bytes(bytes: &[u8]) -> Self {
        let mut points = Vec::with_capacity(bytes.len());
        let mut rest = bytes;
        while !rest.is_empty() {
            match std::str::from_utf8(rest) {
                Ok(text) => {
                    points.extend(text.chars().map(u32::from));
                    break;
                }
                Err(error) => {
                    let (valid, invalid) = rest.split_at(error.valid_up_to());
                    if let Ok(text) = std::str::from_utf8(valid) {
                        points.extend(text.chars().map(u32::from));
                    }
                    let length = error.error_len().unwrap_or(invalid.len());
                    points.extend(
                        invalid[..length]
                            .iter()
                            .map(|byte| 0xdc00 + u32::from(*byte)),
                    );
                    rest = &invalid[length..];
                }
            }
        }
        Self(points)
    }

    pub fn as_code_points(&self) -> &[u32] {
        &self.0
    }

    pub fn into_code_points(self) -> Vec<u32> {
        self.0
    }

    /// `len(str)`(文字の数)。
    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn push(&mut self, code: u32) {
        self.0.push(code);
    }

    pub fn push_str(&mut self, text: &str) {
        self.0.extend(text.chars().map(u32::from));
    }

    pub fn push_py(&mut self, other: &PyStr) {
        self.0.extend_from_slice(&other.0);
    }

    /// 文字を含むか。
    pub fn contains(&self, code: u32) -> bool {
        self.0.contains(&code)
    }

    /// UTF-8 と surrogateescape で書く(標準出力・ファイルシステムの文字コード)。
    ///
    /// U+DC80〜U+DCFF は元のバイトへ戻す。ほかのサロゲートは書けない。そのときは、CPython の
    /// `unicode_encode_utf8` と同じく、書けない文字を含むサロゲートの続きの範囲(始まりと終わり。
    /// 終わりは含まない)を返す。
    pub fn encode_surrogateescape(&self) -> Result<Vec<u8>, (usize, usize)> {
        let mut bytes = Vec::with_capacity(self.0.len());
        let mut index = 0;
        while index < self.0.len() {
            let code = self.0[index];
            if !is_surrogate(code) {
                push_utf8(&mut bytes, code);
                index += 1;
                continue;
            }
            let start = index;
            let mut end = index + 1;
            while end < self.0.len() && is_surrogate(self.0[end]) {
                end += 1;
            }
            for (position, code) in self.0[start..end].iter().enumerate() {
                if !(0xdc80..=0xdcff).contains(code) {
                    return Err((start + position, end));
                }
                bytes.push((code & 0xff) as u8);
            }
            index = end;
        }
        Ok(bytes)
    }

    /// UTF-8 と backslashreplace で書く(標準エラー)。サロゲートは `\udcXX` の形にする。
    pub fn encode_backslashreplace(&self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(self.0.len());
        for &code in &self.0 {
            if is_surrogate(code) {
                bytes.extend_from_slice(format!("\\u{code:04x}").as_bytes());
            } else {
                push_utf8(&mut bytes, code);
            }
        }
        bytes
    }

    /// 試験の出力用。サロゲートは U+FFFD にする。
    #[cfg(test)]
    pub fn to_string_lossy(&self) -> String {
        self.0
            .iter()
            .map(|code| char::from_u32(*code).unwrap_or('\u{fffd}'))
            .collect()
    }
}

impl From<&str> for PyStr {
    fn from(text: &str) -> Self {
        Self(text.chars().map(u32::from).collect())
    }
}

impl From<String> for PyStr {
    fn from(text: String) -> Self {
        Self::from(text.as_str())
    }
}

impl fmt::Debug for PyStr {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{}",
            errors::repr(self).encode_backslashreplace().escape_ascii()
        )
    }
}

fn is_surrogate(code: u32) -> bool {
    (0xd800..=0xdfff).contains(&code)
}

/// サロゲートでない文字を UTF-8 で足す。
fn push_utf8(bytes: &mut Vec<u8>, code: u32) {
    let mut buffer = [0; 4];
    let text = char::from_u32(code).map_or("\u{fffd}", |c| c.encode_utf8(&mut buffer));
    bytes.extend_from_slice(text.as_bytes());
}

// ---------------------------------------------------------------- 起動の設定

/// 起動を変える環境変数の値(CPython 3.10.11 の `PyConfig` の該当の欄)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    /// `PYTHONUNBUFFERED`(`buffered_stdio` が偽)。
    pub unbuffered: bool,
    /// `PYTHONOPTIMIZE`(`optimization_level`)。2 以上で docstring が消える(`--help` の説明文)。
    pub optimize: i32,
    /// `PYTHONINTMAXSTRDIGITS`(`int_max_str_digits`)。未設定は -1(既定の 4300 を使う)、0 は上限なし。
    pub int_max_str_digits: i32,
}

/// 既定の `int` の文字列の桁数の上限(`_PY_LONG_DEFAULT_MAX_STR_DIGITS`)。
pub const DEFAULT_INT_MAX_STR_DIGITS: u32 = 4300;

impl Config {
    /// 起動の設定を読む。読めない値は、旧版と同じ `Fatal Python error` の内容を返す。
    ///
    /// 読む順は CPython の `config_read_env_vars`(`PYTHONOPTIMIZE`・`PYTHONUNBUFFERED`・
    /// `PYTHONHASHSEED`)→ `config_read_complex_options`(`PYTHONINTMAXSTRDIGITS`)。空の値は
    /// 無いのと同じ(`_Py_GetEnv`)。
    pub fn read(env: &dyn Fn(&str) -> Option<Vec<u8>>) -> Result<Self, Fatal> {
        let get = |name: &str| env(name).filter(|value| !value.is_empty());
        let optimize = env_flag(get("PYTHONOPTIMIZE").as_deref());
        let unbuffered = env_flag(get("PYTHONUNBUFFERED").as_deref()) > 0;
        if let Some(seed) = get("PYTHONHASHSEED")
            && !hash_seed_is_valid(&seed)
        {
            return Err(Fatal::preinitialized(
                "config_init_hash_seed",
                "PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]",
            ));
        }
        let mut int_max_str_digits = -1;
        if let Some(value) = get("PYTHONINTMAXSTRDIGITS") {
            match c_str_to_int(&value) {
                Some(digits) if digits == 0 || digits >= 640 => int_max_str_digits = digits,
                _ => {
                    return Err(Fatal::preinitialized(
                        "config_init_int_max_str_digits",
                        "PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.",
                    ));
                }
            }
        }
        Ok(Self {
            unbuffered,
            optimize,
            int_max_str_digits,
        })
    }

    /// `int(文字列)` が受け付ける桁数の上限。0 は上限なし。
    pub fn int_max_str_digits_limit(&self) -> u32 {
        match self.int_max_str_digits {
            -1 => DEFAULT_INT_MAX_STR_DIGITS,
            digits => digits as u32,
        }
    }

    /// docstring が消えるか(`-OO` と同じ。`--help` に説明文が出ない)。
    pub fn strips_docstrings(&self) -> bool {
        self.optimize >= 2
    }
}

/// C の `isspace`(C の文字の分類)。
fn c_isspace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// C の `strtol`・`strtoul`(10 進)の読み方。`(負か, 値, 桁の後の位置)` を返す。値は 64 ビットに
/// 収まらなければ `None`(ERANGE)。桁が 1 つも無ければ、位置は 0(読めた所が無い)。
fn c_parse_decimal(bytes: &[u8]) -> (bool, Option<u64>, usize) {
    let mut index = 0;
    while index < bytes.len() && c_isspace(bytes[index]) {
        index += 1;
    }
    let mut negative = false;
    if index < bytes.len() && matches!(bytes[index], b'+' | b'-') {
        negative = bytes[index] == b'-';
        index += 1;
    }
    let start = index;
    let mut value: Option<u64> = Some(0);
    while index < bytes.len() && bytes[index].is_ascii_digit() {
        value = value
            .and_then(|current| current.checked_mul(10))
            .and_then(|current| current.checked_add(u64::from(bytes[index] - b'0')));
        index += 1;
    }
    if index == start {
        return (false, Some(0), 0);
    }
    (negative, value, index)
}

/// CPython の `_Py_str_to_int`: `strtol` で全体を読めて、C の `int` に収まる値だけを返す。
pub fn c_str_to_int(bytes: &[u8]) -> Option<i32> {
    let (negative, value, end) = c_parse_decimal(bytes);
    if end != bytes.len() || end == 0 {
        return None;
    }
    let magnitude = value?;
    // strtol は long(64 ビット)の範囲を超えると ERANGE。
    let long = if negative {
        if magnitude > i64::MAX as u64 + 1 {
            return None;
        }
        (magnitude as i64).wrapping_neg()
    } else {
        i64::try_from(magnitude).ok()?
    };
    i32::try_from(long).ok()
}

/// CPython の `_Py_get_env_flag`(初期値 0): 読めない値と負の値は 1、ほかは読んだ値。
fn env_flag(value: Option<&[u8]>) -> i32 {
    match value {
        None => 0,
        Some(bytes) => match c_str_to_int(bytes) {
            Some(number) if number >= 0 => number,
            _ => 1,
        },
    }
}

/// CPython の `config_init_hash_seed`: `random` か、`strtoul` で全体を読めた 0〜4294967295。
fn hash_seed_is_valid(bytes: &[u8]) -> bool {
    if bytes == b"random" {
        return true;
    }
    let (negative, value, end) = c_parse_decimal(bytes);
    if end != bytes.len() || end == 0 {
        return false;
    }
    // strtoul は範囲を超えると ULONG_MAX と ERANGE。負の値は 2^64 を法とした値になる。
    let Some(magnitude) = value else {
        return false;
    };
    let seed = if negative {
        magnitude.wrapping_neg()
    } else {
        magnitude
    };
    seed <= 4_294_967_295
}

// ---------------------------------------------------------------- 起動の失敗

/// Python の実行時の状態(`Python runtime state:` の行)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeState {
    /// 設定を読む段階(`preinitialized`)。thread の状態がまだ無い。
    Preinitialized,
    /// 標準入出力を作る段階(`core initialized`)。
    CoreInitialized,
}

/// 起動の失敗(`Fatal Python error`)の内容。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fatal {
    pub function: &'static str,
    pub message: &'static str,
    pub state: RuntimeState,
    /// 失敗の元の例外(標準出力・標準エラーがディレクトリのときの `IsADirectoryError`)。
    pub exception: Option<PyException>,
}

impl Fatal {
    fn preinitialized(function: &'static str, message: &'static str) -> Self {
        Self {
            function,
            message,
            state: RuntimeState::Preinitialized,
            exception: None,
        }
    }

    fn sys_streams(message: &'static str, exception: Option<PyException>) -> Self {
        Self {
            function: "init_sys_streams",
            message,
            state: RuntimeState::CoreInitialized,
            exception,
        }
    }

    /// CPython の `fatal_error` と同じ単位で、fd 2 へ書く(誤りは捨てる)。
    ///
    /// thread の状態がある段階では、元の例外(あれば。呼出履歴は無い)と、`Current thread` の行を
    /// 続ける。値は `pthread_self()` の 16 桁の 16 進。
    fn write(&self, os: &mut dyn Os) {
        let mut puts = |text: &str| write_noraise(os, Stream::Stderr, text.as_bytes());
        puts("Fatal Python error: ");
        puts(self.function);
        puts(": ");
        puts(self.message);
        puts("\n");
        puts("Python runtime state: ");
        puts(match self.state {
            RuntimeState::Preinitialized => "preinitialized",
            RuntimeState::CoreInitialized => "core initialized",
        });
        puts("\n");
        if self.state == RuntimeState::Preinitialized {
            // thread の状態が無いので、呼出履歴の表示は空の行だけになる。
            puts("\n");
            return;
        }
        if let Some(exception) = &self.exception {
            // 仮の標準エラー(fd 2 へ直接書く)での PyErr_Display。呼出履歴は無い。
            for piece in exception_line_pieces(exception) {
                write_noraise(os, Stream::Stderr, &piece.encode_backslashreplace());
            }
        }
        let thread = format!("{:016x}", os.thread_id());
        let mut puts = |text: &str| write_noraise(os, Stream::Stderr, text.as_bytes());
        puts("\n");
        puts("Current thread 0x");
        puts(&thread);
        puts(" (most recent call first):\n");
        puts("  <no Python frame>\n");
    }
}

/// `print_exception` の最後の行の書き込みの単位(`[module.]名前`・`: `・文・改行)。
fn exception_line_pieces(exception: &PyException) -> Vec<PyStr> {
    let mut pieces = Vec::new();
    if exception.kind.module != "builtins" {
        pieces.push(PyStr::from(exception.kind.module));
        pieces.push(PyStr::from("."));
    }
    pieces.push(PyStr::from(exception.kind.name));
    if !exception.message.is_empty() {
        pieces.push(PyStr::from(": "));
    }
    pieces.push(exception.message.clone());
    pieces.push(PyStr::from("\n"));
    pieces
}

/// `_Py_write_noraise`: 1 回の `write`。`EINTR` だけ書き直し、ほかの誤りは捨てる。
fn write_noraise(os: &mut dyn Os, stream: Stream, data: &[u8]) {
    while let Err(errors::EINTR) = os.write(stream, data) {}
}

// ---------------------------------------------------------------- OS の境界

/// 標準出力(fd 1)か標準エラー(fd 2)。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Stream {
    Stdout,
    Stderr,
}

impl Stream {
    /// `sys.stdout` などの `name`(`<stdout>`)。
    pub fn name(self) -> &'static str {
        match self {
            Self::Stdout => "<stdout>",
            Self::Stderr => "<stderr>",
        }
    }
}

/// [`Runtime`] が使う OS の操作。製品の実装は [`SystemOs`]。試験は書き込みと信号を決めた動きにする。
pub trait Os {
    /// fd へ 1 回だけ `write` する。誤りは errno(`EINTR` も返す)。
    fn write(&mut self, stream: Stream, data: &[u8]) -> Result<usize, i32>;
    /// 届いた `SIGINT` の印を取り出す(Python の `PyErr_CheckSignals` が `KeyboardInterrupt` を
    /// 投げる時点)。扱いを置いていなければ、いつも偽。
    fn take_sigint(&mut self) -> bool;
    /// 端末か(`FileIO.isatty`)。
    fn is_terminal(&mut self, stream: Stream) -> bool;
    /// `st_blksize`(読めなければ 0)。
    fn block_size(&mut self, stream: Stream) -> i64;
    /// `pthread_self()` の値。
    fn thread_id(&mut self) -> usize;
    /// `SIGPIPE` を無視(真)か既定(偽)にする。
    fn set_sigpipe_ignored(&mut self, ignored: bool);
    /// `SIGXFSZ` を無視する。
    fn ignore_sigxfsz(&mut self);
    /// 起動時に既定なら、`SIGINT` の扱いを置く。
    fn install_sigint(&mut self) -> SigintSetup;
    /// 置いた `SIGINT` の扱いを既定へ戻し、取り出していない印を捨てる。
    fn restore_default_sigint(&mut self);
    /// `exit_sigint`: `SIGINT` を既定へ戻して自分へ送る。戻ったら返す終了値。
    fn exit_sigint(&mut self) -> ExitCode;
}

/// 製品の OS の操作(`dev_workflow_platform::r4e`)。
pub struct SystemOs;

impl Os for SystemOs {
    fn write(&mut self, stream: Stream, data: &[u8]) -> Result<usize, i32> {
        let result = match stream {
            Stream::Stdout => r4e::write_once(std::io::stdout(), data),
            Stream::Stderr => r4e::write_once(std::io::stderr(), data),
        };
        result.map_err(|error| error.raw_os_error().unwrap_or(errors::EIO))
    }

    fn take_sigint(&mut self) -> bool {
        r4e::take_sigint()
    }

    fn is_terminal(&mut self, stream: Stream) -> bool {
        match stream {
            Stream::Stdout => r4e::is_terminal(std::io::stdout()),
            Stream::Stderr => r4e::is_terminal(std::io::stderr()),
        }
    }

    fn block_size(&mut self, stream: Stream) -> i64 {
        let result = match stream {
            Stream::Stdout => r4e::block_size(std::io::stdout()),
            Stream::Stderr => r4e::block_size(std::io::stderr()),
        };
        result.unwrap_or(0)
    }

    fn thread_id(&mut self) -> usize {
        r4e::current_thread_id()
    }

    fn set_sigpipe_ignored(&mut self, ignored: bool) {
        // Python の PyOS_setsig も失敗を無視する。
        let _ = r4e::set_sigpipe_ignored(ignored);
    }

    fn ignore_sigxfsz(&mut self) {
        let _ = r4e::ignore_sigxfsz();
    }

    fn install_sigint(&mut self) -> SigintSetup {
        r4e::install_python_sigint().unwrap_or(SigintSetup::Kept)
    }

    fn restore_default_sigint(&mut self) {
        let _ = r4e::restore_default_sigint();
    }

    fn exit_sigint(&mut self) -> ExitCode {
        if let Err(r4e::SigintResendError::Disposition(error)) = r4e::resend_sigint_by_default() {
            // Python の `perror("signal")`。
            let errno = error.raw_os_error().unwrap_or(errors::EIO);
            let line = format!("signal: {}\n", errors::strerror(errno));
            write_noraise(self, Stream::Stderr, line.as_bytes());
        }
        ExitCode::new(128 + 2)
    }
}

// ---------------------------------------------------------------- 溜め(BufferedWriter)

/// `_io.BufferedWriter`(書くだけ)の状態。`buffer[write_pos..write_end]` がまだ書いていない分。
#[derive(Debug)]
struct Buffered {
    buffer: Vec<u8>,
    pos: usize,
    write_pos: usize,
    /// `None` は C の -1(有効な書き込みの溜めが無い)。
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

    /// `_bufferedwriter_flush_unlocked`。書けた分ごとに信号を確かめる。`FileIO.write` が `EAGAIN`
    /// で `None` を返したら `BlockingIOError` にし、書けなかった分を溜めに残す。
    fn flush(&mut self, os: &mut dyn Os, stream: Stream) -> Result<(), PyException> {
        if let Some(end) = self.write_end {
            while self.write_pos < end {
                let Some(written) = raw_write(os, stream, &self.buffer[self.write_pos..end])?
                else {
                    return Err(blocking_io_error());
                };
                self.write_pos += written;
                check_signals(os)?;
            }
        }
        self.write_pos = 0;
        self.write_end = None;
        Ok(())
    }

    /// `_io_BufferedWriter_write_impl`(閉じていない fd の場合)。
    ///
    /// 溜めに収まらなければ、先に溜めを書き、溜めより大きい分は直接書き、残りを溜める。書き込みを
    /// 待たない fd が一杯(`EAGAIN`)のときは、CPython と同じく次のとおりにする。
    /// - 先の溜めの書き出しが `BlockingIOError` なら、書けなかった分を溜めの先頭へ寄せる。続けて
    ///   `data` が全部入れば溜めて成功、入らなければ入る分だけ溜めて `BlockingIOError`。
    /// - 直接の書き込みが `EAGAIN` なら、溜めの大きさの分だけ溜めて `BlockingIOError`(残りは捨てる)。
    fn write(&mut self, os: &mut dyn Os, stream: Stream, data: &[u8]) -> Result<(), PyException> {
        if self.write_end.is_none() {
            self.pos = 0;
        }
        let available = self.size() - self.pos;
        if data.len() <= available {
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
        if let Err(error) = self.flush(os, stream) {
            if !error.kind.is(types::BLOCKING_IO_ERROR.name) {
                return Err(error);
            }
            // 書き出しが失敗したので、有効な書き込みの溜めがある。
            let end = self.write_end.unwrap_or(self.write_pos);
            self.buffer.copy_within(self.write_pos..end, 0);
            let end = end - self.write_pos;
            self.pos = self.pos.saturating_sub(self.write_pos);
            self.write_pos = 0;
            let available = self.size() - end;
            if data.len() <= available {
                self.buffer[end..end + data.len()].copy_from_slice(data);
                self.write_end = Some(end + data.len());
                self.pos += data.len();
                return Ok(());
            }
            self.buffer[end..].copy_from_slice(&data[..available]);
            self.write_end = Some(self.size());
            self.pos += available;
            return Err(blocking_io_error());
        }
        let mut written = 0;
        let mut remaining = data.len();
        while remaining > self.size() {
            let Some(count) = raw_write(os, stream, &data[written..])? else {
                let size = self.size();
                self.buffer.copy_from_slice(&data[written..written + size]);
                self.write_pos = 0;
                self.write_end = Some(size);
                self.pos = size;
                return Err(blocking_io_error());
            };
            written += count;
            remaining -= count;
            check_signals(os)?;
        }
        self.buffer[..remaining].copy_from_slice(&data[written..]);
        self.write_pos = 0;
        self.write_end = Some(remaining);
        self.pos = remaining;
        Ok(())
    }
}

/// `FileIO.write` → `_Py_write`: `EINTR` のたびに信号を確かめ、`KeyboardInterrupt` でなければ
/// 書き直す。書けた数(途中まででも)を返す。書き込みを待たない fd が一杯(`EAGAIN`)なら、例外に
/// せず `None` を返す(`_io_FileIO_write_impl`)。
fn raw_write(os: &mut dyn Os, stream: Stream, data: &[u8]) -> Result<Option<usize>, PyException> {
    loop {
        match os.write(stream, data) {
            Ok(count) => return Ok(Some(count)),
            Err(errors::EINTR) => check_signals(os)?,
            Err(errors::EAGAIN) => return Ok(None),
            Err(errno) => return Err(PyException::os(errno)),
        }
    }
}

/// `BufferedWriter` の `_set_BlockingIOError("write could not complete without blocking", …)`。
/// errno は直前の `write` の `EAGAIN`。書けた数(`characters_written`)は表示に出ないので持たない。
fn blocking_io_error() -> PyException {
    let mut error = PyException::new(
        types::BLOCKING_IO_ERROR,
        format!(
            "[Errno {}] write could not complete without blocking",
            errors::EAGAIN
        ),
    );
    error.errno = Some(errors::EAGAIN);
    error
}

/// `PyErr_CheckSignals`: 届いた `SIGINT` があれば `KeyboardInterrupt`(呼出履歴は呼出側が足す)。
fn check_signals(os: &mut dyn Os) -> Result<(), PyException> {
    if os.take_sigint() {
        return Err(PyException::keyboard_interrupt());
    }
    Ok(())
}

// ---------------------------------------------------------------- 文字の流れ(TextIOWrapper)

/// `TextIOWrapper` の `chunk_size`。
const CHUNK_SIZE: usize = 8192;
/// `io.DEFAULT_BUFFER_SIZE`(`st_blksize` が 1 以下のとき)。
const DEFAULT_BUFFER_SIZE: usize = 8192;

/// `sys.stdout`・`sys.stderr` の 1 本(`TextIOWrapper`)。
#[derive(Debug)]
struct TextIo {
    stream: Stream,
    line_buffering: bool,
    write_through: bool,
    /// 文字コードを当てた後の、まだ下へ渡していないバイト(`pending_bytes`)。
    pending: Option<Vec<u8>>,
    pending_count: usize,
    /// `None` は溜めの無い `FileIO`(`PYTHONUNBUFFERED`)。
    buffer: Option<Buffered>,
    closed: bool,
}

impl TextIo {
    /// CPython の `create_stdio` と `io.open(fd, "wb", buffering, closefd=False)`。
    fn new(stream: Stream, config: &Config, os: &mut dyn Os) -> Self {
        let buffered = !config.unbuffered;
        let terminal = os.is_terminal(stream);
        let buffer = buffered.then(|| {
            let size = os.block_size(stream);
            Buffered::new(if size > 1 {
                size as usize
            } else {
                DEFAULT_BUFFER_SIZE
            })
        });
        Self {
            stream,
            line_buffering: buffered && (terminal || stream == Stream::Stderr),
            write_through: !buffered,
            pending: None,
            pending_count: 0,
            buffer,
            closed: false,
        }
    }

    fn encode(&self, text: &PyStr) -> Result<Vec<u8>, PyException> {
        match self.stream {
            Stream::Stdout => text
                .encode_surrogateescape()
                .map_err(|(start, end)| errors::unicode_encode_error(text, start, end)),
            Stream::Stderr => Ok(text.encode_backslashreplace()),
        }
    }

    /// `_textiowrapper_writeflush`: 溜めた分を下へ 1 回で渡す(渡す前に溜めを空にする)。
    fn write_flush(&mut self, os: &mut dyn Os) -> Result<(), PyException> {
        let Some(pending) = self.pending.take() else {
            return Ok(());
        };
        self.pending_count = 0;
        match &mut self.buffer {
            Some(buffer) => buffer.write(os, self.stream, &pending),
            // FileIO.write の戻り値(書けた数か、EAGAIN の None)は見ない。途中までしか書けないか、
            // 一杯で書けなければ、残りは失われる。
            None => raw_write(os, self.stream, &pending).map(drop),
        }
    }

    /// `TextIOWrapper.write`。
    fn write(&mut self, os: &mut dyn Os, text: &PyStr) -> Result<(), PyException> {
        if self.closed {
            return Err(PyException::new(
                types::VALUE_ERROR,
                "I/O operation on closed file.",
            ));
        }
        let has_lf = self.line_buffering && text.contains(u32::from('\n'));
        let need_flush = self.line_buffering && (has_lf || text.contains(u32::from('\r')));
        let bytes = self.encode(text)?;
        let length = bytes.len();
        match &mut self.pending {
            None => {
                self.pending_count = 0;
                self.pending = Some(bytes);
            }
            Some(_) if self.pending_count + length > CHUNK_SIZE => {
                self.write_flush(os)?;
                self.pending = Some(bytes);
            }
            Some(pending) => pending.extend_from_slice(&bytes),
        }
        self.pending_count += length;
        if self.pending_count >= CHUNK_SIZE || need_flush || self.write_through {
            self.write_flush(os)?;
        }
        if need_flush && let Some(buffer) = &mut self.buffer {
            buffer.flush(os, self.stream)?;
        }
        Ok(())
    }

    /// `TextIOWrapper.flush`。
    fn flush(&mut self, os: &mut dyn Os) -> Result<(), PyException> {
        if self.closed {
            return Err(PyException::new(
                types::VALUE_ERROR,
                "I/O operation on closed file.",
            ));
        }
        self.write_flush(os)?;
        match &mut self.buffer {
            Some(buffer) => buffer.flush(os, self.stream),
            None => Ok(()),
        }
    }

    /// 終了処理での `close`(`iobase_finalize` から。誤りは捨てる)。溜めがあれば、
    /// `TextIOWrapper.close` の `flush` と `BufferedWriter.close` の `flush` の 2 回を試す。
    fn close(&mut self, os: &mut dyn Os) {
        if self.closed {
            return;
        }
        let _ = self.flush(os);
        if let Some(buffer) = &mut self.buffer {
            let _ = buffer.flush(os, self.stream);
        }
        self.closed = true;
    }
}

// ---------------------------------------------------------------- 実行時

/// 旧版の終わり方。
pub enum Outcome {
    /// `sys.exit(main())` の値(`SystemExit`)。
    Exit(i32),
    /// 捕まえられずにモジュールの外へ出た例外。
    Raised(Box<Uncaught>),
}

/// 終了処理で `sys.last_traceback` を消すとき(標準出力の最後の書き出しと信号の後始末の後、
/// 標準入出力を閉じる前)に動く処理。呼出履歴の中の値の後始末(例: `ZipFile.__del__`)を書く。
pub type Cleanup = Box<dyn FnOnce(&mut Runtime)>;

/// 捕まえられなかった例外と、その呼出履歴を消すときに動く後始末。
pub struct Uncaught {
    pub exception: PyException,
    pub on_traceback_cleared: Option<Cleanup>,
}

impl Outcome {
    pub fn raised(exception: PyException) -> Self {
        Self::Raised(Box::new(Uncaught {
            exception,
            on_traceback_cleared: None,
        }))
    }
}

/// 旧版の Python の実行時(`sys.stdout`・`sys.stderr`・信号・終了処理)。
pub struct Runtime {
    config: Config,
    os: Box<dyn Os>,
    stdout: Option<TextIo>,
    stderr: Option<TextIo>,
    sigint: SigintSetup,
}

impl Runtime {
    /// 旧版の起動を再現する。失敗は `Fatal Python error` を fd 2 へ書き、終了値 1 を返す。
    ///
    /// 順は CPython と同じ: 継いだ `SIGPIPE` の扱いのまま設定を読む → `SIGPIPE`・`SIGXFSZ` を無視、
    /// `SIGINT` の扱いを置く → 標準入力がディレクトリか → `sys.stdout` → `sys.stderr` を作る。
    /// 起動の時点で閉じていた fd 1・fd 2(`StartupState` の `python_valid` が偽)は `None` にする。
    pub fn start(startup: &StartupState) -> Result<Self, ExitCode> {
        Self::start_with(startup, &system_env, Box::new(SystemOs))
    }

    /// [`Runtime::start`] の環境変数と OS の操作を差し替えられる形。
    pub fn start_with(
        startup: &StartupState,
        env: &dyn Fn(&str) -> Option<Vec<u8>>,
        mut os: Box<dyn Os>,
    ) -> Result<Self, ExitCode> {
        // Rust の標準ライブラリは main の前に SIGPIPE を無視へ変えた。Python は設定を読む間は
        // 継いだ扱いのままなので、設定の誤りを書くときに読み手の無いパイプなら SIGPIPE で終わる。
        if startup.sigpipe == Some(Disposition::Default) {
            os.set_sigpipe_ignored(false);
        }
        let config = match Config::read(env) {
            Ok(config) => config,
            Err(fatal) => {
                fatal.write(&mut *os);
                return Err(ExitCode::new(1));
            }
        };
        os.set_sigpipe_ignored(true);
        os.ignore_sigxfsz();
        let sigint = os.install_sigint();
        if startup.fds[0].state == FdState::Open(FdKind::Directory) {
            Fatal::sys_streams("<stdin> is a directory, cannot continue", None).write(&mut *os);
            return Err(ExitCode::new(1));
        }
        let mut streams = [None, None];
        for (slot, (stream, fd)) in streams
            .iter_mut()
            .zip([(Stream::Stdout, 1), (Stream::Stderr, 2)])
        {
            let record = &startup.fds[fd];
            if !record.python_valid() {
                continue;
            }
            if record.state == FdState::Open(FdKind::Directory) {
                let exception =
                    PyException::os_with_filename(errors::EISDIR, Filename::Int(fd as i64));
                Fatal::sys_streams("can't initialize sys standard streams", Some(exception))
                    .write(&mut *os);
                return Err(ExitCode::new(1));
            }
            *slot = Some(TextIo::new(stream, &config, &mut *os));
        }
        let [stdout, stderr] = streams;
        Ok(Self {
            config,
            os,
            stdout,
            stderr,
            sigint,
        })
    }

    pub fn config(&self) -> &Config {
        &self.config
    }

    /// 起動時の `SIGINT` の扱いの結果。試験で使う。
    #[cfg(test)]
    pub fn sigint_setup(&self) -> SigintSetup {
        self.sigint
    }

    /// `sys.stdout`(`sys.stderr`)が `None` でないか。
    pub fn has(&self, stream: Stream) -> bool {
        self.stream_ref(stream).is_some()
    }

    fn stream_ref(&self, stream: Stream) -> Option<&TextIo> {
        match stream {
            Stream::Stdout => self.stdout.as_ref(),
            Stream::Stderr => self.stderr.as_ref(),
        }
    }

    fn parts(&mut self, stream: Stream) -> (Option<&mut TextIo>, &mut dyn Os) {
        let text = match stream {
            Stream::Stdout => self.stdout.as_mut(),
            Stream::Stderr => self.stderr.as_mut(),
        };
        (text, &mut *self.os)
    }

    /// 標準出力の端末の幅(`os.get_terminal_size(sys.__stdout__.fileno())`)。`sys.__stdout__` が
    /// `None` か端末でなければ `None`。0 もそのまま返す。
    pub fn stdout_terminal_columns(&self) -> Option<u16> {
        self.stdout.as_ref()?;
        r4e::stdout_terminal_columns()
    }

    /// `sys.<stream>.write(text)`。`None` なら `AttributeError`。
    pub fn write(&mut self, stream: Stream, text: &PyStr) -> Result<(), PyException> {
        match self.parts(stream) {
            (Some(io), os) => io.write(os, text),
            (None, _) => Err(PyException::none_has_no_attribute("write")),
        }
    }

    /// `sys.<stream>.flush()`。`None` なら `AttributeError`。
    pub fn flush(&mut self, stream: Stream) -> Result<(), PyException> {
        match self.parts(stream) {
            (Some(io), os) => io.flush(os),
            (None, _) => Err(PyException::none_has_no_attribute("flush")),
        }
    }

    /// 組込みの `print(text, file=…)`(区切りと終わりは既定)。
    ///
    /// `file` が `None` か、渡したものが `None`(起動時に閉じていた fd)なら `sys.stdout` へ書き、
    /// それも `None` なら何もしない。文と改行は別々の `write` にする。
    pub fn print(&mut self, file: Option<Stream>, text: &PyStr) -> Result<(), PyException> {
        let target = match file {
            Some(stream) if self.has(stream) => stream,
            _ if self.has(Stream::Stdout) => Stream::Stdout,
            _ => return Ok(()),
        };
        self.file_write(target, text)?;
        self.file_write(target, &PyStr::from("\n"))
    }

    /// `PyFile_WriteObject(v, f, Py_PRINT_RAW)`・`PyFile_WriteString`(誤りが残っていないとき):
    /// `str(v)`(`PyObject_Str` は最初に `PyErr_CheckSignals` をする)の後に `file.write` を 1 回。
    /// `print`・呼出履歴の表示・`Exception ignored in` の表示の各単位は、これで書く。
    fn file_write(&mut self, stream: Stream, text: &PyStr) -> Result<(), PyException> {
        self.check_signals()?;
        self.write(stream, text)
    }

    /// `PyErr_SetFromErrno(PyExc_OSError)`: errno の `OSError`。`EINTR` で、届いた `SIGINT` が
    /// あれば、代わりに `KeyboardInterrupt`(信号の扱いが投げる例外)。閉じるとき(`close(2)`)の
    /// 誤りのように、書き直さない呼出しで使う。
    pub fn os_error(&mut self, number: i32) -> PyException {
        if number == errors::EINTR
            && let Err(interrupt) = self.check_signals()
        {
            return interrupt;
        }
        PyException::os(number)
    }

    /// 例外を処理する所(`except`・`finally`・`with` の後始末)に入る所の信号の確かめ。CPython は
    /// 後始末に入る最初の命令で信号を確かめる。届いていれば、処理中の `handled`(呼出履歴の外側の
    /// `outer` 段は通っていない)を `__context__` に持つ `KeyboardInterrupt`(呼出履歴は `stack`)。
    ///
    /// 届いていなければ `Ok(handled)`(後始末を続ける)、届いていれば `Err(KeyboardInterrupt)`。
    pub fn enter_handler(
        &mut self,
        handled: PyException,
        outer: usize,
        stack: &[errors::Frame],
    ) -> Result<PyException, PyException> {
        match self.check_signals() {
            Ok(()) => Ok(handled),
            Err(interrupt) => Err(interrupt
                .called_from(stack)
                .with_context(handled.caught_after(outer))),
        }
    }

    /// Python の評価の合間の信号の確かめ。届いた `SIGINT` があれば `KeyboardInterrupt`(呼出履歴は
    /// 呼出側が足す)。
    pub fn check_signals(&mut self) -> Result<(), PyException> {
        check_signals(&mut *self.os)
    }

    /// `warnings._showwarnmsg_impl`: 書式を当てた警告の文を `sys.stderr` へ 1 回の `write` で書く。
    /// `sys.stderr` が `None` なら捨て、`OSError` は無視する(書けなかった分は溜めに残る)。
    pub fn show_warning(&mut self, text: &PyStr) -> Result<(), PyException> {
        if !self.has(Stream::Stderr) {
            return Ok(());
        }
        match self.write(Stream::Stderr, text) {
            Err(error) if error.kind.is("OSError") => Ok(()),
            other => other,
        }
    }

    /// `PyErr_WriteUnraisable(obj)` の既定の表示: `Exception ignored in: <repr>` と、呼出履歴
    /// (あれば)と、例外の行(文が空でも `: ` を付ける)を `sys.stderr` へ書く。
    ///
    /// 書けなければ、CPython と同じく `sys.unraisablehook` の失敗として、その誤りを
    /// `Exception ignored in sys.unraisablehook: <built-in function unraisablehook>` の形で
    /// もう 1 度だけ書こうとする(その誤りは捨てる)。
    pub fn write_unraisable(&mut self, object_repr: &PyStr, exception: &PyException) {
        if !self.has(Stream::Stderr) {
            return;
        }
        if let Err(error) = self.write_unraisable_once(None, object_repr, exception) {
            let hook = PyStr::from("<built-in function unraisablehook>");
            let message = "Exception ignored in sys.unraisablehook";
            let _ = self.write_unraisable_once(Some(message), &hook, &error);
        }
    }

    /// `write_unraisable_exc_file`。書き込みの単位は CPython と同じにする。
    ///
    /// - `err_msg` が無ければ `Exception ignored in: ` を 1 回で書き、あれば文と `: ` を別々に書く。
    /// - `repr` を書けなければ誤りを捨てて `<object repr() failed>` を書き、例外の文を書けなければ
    ///   誤りを捨てて `<exception str() failed>` を書く(`PyFile_WriteObject` の失敗の扱い)。
    /// - 呼出履歴が書けなくても続ける。
    fn write_unraisable_once(
        &mut self,
        err_msg: Option<&str>,
        object_repr: &PyStr,
        exception: &PyException,
    ) -> Result<(), PyException> {
        let stderr = Stream::Stderr;
        match err_msg {
            Some(message) => {
                self.file_write(stderr, &PyStr::from(message))?;
                self.file_write(stderr, &PyStr::from(": "))?;
            }
            None => self.file_write(stderr, &PyStr::from("Exception ignored in: "))?,
        }
        // PyFile_WriteObject(obj, file, 0): PyObject_Repr も最初に信号を確かめる。
        if self.file_write(stderr, object_repr).is_err() {
            self.file_write(stderr, &PyStr::from("<object repr() failed>"))?;
        }
        self.file_write(stderr, &PyStr::from("\n"))?;
        if !exception.traceback.is_empty() {
            let _ = self.print_traceback(stderr, &exception.traceback);
        }
        if exception.kind.module != "builtins" {
            self.file_write(stderr, &PyStr::from(exception.kind.module))?;
            self.file_write(stderr, &PyStr::from("."))?;
        }
        self.file_write(stderr, &PyStr::from(exception.kind.name))?;
        self.file_write(stderr, &PyStr::from(": "))?;
        if self.file_write(stderr, &exception.message).is_err() {
            self.file_write(stderr, &PyStr::from("<exception str() failed>"))?;
        }
        self.file_write(stderr, &PyStr::from("\n"))?;
        self.flush(stderr)
    }

    /// `PyTraceBack_Print`: `Traceback (most recent call last):` と各段。最後の 1000 段だけを出し、
    /// 同じ段が 4 回以上続いたら 4 回目から `[Previous line repeated N more times]` にまとめる。
    /// 各単位は [`Self::file_write`](書く前に信号を確かめる)で書き、段ごとにも信号を確かめる。
    /// ソースの行を書けなかった誤り(`KeyboardInterrupt` も)は捨てて続ける(`tb_displayline`)。
    fn print_traceback(
        &mut self,
        stream: Stream,
        frames: &[errors::Frame],
    ) -> Result<(), PyException> {
        const LIMIT: usize = 1000;
        const CUTOFF: usize = 3;
        self.file_write(stream, &PyStr::from("Traceback (most recent call last):\n"))?;
        let frames = &frames[frames.len().saturating_sub(LIMIT)..];
        let mut last: Option<&errors::Frame> = None;
        let mut count = 0;
        for frame in frames {
            if !last.is_some_and(|previous| previous.same_place(frame)) {
                if count > CUTOFF {
                    self.file_write(stream, &errors::repeated_line(count - CUTOFF))?;
                }
                last = Some(frame);
                count = 0;
            }
            count += 1;
            if count <= CUTOFF {
                self.file_write(stream, &frame.file_line())?;
                if let Some(source) = frame.display_source() {
                    let _ = self
                        .file_write(stream, &PyStr::from("    "))
                        .and_then(|()| self.file_write(stream, &PyStr::from(source)))
                        .and_then(|()| self.file_write(stream, &PyStr::from("\n")));
                }
                self.check_signals()?;
            }
        }
        if count > CUTOFF {
            self.file_write(stream, &errors::repeated_line(count - CUTOFF))?;
        }
        Ok(())
    }

    /// `print_exception`(呼出履歴と例外の行)。CPython 3.10 の書き込みの順と失敗の扱いに合わせる。
    ///
    /// - `pending` は呼出元で残っている誤り(つなぎの文を書けなかったとき)。残っていれば
    ///   `PyFile_WriteString` は書かずに失敗するので、最後の改行のほかは書かない。
    /// - 呼出履歴を書けなければ例外の行を書かない。例外の行の途中で書けなくなれば残りを書かない。
    /// - 型の名前の後の `str(例外)` が信号の確かめで失敗したら、誤りを捨てて
    ///   `: <exception str() failed>` を書く。
    /// - どの場合も、誤りを消してから、最後の改行は書こうとする。
    fn print_exception(&mut self, stream: Stream, exception: &PyException, pending: bool) {
        let mut failed = pending;
        if !failed && !exception.traceback.is_empty() {
            failed = self.print_traceback(stream, &exception.traceback).is_err();
        }
        if !failed {
            let mut name = Ok(());
            if exception.kind.module != "builtins" {
                name = self
                    .file_write(stream, &PyStr::from(exception.kind.module))
                    .and_then(|()| self.file_write(stream, &PyStr::from(".")));
            }
            failed = name
                .and_then(|()| self.file_write(stream, &PyStr::from(exception.kind.name)))
                .is_err();
        }
        if !failed {
            if self.check_signals().is_err() {
                // PyObject_Str(value) の失敗。
                let _ = self.file_write(stream, &PyStr::from(": <exception str() failed>"));
            } else {
                let separator = if exception.message.is_empty() {
                    Ok(())
                } else {
                    self.file_write(stream, &PyStr::from(": "))
                };
                let _ = separator.and_then(|()| self.file_write(stream, &exception.message));
            }
        }
        let _ = self.file_write(stream, &PyStr::from("\n"));
    }

    /// `print_exception_recursive`: 連鎖の元を先に表示し、つなぎの文を挟む。つなぎの文を書けな
    /// ければ、誤りが残ったまま例外を表示しようとする(最後の改行だけになる)。
    fn print_exception_recursive(&mut self, stream: Stream, exception: &PyException) {
        let link = if let Some(cause) = &exception.cause {
            self.print_exception_recursive(stream, cause);
            Some(errors::CAUSE_MESSAGE)
        } else if let Some(context) = exception
            .context
            .as_ref()
            .filter(|_| !exception.suppress_context)
        {
            self.print_exception_recursive(stream, context);
            Some(errors::CONTEXT_MESSAGE)
        } else {
            None
        };
        let pending = link.is_some_and(|link| self.file_write(stream, &PyStr::from(link)).is_err());
        self.print_exception(stream, exception, pending);
    }

    /// `sys.excepthook` → `PyErr_Display`: `sys.stderr` へ例外を表示して `flush` する(誤りは捨てる)。
    /// `sys.stderr` が `None` なら何もしない。
    pub fn display_exception(&mut self, exception: &PyException) {
        if !self.has(Stream::Stderr) {
            return;
        }
        self.print_exception_recursive(Stream::Stderr, exception);
        let _ = self.flush(Stream::Stderr);
    }

    /// `flush_io`: 標準エラー、標準出力の順に `flush` し、誤り(`KeyboardInterrupt` も)は捨てる。
    fn flush_io(&mut self) {
        let _ = self.flush(Stream::Stderr);
        let _ = self.flush(Stream::Stdout);
    }

    /// `flush_std_files`: 標準出力の失敗は `Exception ignored in` を書く。どちらかが失敗したら偽。
    fn flush_std_files(&mut self) -> bool {
        let mut ok = true;
        if self.has(Stream::Stdout)
            && let Err(error) = self.flush(Stream::Stdout)
        {
            let repr = PyStr::from(format!(
                "<_io.TextIOWrapper name='{}' mode='w' encoding='utf-8'>",
                Stream::Stdout.name()
            ));
            self.write_unraisable(&repr, &error);
            ok = false;
        }
        if self.has(Stream::Stderr) && self.flush(Stream::Stderr).is_err() {
            ok = false;
        }
        ok
    }

    /// 旧版の終わり方を再現して終了値を返す(`KeyboardInterrupt` なら `SIGINT` で終わる)。
    ///
    /// 1. `flush_io`(誤りは捨てる)。
    /// 2. 例外なら `sys.stderr` へ表示し、終了値 1。`sys.exit` ならその値。
    /// 3. `Py_FinalizeEx`: `flush_std_files`(失敗なら終了値 120)→ `SIGINT` を既定へ戻す →
    ///    呼出履歴を消す(後始末)→ 標準エラー、標準出力の順に閉じる(誤りは捨てる)。
    /// 4. 捕まえられなかった例外が `KeyboardInterrupt` なら、`SIGINT` を自分へ送る。
    pub fn finish(mut self, outcome: Outcome) -> ExitCode {
        self.flush_io();
        let (mut status, keyboard_interrupt, on_traceback_cleared) = match outcome {
            Outcome::Exit(code) => (code, false, None),
            Outcome::Raised(uncaught) => {
                let Uncaught {
                    exception,
                    on_traceback_cleared,
                } = *uncaught;
                self.display_exception(&exception);
                let interrupted = exception.kind == types::KEYBOARD_INTERRUPT;
                (1, interrupted, on_traceback_cleared)
            }
        };
        if !self.flush_std_files() {
            status = 120;
        }
        if self.sigint == SigintSetup::Installed {
            self.os.restore_default_sigint();
        }
        if let Some(cleanup) = on_traceback_cleared {
            cleanup(&mut self);
        }
        for stream in [Stream::Stderr, Stream::Stdout] {
            if let (Some(io), os) = self.parts(stream) {
                io.close(os);
            }
        }
        if keyboard_interrupt {
            return self.os.exit_sigint();
        }
        ExitCode::new((status & 0xff) as u8)
    }
}

fn system_env(name: &str) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    std::env::var_os(name).map(OsStringExt::into_vec)
}

#[cfg(test)]
pub(crate) mod fake {
    //! 試験用の OS: 書き込みと信号を決めた動きにし、書いたバイトと呼出しを記録する。
    use super::*;
    use std::cell::RefCell;
    use std::collections::VecDeque;
    use std::rc::Rc;

    /// 1 回の `write` の動き。
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Step {
        /// 書ける(最大のバイト数。`usize::MAX` は全部)。
        Accept(usize),
        /// errno で失敗する。
        Fail(i32),
        /// 待ちの間に `SIGINT` が届き、`EINTR` で戻る(既定へ戻した後なら、その信号で終わる)。
        Interrupt,
    }

    #[derive(Default, Debug)]
    pub struct State {
        pub written: [Vec<u8>; 2],
        /// 呼ばれた `write` の (fd, 渡したバイト, 結果)。
        pub calls: Vec<(Stream, Vec<u8>, Result<usize, i32>)>,
        pub steps: [VecDeque<Step>; 2],
        /// 決めた動きが尽きた後の動き(既定は全部書ける)。
        pub after: [Option<Step>; 2],
        /// RLIMIT_FSIZE のような、合わせて書ける量(超えると EFBIG)。
        pub limit: [Option<usize>; 2],
        pub terminal: [bool; 2],
        pub block_size: [i64; 2],
        pub sigint_disposition: Option<SigintSetup>,
        pub pending: bool,
        pub restored: bool,
        /// 既定へ戻した後に `SIGINT` が届いて終わった。
        pub killed_by_sigint: bool,
        pub exit_sigint_called: bool,
        pub sigpipe_ignored: Vec<bool>,
        pub sigxfsz_ignored: bool,
        /// 信号の印を確かめた回数(`take_sigint` の呼出し)。
        pub checks: usize,
        /// この回数だけ確かめた直後に `SIGINT` が届く(次の確かめで `KeyboardInterrupt`)。
        pub sigint_after_checks: Option<usize>,
    }

    fn index(stream: Stream) -> usize {
        match stream {
            Stream::Stdout => 0,
            Stream::Stderr => 1,
        }
    }

    #[derive(Clone, Default)]
    pub struct FakeOs(pub Rc<RefCell<State>>);

    impl FakeOs {
        pub fn new() -> Self {
            let os = Self::default();
            os.0.borrow_mut().block_size = [4096, 4096];
            os.0.borrow_mut().sigint_disposition = Some(SigintSetup::Installed);
            os
        }

        pub fn steps(&self, stream: Stream, steps: &[Step]) -> &Self {
            self.0.borrow_mut().steps[index(stream)].extend(steps.iter().copied());
            self
        }

        pub fn after(&self, stream: Stream, step: Step) -> &Self {
            self.0.borrow_mut().after[index(stream)] = Some(step);
            self
        }

        pub fn limit(&self, stream: Stream, bytes: usize) -> &Self {
            self.0.borrow_mut().limit[index(stream)] = Some(bytes);
            self
        }

        pub fn written(&self, stream: Stream) -> String {
            String::from_utf8_lossy(&self.0.borrow().written[index(stream)]).into_owned()
        }

        pub fn written_bytes(&self, stream: Stream) -> Vec<u8> {
            self.0.borrow().written[index(stream)].clone()
        }
    }

    impl Os for FakeOs {
        fn write(&mut self, stream: Stream, data: &[u8]) -> Result<usize, i32> {
            let mut state = self.0.borrow_mut();
            if state.killed_by_sigint {
                return Ok(data.len());
            }
            let slot = index(stream);
            let step = state.steps[slot]
                .pop_front()
                .or(state.after[slot])
                .unwrap_or(Step::Accept(usize::MAX));
            let result = match step {
                Step::Accept(most) => {
                    let mut count = data.len().min(most);
                    if let Some(limit) = state.limit[slot] {
                        let used = state.written[slot].len();
                        if used >= limit && !data.is_empty() {
                            state
                                .calls
                                .push((stream, data.to_vec(), Err(errors::EFBIG)));
                            return Err(errors::EFBIG);
                        }
                        count = count.min(limit - used);
                    }
                    state.written[slot].extend_from_slice(&data[..count]);
                    Ok(count)
                }
                Step::Fail(errno) => Err(errno),
                Step::Interrupt => {
                    if state.restored || state.sigint_disposition != Some(SigintSetup::Installed) {
                        if state.sigint_disposition == Some(SigintSetup::Installed) {
                            state.killed_by_sigint = true;
                            return Ok(data.len());
                        }
                        // 無視された SIGINT は待ちを中断しない。そのまま書ける。
                        state.written[slot].extend_from_slice(data);
                        Ok(data.len())
                    } else {
                        state.pending = true;
                        Err(errors::EINTR)
                    }
                }
            };
            state.calls.push((stream, data.to_vec(), result));
            result
        }

        fn take_sigint(&mut self) -> bool {
            let mut state = self.0.borrow_mut();
            let pending = std::mem::take(&mut state.pending);
            state.checks += 1;
            if state.sigint_after_checks == Some(state.checks) {
                state.pending = true;
            }
            pending
        }

        fn is_terminal(&mut self, stream: Stream) -> bool {
            self.0.borrow().terminal[index(stream)]
        }

        fn block_size(&mut self, stream: Stream) -> i64 {
            self.0.borrow().block_size[index(stream)]
        }

        fn thread_id(&mut self) -> usize {
            0x7f12_3456_789a
        }

        fn set_sigpipe_ignored(&mut self, ignored: bool) {
            self.0.borrow_mut().sigpipe_ignored.push(ignored);
        }

        fn ignore_sigxfsz(&mut self) {
            self.0.borrow_mut().sigxfsz_ignored = true;
        }

        fn install_sigint(&mut self) -> SigintSetup {
            self.0
                .borrow()
                .sigint_disposition
                .unwrap_or(SigintSetup::Installed)
        }

        fn restore_default_sigint(&mut self) {
            let mut state = self.0.borrow_mut();
            state.restored = true;
            state.pending = false;
        }

        fn exit_sigint(&mut self) -> ExitCode {
            self.0.borrow_mut().exit_sigint_called = true;
            ExitCode::new(130)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::{FakeOs, Step};
    use super::*;
    use crate::md_tables_to_xlsx::errors::frames;
    use dev_workflow_platform::r4e::StandardFd;

    const EPIPE: i32 = 32;
    const ENOSPC: i32 = 28;

    fn startup(fds: [FdState; 3], sigpipe: Disposition) -> StartupState {
        let fd = |state: FdState| StandardFd {
            state,
            python_check_errno: (state == FdState::Closed).then_some(9),
        };
        StartupState {
            fds: fds.map(fd),
            sigpipe: Some(sigpipe),
            sigint: Some(Disposition::Default),
        }
    }

    fn pipes() -> StartupState {
        startup(
            [
                FdState::Open(FdKind::CharacterDevice),
                FdState::Open(FdKind::Fifo),
                FdState::Open(FdKind::Fifo),
            ],
            Disposition::Default,
        )
    }

    fn env_of(pairs: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<Vec<u8>> {
        move |name| {
            pairs
                .iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| value.as_bytes().to_vec())
        }
    }

    fn start(os: &FakeOs, pairs: &'static [(&'static str, &'static str)]) -> Runtime {
        Runtime::start_with(&pipes(), &env_of(pairs), Box::new(os.clone())).unwrap()
    }

    fn ok_line() -> PyStr {
        PyStr::from("OK out/book.xlsx にシート 1 件を出力")
    }

    /// 旧版の main の出力の流れ(WARN → OK)を、呼出履歴つきで再現する。
    fn legacy_flow(py: &mut Runtime, missing: &[&str], ok: bool) -> Outcome {
        for path in missing {
            let mut line = PyStr::from("WARN 入力が存在しない: ");
            line.push_str(path);
            if let Err(error) = py.print(Some(Stream::Stderr), &line) {
                return Outcome::raised(error.called_from(frames::PRINT_WARN));
            }
        }
        if !ok {
            return match py.print(None, &PyStr::from("表が見つからなかった(出力なし)"))
            {
                Ok(()) => Outcome::Exit(1),
                Err(error) => Outcome::raised(error.called_from(frames::PRINT_NO_TABLES)),
            };
        }
        match py.print(None, &ok_line()) {
            Ok(()) => Outcome::Exit(0),
            Err(error) => Outcome::raised(error.called_from(frames::PRINT_OK)),
        }
    }

    const STDOUT_REPR: &str =
        "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n";

    fn traceback_of(lines: &[(&str, u32, &str, &str)], last: &str) -> String {
        let mut text = String::from("Traceback (most recent call last):\n");
        for (file, line, function, source) in lines {
            text.push_str(&format!(
                "  File \"{file}\", line {line}, in {function}\n    {source}\n"
            ));
        }
        text.push_str(last);
        text.push('\n');
        text
    }

    fn print_ok_traceback(last: &str) -> String {
        traceback_of(
            &[
                ("md_tables_to_xlsx.py", 241, "<module>", "sys.exit(main())"),
                (
                    "md_tables_to_xlsx.py",
                    233,
                    "main",
                    "write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)",
                ),
                (
                    "md_tables_to_xlsx.py",
                    197,
                    "write_xlsx",
                    "print(f\"OK {out} にシート {len(wb.sheetnames)} 件を出力\")",
                ),
            ],
            last,
        )
    }

    fn print_no_tables_traceback(last: &str) -> String {
        traceback_of(
            &[
                ("md_tables_to_xlsx.py", 241, "<module>", "sys.exit(main())"),
                (
                    "md_tables_to_xlsx.py",
                    223,
                    "main",
                    "print(\"表が見つからなかった(出力なし)\")",
                ),
            ],
            last,
        )
    }

    // ------------------------------------------------------------ 文字列

    #[test]
    fn os_bytes_use_surrogateescape_both_ways() {
        let text = PyStr::from_os_bytes(b"a\xff\xe3\x81\x82\xe3\x81Z\xed\xa0\x80");
        assert_eq!(
            text.as_code_points(),
            [
                0x61, 0xdcff, 0x3042, 0xdce3, 0xdc81, 0x5a, 0xdced, 0xdca0, 0xdc80
            ]
        );
        assert_eq!(
            text.encode_surrogateescape().unwrap(),
            b"a\xff\xe3\x81\x82\xe3\x81Z\xed\xa0\x80"
        );
        assert_eq!(
            text.encode_backslashreplace(),
            "a\\udcffあ\\udce3\\udc81Z\\udced\\udca0\\udc80".as_bytes()
        );
        let lone = PyStr::from_code_points(vec![0x41, 0xdc80, 0xd800, 0xdcff, 0x42]);
        assert_eq!(lone.encode_surrogateescape(), Err((2, 4)));
    }

    // ------------------------------------------------------------ 設定

    #[test]
    fn env_numbers_follow_c_strtol_and_strtoul() {
        for (value, expected) in [
            ("2", 2),
            (" 2", 2),
            ("\t2", 2),
            ("\x0b2", 2),
            ("+2", 2),
            ("02", 2),
            ("-0", 0),
            ("2147483647", 2147483647),
            ("2 ", 1),
            ("0x2", 1),
            ("2147483648", 1),
            ("99999999999", 1),
            ("99999999999999999999", 1),
            ("-2", 1),
            ("abc", 1),
            ("+", 1),
            (" ", 1),
            ("1", 1),
            ("3", 3),
        ] {
            assert_eq!(env_flag(Some(value.as_bytes())), expected, "{value:?}");
        }
        assert_eq!(env_flag(None), 0);
        for (value, valid) in [
            ("random", true),
            (" 5", true),
            ("\t5", true),
            ("+5", true),
            ("-0", true),
            ("0", true),
            ("4294967295", true),
            ("-18446744073709551615", true),
            ("5 ", false),
            ("4294967296", false),
            ("-1", false),
            ("abc", false),
            ("Random", false),
            ("18446744073709551616", false),
            (" ", false),
            ("+", false),
        ] {
            assert_eq!(hash_seed_is_valid(value.as_bytes()), valid, "{value:?}");
        }
    }

    #[test]
    fn config_reads_python_variables() {
        let config = Config::read(&env_of(&[
            ("PYTHONOPTIMIZE", " 2"),
            ("PYTHONUNBUFFERED", "0"),
        ]))
        .unwrap();
        assert_eq!(config.optimize, 2);
        assert!(config.strips_docstrings());
        assert!(!config.unbuffered);
        assert_eq!(config.int_max_str_digits_limit(), 4300);
        let config = Config::read(&env_of(&[
            ("PYTHONOPTIMIZE", "2 "),
            ("PYTHONUNBUFFERED", "x"),
            ("PYTHONINTMAXSTRDIGITS", "-0"),
        ]))
        .unwrap();
        assert_eq!(config.optimize, 1);
        assert!(!config.strips_docstrings());
        assert!(config.unbuffered);
        assert_eq!(config.int_max_str_digits_limit(), 0);
        let empty = Config::read(&env_of(&[
            ("PYTHONOPTIMIZE", ""),
            ("PYTHONUNBUFFERED", ""),
            ("PYTHONHASHSEED", ""),
            ("PYTHONINTMAXSTRDIGITS", ""),
        ]))
        .unwrap();
        assert_eq!(
            empty,
            Config {
                unbuffered: false,
                optimize: 0,
                int_max_str_digits: -1
            }
        );
        for (value, ok) in [
            ("0", true),
            ("640", true),
            (" 640", true),
            ("+640", true),
            ("639", false),
            ("1", false),
            ("abc", false),
            ("-1", false),
            ("640 ", false),
            ("99999999999999999999", false),
        ] {
            let pairs: &'static [(&str, &str)] =
                Box::leak(vec![("PYTHONINTMAXSTRDIGITS", value)].into_boxed_slice());
            assert_eq!(Config::read(&env_of(pairs)).is_ok(), ok, "{value:?}");
        }
    }

    // ------------------------------------------------------------ 起動の失敗

    #[test]
    fn startup_failures_match_the_legacy_text() {
        let os = FakeOs::new();
        let result = Runtime::start_with(
            &pipes(),
            &env_of(&[("PYTHONINTMAXSTRDIGITS", "639"), ("PYTHONHASHSEED", "abc")]),
            Box::new(os.clone()),
        );
        assert_eq!(result.err(), Some(ExitCode::new(1)));
        // 両方が不正なら、先に読む PYTHONHASHSEED の誤り。SIGPIPE は継いだ既定のまま。
        assert_eq!(
            os.written(Stream::Stderr),
            "Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\nPython runtime state: preinitialized\n\n"
        );
        assert_eq!(os.0.borrow().sigpipe_ignored, [false]);
        assert!(!os.0.borrow().sigxfsz_ignored);

        let os = FakeOs::new();
        let result = Runtime::start_with(
            &pipes(),
            &env_of(&[("PYTHONINTMAXSTRDIGITS", "639")]),
            Box::new(os.clone()),
        );
        assert_eq!(result.err(), Some(ExitCode::new(1)));
        assert_eq!(
            os.written(Stream::Stderr),
            "Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\nPython runtime state: preinitialized\n\n"
        );

        let directory = FdState::Open(FdKind::Directory);
        let fifo = FdState::Open(FdKind::Fifo);
        let current =
            "\nCurrent thread 0x00007f123456789a (most recent call first):\n  <no Python frame>\n";
        for (fds, expected) in [
            (
                [directory, directory, fifo],
                format!(
                    "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n{current}"
                ),
            ),
            (
                [fifo, directory, fifo],
                format!(
                    "Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 1\n{current}"
                ),
            ),
            (
                [fifo, fifo, directory],
                format!(
                    "Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 2\n{current}"
                ),
            ),
        ] {
            let os = FakeOs::new();
            let result = Runtime::start_with(
                &startup(fds, Disposition::Ignore),
                &env_of(&[]),
                Box::new(os.clone()),
            );
            assert_eq!(result.err(), Some(ExitCode::new(1)));
            assert_eq!(os.written(Stream::Stderr), expected);
            // 継いだ SIGPIPE が無視なら戻さない。信号の初期化は標準入出力の前。
            assert_eq!(os.0.borrow().sigpipe_ignored, [true]);
            assert!(os.0.borrow().sigxfsz_ignored);
        }
        // 標準入力が閉じていれば、ディレクトリの判定はしない。
        let os = FakeOs::new();
        let closed_stdin = startup([FdState::Closed, fifo, fifo], Disposition::Default);
        assert!(Runtime::start_with(&closed_stdin, &env_of(&[]), Box::new(os.clone())).is_ok());
        assert_eq!(os.0.borrow().sigpipe_ignored, [false, true]);
    }

    // ------------------------------------------------------------ 溜め方

    #[test]
    fn stdout_is_written_at_exit_and_stderr_per_line() {
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        assert!(matches!(
            legacy_flow(&mut py, &["doc/none.md"], true),
            Outcome::Exit(0)
        ));
        // 標準エラーは print の時点、標準出力はまだ書かない。
        assert_eq!(
            os.written(Stream::Stderr),
            "WARN 入力が存在しない: doc/none.md\n"
        );
        assert_eq!(os.written(Stream::Stdout), "");
        let code = py.finish(Outcome::Exit(0));
        assert_eq!(code, ExitCode::new(0));
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );
        // 標準出力は 1 回の write、標準エラーは 1 行で 1 回の write。
        let calls = os.0.borrow().calls.clone();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].0, Stream::Stderr);
        assert_eq!(calls[1].0, Stream::Stdout);
    }

    #[test]
    fn a_terminal_stdout_is_written_per_line() {
        let os = FakeOs::new();
        os.0.borrow_mut().terminal[0] = true;
        let mut py = start(&os, &[]);
        py.print(None, &ok_line()).unwrap();
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );
        assert_eq!(py.finish(Outcome::Exit(0)), ExitCode::new(0));
        assert_eq!(os.0.borrow().calls.len(), 1);
    }

    #[test]
    fn unbuffered_streams_write_each_piece_immediately() {
        let os = FakeOs::new();
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        py.print(None, &ok_line()).unwrap();
        let calls = os.0.borrow().calls.clone();
        let pieces = calls.iter().map(|call| call.1.clone()).collect::<Vec<_>>();
        assert_eq!(
            pieces,
            [ok_line().encode_surrogateescape().unwrap(), b"\n".to_vec()]
        );
    }

    #[test]
    fn large_output_is_chunked_like_python() {
        // 標準エラーが閉じた起動の WARN は標準出力へ溜まり、8192 バイトごとに下へ渡る。
        let os = FakeOs::new();
        let closed_stderr = startup(
            [
                FdState::Open(FdKind::CharacterDevice),
                FdState::Open(FdKind::Fifo),
                FdState::Closed,
            ],
            Disposition::Default,
        );
        let mut py =
            Runtime::start_with(&closed_stderr, &env_of(&[]), Box::new(os.clone())).unwrap();
        let names = (0..400)
            .map(|index| format!("doc/missing{index:04}.md"))
            .collect::<Vec<_>>();
        let names = names.iter().map(String::as_str).collect::<Vec<_>>();
        assert!(matches!(
            legacy_flow(&mut py, &names, true),
            Outcome::Exit(0)
        ));
        let lengths = || {
            os.0.borrow()
                .calls
                .iter()
                .map(|call| call.1.len())
                .collect::<Vec<_>>()
        };
        // 50 バイトの行が 163 個(8150 バイト)溜まると、次の行の文で 8192 を超えるので、その前に
        // 下へ渡す。溜め(4096)より大きいので、BufferedWriter は直接 1 回で書く。
        assert_eq!(lengths(), [8150, 8150]);
        assert_eq!(py.finish(Outcome::Exit(0)), ExitCode::new(0));
        // 残りの 74 行と OK の行(3745 バイト)は溜めに収まり、終了時に 1 回で書く。
        assert_eq!(lengths(), [8150, 8150, 3745]);
        let text = os.written(Stream::Stdout);
        assert_eq!(text.lines().count(), 401);
        assert!(text.ends_with("OK out/book.xlsx にシート 1 件を出力\n"));
    }

    // ------------------------------------------------------------ 閉じた fd

    #[test]
    fn closed_fds_redirect_or_drop_like_python() {
        let fifo = FdState::Open(FdKind::Fifo);
        // fd 2 が閉じた: WARN は標準出力へ。呼出履歴は捨てる。
        let os = FakeOs::new();
        let mut py = Runtime::start_with(
            &startup([fifo, fifo, FdState::Closed], Disposition::Default),
            &env_of(&[]),
            Box::new(os.clone()),
        )
        .unwrap();
        assert!(!py.has(Stream::Stderr));
        assert!(matches!(
            legacy_flow(&mut py, &["none.md"], true),
            Outcome::Exit(0)
        ));
        let error = PyException::new(types::VALUE_ERROR, "x").called_from(frames::PRINT_OK);
        assert_eq!(py.finish(Outcome::raised(error)), ExitCode::new(1));
        assert_eq!(
            os.written(Stream::Stdout),
            "WARN 入力が存在しない: none.md\nOK out/book.xlsx にシート 1 件を出力\n"
        );
        assert_eq!(os.written(Stream::Stderr), "");

        // fd 1 が閉じた: OK の行は出ない。WARN は標準エラーへ。
        let os = FakeOs::new();
        let mut py = Runtime::start_with(
            &startup([fifo, FdState::Closed, fifo], Disposition::Default),
            &env_of(&[]),
            Box::new(os.clone()),
        )
        .unwrap();
        assert!(matches!(
            legacy_flow(&mut py, &["none.md"], true),
            Outcome::Exit(0)
        ));
        assert_eq!(py.finish(Outcome::Exit(0)), ExitCode::new(0));
        assert_eq!(os.written(Stream::Stdout), "");
        assert_eq!(
            os.written(Stream::Stderr),
            "WARN 入力が存在しない: none.md\n"
        );

        // 両方が閉じた: 何も書かない。write は AttributeError。
        let os = FakeOs::new();
        let mut py = Runtime::start_with(
            &startup(
                [fifo, FdState::Closed, FdState::Closed],
                Disposition::Default,
            ),
            &env_of(&[]),
            Box::new(os.clone()),
        )
        .unwrap();
        assert!(matches!(
            legacy_flow(&mut py, &["none.md"], true),
            Outcome::Exit(0)
        ));
        let error = py.write(Stream::Stderr, &PyStr::from("x")).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "AttributeError: 'NoneType' object has no attribute 'write'"
        );
        assert_eq!(py.finish(Outcome::Exit(0)), ExitCode::new(0));
        assert!(os.0.borrow().calls.is_empty());
    }

    // ------------------------------------------------------------ 故障

    #[test]
    fn exit_flush_failures_end_with_120() {
        for (errno, line) in [
            (EPIPE, "BrokenPipeError: [Errno 32] Broken pipe\n"),
            (ENOSPC, "OSError: [Errno 28] No space left on device\n"),
        ] {
            let os = FakeOs::new();
            os.after(Stream::Stdout, Step::Fail(errno));
            let mut py = start(&os, &[]);
            let outcome = legacy_flow(&mut py, &[], true);
            assert_eq!(py.finish(outcome), ExitCode::new(120));
            assert_eq!(os.written(Stream::Stderr), format!("{STDOUT_REPR}{line}"));
        }
    }

    #[test]
    fn print_failures_unbuffered_raise_at_the_print() {
        let os = FakeOs::new();
        os.after(Stream::Stdout, Step::Fail(EPIPE));
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &[], true);
        assert_eq!(py.finish(outcome), ExitCode::new(1));
        assert_eq!(
            os.written(Stream::Stderr),
            print_ok_traceback("BrokenPipeError: [Errno 32] Broken pipe")
        );
    }

    #[test]
    fn partial_writes_follow_the_legacy_measurements() {
        // 標準出力のファイルの上限 10 バイト(cases.json の measured の partial_write)。
        let os = FakeOs::new();
        os.limit(Stream::Stdout, 10);
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &[], false);
        assert_eq!(py.finish(outcome), ExitCode::new(120));
        assert_eq!(
            os.written_bytes(Stream::Stdout),
            b"\xe8\xa1\xa8\xe3\x81\x8c\xe8\xa6\x8b\xe3"
        );
        assert_eq!(
            os.written(Stream::Stderr),
            format!("{STDOUT_REPR}OSError: [Errno 27] File too large\n")
        );

        let os = FakeOs::new();
        os.limit(Stream::Stdout, 10);
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &[], false);
        assert_eq!(py.finish(outcome), ExitCode::new(1));
        assert_eq!(
            os.written_bytes(Stream::Stdout),
            b"\xe8\xa1\xa8\xe3\x81\x8c\xe8\xa6\x8b\xe3"
        );
        assert_eq!(
            os.written(Stream::Stderr),
            print_no_tables_traceback("OSError: [Errno 27] File too large")
        );

        // 上限 0 は、最初の書き込みから失敗する。
        let os = FakeOs::new();
        os.limit(Stream::Stdout, 0);
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &[], false);
        assert_eq!(py.finish(outcome), ExitCode::new(1));
        assert_eq!(os.written_bytes(Stream::Stdout), b"");

        // 標準エラーのファイルの上限 10 バイト: WARN の先頭 10 バイトが残る。
        for (unbuffered, code) in [(false, 120), (true, 1)] {
            let os = FakeOs::new();
            os.limit(Stream::Stderr, 10);
            let mut py = if unbuffered {
                start(&os, &[("PYTHONUNBUFFERED", "1")])
            } else {
                start(&os, &[])
            };
            let outcome = legacy_flow(&mut py, &["none.md"], true);
            assert_eq!(py.finish(outcome), ExitCode::new(code), "{unbuffered}");
            assert_eq!(
                os.written_bytes(Stream::Stderr),
                "WARN 入\u{529b}".as_bytes()[..10].to_vec()
            );
        }
    }

    // ------------------------------------------------------------ 書き込みを待たない fd

    const BLOCKING_LINE: &str =
        "BlockingIOError: [Errno {EAGAIN}] write could not complete without blocking";

    fn blocking_line() -> String {
        BLOCKING_LINE.replace("{EAGAIN}", &errors::EAGAIN.to_string())
    }

    #[test]
    fn a_full_nonblocking_stdout_fails_at_exit_with_the_buffered_writer_text() {
        // 既定の溜め方: OK の行は溜めに残り、終了時の書き出しが BlockingIOError で終了 120。
        let os = FakeOs::new();
        os.after(Stream::Stdout, Step::Fail(errors::EAGAIN));
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &["none.md"], true);
        assert!(matches!(outcome, Outcome::Exit(0)));
        assert_eq!(py.finish(outcome), ExitCode::new(120));
        assert_eq!(
            os.written(Stream::Stderr),
            format!(
                "WARN 入力が存在しない: none.md\n{STDOUT_REPR}{}\n",
                blocking_line()
            )
        );
        assert_eq!(os.written(Stream::Stdout), "");

        // PYTHONUNBUFFERED: FileIO.write は None を返し、TextIOWrapper はそれを見ないので、行は
        // 黙って捨てられ終了 0。
        let os = FakeOs::new();
        os.after(Stream::Stdout, Step::Fail(errors::EAGAIN));
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &[], true);
        assert!(matches!(outcome, Outcome::Exit(0)));
        assert_eq!(py.finish(outcome), ExitCode::new(0));
        assert_eq!(os.written(Stream::Stdout), "");
        assert_eq!(os.written(Stream::Stderr), "");
        // print の文と改行の 2 回を書こうとした(書き直さない)。
        let tried = os.0.borrow().calls.len();
        assert_eq!(tried, 2);
    }

    #[test]
    fn a_full_nonblocking_stderr_drops_unbuffered_lines_and_raises_buffered_ones() {
        // PYTHONUNBUFFERED: WARN の行は捨てられ、続きは進む(終了 0)。拒否の行も捨てて終了 2。
        let os = FakeOs::new();
        os.after(Stream::Stderr, Step::Fail(errors::EAGAIN));
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &["none.md"], true);
        assert!(matches!(outcome, Outcome::Exit(0)));
        py.print(Some(Stream::Stderr), &PyStr::from("ERROR 出力を拒否: x"))
            .unwrap();
        assert_eq!(py.finish(Outcome::Exit(2)), ExitCode::new(2));
        assert_eq!(os.written(Stream::Stderr), "");
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );

        // 既定の溜め方: 行ごとの書き出しが BlockingIOError になり、print から例外が出る。終了時の
        // 書き出しも失敗して終了 120。
        let os = FakeOs::new();
        os.after(Stream::Stderr, Step::Fail(errors::EAGAIN));
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &["none.md"], true);
        let Outcome::Raised(uncaught) = &outcome else {
            panic!("print から例外が出る");
        };
        assert_eq!(
            uncaught.exception.type_line().to_string_lossy(),
            blocking_line()
        );
        assert_eq!(py.finish(outcome), ExitCode::new(120));
        assert_eq!(os.written(Stream::Stderr), "");
        assert_eq!(os.written(Stream::Stdout), "");
    }

    #[test]
    fn the_buffered_writer_keeps_unwritten_bytes_like_cpython() {
        let os = FakeOs::new();
        let mut boxed: Box<dyn Os> = Box::new(os.clone());
        let stream = Stream::Stdout;
        let mut buffer = Buffered::new(16);
        // 溜めに 10 バイト。書き出しは 4 バイトだけ通って一杯になる。
        buffer.write(&mut *boxed, stream, b"0123456789").unwrap();
        os.steps(stream, &[Step::Accept(4), Step::Fail(errors::EAGAIN)]);
        let error = buffer.flush(&mut *boxed, stream).unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), blocking_line());
        assert_eq!(error.errno, Some(errors::EAGAIN));
        assert_eq!(os.written(stream), "0123");
        // 収まらない 12 バイト: 先の書き出しが一杯なので、残りの 6 バイトを先頭へ寄せ、入る 10 バイト
        // だけ溜めて BlockingIOError。
        os.steps(stream, &[Step::Fail(errors::EAGAIN)]);
        let error = buffer
            .write(&mut *boxed, stream, b"abcdefghijkl")
            .unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), blocking_line());
        buffer.flush(&mut *boxed, stream).unwrap();
        assert_eq!(os.written(stream), "0123456789abcdefghij");
        // 寄せた後に全部が入るときは、溜めて成功する(例外は出ない)。
        buffer.write(&mut *boxed, stream, b"0123456789AB").unwrap();
        os.steps(stream, &[Step::Accept(8), Step::Fail(errors::EAGAIN)]);
        buffer.flush(&mut *boxed, stream).unwrap_err();
        os.steps(stream, &[Step::Fail(errors::EAGAIN)]);
        buffer.write(&mut *boxed, stream, b"CDEFGH").unwrap();
        assert!(os.0.borrow().steps[0].is_empty());
        buffer.flush(&mut *boxed, stream).unwrap();
        assert_eq!(os.written(stream), "0123456789abcdefghij0123456789ABCDEFGH");
        // 溜めより大きい直接の書き込みが一杯: 溜めの大きさ(16 バイト)だけ溜めて、残りは捨てる。
        os.steps(stream, &[Step::Fail(errors::EAGAIN)]);
        let error = buffer
            .write(&mut *boxed, stream, b"ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            .unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), blocking_line());
        buffer.flush(&mut *boxed, stream).unwrap();
        assert!(os.written(stream).ends_with("GHABCDEFGHIJKLMNOP"));
    }

    #[test]
    fn unraisable_lines_use_the_cpython_write_units() {
        // PYTHONUNBUFFERED の標準エラー: 1 回の write が 1 つの単位。
        let os = FakeOs::new();
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        py.write_unraisable(&PyStr::from("<x>"), &PyException::os(ENOSPC));
        let pieces =
            os.0.borrow()
                .calls
                .iter()
                .map(|call| String::from_utf8_lossy(&call.1).into_owned())
                .collect::<Vec<_>>();
        assert_eq!(
            pieces,
            [
                "Exception ignored in: ",
                "<x>",
                "\n",
                "OSError",
                ": ",
                "[Errno 28] No space left on device",
                "\n"
            ]
        );
        // repr と例外の文を書けなければ、誤りを捨てて代わりの文を書く。
        let os = FakeOs::new();
        os.steps(
            Stream::Stderr,
            &[
                Step::Accept(usize::MAX),
                Step::Fail(EPIPE),
                Step::Accept(usize::MAX),
                Step::Accept(usize::MAX),
                Step::Accept(usize::MAX),
                Step::Accept(usize::MAX),
                Step::Fail(EPIPE),
            ],
        );
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        py.write_unraisable(&PyStr::from("<x>"), &PyException::os(ENOSPC));
        assert_eq!(
            os.written(Stream::Stderr),
            "Exception ignored in: <object repr() failed>\nOSError: <exception str() failed>\n"
        );
        // 一杯の標準エラー(PYTHONUNBUFFERED)では、どの write も None で、失敗にならない。
        let os = FakeOs::new();
        os.after(Stream::Stderr, Step::Fail(errors::EAGAIN));
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        py.write_unraisable(&PyStr::from("<x>"), &PyException::os(ENOSPC));
        assert_eq!(os.0.borrow().calls.len(), 7);
        assert_eq!(os.written(Stream::Stderr), "");
    }

    #[test]
    fn warning_write_errors_are_ignored_but_stay_buffered() {
        let warning = PyStr::from(
            "openpyxl/workbook/child.py:99: UserWarning: Title is more than 31 characters. Some applications may not be able to read the file\n  warnings.warn(\"Title is more than 31 characters. Some applications may not be able to read the file\")\n",
        );
        for errno in [EPIPE, errors::EAGAIN] {
            for (unbuffered, code) in [(false, 120), (true, 0)] {
                let os = FakeOs::new();
                os.after(Stream::Stderr, Step::Fail(errno));
                let mut py = if unbuffered {
                    start(&os, &[("PYTHONUNBUFFERED", "1")])
                } else {
                    start(&os, &[])
                };
                py.show_warning(&warning).unwrap();
                let outcome = legacy_flow(&mut py, &[], true);
                assert_eq!(
                    py.finish(outcome),
                    ExitCode::new(code),
                    "{errno} {unbuffered}"
                );
            }
        }
        // 標準エラーが閉じた起動では捨てる。
        let os = FakeOs::new();
        let fifo = FdState::Open(FdKind::Fifo);
        let mut py = Runtime::start_with(
            &startup([fifo, fifo, FdState::Closed], Disposition::Default),
            &env_of(&[]),
            Box::new(os.clone()),
        )
        .unwrap();
        py.show_warning(&warning).unwrap();
        assert_eq!(py.finish(Outcome::Exit(0)), ExitCode::new(0));
        assert!(os.0.borrow().calls.is_empty());
    }

    // ------------------------------------------------------------ SIGINT

    #[test]
    fn sigint_while_stdout_flush_blocks_at_exit_counts_like_python() {
        // 1 回: flush_io の中で KeyboardInterrupt が捨てられ、書き直して終了 0。
        let os = FakeOs::new();
        os.steps(Stream::Stdout, &[Step::Interrupt]);
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &[], true);
        assert_eq!(py.finish(outcome), ExitCode::new(0));
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );
        assert_eq!(os.written(Stream::Stderr), "");

        // 2 回: flush_std_files の中で Exception ignored と KeyboardInterrupt: 。閉じるときに書けて終了 120。
        let os = FakeOs::new();
        os.steps(Stream::Stdout, &[Step::Interrupt, Step::Interrupt]);
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &[], true);
        assert_eq!(py.finish(outcome), ExitCode::new(120));
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );
        assert_eq!(
            os.written(Stream::Stderr),
            format!("{STDOUT_REPR}KeyboardInterrupt: \n")
        );
        assert!(!os.0.borrow().killed_by_sigint);

        // 3 回: 既定へ戻した後の SIGINT で終わる。
        let os = FakeOs::new();
        os.steps(
            Stream::Stdout,
            &[Step::Interrupt, Step::Interrupt, Step::Interrupt],
        );
        let mut py = start(&os, &[]);
        let outcome = legacy_flow(&mut py, &[], true);
        let _ = py.finish(outcome);
        assert!(os.0.borrow().killed_by_sigint);
        assert_eq!(os.written(Stream::Stdout), "");
        assert_eq!(
            os.written(Stream::Stderr),
            format!("{STDOUT_REPR}KeyboardInterrupt: \n")
        );
    }

    #[test]
    fn sigint_at_an_unbuffered_print_raises_keyboard_interrupt() {
        let os = FakeOs::new();
        os.steps(Stream::Stdout, &[Step::Interrupt]);
        let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
        let outcome = legacy_flow(&mut py, &[], true);
        assert_eq!(py.finish(outcome), ExitCode::new(130));
        assert!(os.0.borrow().exit_sigint_called);
        assert_eq!(
            os.written(Stream::Stderr),
            print_ok_traceback("KeyboardInterrupt")
        );
    }

    #[test]
    fn an_ignored_sigint_is_not_interrupting() {
        let os = FakeOs::new();
        os.0.borrow_mut().sigint_disposition = Some(SigintSetup::Ignored);
        os.steps(Stream::Stdout, &[Step::Interrupt]);
        let mut py = start(&os, &[]);
        assert_eq!(py.sigint_setup(), SigintSetup::Ignored);
        let outcome = legacy_flow(&mut py, &[], true);
        assert_eq!(py.finish(outcome), ExitCode::new(0));
        assert_eq!(
            os.written(Stream::Stdout),
            "OK out/book.xlsx にシート 1 件を出力\n"
        );
        assert!(!os.0.borrow().exit_sigint_called);
    }

    #[test]
    fn sigint_while_stderr_is_full_counts_like_python() {
        let warn_frames = traceback_of(
            &[
                ("md_tables_to_xlsx.py", 241, "<module>", "sys.exit(main())"),
                (
                    "md_tables_to_xlsx.py",
                    219,
                    "main",
                    "print(f\"WARN 入力が存在しない: {p}\", file=sys.stderr)",
                ),
            ],
            "KeyboardInterrupt",
        );
        let warn = |index: usize| format!("WARN 入力が存在しない: doc/missing{index:04}.md\n");
        let names = (0..3)
            .map(|index| format!("doc/missing{index:04}.md"))
            .collect::<Vec<_>>();
        let names = names.iter().map(String::as_str).collect::<Vec<_>>();
        // 既定の溜め方: 2 行書けた後、3 行目で詰まる。
        let base = [Step::Accept(usize::MAX), Step::Accept(usize::MAX)];
        // 1 回: 呼出履歴の全部。詰まった WARN の行も後から書ける。
        // 2 回: 2 回目は flush_io の中で捨てられ、同じ結果。
        // 3 回: 呼出履歴の最初の行を書く所で中断され、改行だけが続く。
        for (count, tail) in [
            (1, warn_frames.clone()),
            (2, warn_frames.clone()),
            (3, "Traceback (most recent call last):\n\n".to_owned()),
        ] {
            let os = FakeOs::new();
            os.steps(Stream::Stderr, &base);
            os.steps(Stream::Stderr, &vec![Step::Interrupt; count]);
            let mut py = start(&os, &[]);
            let outcome = legacy_flow(&mut py, &names, true);
            assert_eq!(py.finish(outcome), ExitCode::new(130), "{count}");
            assert_eq!(
                os.written(Stream::Stderr),
                format!("{}{}{}{tail}", warn(0), warn(1), warn(2)),
                "{count}"
            );
            assert_eq!(os.written(Stream::Stdout), "", "{count}");
        }
        // PYTHONUNBUFFERED: 詰まった WARN は失われる。2 回目は呼出履歴の 2 行目で中断され、
        // 改行だけが続く(パイプの残りに入る短い行は書ける)。
        for (count, tail) in [
            (1, warn_frames.clone()),
            (2, "Traceback (most recent call last):\n\n".to_owned()),
        ] {
            let os = FakeOs::new();
            // 1 行は WARN の文と改行の 2 回の write。
            os.steps(Stream::Stderr, &[Step::Accept(usize::MAX); 4]);
            os.steps(Stream::Stderr, &[Step::Interrupt]);
            if count == 2 {
                os.steps(Stream::Stderr, &[Step::Accept(usize::MAX), Step::Interrupt]);
            }
            let mut py = start(&os, &[("PYTHONUNBUFFERED", "1")]);
            let outcome = legacy_flow(&mut py, &names, true);
            assert_eq!(py.finish(outcome), ExitCode::new(130), "{count}");
            assert_eq!(
                os.written(Stream::Stderr),
                format!("{}{}{tail}", warn(0), warn(1)),
                "{count}"
            );
        }
    }

    // ------------------------------------------------------------ 例外の表示

    /// 旧版の実測(読み取り専用のディレクトリの下に 6 階層の無い親を作る
    /// `pathlib.Path(...).mkdir(parents=True)`)と同じ字面になる。各階層の FileNotFoundError が
    /// 処理中の例外として順につながり、最後の PermissionError の再帰の段は 4 回目からまとめる。
    #[test]
    fn chained_and_repeated_tracebacks_render_like_python() {
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        let caller = errors::Frame::new(
            "W/t.py",
            2,
            "<module>",
            "pathlib.Path(\"W/ro/a/b/c/d/e/f\").mkdir(parents=True)",
        );
        let mut chained: Option<PyException> = None;
        for path in [
            "W/ro/a/b/c/d/e/f",
            "W/ro/a/b/c/d/e",
            "W/ro/a/b/c/d",
            "W/ro/a/b/c",
            "W/ro/a/b",
        ] {
            let mut error = PyException::os_with_filename(2, Filename::Str(PyStr::from(path)))
                .called_from(&[errors::frames::PATHLIB_MKDIR_ACCESSOR]);
            if let Some(previous) = chained.take() {
                error = error.with_context(previous);
            }
            chained = Some(error);
        }
        let outer = PyException::os_with_filename(13, Filename::Str(PyStr::from("W/ro/a")))
            .called_from(&[errors::frames::PATHLIB_MKDIR_ACCESSOR])
            .called_from(&[errors::frames::PATHLIB_MKDIR_PARENT; 5])
            .called_from(&[caller])
            .with_context(chained.unwrap());
        py.display_exception(&outer);
        let mkdir =
            "  File \"pathlib.py\", line 1175, in mkdir\n    self._accessor.mkdir(self, mode)\n";
        let parent = "  File \"pathlib.py\", line 1179, in mkdir\n    self.parent.mkdir(parents=True, exist_ok=True)\n";
        let link = "\nDuring handling of the above exception, another exception occurred:\n\n";
        let mut expected = String::new();
        for path in [
            "W/ro/a/b/c/d/e/f",
            "W/ro/a/b/c/d/e",
            "W/ro/a/b/c/d",
            "W/ro/a/b/c",
            "W/ro/a/b",
        ] {
            expected.push_str(&format!(
                "Traceback (most recent call last):\n{mkdir}FileNotFoundError: [Errno 2] No such file or directory: '{path}'\n{link}"
            ));
        }
        expected.push_str(&format!(
            "Traceback (most recent call last):\n  File \"W/t.py\", line 2, in <module>\n    pathlib.Path(\"W/ro/a/b/c/d/e/f\").mkdir(parents=True)\n{parent}{parent}{parent}  [Previous line repeated 2 more times]\n{mkdir}PermissionError: [Errno 13] Permission denied: 'W/ro/a'\n"
        ));
        assert_eq!(os.written(Stream::Stderr), expected);
        // `from` でつないだ元は、別の文でつなぎ、処理中の例外は出さない。
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        let cause = PyException::new(types::RECURSION_ERROR, "maximum recursion depth exceeded");
        let outer = PyException::new(types::VALUE_ERROR, "x")
            .with_context(PyException::new(types::RUNTIME_ERROR, "hidden"))
            .with_cause(cause);
        py.display_exception(&outer);
        assert_eq!(
            os.written(Stream::Stderr),
            "RecursionError: maximum recursion depth exceeded\n\nThe above exception was the direct cause of the following exception:\n\nValueError: x\n"
        );
        let _ = py.finish(Outcome::Exit(0));
    }

    /// 呼出履歴は最後の 1000 段だけ。同じ段の繰り返しは 3 回まで出す。
    #[test]
    fn tracebacks_keep_the_last_thousand_frames() {
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        let mut frames = vec![errors::frames::MODULE];
        frames.extend([errors::frames::PATHLIB_MKDIR_PARENT; 1200]);
        let error = PyException::new(types::RECURSION_ERROR, "maximum recursion depth exceeded")
            .called_from(&frames);
        py.display_exception(&error);
        let parent = "  File \"pathlib.py\", line 1179, in mkdir\n    self.parent.mkdir(parents=True, exist_ok=True)\n";
        assert_eq!(
            os.written(Stream::Stderr),
            format!(
                "Traceback (most recent call last):\n{parent}{parent}{parent}  [Previous line repeated 997 more times]\nRecursionError: maximum recursion depth exceeded\n"
            )
        );
        let _ = py.finish(Outcome::Exit(0));
    }

    #[test]
    fn unraisable_exceptions_with_tracebacks_and_modules() {
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        let error = PyException::new(
            errors::ExcType {
                module: "openpyxl.utils.exceptions",
                name: "IllegalCharacterError",
                bases: &["Exception", "BaseException"],
            },
            "",
        )
        .called_from(&[frames::MODULE]);
        py.write_unraisable(
            &PyStr::from("<function ZipFile.__del__ at 0x7f0000000000>"),
            &error,
        );
        assert_eq!(
            os.written(Stream::Stderr),
            "Exception ignored in: <function ZipFile.__del__ at 0x7f0000000000>\nTraceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 241, in <module>\n    sys.exit(main())\nopenpyxl.utils.exceptions.IllegalCharacterError: \n"
        );
        let _ = py.finish(Outcome::Exit(0));
    }

    #[test]
    fn the_traceback_cleanup_runs_after_the_exit_flush_and_before_close() {
        let os = FakeOs::new();
        let mut py = start(&os, &[]);
        py.print(None, &ok_line()).unwrap();
        let error = PyException::os(28).called_from(frames::PRINT_OK);
        let marker = os.clone();
        let outcome = Outcome::Raised(Box::new(Uncaught {
            exception: error,
            on_traceback_cleared: Some(Box::new(move |py: &mut Runtime| {
                // この時点で、標準出力は flush 済み、SIGINT は既定へ戻っている。
                assert!(marker.0.borrow().restored);
                assert_eq!(
                    marker.written(Stream::Stdout),
                    "OK out/book.xlsx にシート 1 件を出力\n"
                );
                py.write_unraisable(
                    &PyStr::from("<function ZipFile.__del__ at 0x1>"),
                    &PyException::os(28),
                );
            })),
        }));
        assert_eq!(py.finish(outcome), ExitCode::new(1));
        assert!(
            os.written(Stream::Stderr)
                .ends_with("OSError: [Errno 28] No space left on device\nException ignored in: <function ZipFile.__del__ at 0x1>\nOSError: [Errno 28] No space left on device\n")
        );
    }

    #[test]
    fn a_failing_unraisable_hook_reports_itself() {
        let os = FakeOs::new();
        // 最初の行の改行で詰まって失敗し、代わりの文も同じく失敗する。
        os.after(Stream::Stderr, Step::Fail(EPIPE));
        let mut py = start(&os, &[]);
        py.write_unraisable(&PyStr::from("<x>"), &PyException::keyboard_interrupt());
        let calls = os.0.borrow().calls.clone();
        let tried = calls
            .iter()
            .map(|call| String::from_utf8_lossy(&call.1).into_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            tried,
            [
                "Exception ignored in: <x>\n",
                "Exception ignored in: <x>\nException ignored in sys.unraisablehook: <built-in function unraisablehook>\n"
            ]
        );
    }
}
