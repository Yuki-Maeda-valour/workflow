use std::io::{self, Read, Write};

/// コマンドと出力処理が共通で使う終了コード。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ExitCode(u8);

impl ExitCode {
    pub const SUCCESS: Self = Self::new(0);
    pub const RUNTIME: Self = Self::new(1);
    pub const USAGE: Self = Self::new(2);

    pub const fn new(value: u8) -> Self {
        Self(value)
    }

    pub const fn as_u8(self) -> u8 {
        self.0
    }
}

impl From<ExitCode> for std::process::ExitCode {
    fn from(value: ExitCode) -> Self {
        Self::from(value.as_u8())
    }
}

#[derive(Debug)]
pub enum CommandError {
    Runtime(String),
    Usage(String),
}

impl CommandError {
    pub fn runtime(message: impl Into<String>) -> Self {
        Self::Runtime(message.into())
    }

    pub fn usage(message: impl Into<String>) -> Self {
        Self::Usage(message.into())
    }
}

/// 加工せずに書き出す最終出力と、そのコマンドの終了値。
#[derive(Debug, PartialEq, Eq)]
pub struct CommandOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub code: ExitCode,
}

impl CommandOutput {
    pub fn stdout(stdout: impl Into<Vec<u8>>) -> Self {
        Self {
            stdout: stdout.into(),
            stderr: Vec::new(),
            code: ExitCode::SUCCESS,
        }
    }
}

pub type CommandResult = Result<CommandOutput, CommandError>;

/// 実行中だけ借りる入出力。最初の出力失敗の後は通常出力を止める。
pub struct CommandIo<'a> {
    input: &'a mut dyn Read,
    stdout: &'a mut dyn Write,
    stderr: &'a mut dyn Write,
    output_failure: Option<io::ErrorKind>,
}

impl CommandIo<'_> {
    /// 先読みや再試行をせず、渡されたバッファへ1回だけ読む。
    pub fn read_input(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.input.read(buffer)
    }

    /// 生バイトをすべて書き、同じ出力先の flush 成功後に返す。
    pub fn write_stdout(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.check_output()?;
        let result = self
            .stdout
            .write_all(bytes)
            .and_then(|()| self.stdout.flush());
        if let Err(error) = &result {
            self.output_failure = Some(error.kind());
            let _ = diagnostic(self.stderr, &format!("標準出力に書けません: {error}"));
        }
        result
    }

    /// 標準エラーの失敗では、追加の診断を出さない。
    pub fn write_stderr(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.check_output()?;
        let result = self
            .stderr
            .write_all(bytes)
            .and_then(|()| self.stderr.flush());
        if let Err(error) = &result {
            self.output_failure = Some(error.kind());
        }
        result
    }

    fn check_output(&self) -> io::Result<()> {
        match self.output_failure {
            Some(kind) => Err(io::Error::new(kind, "先の出力が失敗しています")),
            None => Ok(()),
        }
    }
}

/// 実際の標準入出力を、コマンドの実行と最終出力の間だけ借りる。
pub fn execute(command: impl FnOnce(&mut CommandIo<'_>) -> CommandResult) -> ExitCode {
    execute_to(
        &mut io::stdin().lock(),
        &mut io::stdout().lock(),
        &mut io::stderr().lock(),
        command,
    )
}

/// 途中の出力失敗を無視したコマンドでも、最終出力を止めて終了1にする。
pub fn execute_to(
    input: &mut dyn Read,
    stdout: &mut dyn Write,
    stderr: &mut dyn Write,
    command: impl FnOnce(&mut CommandIo<'_>) -> CommandResult,
) -> ExitCode {
    let mut io = CommandIo {
        input,
        stdout,
        stderr,
        output_failure: None,
    };
    let result = command(&mut io);
    if io.output_failure.is_some() {
        ExitCode::RUNTIME
    } else {
        emit_to(result, io.stdout, io.stderr)
    }
}

/// 出力に失敗したら終了 1。BrokenPipe も成功として扱わない。
pub fn emit(result: CommandResult) -> ExitCode {
    emit_to(result, &mut io::stdout().lock(), &mut io::stderr().lock())
}

/// 両方の出力先で書き込みと flush を確かめる。
pub fn emit_to(
    result: CommandResult,
    stdout: &mut (impl Write + ?Sized),
    stderr: &mut (impl Write + ?Sized),
) -> ExitCode {
    let output = match result {
        Ok(output) => output,
        Err(error) => {
            let (code, message) = match error {
                CommandError::Runtime(message) => (ExitCode::RUNTIME, message),
                CommandError::Usage(message) => (ExitCode::USAGE, message),
            };
            return if diagnostic(stderr, &message).is_err() {
                ExitCode::RUNTIME
            } else {
                code
            };
        }
    };
    if let Err(error) = stdout
        .write_all(&output.stdout)
        .and_then(|()| stdout.flush())
    {
        let _ = diagnostic(stderr, &format!("標準出力に書けません: {error}"));
        return ExitCode::RUNTIME;
    }
    if stderr
        .write_all(&output.stderr)
        .and_then(|()| stderr.flush())
        .is_err()
    {
        return ExitCode::RUNTIME;
    }
    output.code
}

fn diagnostic(writer: &mut (impl Write + ?Sized), message: &str) -> io::Result<()> {
    writeln!(writer, "dev-workflow: {message}")?;
    writer.flush()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    struct Fails {
        on_flush: bool,
    }

    impl Write for Fails {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.on_flush {
                Ok(bytes.len())
            } else {
                Err(io::ErrorKind::BrokenPipe.into())
            }
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
    }

    #[test]
    fn output_and_exit_codes() {
        for (result, expected, out, err) in [
            (
                Ok(CommandOutput::stdout(b"ok\n")),
                ExitCode::SUCCESS,
                "ok\n",
                "",
            ),
            (
                Err(CommandError::runtime("失敗")),
                ExitCode::RUNTIME,
                "",
                "dev-workflow: 失敗\n",
            ),
            (
                Err(CommandError::usage("引数")),
                ExitCode::USAGE,
                "",
                "dev-workflow: 引数\n",
            ),
        ] {
            let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
            assert_eq!(emit_to(result, &mut stdout, &mut stderr), expected);
            assert_eq!(stdout, out.as_bytes());
            assert_eq!(stderr, err.as_bytes());
        }
    }

    #[test]
    fn completed_outputs_keep_nonzero_codes_and_check_both_streams() {
        for code in [ExitCode::SUCCESS, ExitCode::RUNTIME, ExitCode::USAGE] {
            let output = || {
                Ok(CommandOutput {
                    stdout: b"json\n".to_vec(),
                    stderr: b"detail\n".to_vec(),
                    code,
                })
            };
            let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
            assert_eq!(emit_to(output(), &mut stdout, &mut stderr), code);
            assert_eq!(stdout, b"json\n");
            assert_eq!(stderr, b"detail\n");
            for on_flush in [false, true] {
                assert_eq!(
                    emit_to(output(), &mut Vec::new(), &mut Fails { on_flush }),
                    ExitCode::RUNTIME
                );
                assert_eq!(
                    emit_to(output(), &mut Fails { on_flush }, &mut Vec::new()),
                    ExitCode::RUNTIME
                );
            }
        }
    }

    #[test]
    fn write_and_flush_failures_are_runtime_errors() {
        for on_flush in [false, true] {
            let mut stderr = Vec::new();
            assert_eq!(
                emit_to(
                    Ok(CommandOutput::stdout(b"ok\n")),
                    &mut Fails { on_flush },
                    &mut stderr
                ),
                ExitCode::RUNTIME
            );
            assert!(String::from_utf8(stderr).unwrap().contains("標準出力"));
            let mut stdout = Vec::new();
            assert_eq!(
                emit_to(
                    Err(CommandError::usage("引数")),
                    &mut stdout,
                    &mut Fails { on_flush }
                ),
                ExitCode::RUNTIME
            );
            assert!(stdout.is_empty());
        }
    }

    #[test]
    fn every_numeric_code_preserves_silent_and_raw_outputs() {
        assert_eq!(ExitCode::SUCCESS.as_u8(), 0);
        assert_eq!(ExitCode::RUNTIME.as_u8(), 1);
        assert_eq!(ExitCode::USAGE.as_u8(), 2);
        for value in u8::MIN..=u8::MAX {
            let code = ExitCode::new(value);
            assert_eq!(code.as_u8(), value);
            assert_eq!(std::process::ExitCode::from(code), value.into());
            for (out, err) in [(b"".as_slice(), b"".as_slice()), (b"\0\xff", b"\xfe\0")] {
                let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
                assert_eq!(
                    execute_to(&mut io::empty(), &mut stdout, &mut stderr, |_io| {
                        Ok(CommandOutput {
                            stdout: out.to_vec(),
                            stderr: err.to_vec(),
                            code,
                        })
                    }),
                    code
                );
                assert_eq!(stdout, out);
                assert_eq!(stderr, err);
            }
        }
    }

    #[derive(Debug, PartialEq, Eq)]
    enum Event {
        Write(&'static str, Vec<u8>),
        Flush(&'static str),
    }

    struct OrderedWriter {
        name: &'static str,
        events: Rc<RefCell<Vec<Event>>>,
    }

    impl Write for OrderedWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.events
                .borrow_mut()
                .push(Event::Write(self.name, bytes.to_vec()));
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            self.events.borrow_mut().push(Event::Flush(self.name));
            Ok(())
        }
    }

    #[test]
    fn intermediate_outputs_flush_in_call_order_before_final_outputs() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let mut stdout = OrderedWriter {
            name: "stdout",
            events: events.clone(),
        };
        let mut stderr = OrderedWriter {
            name: "stderr",
            events: events.clone(),
        };
        let code = execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
            io.write_stderr(b"\xff").unwrap();
            assert_eq!(events.borrow().last(), Some(&Event::Flush("stderr")));
            io.write_stdout(b"\0").unwrap();
            assert_eq!(events.borrow().last(), Some(&Event::Flush("stdout")));
            Ok(CommandOutput {
                stdout: b"last-out".to_vec(),
                stderr: b"last-err".to_vec(),
                code: ExitCode::new(30),
            })
        });
        assert_eq!(code, ExitCode::new(30));
        assert_eq!(
            *events.borrow(),
            [
                Event::Write("stderr", b"\xff".to_vec()),
                Event::Flush("stderr"),
                Event::Write("stdout", b"\0".to_vec()),
                Event::Flush("stdout"),
                Event::Write("stdout", b"last-out".to_vec()),
                Event::Flush("stdout"),
                Event::Write("stderr", b"last-err".to_vec()),
                Event::Flush("stderr"),
            ]
        );
    }

    struct PartialWriter {
        bytes: Vec<u8>,
        limit: usize,
        writes: usize,
        flushes: usize,
        fail_write: Option<usize>,
        fail_flush: bool,
    }

    impl PartialWriter {
        fn new(limit: usize) -> Self {
            Self {
                bytes: Vec::new(),
                limit,
                writes: 0,
                flushes: 0,
                fail_write: None,
                fail_flush: false,
            }
        }
    }

    impl Write for PartialWriter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.writes += 1;
            if self.fail_write == Some(self.writes) {
                return Err(io::Error::new(io::ErrorKind::BrokenPipe, "出力の故障"));
            }
            let count = bytes.len().min(self.limit);
            self.bytes.extend_from_slice(&bytes[..count]);
            Ok(count)
        }

        fn flush(&mut self) -> io::Result<()> {
            self.flushes += 1;
            if self.fail_flush {
                Err(io::Error::new(io::ErrorKind::BrokenPipe, "出力の故障"))
            } else {
                Ok(())
            }
        }
    }

    #[test]
    fn short_writes_complete_before_flushing() {
        let (mut stdout, mut stderr) = (PartialWriter::new(2), PartialWriter::new(1));
        assert_eq!(
            execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
                io.write_stdout(b"a\0\xffbc").unwrap();
                io.write_stderr(b"\xfe\0z").unwrap();
                Ok(CommandOutput::stdout(b"end"))
            }),
            ExitCode::SUCCESS
        );
        assert_eq!(stdout.bytes, b"a\0\xffbcend");
        assert_eq!(stdout.writes, 5);
        assert_eq!(stdout.flushes, 2);
        assert_eq!(stderr.bytes, b"\xfe\0z");
        assert_eq!(stderr.writes, 3);
        assert_eq!(stderr.flushes, 2);
    }

    #[test]
    fn ignored_stdout_failures_block_later_output_and_diagnose_once() {
        for on_flush in [false, true] {
            for finish in 0..3 {
                let mut stdout = PartialWriter::new(2);
                // 書込み失敗の場合は、2バイトだけ出た後に失敗させる。
                stdout.fail_write = (!on_flush).then_some(2);
                stdout.fail_flush = on_flush;
                let mut stderr = PartialWriter::new(usize::MAX);
                let code = execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
                    let error = io.write_stdout(b"start").unwrap_err();
                    assert_eq!(error.kind(), io::ErrorKind::BrokenPipe);
                    assert_eq!(error.to_string(), "出力の故障");
                    for result in [io.write_stderr(b"extra-err"), io.write_stdout(b"extra-out")] {
                        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::BrokenPipe);
                    }
                    match finish {
                        0 => Ok(CommandOutput::stdout(b"final")),
                        1 => Ok(CommandOutput {
                            stdout: b"final".to_vec(),
                            stderr: b"final-err".to_vec(),
                            code: ExitCode::new(255),
                        }),
                        _ => Err(CommandError::usage("後からの診断")),
                    }
                });
                assert_eq!(code, ExitCode::RUNTIME);
                assert_eq!(
                    stdout.bytes,
                    if on_flush { b"start".as_slice() } else { b"st" }
                );
                assert_eq!(stdout.writes, if on_flush { 3 } else { 2 });
                assert_eq!(stdout.flushes, usize::from(on_flush));
                assert_eq!(
                    stderr.bytes,
                    "dev-workflow: 標準出力に書けません: 出力の故障\n".as_bytes()
                );
                assert_eq!(stderr.flushes, 1);
            }
        }
    }

    #[test]
    fn ignored_stderr_failures_block_both_streams_without_another_diagnostic() {
        for on_flush in [false, true] {
            let mut stdout = PartialWriter::new(usize::MAX);
            let mut stderr = PartialWriter::new(usize::MAX);
            stderr.fail_write = (!on_flush).then_some(1);
            stderr.fail_flush = on_flush;
            assert_eq!(
                execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
                    assert_eq!(
                        io.write_stderr(b"detail").unwrap_err().to_string(),
                        "出力の故障"
                    );
                    assert!(io.write_stdout(b"extra-out").is_err());
                    assert!(io.write_stderr(b"extra-err").is_err());
                    Err(CommandError::runtime("後からの診断"))
                }),
                ExitCode::RUNTIME
            );
            assert_eq!((stdout.writes, stdout.flushes), (0, 0));
            assert_eq!(stderr.writes, 1);
            assert_eq!(stderr.flushes, usize::from(on_flush));
            assert_eq!(
                stderr.bytes,
                if on_flush { b"detail".as_slice() } else { b"" }
            );
        }
    }

    #[test]
    fn zero_byte_writes_fail_and_do_not_flush_or_retry() {
        for fail_stdout in [false, true] {
            let (mut failing, mut other) = (PartialWriter::new(0), Vec::new());
            let (stdout, stderr): (&mut dyn Write, &mut dyn Write) = if fail_stdout {
                (&mut failing, &mut other)
            } else {
                (&mut other, &mut failing)
            };
            let mut error_message = String::new();
            assert_eq!(
                execute_to(&mut io::empty(), stdout, stderr, |io| {
                    let error = if fail_stdout {
                        io.write_stdout(b"x")
                    } else {
                        io.write_stderr(b"x")
                    }
                    .unwrap_err();
                    assert_eq!(error.kind(), io::ErrorKind::WriteZero);
                    error_message = error.to_string();
                    assert!(io.write_stdout(b"more").is_err());
                    assert!(io.write_stderr(b"more").is_err());
                    Ok(CommandOutput::stdout(b"final"))
                }),
                ExitCode::RUNTIME
            );
            assert_eq!((failing.writes, failing.flushes), (1, 0));
            assert_eq!(
                other,
                if fail_stdout {
                    format!("dev-workflow: 標準出力に書けません: {error_message}\n").into_bytes()
                } else {
                    Vec::new()
                }
            );
        }
    }

    #[test]
    fn stdout_diagnostic_failure_is_not_retried() {
        for on_flush in [false, true] {
            let mut stdout = PartialWriter::new(usize::MAX);
            stdout.fail_write = Some(1);
            let mut stderr = PartialWriter::new(usize::MAX);
            stderr.fail_write = (!on_flush).then_some(1);
            stderr.fail_flush = on_flush;
            assert_eq!(
                execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
                    assert!(io.write_stdout(b"x").is_err());
                    assert!(io.write_stderr(b"more").is_err());
                    Ok(CommandOutput::stdout(b"final"))
                }),
                ExitCode::RUNTIME
            );
            assert_eq!((stdout.writes, stdout.flushes), (1, 0));
            assert_eq!(stderr.flushes, usize::from(on_flush));
            assert_eq!(
                stderr.bytes,
                if on_flush {
                    "dev-workflow: 標準出力に書けません: 出力の故障\n".as_bytes()
                } else {
                    b""
                }
            );
            if !on_flush {
                assert_eq!(stderr.writes, 1);
            }
        }
    }

    struct ShortReader<'a> {
        bytes: &'a [u8],
        requests: Vec<usize>,
    }

    impl Read for ShortReader<'_> {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            self.requests.push(buffer.len());
            let count = buffer.len().min(2).min(self.bytes.len());
            buffer[..count].copy_from_slice(&self.bytes[..count]);
            self.bytes = &self.bytes[count..];
            Ok(count)
        }
    }

    #[test]
    fn input_preserves_short_reads_raw_bytes_eof_and_buffer_boundaries() {
        let mut input = ShortReader {
            bytes: b"\xff\0abc",
            requests: Vec::new(),
        };
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        assert_eq!(
            execute_to(&mut input, &mut stdout, &mut stderr, |io| {
                let mut buffer = [0xa5; 4];
                assert_eq!(io.read_input(&mut buffer).unwrap(), 2);
                assert_eq!(buffer, [0xff, 0, 0xa5, 0xa5]);
                assert_eq!(io.read_input(&mut buffer[..1]).unwrap(), 1);
                assert_eq!(buffer, [b'a', 0, 0xa5, 0xa5]);
                assert_eq!(io.read_input(&mut []).unwrap(), 0);
                assert_eq!(io.read_input(&mut buffer).unwrap(), 2);
                assert_eq!(buffer, [b'b', b'c', 0xa5, 0xa5]);
                assert_eq!(io.read_input(&mut buffer).unwrap(), 0);
                assert_eq!(buffer, [b'b', b'c', 0xa5, 0xa5]);
                Ok(CommandOutput::stdout(b""))
            }),
            ExitCode::SUCCESS
        );
        assert_eq!(input.requests, [4, 1, 0, 4, 4]);
        assert!(stdout.is_empty());
        assert!(stderr.is_empty());
    }

    #[test]
    fn input_errors_are_not_retried_or_latched_as_output_failures() {
        struct InputError {
            kind: io::ErrorKind,
            requests: Vec<usize>,
        }
        impl Read for InputError {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                self.requests.push(buffer.len());
                Err(io::Error::new(self.kind, "入力の故障"))
            }
        }
        for kind in [io::ErrorKind::Interrupted, io::ErrorKind::PermissionDenied] {
            let mut input = InputError {
                kind,
                requests: Vec::new(),
            };
            let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
            assert_eq!(
                execute_to(&mut input, &mut stdout, &mut stderr, |io| {
                    let error = io.read_input(&mut [0; 3]).unwrap_err();
                    assert_eq!(error.kind(), kind);
                    assert_eq!(error.to_string(), "入力の故障");
                    io.write_stdout(b"\xff").unwrap();
                    Ok(CommandOutput {
                        stdout: Vec::new(),
                        stderr: b"fixed".to_vec(),
                        code: ExitCode::new(42),
                    })
                }),
                ExitCode::new(42)
            );
            assert_eq!(input.requests, [3]);
            assert_eq!(stdout, b"\xff");
            assert_eq!(stderr, b"fixed");
        }
    }
}
