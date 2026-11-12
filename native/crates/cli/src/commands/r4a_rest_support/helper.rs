//! `-c` で動かす loop の 2 本(`loop-task-text`・`loop-child-output`)の共通部品。
//!
//! 想定外の例外は `補助の失敗: <repr>` を標準エラーへ出して終了 9。`SystemExit` は別扱い。

use super::chars::{self, PyStr};
use super::exception::{Frame, PyException};
use super::stream::{Program, Raised, StdinRead};

pub const STRING_FILE: &str = "<string>";

pub const fn string_frame(line: u32, func: &'static str) -> Frame {
    Frame {
        file: STRING_FILE,
        line,
        func,
        source: None,
    }
}

/// 例外を `補助の失敗:` の出力にして終了 9(`except Exception` の枝)。
/// `site` は `print(..., file=sys.stderr)` の行。その print が失敗したら元の例外が文脈に残る。
pub fn helper_failure(
    program: &mut Program,
    exception: PyException,
    site: Frame,
) -> Result<i32, Raised> {
    // `except Exception` は `KeyboardInterrupt` を捕まえない。
    if exception.kind == "KeyboardInterrupt" {
        return Err(Raised::Exception(exception));
    }
    let mut body = chars::from_str("補助の失敗: ");
    body.extend(exception.repr());
    program
        .print_stderr(&body, &[site])
        .map_err(|mut failure| {
            failure.context = Some(Box::new(exception));
            Raised::Exception(failure)
        })?;
    Ok(9)
}

/// `open(path, "rb").read()` か `sys.stdin.buffer.read()`(`-`)。
/// 失敗は呼出元が `OSError` などの例外にする。
pub enum Read {
    Bytes(Vec<u8>),
    Error(PyException),
}

pub fn read_source(program: &mut Program, path: &PyStr, frames: &[Frame]) -> Read {
    if *path == [45] {
        return match program.read_stdin() {
            StdinRead::Bytes(bytes) => Read::Bytes(bytes),
            StdinRead::Closed => Read::Error(PyException::new(
                "AttributeError",
                "'NoneType' object has no attribute 'buffer'",
                frames,
            )),
            StdinRead::NoData => Read::Error(PyException::new(
                "AttributeError",
                "'NoneType' object has no attribute 'decode'",
                frames,
            )),
            StdinRead::Interrupted => Read::Error(super::stream::keyboard_interrupt(frames)),
            StdinRead::Failed(error) => Read::Error(io_exception(&error, frames)),
        };
    }
    let bytes = chars::to_os_bytes(path).unwrap_or_default();
    match fs_read(&bytes) {
        Ok(data) => Read::Bytes(data),
        Err(error) => Read::Error(io_exception(&error, frames)),
    }
}

pub fn io_exception(error: &std::io::Error, frames: &[Frame]) -> PyException {
    // 旧 Python は EINTR を再試行し、SIGINT を受け取っていれば KeyboardInterrupt にする(EINTR の OSError は出ない)。
    if error.raw_os_error() == Some(4) {
        return super::stream::keyboard_interrupt(frames);
    }
    match error.raw_os_error() {
        Some(errno) => super::exception::os_error(errno, frames),
        None => PyException::new("OSError", "I/O error", frames),
    }
}

/// `COMMANDS[sys.argv[1]]` の引数の数の検査(関数の引数の数と違うときの `TypeError`)。
pub fn arity_error(function: &str, expected: usize, given: usize, frames: &[Frame]) -> PyException {
    let message = if given < expected {
        format!(
            "{function}() missing {} required positional argument{}: {}",
            expected - given,
            if expected - given == 1 { "" } else { "s" },
            if expected == 1 { "'path'" } else { "" }
        )
    } else {
        format!(
            "{function}() takes {expected} positional argument{} but {given} {} given",
            if expected == 1 { "" } else { "s" },
            if given == 1 { "was" } else { "were" }
        )
    };
    PyException::new("TypeError", &message, frames)
}

/// `open(path, "rb").read()`(`-` も普通のファイル名)。
pub fn read_file(path: &PyStr, frames: &[Frame]) -> Result<Vec<u8>, PyException> {
    let bytes = chars::to_os_bytes(path).unwrap_or_default();
    fs_read(&bytes).map_err(|error| io_exception(&error, frames))
}

/// `open(path, "rb").read()`。EINTR は SIGINT を受け取るまで再試行する(受け取ったら EINTR の失敗にし、
/// 旗は残す。KeyboardInterrupt にする所(`io_exception`・次の出力の前の切れ目)が下ろす)。
pub fn fs_read(path: &[u8]) -> std::io::Result<Vec<u8>> {
    crate::commands::r4a_platform::read_file(path)
        .map_err(|errno| std::io::Error::from_raw_os_error(errno.raw_os_error()))
}

/// `one_line(text, limit)`: 制御文字(U+0000..U+001F・U+007F)を空白にして `limit` 文字で切る。
pub fn one_line(text: &[u32], limit: usize) -> PyStr {
    text.iter()
        .map(|&c| if c < 0x20 || c == 0x7f { 32 } else { c })
        .take(limit)
        .collect()
}
