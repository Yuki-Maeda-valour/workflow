//! オプションを持たない Python 3.10 argparse の位置引数1個。
use super::bytes::{concat, raw};
use dev_workflow_core::{CommandOutput, ExitCode};
use std::ffi::OsString;

const PROG: &str = "task-digest.py";

fn columns() -> usize {
    configured_columns().unwrap_or_else(dev_workflow_platform::task_digest::stdout_columns)
}

// CPython 3.10の起動時に有効な整数変換上限だけを読む。
// 無効な設定によるPython起動前の失敗、-X、sys.set_int_max_str_digitsは扱わない。
fn int_max_str_digits() -> usize {
    let configured = || {
        let value = std::env::var("PYTHONINTMAXSTRDIGITS").ok()?;
        let value = value.trim_start_matches([' ', '\t', '\n', '\r', '\u{b}', '\u{c}']);
        let (negative, value) = if let Some(value) = value.strip_prefix('-') {
            (true, value)
        } else {
            (false, value.strip_prefix('+').unwrap_or(value))
        };
        if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        let total = value.bytes().try_fold(0i32, |total, byte| {
            total.checked_mul(10)?.checked_add(i32::from(byte - b'0'))
        })?;
        if (negative && total != 0) || (total != 0 && total < 640) {
            return None;
        }
        Some(total as usize)
    };
    configured().unwrap_or(4300)
}

fn configured_columns() -> Option<usize> {
    let value = std::env::var("COLUMNS").unwrap_or_default();
    let value = value.trim();
    let value = value.strip_prefix('+').unwrap_or(value);
    let max_digits = int_max_str_digits();
    let mut digits = 0usize;
    let mut total = 0usize;
    let mut previous = false;
    for c in value.chars() {
        if c == '_' && previous {
            previous = false;
            continue;
        }
        let digit = super::unicode::decimal(c as u32)?;
        digits += 1;
        if max_digits != 0 && digits > max_digits {
            return None;
        }
        total = total.saturating_mul(10).saturating_add(usize::from(digit));
        previous = true;
    }
    (previous && total > 0).then_some(total)
}

fn usage() -> String {
    let width = columns().saturating_sub(2);
    if 7 + PROG.len() + 5 <= width {
        format!("usage: {PROG} path\n")
    } else {
        format!("usage: {PROG}\n       path\n")
    }
}

fn error(message: &[u8]) -> CommandOutput {
    CommandOutput {
        stdout: vec![],
        stderr: super::errors::stderr(&concat(&[
            usage().as_bytes(),
            PROG.as_bytes(),
            b": error: ",
            message,
            b"\n",
        ])),
        code: ExitCode::USAGE,
    }
}

fn negative_number(bytes: &[u8]) -> bool {
    let bytes = bytes.strip_suffix(b"\n").unwrap_or(bytes);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    let Some(text) = text.strip_prefix('-') else {
        return false;
    };
    let digits = |s: &str| {
        s.chars()
            .all(|c| super::unicode::decimal(c as u32).is_some())
    };
    if let Some((left, right)) = text.split_once('.') {
        digits(left) && !right.is_empty() && digits(right)
    } else {
        !text.is_empty() && digits(text)
    }
}

pub fn parse(args: &[OsString]) -> Result<&OsString, CommandOutput> {
    let separator = args.iter().position(|s| raw(s) == b"--");
    let is_value = |i: usize| {
        let s = raw(&args[i]);
        separator.is_some_and(|at| i > at)
            || s.is_empty()
            || s[0] != b'-'
            || s == b"-"
            || negative_number(s)
            || s.contains(&b' ')
    };
    let Some(path) = (0..args.len()).find(|&i| Some(i) != separator && is_value(i)) else {
        return Err(error(b"the following arguments are required: path"));
    };
    let mut extras = Vec::new();
    for (i, value) in args.iter().enumerate() {
        if i == path || (Some(i) == separator && (i + 1 == path || path + 1 == i)) {
            continue;
        }
        extras.push(raw(value));
    }
    if !extras.is_empty() {
        return Err(error(&concat(&[
            b"unrecognized arguments: ",
            &extras.join(&b" "[..]),
        ])));
    }
    Ok(&args[path])
}
