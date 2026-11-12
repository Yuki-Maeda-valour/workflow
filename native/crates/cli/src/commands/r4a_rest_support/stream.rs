//! Python の標準出力・標準エラーと、プロセスの終了までの工程(R4A 専用)。
//!
//! `print` は本文と LF を別々の write にする。出力の失敗は未処理の例外になり、
//! 終了時の flush の失敗は終了値 120 にする。標準出力が `None`(起動時に閉じていた fd)の
//! ときは、`print` は何もせず、`stream.write` は `AttributeError` になる。

use super::chars::{self, EncodeError, PyStr};
use super::exception::{self, Frame, PyException};
use super::runtime::{BlockingWrite, Sink, TextOutput};
use std::io;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Errors {
    /// 標準出力: UTF-8 + surrogateescape(承認済みの固定)。
    SurrogateEscape,
    /// 標準エラー: 旧版の既定どおりの backslashreplace。
    BackslashReplace,
}

pub struct PyStream {
    pub out: TextOutput<Box<dyn Sink>>,
    pub errors: Errors,
    /// `Exception ignored in:` に出す `repr`。
    pub repr: &'static str,
}

#[derive(Debug)]
pub enum Fault {
    Encode { error: EncodeError, text: PyStr },
    Os(io::Error, Option<BlockingWrite>),
}

impl Fault {
    pub fn into_exception(self, frames: &[Frame]) -> PyException {
        match self {
            Fault::Encode { error, text } => {
                let position = if error.end - error.start == 1 {
                    format!(
                        "character '\\u{:x}' in position {}",
                        text[error.start], error.start
                    )
                } else {
                    format!("characters in position {}-{}", error.start, error.end - 1)
                };
                let mut e = PyException::new(
                    "UnicodeEncodeError",
                    &format!("'utf-8' codec can't encode {position}: surrogates not allowed"),
                    frames,
                );
                let mut args = chars::from_str("'utf-8', ");
                args.extend(chars::repr(&text));
                args.extend(chars::from_str(&format!(
                    ", {}, {}, 'surrogates not allowed'",
                    error.start, error.end
                )));
                e.repr_args = Some(args);
                e
            }
            // platform の `write_once` が、SIGINT を受け取った EINTR で返す(旧 Python は書込み中の EINTR で KeyboardInterrupt)。
            Fault::Os(error, _) if error.raw_os_error() == Some(4) => {
                PyException::new("KeyboardInterrupt", "", frames)
            }
            Fault::Os(error, blocking) => match (error.kind(), blocking) {
                (io::ErrorKind::WouldBlock, Some(b)) => {
                    let mut e = PyException::new(
                        "BlockingIOError",
                        &format!(
                            "[Errno {}] write could not complete without blocking",
                            b.errno
                        ),
                        frames,
                    );
                    e.repr_args = Some(chars::from_str(&format!(
                        "{}, 'write could not complete without blocking', {}",
                        b.errno, b.characters_written
                    )));
                    e
                }
                _ => match error.raw_os_error() {
                    Some(errno) => exception::os_error(errno, frames),
                    None => PyException::new("OSError", "I/O error", frames),
                },
            },
        }
    }
}

impl PyStream {
    fn encode(&self, text: &[u32]) -> Result<Vec<u8>, Fault> {
        match self.errors {
            Errors::SurrogateEscape => chars::to_os_bytes(text).map_err(|error| Fault::Encode {
                error,
                text: text.to_vec(),
            }),
            Errors::BackslashReplace => {
                let mut out = Vec::with_capacity(text.len());
                for &code in text {
                    match char::from_u32(code) {
                        Some(ch) => {
                            let mut buffer = [0_u8; 4];
                            out.extend_from_slice(ch.encode_utf8(&mut buffer).as_bytes());
                        }
                        None => out.extend_from_slice(format!("\\u{code:04x}").as_bytes()),
                    }
                }
                Ok(out)
            }
        }
    }

    pub fn write(&mut self, text: &[u32]) -> Result<(), Fault> {
        let bytes = self.encode(text)?;
        self.out
            .write(&bytes)
            .map_err(|e| Fault::Os(e, self.out.last_blocking()))
    }

    pub fn print(&mut self, body: &[u32]) -> Result<(), Fault> {
        self.write(body)?;
        self.write(&[10])
    }

    pub fn flush(&mut self) -> Result<(), Fault> {
        self.out
            .flush()
            .map_err(|e| Fault::Os(e, self.out.last_blocking()))
    }
}

/// 受け取った SIGINT を `KeyboardInterrupt` にする(旗を下ろす。例外の表示の書込みが、残った旗で中断されないため)。
pub fn keyboard_interrupt(frames: &[Frame]) -> PyException {
    crate::commands::r4a_platform::take_sigint();
    PyException::new("KeyboardInterrupt", "", frames)
}

/// 届いている SIGINT があれば旗を下ろして `KeyboardInterrupt` にする(旧 Python が次のバイトコードの切れ目で起こす例外)。
pub fn interrupt(frames: &[Frame]) -> Option<PyException> {
    crate::commands::r4a_platform::take_sigint()
        .then(|| PyException::new("KeyboardInterrupt", "", frames))
}

fn text(value: &str) -> PyStr {
    chars::from_str(value)
}

const CONTEXT_MESSAGE: &str =
    "\nDuring handling of the above exception, another exception occurred:\n\n";

/// 例外の表示中の書込み。CPython の `PyFile_Write*` は、例外が残っている間は何も書かずに失敗する。
/// 失敗すると例外が残り(`pending`)、`PyErr_Clear` に当たる `clear` までは以後の書込みを行わない。
struct Printer<'a> {
    stream: &'a mut PyStream,
    pending: bool,
}

impl Printer<'_> {
    /// 1 回の書込み。成功なら `true`。
    fn put(&mut self, value: &[u32]) -> bool {
        if self.pending {
            return false;
        }
        if self.stream.write(value).is_err() {
            self.pending = true;
            return false;
        }
        true
    }

    fn clear(&mut self) {
        self.pending = false;
    }

    /// 例外名の書込み。モジュール名つき(`argparse.ArgumentError`)は、モジュール・`.`・名前を順に書く。
    /// モジュール名の書込みが失敗したら、残りは書かない。
    fn put_type_name(&mut self, kind: &str) -> bool {
        match kind.rsplit_once('.') {
            Some((module, name)) => {
                self.put(&text(module)) && self.put(&text(".")) && self.put(&text(name))
            }
            None => self.put(&text(kind)),
        }
    }
}

fn message_text(exception: &PyException) -> PyStr {
    if exception.kind == "KeyError" {
        chars::repr(&exception.message)
    } else {
        exception.message.clone()
    }
}

/// `print_exception_recursive`。
fn print_exception(stream: &mut PyStream, exception: &PyException) {
    let mut printer = Printer {
        stream,
        pending: false,
    };
    print_exception_recursive(&mut printer, exception);
}

fn print_exception_recursive(printer: &mut Printer<'_>, exception: &PyException) {
    if let Some(context) = &exception.context {
        print_exception_recursive(printer, context);
        // 区切りの書込みが失敗したら、外側の `print_exception` の書込みはすべて失敗する(最後の改行だけを試す)。
        let _ = printer.put(&text(CONTEXT_MESSAGE));
    }
    print_one(printer, exception);
    printer.clear();
}

/// `print_exception`(1 つの例外)。
fn print_one(printer: &mut Printer<'_>, exception: &PyException) {
    let mut ok = true;
    if !exception.frames.is_empty() {
        ok = printer.put(&text("Traceback (most recent call last):\n"));
        if ok {
            for frame in &exception.frames {
                ok = printer.put(&text(&format!(
                    "  File \"{}\", line {}, in {}\n",
                    frame.file, frame.line, frame.func
                )));
                if !ok {
                    break;
                }
                if let Some(source) = frame.source {
                    // ソースの行の書込みの失敗は無視する(例外を消して次の Frame へ進む)。
                    // 3 つの書込み(字下げ・本文・改行)のどれが失敗しても、例外を消して次の Frame へ進む。
                    if !(printer.put(&text("    "))
                        && printer.put(&text(source))
                        && printer.put(&text("\n")))
                    {
                        printer.clear();
                    }
                }
            }
        }
    }
    if ok {
        ok = printer.put_type_name(&exception.kind);
    }
    if ok {
        let message = message_text(exception);
        if !message.is_empty() {
            ok = printer.put(&text(": "));
        }
        if ok {
            ok = printer.put(&message);
        }
    }
    // 失敗していたら例外を消してから、改行を必ず試す。
    if !ok {
        printer.clear();
    }
    let _ = printer.put(&text("\n"));
}

/// `write_unraisable_exc_file`: 最初の失敗でやめ、その失敗を返す。
fn unraisable_report(
    stream: &mut PyStream,
    header: &[&str],
    object_repr: &str,
    exception: &PyException,
) -> Result<(), Fault> {
    for piece in header {
        stream.write(&text(piece))?;
    }
    stream.write(&text(object_repr))?;
    stream.write(&text("\n"))?;
    if let Some((module, name)) = exception.kind.rsplit_once('.') {
        stream.write(&text(module))?;
        stream.write(&text("."))?;
        stream.write(&text(name))?;
    } else {
        stream.write(&text(&exception.kind))?;
    }
    let message = message_text(exception);
    if !message.is_empty() {
        stream.write(&text(": "))?;
    }
    stream.write(&message)?;
    stream.write(&text("\n"))?;
    stream.flush()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Launch {
    /// `python3 script.py`: 本体の後に flush(例外を消す)を行う。
    FileScript,
    /// `python3 -c "<保持した本文>"`: 事前の flush を行わない。
    HeldCode,
}

/// 本体の最後の状態。
#[derive(Debug)]
pub enum Raised {
    /// `SystemExit(code)`。
    Exit(i32),
    Exception(PyException),
}

/// 標準入力の raw read 1 回(`Ok(None)` は EAGAIN)。
pub trait Source {
    fn read_once(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>>;
}

/// `sys.stdin.buffer.read()` の結果。
#[derive(Debug)]
pub enum StdinRead {
    /// 起動時に閉じていた(`sys.stdin` が `None`)。
    Closed,
    /// 最初の read が EAGAIN(`read()` が `None` を返す)。
    NoData,
    Bytes(Vec<u8>),
    /// SIGINT で読取りが中断された(`Source` が `ErrorKind::Interrupted` を返した)。
    Interrupted,
    Failed(io::Error),
}

pub struct Program<'a> {
    pub stdout: Option<PyStream>,
    pub stderr: Option<PyStream>,
    pub stdin: Option<Box<dyn Source + 'a>>,
    pub launch: Launch,
}

impl<'a> Program<'a> {
    pub fn new(stdout: Option<PyStream>, stderr: Option<PyStream>, launch: Launch) -> Self {
        Self {
            stdout,
            stderr,
            stdin: None,
            launch,
        }
    }

    pub fn with_stdin(mut self, stdin: Option<Box<dyn Source + 'a>>) -> Self {
        self.stdin = stdin;
        self
    }

    /// `sys.stdin.buffer.read()`。EOF まで読む。最初の EAGAIN は `NoData`、
    /// 1 バイト以上の後の EAGAIN は読めた分を返す。ほかの失敗は読めた分を捨てて失敗にする。
    pub fn read_stdin(&mut self) -> StdinRead {
        let Some(source) = self.stdin.as_mut() else {
            return StdinRead::Closed;
        };
        let mut data = Vec::new();
        let mut buffer = vec![0_u8; 8192];
        loop {
            match source.read_once(&mut buffer) {
                Ok(Some(0)) => return StdinRead::Bytes(data),
                Ok(Some(count)) => data.extend_from_slice(&buffer[..count]),
                Ok(None) if data.is_empty() => return StdinRead::NoData,
                Ok(None) => return StdinRead::Bytes(data),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {
                    return StdinRead::Interrupted;
                }
                Err(error) => return StdinRead::Failed(error),
            }
        }
    }

    /// `print(body)`。stdout が `None` なら何もしない。
    pub fn print_stdout(&mut self, body: &[u32], frames: &[Frame]) -> Result<(), PyException> {
        if let Some(exception) = interrupt(frames) {
            return Err(exception);
        }
        match self.stdout.as_mut() {
            None => Ok(()),
            Some(stream) => stream.print(body).map_err(|f| f.into_exception(frames)),
        }
    }

    /// `print(body, file=sys.stderr)`。stderr が `None` なら stdout を使い、それも `None` なら何もしない。
    pub fn print_stderr(&mut self, body: &[u32], frames: &[Frame]) -> Result<(), PyException> {
        if let Some(exception) = interrupt(frames) {
            return Err(exception);
        }
        match self.stderr.as_mut() {
            Some(stream) => stream.print(body).map_err(|f| f.into_exception(frames)),
            None => self.print_stdout(body, frames),
        }
    }

    fn none_write(frames: &[Frame]) -> PyException {
        PyException::new(
            "AttributeError",
            "'NoneType' object has no attribute 'write'",
            frames,
        )
    }

    /// `sys.stdout.write(text)`。`None` なら `AttributeError`。
    pub fn write_stdout(&mut self, text: &[u32], frames: &[Frame]) -> Result<(), PyException> {
        if let Some(exception) = interrupt(frames) {
            return Err(exception);
        }
        match self.stdout.as_mut() {
            None => Err(Self::none_write(frames)),
            Some(stream) => stream.write(text).map_err(|f| f.into_exception(frames)),
        }
    }

    /// `sys.stderr.write(text)`。
    pub fn write_stderr(&mut self, text: &[u32], frames: &[Frame]) -> Result<(), PyException> {
        if let Some(exception) = interrupt(frames) {
            return Err(exception);
        }
        match self.stderr.as_mut() {
            None => Err(Self::none_write(frames)),
            Some(stream) => stream.write(text).map_err(|f| f.into_exception(frames)),
        }
    }

    /// `PyErr_Display` に当たる。書込みが失敗しても、旧 Python と同じ順で続ける
    /// (`print_exception_recursive`・`print_exception`・`tb_displayline`)。最後に stderr を flush する(失敗は無視)。
    fn display_exception(&mut self, exception: &PyException) {
        let Some(stream) = self.stderr.as_mut() else {
            return;
        };
        print_exception(stream, exception);
        let _ = stream.flush();
    }

    /// `write_unraisable_exc_file` と、その失敗を `sys.unraisablehook` の失敗として出す工程。
    fn write_unraisable(&mut self, object_repr: &str, exception: &PyException) {
        let Some(stream) = self.stderr.as_mut() else {
            return;
        };
        let first = unraisable_report(stream, &["Exception ignored in: "], object_repr, exception);
        if let Err(failure) = first {
            // 書込みの失敗は新しい例外。標準フックの失敗として、その例外を同じ形で出す(この失敗は無視する)。
            let new_exception = failure.into_exception(&[]);
            let _ = unraisable_report(
                stream,
                &["Exception ignored in sys.unraisablehook", ": "],
                "<built-in function unraisablehook>",
                &new_exception,
            );
        }
    }

    /// 本体の結果から終了値までの工程。事前の flush・例外の表示・終了時の flush・破棄の順。
    pub fn finish(self, outcome: Result<i32, Raised>) -> u8 {
        self.finish_with_signal(outcome).0
    }

    /// `finish` と同じ工程。`KeyboardInterrupt` で終わったときは 2 つ目が `true`
    /// (呼出側が SIGINT の既定の動作で自分を終わらせる。旧 Python と同じく信号で終わる)。
    pub fn finish_with_signal(mut self, outcome: Result<i32, Raised>) -> (u8, bool) {
        let interrupted =
            matches!(&outcome, Err(Raised::Exception(e)) if e.kind == "KeyboardInterrupt");
        let outcome = match outcome {
            Ok(code) => Raised::Exit(code),
            Err(raised) => raised,
        };
        if self.launch == Launch::FileScript {
            if let Some(stream) = self.stderr.as_mut() {
                let _ = stream.flush();
            }
            if let Some(stream) = self.stdout.as_mut() {
                let _ = stream.flush();
            }
        }
        let mut code = match outcome {
            Raised::Exit(code) => code,
            Raised::Exception(exception) => {
                self.display_exception(&exception);
                1
            }
        };
        let mut sticky = false;
        if let Some(stream) = self.stdout.as_mut()
            && let Err(fault) = stream.flush()
        {
            sticky = true;
            let repr = stream.repr;
            let exception = fault.into_exception(&[]);
            self.write_unraisable(repr, &exception);
        }
        if let Some(stream) = self.stderr.as_mut()
            && stream.flush().is_err()
        {
            sticky = true;
        }
        if let Some(stream) = self.stdout.as_mut() {
            let _ = stream.out.close();
        }
        if let Some(stream) = self.stderr.as_mut() {
            let _ = stream.out.close();
        }
        if sticky {
            code = 120;
        }
        if interrupted {
            return (130, true);
        }
        ((code & 0xff) as u8, false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::r4a_rest_support::runtime::Buffering;
    use std::cell::RefCell;
    use std::rc::Rc;

    #[derive(Default)]
    struct Shared {
        bytes: Vec<u8>,
        writes: Vec<Vec<u8>>,
        fail: bool,
    }
    struct Memory(Rc<RefCell<Shared>>);
    impl Sink for Memory {
        fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
            let mut shared = self.0.borrow_mut();
            shared.writes.push(bytes.to_vec());
            if shared.fail {
                return Err(io::Error::from_raw_os_error(28));
            }
            shared.bytes.extend_from_slice(bytes);
            Ok(Some(bytes.len()))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn stream(shared: &Rc<RefCell<Shared>>, errors: Errors, repr: &'static str) -> PyStream {
        PyStream {
            out: TextOutput::new(Box::new(Memory(shared.clone())), Buffering::Buffered, 11),
            errors,
            repr,
        }
    }

    fn program(launch: Launch) -> (Program<'static>, Rc<RefCell<Shared>>, Rc<RefCell<Shared>>) {
        let out = Rc::new(RefCell::new(Shared::default()));
        let err = Rc::new(RefCell::new(Shared::default()));
        let p = Program::new(
            Some(stream(
                &out,
                Errors::SurrogateEscape,
                "<_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>",
            )),
            Some(stream(
                &err,
                Errors::BackslashReplace,
                "<_io.TextIOWrapper name='<stderr>' mode='w' encoding='utf-8'>",
            )),
            launch,
        );
        (p, out, err)
    }

    #[test]
    fn success_path_flushes_once_and_returns_the_planned_code() {
        let (mut p, out, _) = program(Launch::FileScript);
        p.print_stdout(&chars::from_str("ok"), &[]).unwrap();
        assert_eq!(p.finish(Ok(0)), 0);
        assert_eq!(out.borrow().bytes, b"ok\n");
    }

    #[test]
    fn stdout_failure_gives_120_and_the_unraisable_report() {
        let (mut p, out, err) = program(Launch::FileScript);
        out.borrow_mut().fail = true;
        p.print_stdout(&chars::from_str("x"), &[]).unwrap();
        assert_eq!(p.finish(Ok(0)), 120);
        let text = String::from_utf8(err.borrow().bytes.clone()).unwrap();
        assert_eq!(
            text,
            "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 28] No space left on device\n"
        );
        // 事前の flush・終了時の flush・破棄の 2 回(合計 4 回)の write を試みる。
        assert_eq!(out.borrow().writes.len(), 4);
    }

    #[test]
    fn held_code_has_no_preflush_and_none_streams_behave() {
        let (mut p, out, _) = program(Launch::HeldCode);
        out.borrow_mut().fail = true;
        p.print_stdout(&chars::from_str("x"), &[]).unwrap();
        assert_eq!(p.finish(Ok(0)), 120);
        assert_eq!(out.borrow().writes.len(), 3);
        let mut none = Program::new(None, None, Launch::FileScript);
        none.print_stderr(&chars::from_str("x"), &[]).unwrap();
        let error = none.write_stdout(&chars::from_str("x"), &[]).unwrap_err();
        assert_eq!(error.kind, "AttributeError");
    }

    struct Scripted(Vec<io::Result<Option<Vec<u8>>>>);
    impl Source for Scripted {
        fn read_once(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
            match self.0.remove(0) {
                Ok(Some(bytes)) => {
                    buffer[..bytes.len()].copy_from_slice(&bytes);
                    Ok(Some(bytes.len()))
                }
                Ok(None) => Ok(None),
                Err(error) => Err(error),
            }
        }
    }

    fn with_stdin(steps: Vec<io::Result<Option<Vec<u8>>>>) -> Program<'static> {
        Program::new(None, None, Launch::HeldCode).with_stdin(Some(Box::new(Scripted(steps))))
    }

    #[test]
    fn stdin_readall_rules() {
        // 最初の read が EOF なら空の bytes(`NoData` ではない)。
        let mut p = with_stdin(vec![Ok(Some(vec![]))]);
        assert!(matches!(p.read_stdin(), StdinRead::Bytes(b) if b.is_empty()));
        // 最初の EAGAIN は `NoData`。
        let mut p = with_stdin(vec![Ok(None)]);
        assert!(matches!(p.read_stdin(), StdinRead::NoData));
        // 1 バイト以上の後の EAGAIN は、読めた分を返す(待たない・エラーにしない)。
        let mut p = with_stdin(vec![Ok(Some(b"ab".to_vec())), Ok(None)]);
        assert!(matches!(p.read_stdin(), StdinRead::Bytes(b) if b == b"ab"));
        // 途中の OS の失敗は、読めた分を返さず失敗にする。
        let mut p = with_stdin(vec![
            Ok(Some(b"ab".to_vec())),
            Err(io::Error::from_raw_os_error(5)),
        ]);
        assert!(matches!(p.read_stdin(), StdinRead::Failed(e) if e.raw_os_error() == Some(5)));
        // SIGINT による中断は、失敗とは別。
        let mut p = with_stdin(vec![Err(io::Error::from(io::ErrorKind::Interrupted))]);
        assert!(matches!(p.read_stdin(), StdinRead::Interrupted));
        // 起動時に閉じていた stdin。
        let mut closed = Program::new(None, None, Launch::HeldCode);
        assert!(matches!(closed.read_stdin(), StdinRead::Closed));
    }

    #[test]
    fn keyboard_interrupt_ends_by_signal_after_the_traceback() {
        let (mut p, _, err) = program(Launch::HeldCode);
        p.print_stderr(&chars::from_str("x"), &[]).unwrap();
        let e = PyException::new("KeyboardInterrupt", "", &[]);
        let (code, signal) = p.finish_with_signal(Err(Raised::Exception(e)));
        assert_eq!((code, signal), (130, true));
        let text = String::from_utf8(err.borrow().bytes.clone()).unwrap();
        assert!(text.ends_with("KeyboardInterrupt\n"), "{text:?}");
    }

    /// 指定した番号(1 から)の書込みだけが失敗する出力先と、書込みの記録。
    struct FailAt {
        fail: Vec<usize>,
        count: usize,
        writes: Rc<RefCell<Vec<Vec<u8>>>>,
    }
    impl Sink for FailAt {
        fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
            self.count += 1;
            if self.fail.contains(&self.count) {
                return Err(io::Error::from_raw_os_error(28));
            }
            self.writes.borrow_mut().push(bytes.to_vec());
            Ok(Some(bytes.len()))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn display_with(fail: Vec<usize>, exception: &PyException) -> String {
        let writes = Rc::new(RefCell::new(Vec::new()));
        let mut stream = PyStream {
            out: TextOutput::new(
                Box::new(FailAt {
                    fail,
                    count: 0,
                    writes: writes.clone(),
                }),
                Buffering::Unbuffered,
                11,
            ),
            errors: Errors::BackslashReplace,
            repr: "stderr",
        };
        print_exception(&mut stream, exception);
        let text: Vec<u8> = writes.borrow().iter().flatten().copied().collect();
        String::from_utf8(text).unwrap()
    }

    fn chained() -> PyException {
        let frame = Frame {
            file: "a.py",
            line: 1,
            func: "f",
            source: Some("x"),
        };
        let inner = PyException::new("json.decoder.JSONDecodeError", "m", &[frame]);
        PyException::new("OSError", "n", &[frame]).with_context(Some(inner))
    }

    #[test]
    fn a_failed_module_name_write_skips_the_dot_and_the_rest() {
        // 書込み: 1 Traceback / 2 File 行 / 3 `    ` / 4 ソース / 5 LF / 6 module / 7 `.` / 8 name ...
        let whole = display_with(vec![], &PyException::new("a.B", "m", &[]));
        assert_eq!(whole, "a.B: m\n");
        // frames が無いので、1 module / 2 `.` / 3 name / 4 `: ` / 5 message / 6 LF。module(1)が失敗したら改行だけ。
        assert_eq!(
            display_with(vec![1], &PyException::new("a.B", "m", &[])),
            "\n"
        );
        assert_eq!(
            display_with(vec![2], &PyException::new("a.B", "m", &[])),
            "a\n"
        );
    }

    #[test]
    fn a_failed_context_separator_leaves_only_the_final_newline_for_the_outer_exception() {
        let ok = display_with(vec![], &chained());
        assert!(ok.contains("During handling of the above exception"));
        // 内側: 1 Traceback 2 File 3 `    ` 4 x 5 LF 6 json.decoder 7 . 8 JSONDecodeError 9 `: ` 10 m 11 LF。
        // 12 が区切り。12 が失敗したら、外側は最後の改行だけ。
        let failed = display_with(vec![12], &chained());
        assert!(failed.starts_with("Traceback (most recent call last):\n"));
        assert!(!failed.contains("During handling"));
        assert!(failed.ends_with("m\n\n"), "{failed:?}");
        assert_eq!(failed.matches("Traceback").count(), 1);
    }

    #[test]
    fn a_failed_source_line_is_ignored_and_the_next_frame_is_printed() {
        let frame = Frame {
            file: "a.py",
            line: 1,
            func: "f",
            source: Some("x"),
        };
        let exception = PyException::new("E", "m", &[frame, frame]);
        // 3 番目の書込み(`    `)が失敗: ソースの行は出ず、次の Frame は出る。
        let out = display_with(vec![3], &exception);
        assert_eq!(out.matches("File \"a.py\"").count(), 2);
        assert!(out.ends_with("E: m\n"));
    }

    #[test]
    fn a_failed_source_write_of_any_of_the_three_goes_on_to_the_next_frame() {
        let frame = Frame {
            file: "a.py",
            line: 1,
            func: "f",
            source: Some("x"),
        };
        let exception = PyException::new("E", "m", &[frame, frame]);
        // 書込み: 1 Traceback / 2 File / 3 字下げ / 4 本文 / 5 改行 / 6 File(2 つ目)/ ...
        for failing in [3, 4, 5] {
            let out = display_with(vec![failing], &exception);
            assert_eq!(
                out.matches("File \"a.py\"").count(),
                2,
                "{failing}: {out:?}"
            );
            assert!(out.ends_with("E: m\n"), "{failing}: {out:?}");
        }
    }

    #[test]
    fn lone_surrogates_differ_by_stream() {
        let (mut p, out, err) = program(Launch::HeldCode);
        let text = vec![0xdcff, 0xd800];
        let failure = p.print_stdout(&text, &[]).unwrap_err();
        assert_eq!(failure.kind, "UnicodeEncodeError");
        assert_eq!(
            String::from_utf8(failure.message.iter().map(|&c| c as u8).collect()).unwrap(),
            "'utf-8' codec can't encode character '\\ud800' in position 1: surrogates not allowed"
        );
        p.print_stderr(&text, &[]).unwrap();
        assert_eq!(p.finish(Ok(0)), 0);
        assert!(out.borrow().bytes.is_empty());
        assert_eq!(err.borrow().bytes, b"\\udcff\\ud800\n");
    }
}
