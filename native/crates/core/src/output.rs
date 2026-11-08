use std::io::{self, Write};

/// コマンドと出力処理が共通で使う終了コード。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum ExitCode {
    Success = 0,
    Runtime = 1,
    Usage = 2,
}

impl From<ExitCode> for std::process::ExitCode {
    fn from(value: ExitCode) -> Self {
        Self::from(value as u8)
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

/// 末尾改行を含む完成した出力と、そのコマンドの終了値。
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
            code: ExitCode::Success,
        }
    }
}

pub type CommandResult = Result<CommandOutput, CommandError>;

/// 出力に失敗したら終了 1。BrokenPipe も成功として扱わない。
pub fn emit(result: CommandResult) -> ExitCode {
    emit_to(result, &mut io::stdout().lock(), &mut io::stderr().lock())
}

/// 両方の出力先で書き込みと flush を確かめる。
pub fn emit_to(
    result: CommandResult,
    stdout: &mut impl Write,
    stderr: &mut impl Write,
) -> ExitCode {
    let output = match result {
        Ok(output) => output,
        Err(error) => {
            let (code, message) = match error {
                CommandError::Runtime(message) => (ExitCode::Runtime, message),
                CommandError::Usage(message) => (ExitCode::Usage, message),
            };
            return if diagnostic(stderr, &message).is_err() {
                ExitCode::Runtime
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
        return ExitCode::Runtime;
    }
    if stderr
        .write_all(&output.stderr)
        .and_then(|()| stderr.flush())
        .is_err()
    {
        return ExitCode::Runtime;
    }
    output.code
}

fn diagnostic(writer: &mut impl Write, message: &str) -> io::Result<()> {
    writeln!(writer, "dev-workflow: {message}")?;
    writer.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

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
                ExitCode::Success,
                "ok\n",
                "",
            ),
            (
                Err(CommandError::runtime("失敗")),
                ExitCode::Runtime,
                "",
                "dev-workflow: 失敗\n",
            ),
            (
                Err(CommandError::usage("引数")),
                ExitCode::Usage,
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
        for code in [ExitCode::Success, ExitCode::Runtime, ExitCode::Usage] {
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
                    ExitCode::Runtime
                );
                assert_eq!(
                    emit_to(output(), &mut Fails { on_flush }, &mut Vec::new()),
                    ExitCode::Runtime
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
                ExitCode::Runtime
            );
            assert!(String::from_utf8(stderr).unwrap().contains("標準出力"));
            let mut stdout = Vec::new();
            assert_eq!(
                emit_to(
                    Err(CommandError::usage("引数")),
                    &mut stdout,
                    &mut Fails { on_flush }
                ),
                ExitCode::Runtime
            );
            assert!(stdout.is_empty());
        }
    }
}
