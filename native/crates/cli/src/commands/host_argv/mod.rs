//! `host-argv.py` の移植。ホストを起動せず、閉じた引数の集合だけを検査する。

use crate::commands::r4a_rest_support::chars::{self, PyStr};
use crate::commands::r4a_rest_support::exception::{Frame, PyException};
use crate::commands::r4a_rest_support::stream::{Program, Raised};

pub const PROG: &str = "host-argv.py";
const EXECUTABLE: &str = "実行ファイルが必要";
const DUPLICATE: &str = "host argv の同じ引数を重複できない";
const INVALID: &str = "host argv の許可した引数ではない";

const MODULE: Frame = Frame {
    file: PROG,
    line: 41,
    func: "<module>",
    source: Some("raise SystemExit(main())"),
};
const MAIN_VALIDATE: Frame = Frame {
    file: PROG,
    line: 33,
    func: "main",
    source: Some("validate_argv(values)"),
};
const MAIN_PRINT: Frame = Frame {
    file: PROG,
    line: 35,
    func: "main",
    source: Some("print('ERROR [host-argv] ' + str(exc), file=sys.stderr)"),
};

fn raise_frame(line: u32, message: &'static str) -> Frame {
    let source = match message {
        EXECUTABLE => "raise ValueError('実行ファイルが必要')",
        DUPLICATE => "raise ValueError('host argv の同じ引数を重複できない')",
        _ => "raise ValueError('host argv の許可した引数ではない')",
    };
    Frame {
        file: PROG,
        line,
        func: "validate_argv",
        source: Some(source),
    }
}

/// 検査に失敗した行番号と本文。
pub type Failure = (u32, &'static str);

pub fn validate(values: &[PyStr]) -> Result<(), Failure> {
    if values
        .first()
        .is_none_or(|v| v.is_empty() || v[0] == u32::from(b'-'))
    {
        return Err((9, EXECUTABLE));
    }
    let mut seen: Vec<&[u32]> = Vec::new();
    for token in &values[1..] {
        let (name, separator, value) = match token.iter().position(|&c| c == u32::from(b'=')) {
            Some(at) => (&token[..at], true, &token[at + 1..]),
            None => (&token[..], false, &token[token.len()..]),
        };
        if seen.contains(&name) {
            return Err((19, DUPLICATE));
        }
        seen.push(name);
        if *token == chars::from_str("--verbose") {
            continue;
        }
        if !separator || !valid(name, value) {
            return Err((24, INVALID));
        }
    }
    Ok(())
}

fn is_ascii(c: u32, f: impl Fn(u8) -> bool) -> bool {
    u8::try_from(c).is_ok_and(|b| b.is_ascii() && f(b))
}

fn word(value: &[u32]) -> bool {
    value
        .first()
        .is_some_and(|&c| is_ascii(c, |b| b.is_ascii_alphanumeric()))
        && value[1..].iter().all(|&c| {
            is_ascii(c, |b| {
                b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'/' | b'-')
            })
        })
}

fn digits(value: &[u32]) -> bool {
    !value.is_empty() && value.iter().all(|&c| is_ascii(c, |b| b.is_ascii_digit()))
}

fn positive(value: &[u32]) -> bool {
    digits(value) && value[0] != u32::from(b'0')
}

fn valid(name: &[u32], value: &[u32]) -> bool {
    let text = |s: &str| chars::from_str(s) == name;
    if text("--model") || text("--name") {
        word(value)
    } else if text("--effort") {
        ["low", "medium", "high", "xhigh", "max"]
            .iter()
            .any(|e| chars::from_str(e) == value)
    } else if text("--max-turns") {
        positive(value)
    } else if text("--max-budget-usd") {
        let integer = |s: &[u32]| s == [u32::from(b'0')] || positive(s);
        let mut parts = value.split(|&c| c == u32::from(b'.'));
        let left = parts.next().unwrap_or(&[]);
        match (parts.next(), parts.next()) {
            (None, _) => integer(left),
            (Some(right), None) => integer(left) && digits(right),
            _ => false,
        }
    } else {
        false
    }
}

/// `main()`。成功は無出力・0。拒否は標準エラーに 1 行を出して 20。
pub fn main(program: &mut Program, argv: &[PyStr]) -> Result<i32, Raised> {
    let dashes = chars::from_str("--");
    let values: &[PyStr] = if argv.first() == Some(&dashes) {
        &argv[1..]
    } else {
        argv
    };
    match validate(values) {
        Ok(()) => Ok(0),
        Err((line, message)) => {
            let handled = PyException::new(
                "ValueError",
                message,
                &[MAIN_VALIDATE, raise_frame(line, message)],
            );
            let body = chars::from_str(&format!("ERROR [host-argv] {message}"));
            program
                .print_stderr(&body, &[MODULE, MAIN_PRINT])
                .map_err(|mut exception| {
                    exception.context = Some(Box::new(handled));
                    Raised::Exception(exception)
                })?;
            Ok(20)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<PyStr> {
        values.iter().map(|s| chars::from_str(s)).collect()
    }

    #[test]
    fn accepted_and_rejected_values() {
        assert!(
            validate(&args(&[
                "host",
                "--verbose",
                "--model=x/y",
                "--effort=max",
                "--max-budget-usd=1.2",
                "--max-turns=1"
            ]))
            .is_ok()
        );
        assert_eq!(
            validate(&args(&["host", "--model=x", "--model=y"])),
            Err((19, DUPLICATE))
        );
        assert_eq!(
            validate(&args(&["host", "--model", "secret"])),
            Err((24, INVALID))
        );
        assert_eq!(validate(&args(&["", "x"])), Err((9, EXECUTABLE)));
        assert_eq!(validate(&args(&["host", "--max-budget-usd=0.5"])), Ok(()));
        assert_eq!(
            validate(&args(&["host", "--max-budget-usd=00.5"])),
            Err((24, INVALID))
        );
        assert_eq!(
            validate(&args(&["host", "--max-turns=0"])),
            Err((24, INVALID))
        );
        assert_eq!(
            validate(&args(&["host", "--verbose=1"])),
            Err((24, INVALID))
        );
    }
}
