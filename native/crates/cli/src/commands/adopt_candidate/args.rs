//! `adopt-candidate.py` の Python 3.10 argparse 相当。解析の順序・usage・help・端末幅を旧版に合わせる。
//! 引数は生バイトのまま保つ(surrogateescape)。stderr は UTF-8 + backslashreplace に固定する。
use super::bytes::{concat, raw, repr, stderr};
use super::unicode;
use dev_workflow_core::{CommandOutput, ExitCode};
use std::collections::BTreeMap;
use std::ffi::OsString;

/// argparse の prog(旧スクリプトのファイル名)。
pub(crate) const PROG: &str = "adopt-candidate.py";
/// 旧版の `__doc__`。help の description になる。
const DESCRIPTION: &str = "候補の確認と採用改名を一つの入口で行う。本文の設計は呼出側が行う。";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Help,
    Root,
    ExpectSha256,
    Name,
    Accept,
    Unattended,
}

/// `_option_string_actions` の登録順。ambiguous の一覧と短縮の判定がこの順に従う。
const OPTION_STRINGS: [(&[u8], Action); 7] = [
    (b"-h", Action::Help),
    (b"--help", Action::Help),
    (b"--root", Action::Root),
    (b"--expect-sha256", Action::ExpectSha256),
    (b"--name", Action::Name),
    (b"--accept", Action::Accept),
    (b"--unattended", Action::Unattended),
];

impl Action {
    fn takes_value(self) -> bool {
        matches!(
            self,
            Action::Root | Action::ExpectSha256 | Action::Name | Action::Accept
        )
    }

    /// `ArgumentError` の `argument_name`。
    fn display(self) -> &'static str {
        match self {
            Action::Help => "-h/--help",
            Action::Root => "--root",
            Action::ExpectSha256 => "--expect-sha256",
            Action::Name => "--name",
            Action::Accept => "--accept",
            Action::Unattended => "--unattended",
        }
    }
}

/// 解析後の値。`--root=--` のように明示値が `--` だけのとき、旧版の値は空のリストになる。
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ArgValue {
    Text(Vec<u8>),
    EmptyList,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Arguments {
    pub(crate) root: ArgValue,
    pub(crate) expect_sha256: ArgValue,
    pub(crate) name: Option<ArgValue>,
    pub(crate) accept: Option<ArgValue>,
    pub(crate) unattended: bool,
    pub(crate) candidate: ArgValue,
}

// ---- 端末幅 ----

/// CPython 3.10 の起動時に有効な整数変換の上限だけを読む(task-digest と同じ範囲)。
fn int_max_str_digits(value: Option<&str>) -> usize {
    let configured = || {
        let value = value?;
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

/// Python の `int()` が読む空白(`str.isspace` と同じ集合)。
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `shutil.get_terminal_size()` の COLUMNS の読み方。0 以下・不正は None(端末の幅へ進む)。
fn configured_columns(columns: Option<&str>, max_digits: usize) -> Option<usize> {
    let value = columns?.trim_matches(py_space);
    let value = value.strip_prefix('+').unwrap_or(value);
    let mut digits = 0usize;
    let mut total = 0usize;
    let mut previous = false;
    for c in value.chars() {
        if c == '_' && previous {
            previous = false;
            continue;
        }
        let digit = unicode::decimal(c as u32)?;
        digits += 1;
        if max_digits != 0 && digits > max_digits {
            return None;
        }
        total = total.saturating_mul(10).saturating_add(usize::from(digit));
        previous = true;
    }
    (previous && total > 0).then_some(total)
}

/// argparse が使う幅(端末の桁数 - 2)。環境変数と端末の取得は引数で渡す。
pub(crate) fn layout_width(
    columns_env: Option<&str>,
    int_max_digits_env: Option<&str>,
    terminal: impl FnOnce() -> usize,
) -> i64 {
    let columns = configured_columns(columns_env, int_max_str_digits(int_max_digits_env))
        .unwrap_or_else(terminal);
    i64::try_from(columns).unwrap_or(i64::MAX) - 2
}

// ---- usage と help ----

fn char_len(text: &str) -> i64 {
    i64::try_from(text.chars().count()).unwrap_or(i64::MAX)
}

fn get_lines(parts: &[&str], indent: &str, prefix: Option<&str>, text_width: i64) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line: Vec<&str> = Vec::new();
    let mut line_len = prefix.map_or(char_len(indent), char_len) - 1;
    for part in parts {
        if line_len + 1 + char_len(part) > text_width && !line.is_empty() {
            lines.push(format!("{indent}{}", line.join(" ")));
            line.clear();
            line_len = char_len(indent) - 1;
        }
        line.push(part);
        line_len += char_len(part) + 1;
    }
    if !line.is_empty() {
        lines.push(format!("{indent}{}", line.join(" ")));
    }
    if prefix.is_some() && !lines.is_empty() {
        let first = &mut lines[0];
        *first = first[indent.len()..].to_owned();
    }
    lines
}

fn usage(width: i64) -> String {
    const PREFIX: &str = "usage: ";
    let opt_parts = [
        "[-h]",
        "--root",
        "ROOT",
        "--expect-sha256",
        "EXPECT_SHA256",
        "[--name NAME]",
        "[--accept ACCEPT]",
        "[--unattended]",
    ];
    let pos_parts = ["candidate"];
    let all = [&opt_parts[..], &pos_parts[..]].concat().join(" ");
    let text = format!("{PROG} {all}");
    let text_width = width.max(11);
    let body = if char_len(PREFIX) + char_len(&text) > text_width {
        let prog_len = char_len(PROG);
        let prefix_len = char_len(PREFIX);
        let lines = if (prefix_len + prog_len) * 4 <= text_width * 3 {
            let indent = " ".repeat((prefix_len + prog_len + 1) as usize);
            let with_prog = [&[PROG][..], &opt_parts[..]].concat();
            let mut lines = get_lines(&with_prog, &indent, Some(PREFIX), text_width);
            lines.extend(get_lines(&pos_parts, &indent, None, text_width));
            lines
        } else {
            let indent = " ".repeat(PREFIX.len());
            let parts = [&opt_parts[..], &pos_parts[..]].concat();
            let mut lines = get_lines(&parts, &indent, None, text_width);
            if lines.len() > 1 {
                lines = get_lines(&opt_parts, &indent, None, text_width);
                lines.extend(get_lines(&pos_parts, &indent, None, text_width));
            }
            [vec![PROG.to_owned()], lines].concat()
        };
        lines.join("\n")
    } else {
        text
    };
    format!("{PREFIX}{body}\n")
}

/// `textwrap.wrap` のうち、空白で分けた語を貪欲に詰める部分(長い語は幅で切る)。
fn wrap_words(text: &str, width: i64) -> Vec<String> {
    let width = width.max(1) as usize;
    let mut lines: Vec<String> = Vec::new();
    let mut line = String::new();
    let mut length = 0usize;
    for word in text.split_whitespace() {
        let mut chars: Vec<char> = word.chars().collect();
        if length > 0 && length + 1 + chars.len() > width {
            lines.push(std::mem::take(&mut line));
            length = 0;
        }
        if length > 0 {
            line.push(' ');
            length += 1;
        }
        while chars.len() > width - length {
            let take = width - length;
            if take == 0 {
                lines.push(std::mem::take(&mut line));
                length = 0;
                continue;
            }
            line.extend(chars.drain(..take));
            lines.push(std::mem::take(&mut line));
            length = 0;
        }
        length += chars.len();
        line.extend(chars);
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

/// 内容を持つ help 全文。旧版の `format_help()` の戻り値と同じ。
#[cfg(test)]
pub(crate) fn help_text(width: i64) -> String {
    help_text_with(width, true)
}

/// `description` が偽なら、旧版の `__doc__` が無い場合(`PYTHONOPTIMIZE=2` 以上)の help になる。
pub(crate) fn help_text_with(width: i64, description: bool) -> String {
    let text_width = width.max(11);
    let max_help_position = 24.min((width - 20).max(4));
    // 最長の呼出し表記 `--expect-sha256 EXPECT_SHA256`(29)+ 字下げ 2。
    let help_position = (31 + 2).min(max_help_position);
    let help_width = (width - help_position).max(11);
    let action_width = help_position - 4;
    let pad = |n: i64| " ".repeat(n.max(0) as usize);

    let mut text = usage(width);
    text.push('\n');
    if description {
        text.push_str(&wrap_words(DESCRIPTION, text_width).join("\n"));
        text.push_str("\n\n");
    }
    text.push_str("positional arguments:\n  candidate\n\noptions:\n");
    let help_lines = wrap_words("show this help message and exit", help_width);
    let header = "-h, --help";
    if char_len(header) <= action_width {
        text.push_str(&format!(
            "  {header}{}  {}\n",
            pad(action_width - char_len(header)),
            help_lines[0]
        ));
    } else {
        text.push_str(&format!(
            "  {header}\n{}{}\n",
            pad(help_position),
            help_lines[0]
        ));
    }
    for line in &help_lines[1..] {
        text.push_str(&format!("{}{line}\n", pad(help_position)));
    }
    text.push_str("  --root ROOT\n  --expect-sha256 EXPECT_SHA256\n  --name NAME\n");
    text.push_str("  --accept ACCEPT\n  --unattended\n");
    text
}

fn help(width: i64, description: bool) -> CommandOutput {
    CommandOutput::stdout(help_text_with(width, description).into_bytes())
}

/// `parser.error(message)`: usage に続けて `prog: error: メッセージ`。終了 2。
fn error(width: i64, message: &[u8]) -> CommandOutput {
    CommandOutput {
        stdout: Vec::new(),
        stderr: stderr(&concat(&[
            usage(width).as_bytes(),
            PROG.as_bytes(),
            b": error: ",
            message,
            b"\n",
        ])),
        code: ExitCode::USAGE,
    }
}

fn argument_error(width: i64, action: Action, message: &[u8]) -> CommandOutput {
    error(
        width,
        &concat(&[b"argument ", action.display().as_bytes(), b": ", message]),
    )
}

// ---- 解析 ----

fn negative_number(bytes: &[u8]) -> bool {
    // Python の正規表現の `$` は末尾の LF 1 個の直前にも一致する。判定だけで外す。
    let bytes = bytes.strip_suffix(b"\n").unwrap_or(bytes);
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    let Some(rest) = text.strip_prefix('-') else {
        return false;
    };
    let digit = |c: char| unicode::decimal(c as u32).is_some();
    if let Some((left, right)) = rest.split_once('.') {
        left.chars().all(digit) && !right.is_empty() && right.chars().all(digit)
    } else {
        !rest.is_empty() && rest.chars().all(digit)
    }
}

/// `_parse_optional` の結果。`action` が None なら未知のオプション。
#[derive(Clone)]
struct OptionTuple {
    action: Option<Action>,
    option_string: Vec<u8>,
    explicit: Option<Vec<u8>>,
}

fn exact(option: &[u8]) -> Option<Action> {
    OPTION_STRINGS
        .iter()
        .find(|(name, _)| *name == option)
        .map(|(_, action)| *action)
}

fn option_tuples(arg: &[u8]) -> Vec<OptionTuple> {
    let mut result = Vec::new();
    if arg.len() >= 2 && arg[1] == b'-' {
        let (prefix, explicit) = match arg.iter().position(|&byte| byte == b'=') {
            Some(at) => (&arg[..at], Some(arg[at + 1..].to_vec())),
            None => (arg, None),
        };
        for (name, action) in OPTION_STRINGS {
            if name.starts_with(prefix) {
                result.push(OptionTuple {
                    action: Some(action),
                    option_string: name.to_vec(),
                    explicit: explicit.clone(),
                });
            }
        }
    } else if arg.len() >= 2 {
        // 先頭 2 文字(`-` と 1 文字)が `-h` のときだけ、短い指定として一致する。
        for (name, action) in OPTION_STRINGS {
            if arg.starts_with(b"-h") && name == b"-h" {
                result.push(OptionTuple {
                    action: Some(action),
                    option_string: name.to_vec(),
                    explicit: Some(arg[2..].to_vec()),
                });
            } else if name.starts_with(arg) {
                result.push(OptionTuple {
                    action: Some(action),
                    option_string: name.to_vec(),
                    explicit: None,
                });
            }
        }
    }
    result
}

fn parse_optional(arg: &[u8], width: i64) -> Result<Option<OptionTuple>, CommandOutput> {
    if arg.is_empty() || arg[0] != b'-' {
        return Ok(None);
    }
    if let Some(action) = exact(arg) {
        return Ok(Some(OptionTuple {
            action: Some(action),
            option_string: arg.to_vec(),
            explicit: None,
        }));
    }
    if arg.len() == 1 {
        return Ok(None);
    }
    if let Some(at) = arg.iter().position(|&byte| byte == b'=')
        && let Some(action) = exact(&arg[..at])
    {
        return Ok(Some(OptionTuple {
            action: Some(action),
            option_string: arg[..at].to_vec(),
            explicit: Some(arg[at + 1..].to_vec()),
        }));
    }
    let mut tuples = option_tuples(arg);
    if tuples.len() > 1 {
        let joined = tuples
            .iter()
            .map(|tuple| tuple.option_string.as_slice())
            .collect::<Vec<_>>()
            .join(&b", "[..]);
        return Err(error(
            width,
            &concat(&[b"ambiguous option: ", arg, b" could match ", &joined]),
        ));
    }
    if let Some(tuple) = tuples.pop() {
        return Ok(Some(tuple));
    }
    if negative_number(arg) || arg.contains(&b' ') {
        return Ok(None);
    }
    Ok(Some(OptionTuple {
        action: None,
        option_string: arg.to_vec(),
        explicit: None,
    }))
}

struct Parser<'a> {
    args: Vec<&'a [u8]>,
    pattern: Vec<u8>,
    options: BTreeMap<usize, OptionTuple>,
    width: i64,
    description: bool,
    extras: Vec<&'a [u8]>,
    root: Option<ArgValue>,
    expect_sha256: Option<ArgValue>,
    name: Option<ArgValue>,
    accept: Option<ArgValue>,
    unattended: bool,
    candidate: Option<ArgValue>,
}

impl<'a> Parser<'a> {
    /// `_get_values`: 先頭の `--` を 1 個だけ除き、1 個残れば文字列、0 個なら空のリスト。
    fn values(strings: &[&[u8]]) -> ArgValue {
        let mut strings = strings.to_vec();
        if let Some(at) = strings.iter().position(|value| *value == b"--") {
            strings.remove(at);
        }
        match strings.as_slice() {
            [one] => ArgValue::Text(one.to_vec()),
            _ => ArgValue::EmptyList,
        }
    }

    fn store(&mut self, action: Action, value: ArgValue) {
        match action {
            Action::Root => self.root = Some(value),
            Action::ExpectSha256 => self.expect_sha256 = Some(value),
            Action::Name => self.name = Some(value),
            Action::Accept => self.accept = Some(value),
            Action::Help | Action::Unattended => {}
        }
    }

    fn consume_positionals(&mut self, start: usize) -> usize {
        if self.candidate.is_some() {
            return start;
        }
        let mut end = start;
        while self.pattern.get(end) == Some(&b'-') {
            end += 1;
        }
        if self.pattern.get(end) != Some(&b'A') {
            return start;
        }
        end += 1;
        while self.pattern.get(end) == Some(&b'-') {
            end += 1;
        }
        self.candidate = Some(Self::values(&self.args[start..end]));
        end
    }

    fn consume_optional(&mut self, start: usize) -> Result<usize, CommandOutput> {
        let OptionTuple {
            mut action,
            mut option_string,
            mut explicit,
        } = self.options[&start].clone();
        // (動作, 引数) の順。help は取った時点で終了するので、最初の help だけ意味を持つ。
        let mut taken: Vec<(Action, Vec<&'a [u8]>)> = Vec::new();
        let stop;
        loop {
            let Some(current) = action else {
                self.extras.push(self.args[start]);
                return Ok(start + 1);
            };
            if let Some(value) = explicit.clone() {
                let count = usize::from(current.takes_value());
                if count == 0 && option_string[1] != b'-' && !value.is_empty() {
                    taken.push((current, Vec::new()));
                    if value[0] == b'h' {
                        action = Some(Action::Help);
                        option_string = b"-h".to_vec();
                        explicit = (value.len() > 1).then(|| value[1..].to_vec());
                    } else {
                        return Err(argument_error(
                            self.width,
                            current,
                            &concat(&[b"ignored explicit argument ", &repr(&value)]),
                        ));
                    }
                } else if count == 1 {
                    stop = start + 1;
                    self.store(current, Self::values(&[value.as_slice()]));
                    // 明示値の取り込みは動作の実行順に影響しないので、すぐ保存してよい。
                    break;
                } else {
                    return Err(argument_error(
                        self.width,
                        current,
                        &concat(&[b"ignored explicit argument ", &repr(&value)]),
                    ));
                }
            } else {
                let from = start + 1;
                let count = if current.takes_value() {
                    if self.pattern.get(from) == Some(&b'A') {
                        1
                    } else {
                        return Err(argument_error(
                            self.width,
                            current,
                            b"expected one argument",
                        ));
                    }
                } else {
                    0
                };
                stop = from + count;
                taken.push((current, self.args[from..stop].to_vec()));
                break;
            }
        }
        for (action, strings) in taken {
            match action {
                Action::Help => return Err(help(self.width, self.description)),
                Action::Unattended => self.unattended = true,
                _ => {
                    let value = Self::values(&strings);
                    self.store(action, value);
                }
            }
        }
        Ok(stop)
    }
}

/// 旧 `parser.parse_args()` と同じ結果・同じ失敗の出力にする。
/// 失敗と help は `Err` で、標準出力・標準エラー・終了値を持つ。
#[cfg(test)]
pub(crate) fn parse(args: &[OsString], width: i64) -> Result<Arguments, CommandOutput> {
    parse_with(args, width, true)
}

/// `description` は help に説明の段落を出すか(旧版の `__doc__` の有無)。
pub(crate) fn parse_with(
    args: &[OsString],
    width: i64,
    description: bool,
) -> Result<Arguments, CommandOutput> {
    let strings: Vec<&[u8]> = args.iter().map(|value| raw(value)).collect();
    let mut pattern = Vec::new();
    let mut options = BTreeMap::new();
    let mut separated = false;
    for (index, value) in strings.iter().enumerate() {
        if separated {
            pattern.push(b'A');
        } else if *value == b"--" {
            pattern.push(b'-');
            separated = true;
        } else if let Some(tuple) = parse_optional(value, width)? {
            options.insert(index, tuple);
            pattern.push(b'O');
        } else {
            pattern.push(b'A');
        }
    }
    let mut parser = Parser {
        args: strings.clone(),
        pattern,
        options,
        width,
        description,
        extras: Vec::new(),
        root: None,
        expect_sha256: None,
        name: None,
        accept: None,
        unattended: false,
        candidate: None,
    };
    let last_option = parser.options.keys().next_back().copied();
    let mut start = 0usize;
    if let Some(last) = last_option {
        while start <= last {
            let next = *parser.options.range(start..).next().unwrap().0;
            if start != next {
                let end = parser.consume_positionals(start);
                if end > start {
                    start = end;
                    continue;
                }
            }
            if !parser.options.contains_key(&start) {
                let skipped = parser.args[start..next].to_vec();
                parser.extras.extend(skipped);
                start = next;
            }
            start = parser.consume_optional(start)?;
        }
    }
    let stop = parser.consume_positionals(start);
    let rest = parser.args[stop..].to_vec();
    parser.extras.extend(rest);

    let mut missing: Vec<&str> = Vec::new();
    if parser.root.is_none() {
        missing.push("--root");
    }
    if parser.expect_sha256.is_none() {
        missing.push("--expect-sha256");
    }
    if parser.candidate.is_none() {
        missing.push("candidate");
    }
    if !missing.is_empty() {
        return Err(error(
            width,
            format!(
                "the following arguments are required: {}",
                missing.join(", ")
            )
            .as_bytes(),
        ));
    }
    if !parser.extras.is_empty() {
        return Err(error(
            width,
            &concat(&[b"unrecognized arguments: ", &parser.extras.join(&b" "[..])]),
        ));
    }
    Ok(Arguments {
        root: parser.root.unwrap(),
        expect_sha256: parser.expect_sha256.unwrap(),
        name: parser.name,
        accept: parser.accept,
        unattended: parser.unattended,
        candidate: parser.candidate.unwrap(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStringExt;

    /// 旧 Python 3.10.11 の実出力から採った固定期待(`gen_arg_cases.py`)。
    const CASES: &str = include_str!("../../../../../ci/adopt_candidate_fixtures/arg_cases.json");

    fn hex(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|at| u8::from_str_radix(&text[at..at + 2], 16).unwrap())
            .collect()
    }

    fn describe(value: &ArgValue) -> serde_json::Value {
        match value {
            ArgValue::Text(text) => {
                let digits = text
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>();
                serde_json::json!({ "text": digits })
            }
            ArgValue::EmptyList => serde_json::json!({ "list": 0 }),
        }
    }

    fn describe_all(arguments: &Arguments) -> serde_json::Value {
        serde_json::json!({
            "root": describe(&arguments.root),
            "candidate": describe(&arguments.candidate),
            "name": arguments.name.as_ref().map(describe),
            "expect_sha256": describe(&arguments.expect_sha256),
            "accept": arguments.accept.as_ref().map(describe),
            "unattended": arguments.unattended,
        })
    }

    #[test]
    fn every_recorded_case_matches_the_old_parser() {
        let document: serde_json::Value = serde_json::from_str(CASES).unwrap();
        assert_eq!(
            document["argparse_sha256"],
            "a238b700513237e10d26056e42c5b4e42d4961ea70e15ebeef4c61c76615f004"
        );
        let cases = document["cases"].as_array().unwrap();
        assert!(cases.len() > 100);
        let (mut parsed, mut helped, mut failed) = (0, 0, 0);
        for case in cases {
            let args: Vec<OsString> = case["args"]
                .as_array()
                .unwrap()
                .iter()
                .map(|item| OsString::from_vec(hex(item.as_str().unwrap())))
                .collect();
            let width = layout_width(case["columns"].as_str(), case["max_digits"].as_str(), || 80);
            let result = parse(&args, width);
            let context = format!("args={args:?} columns={}", case["columns"]);
            match (result, case.get("parsed")) {
                (Ok(arguments), Some(expected)) => {
                    parsed += 1;
                    assert_eq!(&describe_all(&arguments), expected, "{context}");
                }
                (Err(output), None) => {
                    if output.code == ExitCode::SUCCESS {
                        helped += 1;
                    } else {
                        failed += 1;
                    }
                    assert_eq!(i64::from(output.code.as_u8()), case["code"], "{context}");
                    assert_eq!(
                        String::from_utf8(output.stdout).unwrap(),
                        case["stdout"].as_str().unwrap(),
                        "{context}"
                    );
                    assert_eq!(
                        String::from_utf8(output.stderr).unwrap(),
                        case["stderr"].as_str().unwrap(),
                        "{context}"
                    );
                }
                (other, _) => panic!("解析の結果の種類が違う: {context} {:?}", other.is_ok()),
            }
        }
        // 3 種類(成功・help・失敗)がすべて固定期待に含まれること。
        assert!(
            parsed > 5 && helped > 10 && failed > 30,
            "{parsed} {helped} {failed}"
        );
    }

    #[test]
    fn terminal_width_follows_columns_then_the_terminal() {
        let width =
            |columns: Option<&str>, digits: Option<&str>| layout_width(columns, digits, || 120);
        assert_eq!(width(None, None), 118);
        assert_eq!(width(Some("50"), None), 48);
        assert_eq!(width(Some(" +1_0 "), None), 8);
        assert_eq!(width(Some("\u{661}\u{660}"), None), 8);
        assert_eq!(width(Some("\u{1c}30\u{1f}"), None), 28);
        for ignored in ["0", "-5", "abc", "", "1__0", "_1", "1_", "1.5"] {
            assert_eq!(width(Some(ignored), None), 118, "{ignored:?}");
        }
        // 既定の整数変換の上限(4300 桁)を超える値は ValueError として無視される。
        assert_eq!(width(Some(&"9".repeat(4300)), None), i64::MAX - 2);
        assert_eq!(width(Some(&"9".repeat(4301)), None), 118);
        assert_eq!(width(Some(&"9".repeat(640)), Some("640")), i64::MAX - 2);
        assert_eq!(width(Some(&"9".repeat(641)), Some("640")), 118);
        assert_eq!(width(Some(&"9".repeat(700)), Some("0")), i64::MAX - 2);
        assert_eq!(width(Some(&"9".repeat(4301)), Some("0")), i64::MAX - 2);
        assert_eq!(width(Some("1"), None), -1);
    }

    #[test]
    fn help_and_usage_wrap_with_the_terminal_width() {
        let wide = help_text(78);
        assert!(wide.starts_with(
            "usage: adopt-candidate.py [-h] --root ROOT --expect-sha256 EXPECT_SHA256\n"
        ));
        assert!(wide.contains("\n  -h, --help            show this help message and exit\n"));
        assert!(wide.ends_with("  --accept ACCEPT\n  --unattended\n"));
        let narrow = help_text(10);
        assert!(narrow.starts_with("usage: adopt-candidate.py\n       [-h]\n"));
        assert!(narrow.contains("\n  -h, --help\n    show this\n    help\n"));
    }

    #[test]
    fn missing_required_arguments_are_listed_in_the_declared_order() {
        let output = parse(&[], 78).unwrap_err();
        assert_eq!(output.code, ExitCode::USAGE);
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(stderr.ends_with(
            "adopt-candidate.py: error: the following arguments are required: --root, --expect-sha256, candidate\n"
        ));
    }
}
