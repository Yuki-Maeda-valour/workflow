//! 未配布の task-digest 本体。製品 CLI の登録表には接続しない。
mod args;
#[path = "../resolve_task_dir/bytes.rs"]
// JSON用関数と、harness=falseでは走らない既存の単体テストのimportを除く。
#[allow(dead_code, unused_imports)]
mod bytes;
pub mod errors;
pub mod normalize;
pub mod output;
#[path = "../resolve_task_dir/unicode.rs"]
mod unicode;

use dev_workflow_core::{CommandIo, CommandOutput, ExitCode};
use dev_workflow_platform::task_digest::{ReadError, RegularPath};
use std::ffi::OsString;
use std::io;
use std::path::Path;

pub fn failure(message: &[u8]) -> CommandOutput {
    CommandOutput {
        stdout: vec![],
        stderr: errors::stderr(&bytes::concat(&[
            "算出できない: ".as_bytes(),
            message,
            b"\n",
        ])),
        code: ExitCode::RUNTIME,
    }
}

/// 入力元だけを借りる。短読は続行し、途中の失敗を成功や部分本文へ変えない。
pub fn read_all(mut read: impl FnMut(&mut [u8]) -> io::Result<usize>) -> io::Result<Vec<u8>> {
    read_bytes(&mut read, false)
}

fn read_stdin(mut read: impl FnMut(&mut [u8]) -> io::Result<usize>) -> io::Result<Vec<u8>> {
    read_bytes(&mut read, true)
}

fn read_bytes(
    read: &mut impl FnMut(&mut [u8]) -> io::Result<usize>,
    stdin: bool,
) -> io::Result<Vec<u8>> {
    let mut data = Vec::new();
    let mut buffer = [0; 8192];
    loop {
        match read(&mut buffer) {
            Ok(0) => return Ok(data),
            Ok(count) => data.extend_from_slice(&buffer[..count]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            // PythonのBufferedReader.read()は、読めた分があればEAGAINで返す。
            // 空のEAGAINだけは呼出元で専用診断へ変え、通常ファイルへ広げない。
            Err(error)
                if stdin && error.kind() == io::ErrorKind::WouldBlock && !data.is_empty() =>
            {
                return Ok(data);
            }
            Err(error) => return Err(error),
        }
    }
}

pub fn from_bytes(data: &[u8]) -> CommandOutput {
    let text = match std::str::from_utf8(data) {
        Ok(text) => text,
        Err(error) => {
            return failure(
                format!("UTF-8 として読めない: {}", errors::utf8(data, error)).as_bytes(),
            );
        }
    };
    match normalize::body(text) {
        Ok(body) => CommandOutput::stdout(format!("{}\n", normalize::digest(&body)).into_bytes()),
        Err(error) => failure(error.as_bytes()),
    }
}

pub fn file_error(error: ReadError, path: &Path) -> CommandOutput {
    match error {
        ReadError::NotRegular => failure("入力パスが通常ファイルでない".as_bytes()),
        ReadError::Io { error, filename } => failure(&bytes::concat(&[
            "ファイルを読めない: ".as_bytes(),
            &errors::os(&error, filename.then_some(path.as_os_str())),
        ])),
    }
}

pub fn run(args: &[OsString], io: &mut CommandIo<'_>) -> CommandOutput {
    let path = match args::parse(args) {
        Ok(path) => path,
        Err(output) => return output,
    };
    let stdin = path == "-";
    let result = if stdin {
        read_stdin(|buffer| io.read_input(buffer))
    } else {
        let path = Path::new(path);
        let mut file = match RegularPath::inspect(path).and_then(RegularPath::open) {
            Ok(file) => file,
            Err(error) => return file_error(error, path),
        };
        read_all(|buffer| file.read_chunk(buffer))
    };
    match result {
        Ok(data) => from_bytes(&data),
        Err(error) if stdin && error.kind() == io::ErrorKind::WouldBlock => {
            failure("非同期の標準入力にデータがない".as_bytes())
        }
        Err(error) => failure(&bytes::concat(&[
            "ファイルを読めない: ".as_bytes(),
            &errors::os(&error, None),
        ])),
    }
}

/// C0a は入力の借用だけに使う。共通出口へは空の成功を sink に渡す。
pub fn execute_to<W: io::Write + std::os::fd::AsFd>(
    args: &[OsString],
    input: &mut dyn io::Read,
    stdout: &mut W,
    stderr: &mut dyn io::Write,
) -> ExitCode {
    let mut result = None;
    let code = dev_workflow_core::execute_to(input, &mut io::sink(), &mut io::sink(), |io| {
        result = Some(run(args, io));
        Ok(CommandOutput::stdout(Vec::new()))
    });
    assert_eq!(
        code,
        ExitCode::SUCCESS,
        "入力の借用だけに使う共通出口が失敗した"
    );
    output::emit_fd_to(result.expect("task-digest の計算が未実行"), stdout, stderr)
}
