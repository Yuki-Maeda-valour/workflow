use dev_workflow_core::{CommandError, CommandOutput, CommandResult, build_info};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Read};

// 引数にはコマンド名を含めない。検査を終えてからファイルを読む。
pub fn run(args: &[OsString]) -> CommandResult {
    let [option] = args else {
        return Err(CommandError::usage(
            "probe は --json か --self-sha256 をどちらか1つだけ指定してください",
        ));
    };
    if option == "--json" {
        json()
    } else if option == "--self-sha256" {
        let path = std::env::current_exe().map_err(|error| {
            CommandError::runtime(format!("自己の実行ファイルのパスを取得できません: {error}"))
        })?;
        let file = File::open(path).map_err(read_error)?;
        self_sha256(file)
    } else {
        Err(CommandError::usage(
            "probe の引数が不正です。--json か --self-sha256 を指定してください",
        ))
    }
}

#[derive(Serialize)]
struct ProbeInfo {
    schema_version: u32,
    ok: bool,
    os: &'static str,
    arch: &'static str,
    native_version: &'static str,
    source_input_sha256: &'static str,
    rustc: &'static str,
}

fn json() -> CommandResult {
    let info = ProbeInfo {
        schema_version: 1,
        ok: true,
        os: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        native_version: build_info::NATIVE_VERSION,
        source_input_sha256: build_info::SOURCE_INPUT_SHA256,
        rustc: build_info::RUSTC,
    };
    let mut bytes = serde_json::to_vec(&info)
        .map_err(|error| CommandError::runtime(format!("JSON を作れません: {error}")))?;
    bytes.push(b'\n');
    Ok(CommandOutput::stdout(bytes))
}

fn read_error(error: io::Error) -> CommandError {
    CommandError::runtime(format!("自己の実行ファイルを読めません: {error}"))
}

fn self_sha256(mut file: impl Read) -> CommandResult {
    let mut hash = Sha256::new();
    let mut buffer = [0; 16 * 1024];
    loop {
        match file.read(&mut buffer) {
            Ok(0) => {
                return Ok(CommandOutput::stdout(
                    format!("{:x}\n", hash.finalize()).into_bytes(),
                ));
            }
            Ok(count) => hash.update(&buffer[..count]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(read_error(error)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_core::{ExitCode, emit_to};

    struct ReadFailure;

    impl Read for ReadFailure {
        fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("読み取りの失敗"))
        }
    }

    #[test]
    fn read_failure_after_partial_input_emits_only_a_runtime_error() {
        let reader = io::Cursor::new(b"partial executable").chain(ReadFailure);
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        assert_eq!(
            emit_to(self_sha256(reader), &mut stdout, &mut stderr),
            ExitCode::RUNTIME
        );
        assert!(stdout.is_empty());
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            "dev-workflow: 自己の実行ファイルを読めません: 読み取りの失敗\n"
        );
    }

    #[test]
    fn interrupted_reads_are_retried() {
        struct InterruptedOnce(bool);

        impl Read for InterruptedOnce {
            fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
                if std::mem::take(&mut self.0) {
                    Err(io::ErrorKind::Interrupted.into())
                } else {
                    Ok(0)
                }
            }
        }

        let reader = InterruptedOnce(true).chain(io::Cursor::new(b"abc"));
        assert_eq!(
            self_sha256(reader).unwrap().stdout,
            b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n"
        );
    }
}
