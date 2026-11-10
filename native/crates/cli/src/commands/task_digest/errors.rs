//! Python の例外表示と標準エラーの backslashreplace。
use super::bytes;
use std::ffi::OsStr;
use std::io;

pub fn stderr(bytes: &[u8]) -> Vec<u8> {
    let encoding = std::env::var("PYTHONIOENCODING").unwrap_or_default();
    if !matches!(
        encoding.split(':').next().unwrap_or(""),
        "ascii" | "us-ascii" | "646"
    ) {
        return bytes::stderr(bytes);
    }
    let mut result = Vec::new();
    for c in bytes::characters(bytes) {
        if c < 0x80 {
            result.push(c as u8);
        } else if c <= 0xff {
            result.extend_from_slice(format!("\\x{c:02x}").as_bytes());
        } else if c <= 0xffff {
            result.extend_from_slice(format!("\\u{c:04x}").as_bytes());
        } else {
            result.extend_from_slice(format!("\\U{c:08x}").as_bytes());
        }
    }
    result
}

pub fn os(error: &io::Error, filename: Option<&OsStr>) -> Vec<u8> {
    let message = error.to_string();
    let mut result = if let Some(errno) = error.raw_os_error() {
        let suffix = format!(" (os error {errno})");
        format!(
            "[Errno {errno}] {}",
            message.strip_suffix(&suffix).unwrap_or(&message)
        )
        .into_bytes()
    } else {
        message.into_bytes()
    };
    if let Some(filename) = filename {
        result.extend_from_slice(b": ");
        result.extend_from_slice(&bytes::repr(bytes::raw(filename)));
    }
    result
}

pub fn utf8(data: &[u8], error: std::str::Utf8Error) -> String {
    let start = error.valid_up_to();
    let end = error.error_len().map_or(data.len(), |n| start + n);
    let reason = if error.error_len().is_none() {
        "unexpected end of data"
    } else if !(0xc2..=0xf4).contains(&data[start]) {
        "invalid start byte"
    } else {
        "invalid continuation byte"
    };
    if end == start + 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            data[start]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    }
}
