//! Python 3.10 argparseの順序を保つ。引数とCOLUMNSは承認済みのUTF-8固定。
use super::bytes::{concat, raw, repr};
use dev_workflow_core::{CommandOutput, ExitCode};
use std::ffi::OsString;

pub struct Arguments {
    pub root: OsString,
    pub task_dir: Option<OsString>,
}

#[derive(Clone)]
enum Kind {
    Value,
    Separator,
    Unknown,
    Option {
        index: usize,
        explicit: Option<Vec<u8>>,
    },
}

fn width() -> usize {
    fn columns(value: &str) -> Option<usize> {
        let text = value.trim();
        let text = text.strip_prefix('+').unwrap_or(text);
        if text.is_empty() {
            return None;
        }
        let mut total = 0usize;
        let mut previous_digit = false;
        for c in text.chars() {
            if c == '_' && previous_digit {
                previous_digit = false;
                continue;
            }
            let digit = super::unicode::decimal(c as u32)?;
            total = total.saturating_mul(10).saturating_add(usize::from(digit));
            previous_digit = true;
        }
        (previous_digit && total > 0).then_some(total)
    }
    std::env::var("COLUMNS")
        .ok()
        .as_deref()
        .and_then(columns)
        .unwrap_or_else(dev_workflow_platform::r4b_process::stdout_columns)
        .saturating_sub(2)
}

fn lines(parts: &[&str], indent: usize, prefix: Option<usize>, width: usize) -> Vec<String> {
    let mut result = Vec::new();
    let mut line = Vec::new();
    let mut length = prefix.unwrap_or(indent).saturating_sub(1);
    for &part in parts {
        if length + 1 + part.len() > width && !line.is_empty() {
            result.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
            line.clear();
            length = indent.saturating_sub(1);
        }
        line.push(part);
        length += 1 + part.len();
    }
    if !line.is_empty() {
        result.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
    };
    if prefix.is_some() && !result.is_empty() {
        result[0].drain(..indent);
    }
    result
}

fn usage(prog: &str, expect: bool) -> String {
    let parts = if expect {
        vec!["[-h]", "--dir", "DIR", "[--expect EXPECT]"]
    } else {
        vec!["[-h]", "--dir", "DIR"]
    };

    let width = width();
    let entire = format!("{prog} {}", parts.join(" "));
    let body = if 7 + entire.len() <= width {
        entire
    } else if (7 + prog.len()) * 4 <= width * 3 {
        lines(
            &[&[prog][..], &parts[..]].concat(),
            7 + prog.len() + 1,
            Some(7),
            width,
        )
        .join("\n")
    } else {
        format!("{prog}\n{}", lines(&parts, 7, None, width).join("\n"))
    };
    format!("usage: {body}\n")
}

fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut result = Vec::new();
    let mut line = String::new();
    for word in text.split_whitespace() {
        if !line.is_empty() && line.len() + 1 + word.len() > width {
            result.push(std::mem::take(&mut line));
        }
        if !line.is_empty() {
            line.push(' ');
        }
        let mut rest = word;
        while rest.len() > width && line.is_empty() {
            result.push(rest[..width].to_owned());
            rest = &rest[width..];
        }
        line.push_str(rest);
    }
    if !line.is_empty() {
        result.push(line);
    }
    result
}

fn help(prog: &str, expect: bool) -> CommandOutput {
    let width = width();
    let position = (if expect { 19 } else { 14 }).min(24.min(width.saturating_sub(20).max(4)));
    let mut text = usage(prog, expect) + "\noptions:\n";
    let header = "-h, --help";
    let lines = wrap(
        "show this help message and exit",
        width.saturating_sub(position).max(11),
    );
    if header.len() <= position.saturating_sub(4) {
        text.push_str(&format!(
            "  {header:padding$}  {}\n",
            lines[0],
            padding = position - 4
        ));
    } else {
        text.push_str(&format!(
            "  {header}\n{}{}\n",
            " ".repeat(position),
            lines[0]
        ));
    }
    for line in &lines[1..] {
        text.push_str(&format!("{}{line}\n", " ".repeat(position)));
    }
    text.push_str("  --dir DIR\n");
    if expect {
        text.push_str("  --expect EXPECT\n");
    }
    CommandOutput::stdout(text.into_bytes())
}
fn error(prog: &str, expect: bool, message: &[u8]) -> CommandOutput {
    CommandOutput {
        stdout: Vec::new(),
        stderr: super::bytes::stderr(&concat(&[
            usage(prog, expect).as_bytes(),
            prog.as_bytes(),
            b": error: ",
            message,
            b"\n",
        ])),
        code: ExitCode::USAGE,
    }
}

fn negative_number(bytes: &[u8]) -> bool {
    // Python の正規表現の $ は末尾の LF 1 個の直前にも一致する。
    // 判定だけで外し、parse が保持する引数のバイト列は変更しない。
    let bytes = bytes.strip_suffix(b"\n").unwrap_or(bytes);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    let digit = |c: char| super::unicode::decimal(c as u32).is_some();
    if let Some((left, right)) = rest.split_once('.') {
        left.chars().all(digit) && !right.is_empty() && right.chars().all(digit)
    } else {
        !rest.is_empty() && rest.chars().all(digit)
    }
}

fn classify(value: &[u8], prog: &str, expect: bool) -> Result<Kind, CommandOutput> {
    let options: &[&[u8]] = if expect {
        &[b"-h", b"--help", b"--dir", b"--expect"]
    } else {
        &[b"-h", b"--help", b"--dir"]
    };
    if value.len() < 2 || value[0] != b'-' {
        return Ok(Kind::Value);
    }
    let split = value.iter().position(|&byte| byte == b'=');
    let prefix = split.map_or(value, |index| &value[..index]);
    let explicit = split.map(|index| value[index + 1..].to_vec());
    if let Some(index) = options.iter().position(|&option| option == prefix) {
        return Ok(Kind::Option { index, explicit });
    }
    if value.starts_with(b"--") {
        let matches = options
            .iter()
            .enumerate()
            .filter(|(_, option)| option.starts_with(prefix))
            .collect::<Vec<_>>();
        if matches.len() > 1 {
            let joined = matches
                .iter()
                .map(|(_, option)| **option)
                .collect::<Vec<_>>()
                .join(&b", "[..]);
            return Err(error(
                prog,
                expect,
                &concat(&[b"ambiguous option: ", value, b" could match ", &joined]),
            ));
        }
        if let Some(&(index, _)) = matches.first() {
            return Ok(Kind::Option { index, explicit });
        }
    } else if value.starts_with(b"-h") {
        return Ok(Kind::Option {
            index: 0,
            explicit: Some(value[2..].to_vec()),
        });
    }
    Ok(if negative_number(value) || value.contains(&b' ') {
        Kind::Value
    } else {
        Kind::Unknown
    })
}

pub fn parse(args: &[OsString], prog: &str, expect: bool) -> Result<Arguments, CommandOutput> {
    let options: &[&[u8]] = if expect {
        &[b"-h", b"--help", b"--dir", b"--expect"]
    } else {
        &[b"-h", b"--help", b"--dir"]
    };
    let mut kinds = Vec::new();
    let mut after_separator = false;
    for value in args {
        let value = raw(value);
        kinds.push(if after_separator {
            Kind::Value
        } else if value == b"--" {
            after_separator = true;
            Kind::Separator
        } else {
            classify(value, prog, expect)?
        });
    }
    let (mut root, mut task_dir) = (None, None);
    let mut extra = Vec::new();
    let mut at = 0;
    while at < args.len() {
        match &kinds[at] {
            Kind::Option { index, explicit } => {
                if *index < 2 {
                    if let Some(explicit) = explicit {
                        let tail = if *index == 0 && !explicit.is_empty() {
                            explicit
                                .iter()
                                .position(|&byte| byte != b'h')
                                .map(|position| &explicit[position..])
                        } else {
                            Some(&explicit[..])
                        };
                        if let Some(tail) = tail {
                            return Err(error(
                                prog,
                                expect,
                                &concat(&[
                                    b"argument -h/--help: ignored explicit argument ",
                                    &repr(tail),
                                ]),
                            ));
                        }
                    }
                    return Err(help(prog, expect));
                }
                let value = if let Some(value) = explicit {
                    use std::os::unix::ffi::OsStringExt;
                    OsString::from_vec(value.clone())
                } else {
                    if !matches!(kinds.get(at + 1), Some(Kind::Value)) {
                        return Err(error(
                            prog,
                            expect,
                            &concat(&[b"argument ", options[*index], b": expected one argument"]),
                        ));
                    }
                    at += 1;
                    args[at].clone()
                };
                if *index == 2 {
                    root = Some(value)
                } else {
                    task_dir = Some(value)
                }
            }
            _ => extra.push(raw(&args[at])),
        }
        at += 1;
    }
    let Some(root) = root else {
        return Err(error(
            prog,
            expect,
            b"the following arguments are required: --dir",
        ));
    };
    if !extra.is_empty() {
        return Err(error(
            prog,
            expect,
            &concat(&[b"unrecognized arguments: ", &extra.join(&b" "[..])]),
        ));
    }
    Ok(Arguments { root, task_dir })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_negative_numbers_keep_the_original_os_bytes() {
        for number in ["-1", "-١", "-١.٢", "-.٥", "-१२", "-１２.５"] {
            for suffix in ["", "\n"] {
                let value = format!("{number}{suffix}");
                assert!(negative_number(value.as_bytes()));
                let input = [OsString::from("--dir"), OsString::from(&value)];
                let args = parse(&input, "origin-repo.py", false)
                    .unwrap_or_else(|_| panic!("number must be a value"));
                assert_eq!(raw(&args.root), value.as_bytes());
                let input = [
                    OsString::from("--dir"),
                    OsString::from("."),
                    OsString::from("--expect"),
                    OsString::from(&value),
                ];
                let args = parse(&input, "git-config-digest.py", true)
                    .unwrap_or_else(|_| panic!("number must be a value"));
                assert_eq!(raw(args.task_dir.as_ref().unwrap()), value.as_bytes());
            }
        }
        for value in ["-١x", "-١\r", "-١\n\n", "-١\r\n"] {
            assert!(!negative_number(value.as_bytes()));
        }
        assert!(!negative_number(b"-\xff"));
    }
}
