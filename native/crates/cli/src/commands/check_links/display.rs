//! 呼出履歴と「Exception ignored」と起動の失敗の診断を、CPython v3.10.11 の C の制御の流れのとおりに書く。
//!
//! 正本は `Python/pythonrun.c`(`print_exception`・`print_exception_recursive`・`_PyErr_Display`)・
//! `Python/traceback.c`(`PyTraceBack_Print`・`tb_printinternal`・`tb_displayline`・`_Py_DisplaySourceLine`・
//! `tb_print_line_repeated`・`write_thread_id`・`dump_traceback`)・`Python/errors.c`
//! (`_PyErr_WriteUnraisableMsg`・`write_unraisable_exc_file`)・`Python/pylifecycle.c`(`fatal_error`・`flush_std_files`)。
//! - 書込みの失敗と SIGINT の後に、どの部品を出し、どれを落とし、どこで改行するかは、この流れで決まる。
//! - 故障の無いときの部品は `traceback::print_exception` と同じになる(単体検査で照らす)。
use super::pyio::{IoError, Py, Which};
use super::traceback::{Exception, Frame, STDOUT_REPR, Traceback};
use dev_workflow_platform::check_links as os;

const LIMIT: usize = 1000;
const CUTOFF: usize = 3;
const CONTEXT_MESSAGE: &[u8] =
    b"\nDuring handling of the above exception, another exception occurred:\n\n";
const HEADER: &[u8] = b"Traceback (most recent call last):\n";

/// 書込みの部品の種類ごとの呼出し(`PyFile_WriteObject` か `PyFile_WriteString` か)。
fn object(py: &mut Py, text: &[u8]) -> i32 {
    if py.write_object(Which::Stderr, text) {
        0
    } else {
        -1
    }
}

fn string(py: &mut Py, text: &[u8]) -> i32 {
    if py.write_string(Which::Stderr, text) {
        0
    } else {
        -1
    }
}

/// `sys.excepthook` → `PyErr_Display`(sys.stderr が無ければ何も書かない)。最後に `file.flush()` の失敗を捨てる。
pub fn display(py: &mut Py, chain: &[Traceback]) {
    if !py.has(Which::Stderr) {
        return;
    }
    print_recursive(py, chain);
    let _ = py.flush(Which::Stderr);
    py.err = None;
}

/// `print_exception_recursive`。`chain` は前の例外から順に並べる(最後が投げた例外)。
fn print_recursive(py: &mut Py, chain: &[Traceback]) {
    let Some((value, context)) = chain.split_last() else {
        return;
    };
    let mut err = 0;
    if !context.is_empty() {
        print_recursive(py, context);
        err |= string(py, CONTEXT_MESSAGE);
    }
    print_exception(py, value);
    if err != 0 {
        py.err = None;
    }
}

/// `print_exception`。
fn print_exception(py: &mut Py, traceback: &Traceback) {
    let mut err = 0;
    if !traceback.frames.is_empty() {
        err = traceback_print(py, &traceback.frames);
    }
    let exception = &traceback.exception;
    if err == 0 {
        err = object(py, exception.type_name.as_bytes());
    }
    if err == 0 {
        // PyObject_Str(value) も先頭で印を見る。
        if py.check_signals() {
            if !exception.value.is_empty() {
                err = string(py, b": ");
            }
            if err == 0 {
                err = object(py, exception.value.as_bytes());
            }
        } else {
            py.err = None;
            err = -1;
            string(py, b": <exception str() failed>");
        }
    }
    // err < 0 なら消す。候補の表示(_Py_Offer_Suggestions)は無く、残った例外も消す。
    py.err = None;
    err += string(py, b"\n");
    if err != 0 {
        py.err = None;
    }
}

/// `PyTraceBack_Print`。
fn traceback_print(py: &mut Py, frames: &[Frame]) -> i32 {
    let err = string(py, HEADER);
    if err != 0 {
        return err;
    }
    printinternal(py, frames)
}

/// `tb_printinternal`。
fn printinternal(py: &mut Py, frames: &[Frame]) -> i32 {
    let frames = &frames[frames.len().saturating_sub(LIMIT)..];
    let mut err = 0;
    let mut last: Option<Frame> = None;
    let mut count = 0;
    for frame in frames {
        if err != 0 {
            break;
        }
        if last != Some(*frame) {
            if count > CUTOFF {
                err = repeated(py, count);
            }
            last = Some(*frame);
            count = 0;
        }
        count += 1;
        if err == 0 && count <= CUTOFF {
            err = displayline(py, frame);
            if err == 0 && !py.check_signals() {
                err = -1;
            }
        }
    }
    if err == 0 && count > CUTOFF {
        err = repeated(py, count);
    }
    err
}

/// `tb_print_line_repeated`。
fn repeated(py: &mut Py, count: usize) -> i32 {
    let more = count - CUTOFF;
    let unit = if more > 1 { "times" } else { "time" };
    object(
        py,
        format!("  [Previous line repeated {more} more {unit}]\n").as_bytes(),
    )
}

/// `tb_displayline`。ソースの行の失敗は消して続ける。
fn displayline(py: &mut Py, frame: &Frame) -> i32 {
    let line = format!(
        "  File \"{}\", line {}, in {}\n",
        frame.file, frame.line, frame.name
    );
    let err = object(py, line.as_bytes());
    if err != 0 {
        return err;
    }
    if source_line(py, frame) != 0 {
        py.err = None;
    }
    0
}

/// `_Py_DisplaySourceLine`。ソースのファイルを開いて読む所で Python のコードが動き、立った印を見る
/// (印があれば、何も書かずに例外を消して戻る)。
fn source_line(py: &mut Py, frame: &Frame) -> i32 {
    if os::take_interrupt() {
        return 0;
    }
    let mut err = string(py, b"    ");
    if err == 0 {
        err = object(py, frame.source.as_bytes());
    }
    if err == 0 {
        err = string(py, b"\n");
    }
    err
}

/// `flush_std_files` が stdout を書き出せなかったとき(`PyErr_WriteUnraisable(fout)`)。
pub fn unraisable_stdout(py: &mut Py, error: IoError) {
    if !py.has(Which::Stderr) {
        return;
    }
    if !unraisable_file(py, None, STDOUT_REPR.as_bytes(), Some(error), true) {
        // sys.unraisablehook が失敗した: その例外を、既定の書き方で「sys.unraisablehook で無視した」として書く。
        let second = py.err.take();
        unraisable_file(
            py,
            Some(b"Exception ignored in sys.unraisablehook"),
            b"<built-in function unraisablehook>",
            second,
            false,
        );
    }
    py.err = None;
}

/// `write_unraisable_exc_file`。`normalized` が偽なら、KeyboardInterrupt は値を持たない(`: ` を書かない)。
fn unraisable_file(
    py: &mut Py,
    message: Option<&[u8]>,
    repr: &[u8],
    error: Option<IoError>,
    normalized: bool,
) -> bool {
    match message {
        Some(message) => {
            if object(py, message) != 0 || string(py, b": ") != 0 {
                return false;
            }
        }
        None => {
            if string(py, b"Exception ignored in: ") != 0 {
                return false;
            }
        }
    }
    if object(py, repr) != 0 {
        py.err = None;
        if string(py, b"<object repr() failed>") != 0 {
            return false;
        }
    }
    if string(py, b"\n") != 0 {
        return false;
    }
    let Some(error) = error else {
        return false;
    };
    let exception = io_exception(error);
    if object(py, exception.type_name.as_bytes()) != 0 {
        return false;
    }
    if normalized || error != IoError::Interrupt {
        if string(py, b": ") != 0 {
            return false;
        }
        if object(py, exception.value.as_bytes()) != 0 {
            py.err = None;
            if string(py, b"<exception str() failed>") != 0 {
                return false;
            }
        }
    }
    if string(py, b"\n") != 0 {
        return false;
    }
    match py.flush(Which::Stderr) {
        Ok(()) => true,
        Err(error) => {
            py.err = Some(error);
            false
        }
    }
}

/// 書込みの例外の種類と本文。
pub fn io_exception(error: IoError) -> Exception {
    match error {
        IoError::Os(errno) => Exception::os(errno, &super::strerror(errno), None),
        IoError::Blocking(errno) => Exception {
            type_name: "BlockingIOError",
            value: format!("[Errno {errno}] write could not complete without blocking"),
        },
        IoError::Interrupt => Exception::keyboard_interrupt(),
    }
}

/// `flush_std_files`。stdout の失敗は「Exception ignored」を書き、stderr の失敗は黙って捨てる。成功かを返す。
pub fn flush_std_files(py: &mut Py) -> bool {
    let mut ok = true;
    if py.has(Which::Stdout)
        && let Err(error) = py.flush(Which::Stdout)
    {
        unraisable_stdout(py, error);
        ok = false;
    }
    if py.has(Which::Stderr) && py.flush(Which::Stderr).is_err() {
        py.err = None;
        ok = false;
    }
    ok
}

/// `PUTS(fd, text)`(`_Py_write_noraise`。EINTR だけ繰り返し、失敗は捨てる)。
fn puts(text: &[u8]) {
    while os::write(2, text) == Err(4) {}
}

/// `_Py_DumpHexadecimal(fd, value, 16)`。
fn hexadecimal(value: usize) -> Vec<u8> {
    format!("{value:016x}").into_bytes()
}

/// 起動の時点の段階(`fatal_error_dump_runtime` の表示)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    Preinitialized,
    CoreInitialized,
}

/// `fatal_error(fd 2, header, func, message)`。`exception` は、そのときに設定されていた例外
/// (sys.stderr の仮の書き手で、呼出履歴なしに書く)。終了値は 1。
pub fn fatal(stage: Stage, function: &str, message: &str, exception: Option<Exception>) {
    puts(b"Fatal Python error: ");
    puts(function.as_bytes());
    puts(b": ");
    puts(message.as_bytes());
    puts(b"\n");
    puts(b"Python runtime state: ");
    puts(match stage {
        Stage::Preinitialized => b"preinitialized",
        Stage::CoreInitialized => b"core initialized",
    });
    puts(b"\n");
    if stage == Stage::CoreInitialized {
        if let Some(exception) = exception {
            let mut py = Py {
                stdout: None,
                stderr: Some(super::pyio::Stream::printer(2)),
                err: None,
            };
            display(
                &mut py,
                &[Traceback {
                    frames: Vec::new(),
                    exception,
                }],
            );
        }
        // _Py_FatalError_DumpTracebacks
        puts(b"\n");
        puts(b"Current thread 0x");
        puts(&hexadecimal(os::thread_ident()));
        puts(b" (most recent call first):\n");
        puts(b"  <no Python frame>\n");
    } else {
        // スレッドの状態がまだ無いので、改行だけを書く。
        puts(b"\n");
    }
}
