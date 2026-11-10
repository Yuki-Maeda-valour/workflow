#![forbid(unsafe_code)]
//! R4B専用の検査入口。製品登録表を変更せず、C0aの最終出力を通す。
#[path = "../src/commands/git_config_digest/mod.rs"]
mod git_config_digest;
#[path = "../src/commands/origin_repo/mod.rs"]
mod origin_repo;
#[path = "../src/commands/r4b_support/mod.rs"]
mod r4b_support;
fn main() -> std::process::ExitCode {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let result = match args.first().and_then(|v| v.to_str()) {
        Some("origin-repo") => origin_repo::run(&args[1..]),
        Some("git-config-digest") => git_config_digest::run(&args[1..]),
        _ => Ok(r4b_support::output("R4B の検査対象を指定してください", 2)),
    };
    if let Some(mode) = std::env::var_os("R4B_OUTPUT_FAULT") {
        return fault_emission(result, &mode.to_string_lossy()).into();
    }
    dev_workflow_core::emit(result).into()
}

/// 検査専用入口だけの故障注入。製品コマンドには接続しない。
fn fault_emission(
    result: dev_workflow_core::CommandResult,
    mode: &str,
) -> dev_workflow_core::ExitCode {
    use std::io::{self, Write};
    struct Writer {
        bytes: Vec<u8>,
        remaining: usize,
        flush: bool,
    }
    impl Write for Writer {
        fn write(&mut self, data: &[u8]) -> io::Result<usize> {
            if self.remaining == 0 {
                return Err(io::Error::from_raw_os_error(32));
            }
            let count = data.len().min(self.remaining);
            self.bytes.extend_from_slice(&data[..count]);
            self.remaining -= count;
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            if self.flush {
                Err(io::Error::from_raw_os_error(32))
            } else {
                Ok(())
            }
        }
    }
    let mut out = Writer {
        bytes: Vec::new(),
        remaining: if mode.starts_with("stdout-write") {
            0
        } else if mode == "stdout-partial" {
            3
        } else {
            usize::MAX
        },
        flush: mode == "stdout-flush",
    };
    let mut err = Writer {
        bytes: Vec::new(),
        remaining: if mode.contains("stderr-write") {
            0
        } else {
            usize::MAX
        },
        flush: mode.contains("stderr-flush"),
    };
    let code = dev_workflow_core::emit_to(result, &mut out, &mut err);
    let value = serde_json::json!({"code":code.as_u8(),"exception":null,"stdout":String::from_utf8(out.bytes).unwrap(),"stderr":String::from_utf8(err.bytes).unwrap()});
    dev_workflow_core::emit(Ok(dev_workflow_core::CommandOutput::stdout(
        format!("{value}\n").into_bytes(),
    )))
}

#[cfg(test)]
mod tests {
    use dev_workflow_core::{CommandOutput, ExitCode, emit_to};
    use std::io::{self, Write};
    struct Fault {
        bytes: Vec<u8>,
        remaining: usize,
        flush: bool,
    }
    impl Write for Fault {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.remaining == 0 {
                return Err(io::ErrorKind::BrokenPipe.into());
            }
            let count = bytes.len().min(self.remaining);
            self.bytes.extend_from_slice(&bytes[..count]);
            self.remaining -= count;
            Ok(count)
        }
        fn flush(&mut self) -> io::Result<()> {
            if self.flush {
                Err(io::ErrorKind::BrokenPipe.into())
            } else {
                Ok(())
            }
        }
    }
    #[test]
    fn both_commands_use_c0a_for_write_flush_partial_and_diagnostic_failures() {
        // コマンドからの確定済み出力を最終出力処理へ渡す境界。
        for output in [
            CommandOutput::stdout(b"{\"origin\": false}\n"),
            CommandOutput::stdout(b"sha256:fixed\n"),
        ] {
            for remaining in [0, 3, usize::MAX] {
                let flush = remaining == usize::MAX;
                let mut stdout = Fault {
                    bytes: Vec::new(),
                    remaining,
                    flush,
                };
                let mut stderr = Vec::new();
                let duplicate = || {
                    Ok(CommandOutput {
                        stdout: output.stdout.clone(),
                        stderr: Vec::new(),
                        code: ExitCode::SUCCESS,
                    })
                };
                assert_eq!(
                    emit_to(duplicate(), &mut stdout, &mut stderr),
                    ExitCode::RUNTIME
                );
                assert_eq!(
                    stdout.bytes,
                    &output.stdout[..if flush {
                        output.stdout.len()
                    } else {
                        remaining
                    }]
                );
                assert!(stderr.starts_with("dev-workflow: 標準出力に書けません: ".as_bytes()));
                for diagnostic_flush in [false, true] {
                    let mut stdout = Fault {
                        bytes: Vec::new(),
                        remaining: 0,
                        flush: false,
                    };
                    let mut stderr = Fault {
                        bytes: Vec::new(),
                        remaining: if diagnostic_flush { usize::MAX } else { 0 },
                        flush: diagnostic_flush,
                    };
                    assert_eq!(
                        emit_to(duplicate(), &mut stdout, &mut stderr),
                        ExitCode::RUNTIME
                    );
                }
            }
        }
        for on_flush in [false, true] {
            let mut stderr = Fault {
                bytes: Vec::new(),
                remaining: if on_flush { usize::MAX } else { 0 },
                flush: on_flush,
            };
            let output = CommandOutput {
                stdout: Vec::new(),
                stderr: b"fixed refusal\n".to_vec(),
                code: ExitCode::USAGE,
            };
            assert_eq!(
                emit_to(Ok(output), &mut Vec::new(), &mut stderr),
                ExitCode::RUNTIME
            );
        }
    }
}
