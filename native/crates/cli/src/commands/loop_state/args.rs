use super::{
    budget::Limits,
    json::{PyInteger, decimal},
    runtime,
};
use std::{
    collections::BTreeMap,
    ffi::{OsStr, OsString},
    os::unix::ffi::{OsStrExt, OsStringExt},
};
pub const COMMANDS: &[&str] = &[
    "snapshot",
    "stop-control",
    "preflight",
    "derive-removed-worktree",
    "state-digest",
    "state-digests",
    "compare",
];
pub struct Arguments {
    pub command: String,
    pub values: BTreeMap<String, Vec<OsString>>,
    pub limits: Limits,
    pub print_sites: std::cell::RefCell<Vec<(usize, u32, &'static str)>>,
}
impl Arguments {
    pub fn raw(&self, key: &str) -> &OsStr {
        self.values
            .get(key)
            .and_then(|v| v.last())
            .map(OsString::as_os_str)
            .unwrap_or(OsStr::new(if key == "wt-admin" { "-" } else { "" }))
    }
    pub fn many(&self, key: &str) -> &[OsString] {
        self.values.get(key).map(Vec::as_slice).unwrap_or(&[])
    }
    pub fn flag(&self, key: &str) -> bool {
        self.values.contains_key(key)
    }
}
#[derive(Clone)]
pub struct ParseOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub code: u8,
}
fn options(command: &str) -> Vec<(&'static str, bool, bool)> {
    let mut list = match command {
        "snapshot" => vec![
            ("out", true, false),
            ("top", true, false),
            ("common", true, false),
            ("repo-admin", true, false),
            ("wt-admin", false, false),
            ("exclude-worktree", false, false),
            ("secret-patterns-from", false, false),
            ("fresh-config-baseline", false, false),
            ("stop-control-worktree", false, false),
            ("stop-control-rel", false, false),
            ("stop-control-new-dir", false, true),
            ("removed-parent-worktree", false, true),
        ],
        "stop-control" | "state-digest" | "state-digests" => {
            vec![("state", true, command == "state-digests")]
        }
        "preflight" => vec![
            ("state", true, false),
            ("top", true, false),
            ("common", true, false),
            ("repo-admin", true, false),
        ],
        "derive-removed-worktree" => vec![
            ("state", true, false),
            ("out", true, false),
            ("removed-worktree", true, false),
            ("own-worktree", true, false),
        ],
        "compare" => vec![
            ("before", true, false),
            ("after", true, false),
            ("self-worktree", false, false),
            ("self-ref", false, false),
        ],
        _ => vec![],
    };
    if !command.is_empty() {
        list.extend([
            ("max-items", false, false),
            ("max-bytes", false, false),
            ("max-file-bytes", false, false),
            ("max-seconds", false, false),
        ]);
    }
    list
}
thread_local! {static DISPLAY_WIDTH:std::cell::Cell<usize>=const{std::cell::Cell::new(78)};}
fn width() -> usize {
    DISPLAY_WIDTH.with(std::cell::Cell::get)
}
fn set_width(int_limit: usize) {
    let width = std::env::var("COLUMNS")
        .ok()
        .and_then(|s| PyInteger::parse(&s, int_limit).ok())
        .filter(PyInteger::positive)
        .map(|v| v.to_usize().unwrap_or(usize::MAX / 4))
        .unwrap_or_else(dev_workflow_platform::task_digest::stdout_columns)
        .min(usize::MAX / 4)
        .saturating_sub(2);
    DISPLAY_WIDTH.with(|v| v.set(width));
}
fn wrap_parts(parts: &[String], indent: usize, prefix: Option<usize>, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line = Vec::new();
    let mut length = prefix.unwrap_or(indent) as i128 - 1;
    for part in parts {
        if length + 1 + part.len() as i128 > width as i128 && !line.is_empty() {
            lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
            line.clear();
            length = indent as i128 - 1;
        }
        line.push(part.clone());
        length += 1 + part.len() as i128;
    }
    if !line.is_empty() {
        lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
    }
    if prefix.is_some() && !lines.is_empty() {
        lines[0].drain(..indent);
    }
    lines
}
fn usage(command: &str) -> String {
    let prog = if command.is_empty() {
        "loop-state.py".into()
    } else {
        format!("loop-state.py {command}")
    };
    let mut optionals = vec!["[-h]".into()];
    let mut positionals = Vec::new();
    if command.is_empty() {
        positionals.push(format!("{{{}}}", COMMANDS.join(",")));
        positionals.push("...".into());
    } else {
        for (name, required, _) in options(command) {
            let option = if name == "fresh-config-baseline" {
                format!("--{name}")
            } else {
                format!("--{name} {}", name.replace('-', "_").to_uppercase())
            };
            if required {
                optionals.extend(option.split(' ').map(str::to_owned));
            } else {
                optionals.push(format!("[{option}]"));
            }
        }
    }
    let width = width();
    let all = [optionals.clone(), positionals.clone()].concat();
    let whole = format!("{prog} {}", all.join(" "));
    let usage = if 7 + whole.len() <= width {
        whole
    } else if (7 + prog.len()) * 4 <= width * 3 {
        let indent = 7 + prog.len() + 1;
        let mut lines = wrap_parts(&[vec![prog], optionals].concat(), indent, Some(7), width);
        lines.extend(wrap_parts(&positionals, indent, None, width));
        lines.join("\n")
    } else {
        let mut lines = vec![prog];
        lines.extend(wrap_parts(&optionals, 7, None, width));
        lines.extend(wrap_parts(&positionals, 7, None, width));
        lines.join("\n")
    };
    format!("usage: {usage}\n")
}
fn output(command: &str, reason: String) -> ParseOutput {
    ParseOutput {
        stdout: Vec::new(),
        stderr: format!(
            "{}loop-state.py{}: error: {reason}\n",
            usage(command),
            if command.is_empty() {
                String::new()
            } else {
                format!(" {command}")
            }
        )
        .into_bytes(),
        code: 2,
    }
}
fn help(command: &str) -> ParseOutput {
    let mut out = usage(command);
    if command.is_empty() {
        out.push_str(&format!(
            "\npositional arguments:\n  {{{}}}\n\n",
            COMMANDS.join(",")
        ));
    } else {
        out.push('\n');
    }
    out.push_str("options:\n");
    let position = 24.min(width().saturating_sub(20).max(4));
    let lines = wrap_parts(
        &"show this help message and exit"
            .split_whitespace()
            .map(str::to_owned)
            .collect::<Vec<_>>(),
        0,
        None,
        width().saturating_sub(position).max(11),
    );
    if 10 <= position.saturating_sub(4) {
        out.push_str(&format!(
            "  -h, --help{}{}\n",
            " ".repeat(position - 12),
            lines[0]
        ));
    } else {
        out.push_str(&format!(
            "  -h, --help\n{}{}\n",
            " ".repeat(position),
            lines[0]
        ));
    }
    for line in &lines[1..] {
        out.push_str(&format!("{}{line}\n", " ".repeat(position)));
    }
    for (name, _, _) in options(command) {
        out.push_str(&format!(
            "  --{name}{}\n",
            if name == "fresh-config-baseline" {
                String::new()
            } else {
                format!(" {}", name.replace('-', "_").to_uppercase())
            }
        ));
    }
    ParseOutput {
        stdout: out.into_bytes(),
        stderr: Vec::new(),
        code: 0,
    }
}
fn negative(raw: &[u8]) -> bool {
    let Ok(s) = std::str::from_utf8(raw.strip_suffix(b"\n").unwrap_or(raw)) else {
        return false;
    };
    let Some(s) = s.strip_prefix('-') else {
        return false;
    };
    if let Some((a, b)) = s.split_once('.') {
        a.chars().all(|c| decimal(c as u32).is_some())
            && !b.is_empty()
            && b.chars().all(|c| decimal(c as u32).is_some())
    } else {
        !s.is_empty() && s.chars().all(|c| decimal(c as u32).is_some())
    }
}
pub fn repr(raw: &[u8]) -> String {
    let s = runtime::decode(raw);
    let quote = if raw.contains(&b'\'') && !raw.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = quote.to_string();
    for &c in s.as_codepoints() {
        match c {
            10 => out.push_str("\\n"),
            13 => out.push_str("\\r"),
            9 => out.push_str("\\t"),
            92 => out.push_str("\\\\"),
            _ if c == quote as u32 => {
                out.push('\\');
                out.push(quote)
            }
            _ if (0xd800..=0xdfff).contains(&c) => out.push_str(&format!("\\u{c:04x}")),
            _ if !super::json::printable(c) => out.push_str(&if c <= 255 {
                format!("\\x{c:02x}")
            } else if c <= 65535 {
                format!("\\u{c:04x}")
            } else {
                format!("\\U{c:08x}")
            }),
            _ => out.push(char::from_u32(c).unwrap()),
        }
    }
    out.push(quote);
    out
}
pub fn parse(raw: &[OsString], int_limit: usize) -> std::result::Result<Arguments, ParseOutput> {
    set_width(int_limit);
    let mut root_extras = Vec::new();
    let mut command_index = 0;
    let root_end = false;
    while command_index < raw.len() {
        let v = raw[command_index].as_bytes();
        if !root_end && v == b"--" {
            if command_index + 1 == raw.len() {
                command_index += 1;
            }
            break;
        }
        if !root_end {
            if let Some(result) = help_option(v, "") {
                return Err(result);
            }
            if v.starts_with(b"-") && v != b"-" && !negative(v) {
                root_extras.push(raw[command_index].clone());
                command_index += 1;
                continue;
            }
        }
        break;
    }
    let Some(command) = raw
        .get(command_index)
        .and_then(|s| s.to_str())
        .filter(|s| COMMANDS.contains(s))
    else {
        return Err(output(
            "",
            if let Some(v) = raw.get(command_index) {
                format!(
                    "argument command: invalid choice: {} (choose from {})",
                    repr(v.as_bytes()),
                    COMMANDS
                        .iter()
                        .map(|v| format!("'{v}'"))
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            } else {
                "the following arguments are required: command".into()
            },
        ));
    };
    let options = options(command);
    for value in raw
        .iter()
        .skip(command_index + 1)
        .take_while(|v| v.as_bytes() != b"--")
    {
        let name = value.as_bytes().split(|c| *c == b'=').next().unwrap();
        if name.starts_with(b"--")
            && !options
                .iter()
                .any(|(n, _, _)| format!("--{n}").as_bytes() == name)
        {
            let matches = options
                .iter()
                .filter(|(n, _, _)| format!("--{n}").as_bytes().starts_with(name))
                .map(|(n, _, _)| format!("--{n}"))
                .chain((b"--help".starts_with(name)).then_some("--help".into()))
                .collect::<Vec<_>>();
            if matches.len() > 1 {
                return Err(output(
                    command,
                    format!(
                        "ambiguous option: {} could match {}",
                        display(value.as_bytes()),
                        matches.join(", ")
                    ),
                ));
            }
        }
    }
    let mut values: BTreeMap<String, Vec<OsString>> = BTreeMap::new();
    let mut limits = Limits::default();
    let mut extras = root_extras;
    let mut index = command_index + 1;
    let mut end = false;
    while index < raw.len() {
        let value = raw[index].as_bytes();
        if value == b"--" {
            end = true;
            extras.push(raw[index].clone());
            index += 1;
            continue;
        }
        if end {
            extras.push(raw[index].clone());
            index += 1;
            continue;
        }
        let split = value.iter().position(|&c| c == b'=');
        let name = split.map_or(value, |i| &value[..i]);
        let explicit = split.map(|i| &value[i + 1..]);
        if let Some(result) = help_option(value, command) {
            return Err(result);
        }
        let matches = options
            .iter()
            .filter(|(n, _, _)| {
                name.starts_with(b"--") && format!("--{n}").as_bytes().starts_with(name)
            })
            .collect::<Vec<_>>();
        let found = options
            .iter()
            .find(|(n, _, _)| name == format!("--{n}").as_bytes());
        let found = if found.is_some() {
            found
        } else {
            if matches.len() > 1 {
                return Err(output(
                    command,
                    format!(
                        "ambiguous option: {} could match {}",
                        display(value),
                        matches
                            .iter()
                            .map(|(n, _, _)| format!("--{n}"))
                            .collect::<Vec<_>>()
                            .join(", ")
                    ),
                ));
            }
            matches.first().copied()
        };
        let Some(&(name, _, append)) = found else {
            extras.push(raw[index].clone());
            index += 1;
            continue;
        };
        let option = if name == "fresh-config-baseline" {
            if let Some(e) = explicit {
                return Err(output(
                    command,
                    format!("argument --{name}: ignored explicit argument {}", repr(e)),
                ));
            }
            OsString::new()
        } else if let Some(e) = explicit {
            OsString::from_vec(e.to_vec())
        } else {
            if raw.get(index + 1).is_none_or(|s| {
                s.as_bytes().starts_with(b"-")
                    && s.as_bytes() != b"-"
                    && !negative(s.as_bytes())
                    && !s.as_bytes().contains(&b' ')
            }) {
                return Err(output(
                    command,
                    format!("argument --{name}: expected one argument"),
                ));
            }
            index += 1;
            raw[index].clone()
        };
        if name.starts_with("max-") {
            let parsed = std::str::from_utf8(option.as_bytes())
                .ok()
                .and_then(|s| PyInteger::parse(s, int_limit).ok())
                .ok_or_else(|| {
                    output(
                        command,
                        format!(
                            "argument --{name}: invalid int value: {}",
                            repr(option.as_bytes())
                        ),
                    )
                })?;
            match name {
                "max-items" => limits.items = parsed,
                "max-bytes" => limits.total = parsed,
                "max-file-bytes" => limits.one = parsed,
                "max-seconds" => limits.seconds = parsed,
                _ => unreachable!(),
            }
        }
        if append {
            values.entry(name.into()).or_default().push(option)
        } else {
            values.insert(name.into(), vec![option]);
        }
        index += 1;
    }
    let missing = options
        .iter()
        .filter(|(name, required, _)| *required && !values.contains_key(*name))
        .map(|(n, _, _)| format!("--{n}"))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(output(
            command,
            format!(
                "the following arguments are required: {}",
                missing.join(", ")
            ),
        ));
    }
    if !extras.is_empty() {
        return Err(output(
            "",
            format!(
                "unrecognized arguments: {}",
                extras
                    .iter()
                    .map(|s| display(s.as_bytes()))
                    .collect::<Vec<_>>()
                    .join(" ")
            ),
        ));
    }
    Ok(Arguments {
        command: command.into(),
        values,
        limits,
        print_sites: std::cell::RefCell::new(Vec::new()),
    })
}

fn display(raw: &[u8]) -> String {
    let text = runtime::decode(raw);
    String::from_utf8(runtime::encode(&text, "utf-8", "backslashreplace").unwrap()).unwrap()
}
fn help_option(raw: &[u8], command: &str) -> Option<ParseOutput> {
    let (name, explicit) = if let Some(i) = raw.iter().position(|c| *c == b'=') {
        (&raw[..i], Some(&raw[i + 1..]))
    } else {
        (raw, None)
    };
    let short = name.starts_with(b"-h") && !name.starts_with(b"--");
    if !(short || (name.starts_with(b"--") && name.len() > 2 && b"--help".starts_with(name))) {
        return None;
    }
    let short_tail = if short {
        name[2..]
            .iter()
            .position(|c| *c != b'h')
            .map(|i| &name[2 + i..])
    } else {
        None
    };
    if let Some(value) = short_tail.or(explicit) {
        Some(output(
            command,
            format!(
                "argument -h/--help: ignored explicit argument {}",
                repr(value)
            ),
        ))
    } else {
        Some(help(command))
    }
}
