//! このコマンドの Python 3.10 argparse の互換処理。
use super::bytes::{concat, raw, repr};
use dev_workflow_core::{CommandOutput, ExitCode};
use std::ffi::OsString;

const PROG: &str = "dev-workflow resolve-task-dir";
const OPTIONS: [&[u8]; 4] = [b"-h", b"--help", b"--project-root", b"--task-dir"];

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
        .unwrap_or(80)
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

fn usage() -> String {
    let parts = [
        "[-h]",
        "--project-root",
        "PROJECT_ROOT",
        "[--task-dir TASK_DIR]",
    ];
    let width = width();
    let entire = format!("{PROG} {}", parts.join(" "));
    let body = if 7 + entire.len() <= width {
        entire
    } else if (7 + PROG.len()) * 4 <= width * 3 {
        lines(
            &[&[PROG][..], &parts[..]].concat(),
            7 + PROG.len() + 1,
            Some(7),
            width,
        )
        .join("\n")
    } else {
        format!("{PROG}\n{}", lines(&parts, 7, None, width).join("\n"))
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

fn help() -> CommandOutput {
    let width = width();
    let position = 29.min(24.min(width.saturating_sub(20).max(4)));
    let mut text = usage() + "\n";
    text.push_str(
        &wrap(
            "Resolve a project's task directory without changing the filesystem.",
            width.max(11),
        )
        .join("\n"),
    );
    text.push_str("\n\noptions:\n");
    let header = "-h, --help";
    let help = wrap(
        "show this help message and exit",
        width.saturating_sub(position).max(11),
    );
    if header.len() <= position.saturating_sub(4) {
        text.push_str(&format!(
            "  {header:padding$}  {}\n",
            help[0],
            padding = position - 4
        ));
    } else {
        text.push_str(&format!(
            "  {header}\n{}{}\n",
            " ".repeat(position),
            help[0]
        ));
    }
    for line in &help[1..] {
        text.push_str(&format!("{}{line}\n", " ".repeat(position)));
    }
    text.push_str("  --project-root PROJECT_ROOT\n  --task-dir TASK_DIR\n");
    CommandOutput::stdout(text.into_bytes())
}

fn error(message: &[u8]) -> CommandOutput {
    CommandOutput {
        stdout: Vec::new(),
        stderr: super::bytes::stderr(&concat(&[
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

fn classify(value: &[u8]) -> Result<Kind, CommandOutput> {
    if value.len() < 2 || value[0] != b'-' {
        return Ok(Kind::Value);
    }
    let split = value.iter().position(|&byte| byte == b'=');
    let prefix = split.map_or(value, |index| &value[..index]);
    let explicit = split.map(|index| value[index + 1..].to_vec());
    if let Some(index) = OPTIONS.iter().position(|&option| option == prefix) {
        return Ok(Kind::Option { index, explicit });
    }
    if value.starts_with(b"--") {
        let matches = OPTIONS
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
            return Err(error(&concat(&[
                b"ambiguous option: ",
                value,
                b" could match ",
                &joined,
            ])));
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

pub fn parse(args: &[OsString]) -> Result<Arguments, CommandOutput> {
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
            classify(value)?
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
                            return Err(error(&concat(&[
                                b"argument -h/--help: ignored explicit argument ",
                                &repr(tail),
                            ])));
                        }
                    }
                    return Err(help());
                }
                let value = if let Some(value) = explicit {
                    use std::os::unix::ffi::OsStringExt;
                    OsString::from_vec(value.clone())
                } else {
                    if !matches!(kinds.get(at + 1), Some(Kind::Value)) {
                        return Err(error(&concat(&[
                            b"argument ",
                            OPTIONS[*index],
                            b": expected one argument",
                        ])));
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
            b"the following arguments are required: --project-root",
        ));
    };
    if !extra.is_empty() {
        return Err(error(&concat(&[
            b"unrecognized arguments: ",
            &extra.join(&b" "[..]),
        ])));
    }
    Ok(Arguments { root, task_dir })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn negative_number_final_lf_is_classified_without_changing_the_value() {
        for number in ["-1", "-1.25", "-.5", "-١٢", "-١.٢٥", "-.٥"] {
            let value = format!("{number}\n");
            assert!(negative_number(value.as_bytes()));
            let arguments = ["--project-root", ".", "--task-dir", &value].map(OsString::from);
            let parsed = parse(&arguments).unwrap();
            assert_eq!(
                parsed.task_dir.as_deref(),
                Some(std::ffi::OsStr::new(&value))
            );
            for suffix in ["\r", "\n\n", "\r\n"] {
                let value = format!("{number}{suffix}");
                assert!(!negative_number(value.as_bytes()));
                let arguments = ["--project-root", ".", "--task-dir", &value].map(OsString::from);
                assert!(parse(&arguments).is_err());
            }
        }
    }
}
