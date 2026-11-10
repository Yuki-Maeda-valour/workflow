//! R4Bの専用互換処理。製品のコマンド登録には接続しない。
pub mod args;
pub mod bytes;
mod unicode;
use dev_workflow_core::{CommandOutput, ExitCode};
pub use dev_workflow_platform::r4b_process::{Captured, Failure, capture};
use sha2::{Digest, Sha256};
use std::ffi::{OsStr, OsString};
use std::process::Command;
use std::time::Duration;
pub const SAFE_GIT: &[&str] = &[
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.ignoreCase=false",
    "-c",
    "core.splitIndex=false",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "push.gpgSign=false",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.clean=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
];
pub fn git(
    directory: &OsStr,
    args: &[OsString],
    timeout: Option<Duration>,
) -> Result<Captured, Failure> {
    capture(
        Command::new("git")
            .arg("-C")
            .arg(directory)
            .args(SAFE_GIT)
            .args(args),
        timeout,
    )
}
pub fn strings(args: &[&str]) -> Vec<OsString> {
    args.iter().map(OsString::from).collect()
}
pub fn output(message: &str, code: u8) -> CommandOutput {
    CommandOutput {
        stdout: Vec::new(),
        stderr: format!("{message}\n").into_bytes(),
        code: ExitCode::new(code),
    }
}
pub fn hash(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
pub fn reason(error: &std::io::Error) -> String {
    error
        .to_string()
        .split(" (os error ")
        .next()
        .unwrap()
        .to_owned()
}
pub fn spawn_error(error: &std::io::Error, name: &str) -> String {
    format!(
        "git を起動できない: [Errno {}] {}: '{}'",
        error.raw_os_error().unwrap_or(0),
        reason(error),
        name
    )
}
/// Pythonのstr.splitlines。LF/CR以外のUnicode改行も同じ境界とする。
pub fn splitlines(text: &str) -> Vec<&str> {
    text.split([
        '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
        '\u{2029}',
    ])
    .filter(|line| !line.is_empty())
    .collect()
}
pub fn text(bytes: &[u8]) -> Result<String, std::str::Utf8Error> {
    Ok(std::str::from_utf8(bytes)?
        .replace("\r\n", "\n")
        .replace('\r', "\n"))
}
/// ensure_ascii=True。無効UTF-8はsurrogateescapeとして各バイトを保つ。
pub fn ascii_json(bytes: &[u8]) -> String {
    json_characters(bytes::characters(bytes))
}
fn json_characters(codes: impl IntoIterator<Item = u32>) -> String {
    let mut result = String::from("\"");
    for code in codes {
        match code {
            34 => result.push_str("\\\""),
            92 => result.push_str("\\\\"),
            8 => result.push_str("\\b"),
            9 => result.push_str("\\t"),
            10 => result.push_str("\\n"),
            12 => result.push_str("\\f"),
            13 => result.push_str("\\r"),
            32..=126 => result.push(char::from_u32(code).unwrap()),
            0..=65535 => result.push_str(&format!("\\u{code:04x}")),
            _ => {
                let pair = code - 65536;
                result.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (pair >> 10),
                    0xdc00 + (pair & 1023)
                ));
            }
        }
    }
    result.push('"');
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ascii_matches_python_boundaries() {
        assert_eq!(ascii_json(b"\xff\n\0"), "\"\\udcff\\n\\u0000\"");
        assert_eq!(ascii_json("😀".as_bytes()), "\"\\ud83d\\ude00\"");
    }
}
