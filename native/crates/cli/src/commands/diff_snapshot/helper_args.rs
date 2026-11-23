//! The old helper's Python 3.10 argparse step for `--cwd TOP (--ref REF)*`.
//! Only argument errors are reproduced: help cannot be reached from this list.
use super::legacy_text;
use dev_workflow_core::loop_state::CodepointString;

const PROG: &str = "secret-profiles.py";
const OPTIONS: [&str; 4] = ["-h", "--help", "--cwd", "--ref"];
const ACTIONS: [&str; 4] = ["-h/--help", "-h/--help", "--cwd", "--ref"];

/// Unicode 13 decimal digit blocks (category Nd), as used by Python 3.10.
fn decimal(code: u32) -> Option<u8> {
    const STARTS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16b50,
        0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    STARTS.iter().find_map(|&start| {
        (start..start + 10)
            .contains(&code)
            .then(|| (code - start) as u8)
    })
}

/// Python's `str.isspace` for `int()` stripping.
fn python_space(code: u32) -> bool {
    matches!(
        code,
        0x09..=0x0d
            | 0x1c..=0x20
            | 0x85
            | 0xa0
            | 0x1680
            | 0x2000..=0x200a
            | 0x2028
            | 0x2029
            | 0x202f
            | 0x205f
            | 0x3000
    )
}

/// `int(os.environ['COLUMNS'])`, or None when Python raises ValueError.
fn python_int(text: &[u32]) -> Option<i128> {
    let start = text.iter().take_while(|c| python_space(**c)).count();
    let end = text.len() - text.iter().rev().take_while(|c| python_space(**c)).count();
    let text = &text[start..end.max(start)];
    let (negative, digits) = match text.first() {
        Some(0x2b) => (false, &text[1..]),
        Some(0x2d) => (true, &text[1..]),
        _ => (false, text),
    };
    if digits.is_empty() {
        return None;
    }
    let mut total: i128 = 0;
    let mut previous_digit = false;
    for &c in digits {
        if c == 0x5f && previous_digit {
            previous_digit = false;
            continue;
        }
        total = total
            .saturating_mul(10)
            .saturating_add(i128::from(decimal(c)?));
        previous_digit = true;
    }
    previous_digit.then_some(if negative { -total } else { total })
}

/// HelpFormatter width: COLUMNS when positive, else the 80-column fallback
/// (the helper's stdout is a file), minus two.
fn width() -> i128 {
    let columns = std::env::var_os("COLUMNS")
        .map(|v| legacy_text::decode_argument(v.as_encoded_bytes()))
        .and_then(|v| python_int(v.as_codepoints()))
        .filter(|n| *n > 0)
        .unwrap_or(80);
    columns - 2
}

fn get_lines(parts: &[&str], indent: usize, prefix: Option<usize>, width: i128) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line: Vec<&str> = Vec::new();
    let mut length = prefix.unwrap_or(indent) as i128 - 1;
    for &part in parts {
        if length + 1 + part.len() as i128 > width && !line.is_empty() {
            lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
            line.clear();
            length = indent as i128 - 1;
        }
        line.push(part);
        length += part.len() as i128 + 1;
    }
    if !line.is_empty() {
        lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
    }
    if prefix.is_some()
        && let Some(first) = lines.first_mut()
    {
        first.drain(..indent);
    }
    lines
}

fn usage() -> String {
    let parts = ["[-h]", "--cwd", "CWD", "[--ref REF]"];
    let width = width();
    let entire = parts.join(" ");
    let body = if 7 + (PROG.len() + 1 + entire.len()) as i128 <= width {
        format!("{PROG} {entire}")
    } else if ((7 + PROG.len()) as i128) * 4 <= width * 3 {
        get_lines(
            &[&[PROG][..], &parts[..]].concat(),
            7 + PROG.len() + 1,
            Some(7),
            width,
        )
        .join("\n")
    } else {
        let lines = get_lines(&parts, 7, None, width);
        format!("{PROG}\n{}", lines.join("\n"))
    };
    format!("usage: {body}\n")
}

enum Kind {
    Value,
    Known,
    Unknown,
}

fn points(value: &str) -> Vec<u32> {
    value.chars().map(u32::from).collect()
}

fn equals(text: &[u32], value: &str) -> bool {
    text.iter().copied().eq(value.chars().map(u32::from))
}

/// `_parse_optional`, including abbreviations and negative numbers.
fn kind(text: &[u32]) -> Result<Kind, Vec<CodepointString>> {
    let dash = u32::from('-');
    if text.first() != Some(&dash) {
        return Ok(Kind::Value);
    }
    if OPTIONS.iter().any(|o| equals(text, o)) {
        return Ok(Kind::Known);
    }
    if text.len() == 1 {
        return Ok(Kind::Value);
    }
    if let Some(i) = text.iter().position(|c| *c == u32::from('='))
        && OPTIONS.iter().any(|o| equals(&text[..i], o))
    {
        return Ok(Kind::Known);
    }
    let matches: Vec<&str> = if text[1] == dash {
        let prefix = match text.iter().position(|c| *c == u32::from('=')) {
            Some(i) => &text[..i],
            None => text,
        };
        OPTIONS
            .iter()
            .copied()
            .filter(|o| points(o).starts_with(prefix))
            .collect()
    } else {
        let short = &text[..2];
        OPTIONS
            .iter()
            .copied()
            .filter(|o| {
                let option = points(o);
                option == short || option.starts_with(text)
            })
            .collect()
    };
    if matches.len() > 1 {
        return Err(vec![
            CodepointString::from("ambiguous option: "),
            CodepointString::try_from_codepoints(text.to_vec()).expect("decoded code points"),
            CodepointString::from(format!(" could match {}", matches.join(", ")).as_str()),
        ]);
    }
    if matches.len() == 1 {
        return Ok(Kind::Known);
    }
    // '^-\d+$|^-\d*\.\d+$' with Unicode decimal digits.
    let body = &text[1..];
    let digits = |s: &[u32]| !s.is_empty() && s.iter().all(|c| decimal(*c).is_some());
    let negative = digits(body)
        || body
            .iter()
            .position(|c| *c == u32::from('.'))
            .is_some_and(|i| {
                body[..i].iter().all(|c| decimal(*c).is_some()) && digits(&body[i + 1..])
            });
    if negative {
        return Ok(Kind::Value);
    }
    if text.contains(&u32::from(' ')) {
        return Ok(Kind::Value);
    }
    Ok(Kind::Unknown)
}

/// The helper's argparse result: None when parsing succeeds, otherwise the
/// helper's stderr bytes (usage and error), encoded as the helper encodes them.
pub fn check(cwd: &[u8], refs: &[Vec<u8>]) -> Option<Vec<u8>> {
    let mut arguments: Vec<Vec<u8>> = vec![b"--cwd".to_vec(), cwd.to_vec()];
    for r in refs {
        arguments.push(b"--ref".to_vec());
        arguments.push(r.clone());
    }
    let decoded = arguments
        .iter()
        .map(|a| legacy_text::decode_argument(a))
        .collect::<Vec<_>>();
    let failure = |message: Vec<CodepointString>| {
        let mut error = vec![CodepointString::from(format!("{PROG}: error: ").as_str())];
        error.extend(message);
        error.push(CodepointString::from("\n"));
        let joined_error = error
            .iter()
            .flat_map(|p| p.as_codepoints().iter().copied())
            .collect::<Vec<_>>();
        Some(legacy_text::write_text(&[
            CodepointString::from(usage().as_str()),
            CodepointString::try_from_codepoints(joined_error).expect("decoded code points"),
        ]))
    };
    // Pattern: '-' for the first "--", then 'A' for everything after it.
    let mut pattern = Vec::new();
    let mut known = Vec::new();
    let mut separated = false;
    for (index, (raw, text)) in arguments.iter().zip(&decoded).enumerate() {
        if separated {
            pattern.push(b'A');
        } else if raw == b"--" {
            pattern.push(b'-');
            separated = true;
        } else {
            match kind(text.as_codepoints()) {
                Ok(Kind::Value) => pattern.push(b'A'),
                Ok(Kind::Known) => {
                    pattern.push(b'O');
                    known.push(index);
                }
                Ok(Kind::Unknown) => pattern.push(b'O'),
                Err(message) => return failure(message),
            }
        }
    }
    // Consume: only the fixed --cwd/--ref positions can be options here.
    let mut extras: Vec<usize> = Vec::new();
    let mut start = 0;
    let options = pattern
        .iter()
        .enumerate()
        .filter(|(_, p)| **p == b'O')
        .map(|(i, _)| i)
        .collect::<Vec<_>>();
    while let Some(&next) = options.iter().find(|i| **i >= start) {
        extras.extend(start..next);
        if !known.contains(&next) {
            extras.push(next);
            start = next + 1;
            continue;
        }
        // nargs=None for an optional: argparse drops '-' from '(-*A-*)', so the
        // next string must be a plain argument ('--' is not accepted).
        if pattern.get(next + 1) != Some(&b'A') {
            let name = OPTIONS
                .iter()
                .position(|o| equals(decoded[next].as_codepoints(), o))
                .map_or("--ref", |i| ACTIONS[i]);
            return failure(vec![CodepointString::from(
                format!("argument {name}: expected one argument").as_str(),
            )]);
        }
        start = next + 2;
    }
    extras.extend(start..arguments.len());
    if extras.is_empty() {
        return None;
    }
    let mut message = vec![CodepointString::from("unrecognized arguments: ")];
    for (i, index) in extras.iter().enumerate() {
        if i > 0 {
            message.push(CodepointString::from(" "));
        }
        message.push(decoded[*index].clone());
    }
    failure(message)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn option_like_values_follow_argparse() {
        for (value, accepted) in [
            ("abc", true),
            ("-", true),
            ("-1", true),
            ("-1.5", true),
            ("-.5", true),
            ("-\u{661}", true),
            ("- x", true),
            ("-x", false),
            ("-h", false),
            ("--cwd", false),
            ("--re", false),
            ("--x", false),
            ("-hx", false),
        ] {
            let refs = [value.as_bytes().to_vec()];
            assert_eq!(check(b"/repo", &refs).is_none(), accepted, "{value:?}");
        }
    }
    #[test]
    fn integer_columns_follow_python() {
        let points = |s: &str| s.chars().map(u32::from).collect::<Vec<_>>();
        assert_eq!(python_int(&points(" 4_0 ")), Some(40));
        assert_eq!(python_int(&points("\u{661}\u{662}")), Some(12));
        assert_eq!(python_int(&points("_40")), None);
        assert_eq!(python_int(&points("40_")), None);
        assert_eq!(python_int(&points("4__0")), None);
        assert_eq!(python_int(&points("")), None);
    }
}
