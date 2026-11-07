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

/// 成功時の標準出力は、末尾改行も含む完成したバイト列で返す。
pub type CommandResult = Result<Vec<u8>, CommandError>;

/// 出力に失敗したら終了 1。BrokenPipe も成功として扱わない。
pub fn emit(result: CommandResult) -> ExitCode {
    emit_to(result, &mut io::stdout().lock(), &mut io::stderr().lock())
}

/// 書き込みと flush の両方を確かめる。標準エラーの失敗時は再帰して報告しない。
pub fn emit_to(
    result: CommandResult,
    stdout: &mut impl Write,
    stderr: &mut impl Write,
) -> ExitCode {
    let (code, message) = match result {
        Ok(bytes) => {
            return match stdout.write_all(&bytes).and_then(|()| stdout.flush()) {
                Ok(()) => ExitCode::Success,
                Err(error) => {
                    let _ = diagnostic(stderr, &format!("標準出力に書けません: {error}"));
                    ExitCode::Runtime
                }
            };
        }
        Err(CommandError::Runtime(message)) => (ExitCode::Runtime, message),
        Err(CommandError::Usage(message)) => (ExitCode::Usage, message),
    };
    if diagnostic(stderr, &message).is_err() {
        ExitCode::Runtime
    } else {
        code
    }
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
            (Ok(b"ok\n".to_vec()), ExitCode::Success, "ok\n", ""),
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
    fn write_and_flush_failures_are_runtime_errors() {
        for on_flush in [false, true] {
            let mut stderr = Vec::new();
            assert_eq!(
                emit_to(Ok(b"ok\n".to_vec()), &mut Fails { on_flush }, &mut stderr),
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
