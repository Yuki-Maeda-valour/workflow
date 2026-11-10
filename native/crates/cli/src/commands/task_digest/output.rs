//! task-digest の実出力。Python の終了時 flush 失敗を終了120にする。
use super::errors;
use dev_workflow_core::{CommandOutput, ExitCode};
use std::ffi::OsStr;
use std::io::{self, Write};
use std::os::fd::AsFd;
use std::os::unix::ffi::OsStrExt;

/// CPython3.10の環境flag。ASCIIの先頭空白・符号・ゼロ列だけを0とする。
pub fn unbuffered_from(value: Option<&OsStr>) -> bool {
    let Some(value) = value else { return false };
    let bytes = value.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    let mut rest = bytes;
    while rest
        .first()
        .is_some_and(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 11 | 12))
    {
        rest = &rest[1..];
    }
    if rest.first().is_some_and(|b| matches!(b, b'+' | b'-')) {
        rest = &rest[1..];
    }
    rest.is_empty() || !rest.iter().all(|b| *b == b'0')
}

#[derive(Clone, Copy)]
enum FailedAt {
    FirstWrite,
    LaterWrite,
    Flush,
}

// 部分出力を取り消した扱いにしないため、失敗の段階と書込済み長も保持する。
fn write_counted(writer: &mut dyn Write, bytes: &[u8]) -> Result<(), (io::Error, usize, FailedAt)> {
    let mut written = 0;
    let mut first = true;
    while written < bytes.len() {
        let stage = if first {
            FailedAt::FirstWrite
        } else {
            FailedAt::LaterWrite
        };
        first = false;
        match writer.write(&bytes[written..]) {
            Ok(0) => return Err((io::ErrorKind::WriteZero.into(), written, stage)),
            Ok(count) => written += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err((error, written, stage)),
        }
    }
    writer
        .flush()
        .map_err(|error| (error, written, FailedAt::Flush))
}

fn flush_failure(error: &io::Error, stream: &str) -> Vec<u8> {
    let kind = if error.kind() == io::ErrorKind::BrokenPipe {
        "BrokenPipeError"
    } else {
        "OSError"
    };
    let encoding = std::env::var("PYTHONIOENCODING").unwrap_or_default();
    let encoding = encoding
        .split(':')
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("utf-8");
    let mut message = format!("Exception ignored in: <_io.TextIOWrapper name='<{stream}>' mode='w' encoding='{encoding}'>\n{kind}: ").into_bytes();
    message.extend_from_slice(&errors::os(error, None));
    message.push(b'\n');
    errors::stderr(&message)
}

/// 任意writerは実stdoutのfd属性を引き継がない。
pub fn emit_to(output: CommandOutput, stdout: &mut dyn Write, stderr: &mut dyn Write) -> ExitCode {
    emit_inner(output, stdout, stderr, false)
}

/// fstatするfdと、実際にwriteするwriterを同じ借用に結び付ける。
pub fn emit_fd_to<W: Write + AsFd>(
    output: CommandOutput,
    stdout: &mut W,
    stderr: &mut dyn Write,
) -> ExitCode {
    let full = dev_workflow_platform::task_digest::is_linux_full(stdout);
    emit_inner(output, stdout, stderr, full)
}

fn emit_inner(
    output: CommandOutput,
    stdout: &mut dyn Write,
    stderr: &mut dyn Write,
    full: bool,
) -> ExitCode {
    let unbuffered = unbuffered_from(std::env::var_os("PYTHONUNBUFFERED").as_deref());
    if !output.stdout.is_empty()
        && let Err((error, written, stage)) = write_counted(stdout, &output.stdout)
    {
        if unbuffered
            && output.code == ExitCode::SUCCESS
            && output.stderr.is_empty()
            && written == 0
            && matches!(stage, FailedAt::FirstWrite)
            && (error.kind() == io::ErrorKind::BrokenPipe
                || (full && error.raw_os_error() == Some(28)))
        {
            let message = if error.kind() == io::ErrorKind::BrokenPipe {
                "標準出力のパイプが閉じている"
            } else {
                "標準出力の空き容量がない"
            };
            let diagnostic = super::failure(message.as_bytes());
            let _ = stderr
                .write_all(&diagnostic.stderr)
                .and_then(|()| stderr.flush());
            return ExitCode::RUNTIME;
        }
        let _ = stderr
            .write_all(&flush_failure(&error, "stdout"))
            .and_then(|()| stderr.flush());
        return ExitCode::new(120);
    }
    if !output.stderr.is_empty() {
        // バッファなしのwrite失敗はPythonの未処理例外と同じ終了1。
        // 終了時flush失敗の120とは分ける。固定診断への追加変更は行わない。
        if stderr.write_all(&output.stderr).is_err() {
            return if unbuffered {
                ExitCode::RUNTIME
            } else {
                ExitCode::new(120)
            };
        }
        if stderr.flush().is_err() {
            return ExitCode::new(120);
        }
    }
    output.code
}
