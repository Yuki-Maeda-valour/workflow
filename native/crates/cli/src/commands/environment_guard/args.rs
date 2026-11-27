//! Python 3.10 の `argparse` の再現。subcommand・短縮・`append`・`type=int`・`REMAINDER`・help と
//! 使い方の誤り。プログラム名は `environment-guard.py`。幅は `COLUMNS` か端末の幅(U5)。
use super::pystr::{cps, decimal, parse_int, repr, split_whitespace};
use super::pyvalue::PyInt;
use super::runtime::StreamKind;
use dev_workflow_core::loop_state::CodepointString;
use std::collections::HashMap;

type Cps = Vec<u32>;

pub const PROG: &str = "environment-guard.py";

/// 解析した値。`--x=--` は旧版で空の list になる(`'--'` を取り除いた結果が空)。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ArgValue {
    Str(CodepointString),
    Int(PyInt),
    /// 旧版の `[]`。
    EmptyList,
}

impl ArgValue {
    /// `str(value)`。空の list は `[]`。
    pub fn py_str(&self) -> Cps {
        match self {
            ArgValue::Str(s) => s.as_codepoints().to_vec(),
            ArgValue::Int(i) => cps(&i.to_decimal(0).expect("上限なし")),
            ArgValue::EmptyList => cps("[]"),
        }
    }
}

/// 解析後の値。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NsValue {
    None,
    Value(ArgValue),
    Many(Vec<ArgValue>),
    /// `REMAINDER` の位置引数。
    Words(Vec<CodepointString>),
    Name(String),
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Namespace {
    values: HashMap<&'static str, NsValue>,
}

impl Namespace {
    /// 全部の項目(テスト用)。
    pub fn entries(&self) -> impl Iterator<Item = (&&'static str, &NsValue)> {
        self.values.iter()
    }

    pub fn get(&self, dest: &str) -> &NsValue {
        self.values.get(dest).unwrap_or(&NsValue::None)
    }

    pub fn value(&self, dest: &str) -> Option<&ArgValue> {
        match self.get(dest) {
            NsValue::Value(v) => Some(v),
            _ => None,
        }
    }

    pub fn many(&self, dest: &str) -> &[ArgValue] {
        match self.get(dest) {
            NsValue::Many(v) => v,
            _ => &[],
        }
    }

    pub fn words(&self, dest: &str) -> &[CodepointString] {
        match self.get(dest) {
            NsValue::Words(v) => v,
            _ => &[],
        }
    }
}

/// 旧版が標準出力・標準エラーへ書いて、そのまま終了する場合。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Exit {
    pub outputs: Vec<(StreamKind, Cps)>,
    pub code: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum OptKind {
    Help,
    Store,
    Append,
}

struct OptSpec {
    flag: &'static str,
    dest: &'static str,
    kind: OptKind,
    required: bool,
    int: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Positional {
    None,
    Subparsers,
    Remainder,
}

struct ParserSpec {
    name: &'static str,
    options: Vec<OptSpec>,
    positional: Positional,
}

fn opt(flag: &'static str, dest: &'static str, required: bool) -> OptSpec {
    OptSpec {
        flag,
        dest,
        kind: OptKind::Store,
        required,
        int: false,
    }
}

fn append(flag: &'static str, dest: &'static str) -> OptSpec {
    OptSpec {
        kind: OptKind::Append,
        ..opt(flag, dest, false)
    }
}

fn help_opt() -> OptSpec {
    OptSpec {
        flag: "-h",
        dest: "help",
        kind: OptKind::Help,
        required: false,
        int: false,
    }
}

fn state_opts() -> Vec<OptSpec> {
    vec![
        help_opt(),
        opt("--state", "state", true),
        opt("--expect-sha256", "expect_sha256", true),
    ]
}

/// 旧版の `main` が作る parser の並び(登録の順が、選択肢の表示の順になる)。
fn subparsers() -> Vec<ParserSpec> {
    let mut list = Vec::new();
    list.push(ParserSpec {
        name: "bootstrap",
        options: vec![
            help_opt(),
            opt("--root", "root", true),
            opt("--output", "output", true),
            opt("--inventory", "inventory", false),
            append("--setting", "setting"),
            append("--config", "config"),
            append("--shell", "shell"),
            append("--skills-dir", "skills_dir"),
            append("--settings-dir", "settings_dir"),
            append("--managed-dir", "managed_dir"),
        ],
        positional: Positional::None,
    });
    for name in ["verify", "exec", "read", "hook"] {
        let mut options = state_opts();
        options.push(opt("--current-inventory", "current_inventory", false));
        if name != "verify" {
            options.push(opt("--path", "path", true));
        }
        list.push(ParserSpec {
            name,
            options,
            positional: if name == "exec" {
                Positional::Remainder
            } else {
                Positional::None
            },
        });
    }
    let mut pair = state_opts();
    pair.push(opt("--current-inventory", "current_inventory", true));
    pair.push(opt("--guard-sha256", "guard_sha256", true));
    pair.push(OptSpec {
        int: true,
        ..opt("--go-timeout", "go_timeout", true)
    });
    list.push(ParserSpec {
        name: "verify-pair",
        options: pair,
        positional: Positional::None,
    });
    list.push(ParserSpec {
        name: "host-settings",
        options: state_opts(),
        positional: Positional::None,
    });
    let mut allowlist = state_opts();
    allowlist.push(append("--allowed-tools", "allowed_tools"));
    list.push(ParserSpec {
        name: "host-allowlist",
        options: allowlist,
        positional: Positional::None,
    });
    list.push(ParserSpec {
        name: "host-paths",
        options: vec![help_opt()],
        positional: Positional::None,
    });
    list
}

fn top_spec() -> ParserSpec {
    ParserSpec {
        name: "",
        options: vec![help_opt()],
        positional: Positional::Subparsers,
    }
}

/// 解析の環境。`columns` は `COLUMNS`(文字番号)、`terminal_columns` は端末の幅(取れなければ 80)。
#[derive(Clone, Debug)]
pub struct ArgEnv {
    pub columns: Option<Cps>,
    pub terminal_columns: usize,
    pub max_str_digits: usize,
}

impl ArgEnv {
    /// `shutil.get_terminal_size().columns - 2`。
    fn width(&self) -> i64 {
        let from_env = self
            .columns
            .as_deref()
            .and_then(|text| parse_int(text, self.max_str_digits).ok())
            .filter(|value| !value.is_negative() && !value.is_zero())
            .map(|value| value.to_usize_saturating());
        let columns = from_env.unwrap_or(self.terminal_columns);
        i64::try_from(columns).unwrap_or(i64::MAX) - 2
    }
}

// ---------------------------------------------------------------- 書式

fn metavar(option: &OptSpec) -> String {
    option.dest.to_uppercase()
}

fn choices_text(names: &[&str]) -> String {
    format!("{{{}}}", names.join(","))
}

/// `-h` を除く表示用の部品(使い方の 1 行を wrap するときの単位)。
fn usage_parts(spec: &ParserSpec, names: &[&str]) -> (Vec<String>, Vec<String>) {
    let mut optionals = Vec::new();
    for option in &spec.options {
        match option.kind {
            OptKind::Help => optionals.push("[-h]".to_owned()),
            _ if option.required => {
                optionals.push(option.flag.to_owned());
                optionals.push(metavar(option));
            }
            _ => optionals.push(format!("[{} {}]", option.flag, metavar(option))),
        }
    }
    let mut positionals = Vec::new();
    match spec.positional {
        Positional::None => {}
        Positional::Subparsers => {
            positionals.push(choices_text(names));
            positionals.push("...".to_owned());
        }
        Positional::Remainder => positionals.push("...".to_owned()),
    }
    (optionals, positionals)
}

fn wrap_lines(
    parts: &[String],
    indent: &str,
    prefix: Option<usize>,
    text_width: i64,
) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line: Vec<&str> = Vec::new();
    let mut line_len = prefix.unwrap_or(indent.len()) as i64 - 1;
    for part in parts {
        if line_len + 1 + part.len() as i64 > text_width && !line.is_empty() {
            lines.push(format!("{indent}{}", line.join(" ")));
            line.clear();
            line_len = indent.len() as i64 - 1;
        }
        line.push(part);
        line_len += part.len() as i64 + 1;
    }
    if !line.is_empty() {
        lines.push(format!("{indent}{}", line.join(" ")));
    }
    if prefix.is_some() && !lines.is_empty() {
        lines[0] = lines[0][indent.len()..].to_owned();
    }
    lines
}

/// `parser.format_usage()`。
fn format_usage(spec: &ParserSpec, prog: &str, names: &[&str], width: i64) -> String {
    let prefix = "usage: ";
    let (optionals, positionals) = usage_parts(spec, names);
    let mut all = optionals.clone();
    all.extend(positionals.clone());
    let action_usage = all.join(" ");
    let usage_line = if action_usage.is_empty() {
        prog.to_owned()
    } else {
        format!("{prog} {action_usage}")
    };
    let text_width = width.max(11);
    let usage = if (prefix.len() + usage_line.len()) as i64 > text_width {
        // `len(prefix) + len(prog) <= 0.75 * text_width` を整数で比べる。
        if ((prefix.len() + prog.len()) * 4) as i64 <= text_width * 3 {
            let indent = " ".repeat(prefix.len() + prog.len() + 1);
            let lines = if !optionals.is_empty() {
                let mut first = vec![prog.to_owned()];
                first.extend(optionals.clone());
                let mut lines = wrap_lines(&first, &indent, Some(prefix.len()), text_width);
                lines.extend(wrap_lines(&positionals, &indent, None, text_width));
                lines
            } else if !positionals.is_empty() {
                let mut first = vec![prog.to_owned()];
                first.extend(positionals.clone());
                wrap_lines(&first, &indent, Some(prefix.len()), text_width)
            } else {
                vec![prog.to_owned()]
            };
            lines.join("\n")
        } else {
            let indent = " ".repeat(prefix.len());
            let mut parts = optionals.clone();
            parts.extend(positionals.clone());
            let mut lines = wrap_lines(&parts, &indent, None, text_width);
            if lines.len() > 1 {
                lines = wrap_lines(&optionals, &indent, None, text_width);
                lines.extend(wrap_lines(&positionals, &indent, None, text_width));
            }
            let mut all_lines = vec![prog.to_owned()];
            all_lines.extend(lines);
            all_lines.join("\n")
        }
    } else {
        usage_line
    };
    format!("{prefix}{usage}\n\n")
}

fn invocation(option: &OptSpec) -> String {
    match option.kind {
        OptKind::Help => "-h, --help".to_owned(),
        _ => format!("{} {}", option.flag, metavar(option)),
    }
}

fn wrap_words(text: &str, width: i64) -> Vec<String> {
    let width = width.max(1) as usize;
    let mut lines: Vec<String> = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        if !current.is_empty() && current.len() + 1 + word.len() > width {
            lines.push(std::mem::take(&mut current));
        }
        if !current.is_empty() {
            current.push(' ');
        }
        current.push_str(word);
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

/// `parser.format_help()`。
fn format_help(spec: &ParserSpec, prog: &str, names: &[&str], width: i64) -> String {
    let max_help_position = 24.min((width - 20).max(4));
    let mut headers: Vec<(String, bool)> = Vec::new();
    let mut positional_headers: Vec<(String, bool)> = Vec::new();
    match spec.positional {
        Positional::None => {}
        Positional::Subparsers => positional_headers.push((choices_text(names), false)),
        Positional::Remainder => positional_headers.push(("argv".to_owned(), false)),
    }
    for option in &spec.options {
        headers.push((invocation(option), option.kind == OptKind::Help));
    }
    let max_length = positional_headers
        .iter()
        .chain(headers.iter())
        .map(|(text, _)| text.len() + 2)
        .max()
        .unwrap_or(0);
    let help_position = ((max_length + 2) as i64).min(max_help_position);
    let help_width = (width - help_position).max(11);
    let action_width = help_position - 2 - 2;
    let entry = |header: &str, has_help: bool| -> String {
        if !has_help {
            return format!("  {header}\n");
        }
        let lines = wrap_words("show this help message and exit", help_width);
        let mut out = String::new();
        if header.len() as i64 <= action_width {
            out.push_str(&format!(
                "  {header:<width$}  {}\n",
                lines[0],
                width = action_width as usize
            ));
        } else {
            out.push_str(&format!(
                "  {header}\n{}{}\n",
                " ".repeat(help_position as usize),
                lines[0]
            ));
        }
        for line in &lines[1..] {
            out.push_str(&format!("{}{line}\n", " ".repeat(help_position as usize)));
        }
        out
    };
    let mut text = format_usage(spec, prog, names, width);
    for (title, group) in [
        ("positional arguments", &positional_headers),
        ("options", &headers),
    ] {
        if group.is_empty() {
            continue;
        }
        text.push_str(&format!("\n{title}:\n"));
        for (header, has_help) in group {
            text.push_str(&entry(header, *has_help));
        }
        text.push('\n');
    }
    // `\n\n\n+` を `\n\n` にし、前後の改行を落として末尾に 1 つだけ付ける。
    let mut collapsed = String::new();
    let mut newlines = 0;
    for c in text.chars() {
        if c == '\n' {
            newlines += 1;
            if newlines <= 2 {
                collapsed.push(c);
            }
        } else {
            newlines = 0;
            collapsed.push(c);
        }
    }
    format!("{}\n", collapsed.trim_matches('\n'))
}

// ---------------------------------------------------------------- 解析

/// `_parse_optional` の結果。
#[derive(Clone, Debug)]
struct OptionTuple {
    /// 見つかった設定の位置(`spec.options`)。無ければ未知の `--x`。
    action: Option<usize>,
    option_string: Cps,
    explicit: Option<Cps>,
}

struct Engine<'a> {
    spec: &'a ParserSpec,
    prog: String,
    names: Vec<&'a str>,
    env: &'a ArgEnv,
    subs: &'a [ParserSpec],
}

fn starts_with(text: &[u32], prefix: &[u32]) -> bool {
    text.len() >= prefix.len() && &text[..prefix.len()] == prefix
}

/// `-5`・`-.5`・`-1.5` のような引数か。`$` が末尾の改行の前にも一致するのを再現する。
fn negative_number(arg: &[u32]) -> bool {
    let arg = arg.strip_suffix(&[0x0a]).unwrap_or(arg);
    let Some(rest) = arg.strip_prefix(&[0x2d]) else {
        return false;
    };
    let digits = |text: &[u32]| text.iter().all(|&c| decimal(c).is_some());
    if let Some(dot) = rest.iter().position(|&c| c == 0x2e) {
        let (left, right) = (&rest[..dot], &rest[dot + 1..]);
        digits(left) && !right.is_empty() && digits(right)
    } else {
        !rest.is_empty() && digits(rest)
    }
}

impl Engine<'_> {
    fn flags(&self) -> Vec<(Cps, usize)> {
        // `_option_string_actions` の並び。
        let mut map = Vec::new();
        for (index, option) in self.spec.options.iter().enumerate() {
            if option.kind == OptKind::Help {
                map.push((cps("-h"), index));
                map.push((cps("--help"), index));
            } else {
                map.push((cps(option.flag), index));
            }
        }
        map
    }

    fn action_name(&self, index: usize) -> String {
        match self.spec.options[index].kind {
            OptKind::Help => "-h/--help".to_owned(),
            _ => self.spec.options[index].flag.to_owned(),
        }
    }

    fn width(&self) -> i64 {
        self.env.width()
    }

    /// `parser.error(message)`。
    fn error(&self, message: &[u32]) -> Exit {
        let usage = format!(
            "{}\n",
            format_usage(self.spec, &self.prog, &self.names, self.width()).trim_end_matches('\n')
        );
        let mut tail = cps(&format!("{}: error: ", self.prog));
        tail.extend_from_slice(message);
        tail.push(0x0a);
        Exit {
            outputs: vec![
                (StreamKind::Stderr, cps(&usage)),
                (StreamKind::Stderr, tail),
            ],
            code: 2,
        }
    }

    fn argument_error(&self, name: &str, message: &[u32]) -> Exit {
        let mut text = cps(&format!("argument {name}: "));
        text.extend_from_slice(message);
        self.error(&text)
    }

    fn help_exit(&self) -> Exit {
        let text = format_help(self.spec, &self.prog, &self.names, self.width());
        Exit {
            outputs: vec![(StreamKind::Stdout, cps(&text))],
            code: 0,
        }
    }

    fn get_option_tuples(&self, arg: &[u32]) -> Vec<OptionTuple> {
        let mut result = Vec::new();
        let flags = self.flags();
        if arg[0] == 0x2d && arg.get(1) == Some(&0x2d) {
            let (prefix, explicit) = match arg.iter().position(|&c| c == 0x3d) {
                Some(at) => (arg[..at].to_vec(), Some(arg[at + 1..].to_vec())),
                None => (arg.to_vec(), None),
            };
            for (flag, index) in flags {
                if starts_with(&flag, &prefix) {
                    result.push(OptionTuple {
                        action: Some(index),
                        option_string: flag,
                        explicit: explicit.clone(),
                    });
                }
            }
        } else if arg[0] == 0x2d && arg.get(1) != Some(&0x2d) {
            let short = &arg[..2.min(arg.len())];
            let short_explicit = arg[2.min(arg.len())..].to_vec();
            for (flag, index) in flags {
                if flag == short {
                    result.push(OptionTuple {
                        action: Some(index),
                        option_string: flag,
                        explicit: Some(short_explicit.clone()),
                    });
                } else if starts_with(&flag, arg) {
                    result.push(OptionTuple {
                        action: Some(index),
                        option_string: flag,
                        explicit: None,
                    });
                }
            }
        }
        result
    }

    /// `_parse_optional`。曖昧な短縮は `Err`(その場で終了)。
    fn parse_optional(&self, arg: &[u32]) -> Result<Option<OptionTuple>, Exit> {
        if arg.first() != Some(&0x2d) {
            return Ok(None);
        }
        let flags = self.flags();
        if let Some((flag, index)) = flags.iter().find(|(flag, _)| flag == arg) {
            return Ok(Some(OptionTuple {
                action: Some(*index),
                option_string: flag.clone(),
                explicit: None,
            }));
        }
        if arg.len() == 1 {
            return Ok(None);
        }
        if let Some(at) = arg.iter().position(|&c| c == 0x3d) {
            let (head, explicit) = (&arg[..at], &arg[at + 1..]);
            if let Some((flag, index)) = flags.iter().find(|(flag, _)| flag == head) {
                return Ok(Some(OptionTuple {
                    action: Some(*index),
                    option_string: flag.clone(),
                    explicit: Some(explicit.to_vec()),
                }));
            }
        }
        let tuples = self.get_option_tuples(arg);
        if tuples.len() > 1 {
            let mut message = cps("ambiguous option: ");
            message.extend_from_slice(arg);
            message.extend(cps(" could match "));
            for (i, tuple) in tuples.iter().enumerate() {
                if i > 0 {
                    message.extend(cps(", "));
                }
                message.extend_from_slice(&tuple.option_string);
            }
            return Err(self.error(&message));
        }
        if let Some(tuple) = tuples.into_iter().next() {
            return Ok(Some(tuple));
        }
        if negative_number(arg) || arg.contains(&0x20) {
            return Ok(None);
        }
        Ok(Some(OptionTuple {
            action: None,
            option_string: arg.to_vec(),
            explicit: None,
        }))
    }

    /// `_get_value`。`type=int` の変換。
    fn convert(&self, index: usize, arg: &[u32]) -> Result<ArgValue, Exit> {
        let option = &self.spec.options[index];
        if option.int {
            return match parse_int(arg, self.env.max_str_digits) {
                Ok(value) => Ok(ArgValue::Int(value)),
                Err(_) => {
                    let mut message = cps("invalid int value: ");
                    message.extend(repr(arg));
                    Err(self.argument_error(&self.action_name(index), &message))
                }
            };
        }
        Ok(ArgValue::Str(
            CodepointString::try_from_codepoints(arg.to_vec()).expect("範囲内"),
        ))
    }

    /// 1 つの設定を取る(`take_action`)。`values` は引数の文字列。
    fn take_option(
        &self,
        index: usize,
        mut values: Vec<Cps>,
        namespace: &mut Namespace,
    ) -> Result<(), Exit> {
        let option = &self.spec.options[index];
        if option.kind == OptKind::Help {
            return Err(self.help_exit());
        }
        // `PARSER`・`REMAINDER` 以外は、最初の `--` を取り除く。
        if let Some(at) = values.iter().position(|v| v == &[0x2d, 0x2d]) {
            values.remove(at);
        }
        let value = if values.len() == 1 {
            self.convert(index, &values[0])?
        } else {
            // 空になったとき、旧版は空の list を値にする。
            ArgValue::EmptyList
        };
        match option.kind {
            OptKind::Store => {
                namespace.values.insert(option.dest, NsValue::Value(value));
            }
            OptKind::Append => {
                let mut items = match namespace.values.get(option.dest) {
                    Some(NsValue::Many(items)) => items.clone(),
                    _ => Vec::new(),
                };
                items.push(value);
                namespace.values.insert(option.dest, NsValue::Many(items));
            }
            OptKind::Help => {}
        }
        Ok(())
    }

    fn parse_known(&self, args: &[Cps]) -> Result<(Namespace, Vec<Cps>), Exit> {
        let mut namespace = Namespace::default();
        for option in &self.spec.options {
            match option.kind {
                OptKind::Help => {}
                OptKind::Store => {
                    namespace.values.insert(option.dest, NsValue::None);
                }
                OptKind::Append => {
                    namespace
                        .values
                        .insert(option.dest, NsValue::Many(Vec::new()));
                }
            }
        }
        match self.spec.positional {
            Positional::Subparsers => {
                namespace.values.insert("command", NsValue::None);
            }
            Positional::Remainder => {
                namespace.values.insert("argv", NsValue::None);
            }
            Positional::None => {}
        }
        // 引数の型('O'・'A'・'-')と、設定の候補。
        let mut pattern: Vec<char> = Vec::new();
        let mut option_indices: HashMap<usize, OptionTuple> = HashMap::new();
        let mut after_separator = false;
        for (i, arg) in args.iter().enumerate() {
            if after_separator {
                pattern.push('A');
            } else if arg == &[0x2d, 0x2d] {
                pattern.push('-');
                after_separator = true;
            } else if let Some(tuple) = self.parse_optional(arg)? {
                option_indices.insert(i, tuple);
                pattern.push('O');
            } else {
                pattern.push('A');
            }
        }
        let mut seen: Vec<String> = Vec::new();
        let mut extras: Vec<Cps> = Vec::new();
        let mut unrecognized: Vec<Cps> = Vec::new();
        let mut positionals_left = self.spec.positional != Positional::None;
        let max_option_index = option_indices.keys().copied().max();
        let mut start = 0usize;
        if let Some(max_index) = max_option_index {
            while start <= max_index {
                let next_option = option_indices
                    .keys()
                    .copied()
                    .filter(|&i| i >= start)
                    .min()
                    .expect("max 以下に 1 つはある");
                if start != next_option {
                    let end = self.consume_positionals(
                        start,
                        args,
                        &pattern,
                        &mut positionals_left,
                        &mut namespace,
                        &mut seen,
                        &mut unrecognized,
                    )?;
                    if end > start {
                        start = end;
                        continue;
                    }
                    start = end;
                }
                if !option_indices.contains_key(&start) {
                    extras.extend(args[start..next_option].iter().cloned());
                    start = next_option;
                }
                start = self.consume_optional(
                    start,
                    args,
                    &pattern,
                    &option_indices,
                    &mut namespace,
                    &mut seen,
                    &mut extras,
                )?;
            }
        }
        let stop = self.consume_positionals(
            start,
            args,
            &pattern,
            &mut positionals_left,
            &mut namespace,
            &mut seen,
            &mut unrecognized,
        )?;
        extras.extend(args[stop.min(args.len())..].iter().cloned());
        // 必須の設定が揃っているか。
        let mut required = Vec::new();
        for (index, option) in self.spec.options.iter().enumerate() {
            if option.required && !seen.iter().any(|name| name == option.dest) {
                required.push(self.action_name(index));
            }
        }
        match self.spec.positional {
            Positional::Subparsers if !seen.iter().any(|n| n == "command") => {
                required.push("command".to_owned());
            }
            Positional::Remainder if !seen.iter().any(|n| n == "argv") => {
                required.push("argv".to_owned());
            }
            _ => {}
        }
        if !required.is_empty() {
            let message = cps(&format!(
                "the following arguments are required: {}",
                required.join(", ")
            ));
            return Err(self.error(&message));
        }
        extras.extend(unrecognized);
        Ok((namespace, extras))
    }

    #[allow(clippy::too_many_arguments)]
    fn consume_optional(
        &self,
        start: usize,
        args: &[Cps],
        pattern: &[char],
        option_indices: &HashMap<usize, OptionTuple>,
        namespace: &mut Namespace,
        seen: &mut Vec<String>,
        extras: &mut Vec<Cps>,
    ) -> Result<usize, Exit> {
        let tuple = option_indices[&start].clone();
        let mut action = tuple.action;
        let mut option_string = tuple.option_string;
        let mut explicit = tuple.explicit;
        let mut taken: Vec<(usize, Vec<Cps>)> = Vec::new();
        let stop;
        loop {
            let Some(index) = action else {
                extras.push(args[start].clone());
                return Ok(start + 1);
            };
            let takes_argument = self.spec.options[index].kind != OptKind::Help;
            if let Some(explicit_arg) = explicit.clone() {
                // 引数を取らない設定の数は 0、取る設定は 1。
                let count = usize::from(takes_argument);
                if count == 0 && option_string.get(1) != Some(&0x2d) && !explicit_arg.is_empty() {
                    taken.push((index, Vec::new()));
                    let next = vec![option_string[0], explicit_arg[0]];
                    let rest = explicit_arg[1..].to_vec();
                    let flags = self.flags();
                    if let Some((flag, found)) = flags.iter().find(|(flag, _)| flag == &next) {
                        option_string = flag.clone();
                        action = Some(*found);
                        explicit = if rest.is_empty() { None } else { Some(rest) };
                    } else {
                        let mut message = cps("ignored explicit argument ");
                        message.extend(repr(&explicit_arg));
                        return Err(self.argument_error(&self.action_name(index), &message));
                    }
                } else if count == 1 {
                    stop = start + 1;
                    taken.push((index, vec![explicit_arg]));
                    break;
                } else {
                    let mut message = cps("ignored explicit argument ");
                    message.extend(repr(&explicit_arg));
                    return Err(self.argument_error(&self.action_name(index), &message));
                }
            } else {
                let from = start + 1;
                if takes_argument {
                    if pattern.get(from) != Some(&'A') {
                        return Err(self.argument_error(
                            &self.action_name(index),
                            &cps("expected one argument"),
                        ));
                    }
                    stop = from + 1;
                    taken.push((index, vec![args[from].clone()]));
                } else {
                    stop = from;
                    taken.push((index, Vec::new()));
                }
                break;
            }
        }
        for (index, values) in taken {
            seen.push(self.spec.options[index].dest.to_owned());
            self.take_option(index, values, namespace)?;
        }
        Ok(stop)
    }

    #[allow(clippy::too_many_arguments)]
    fn consume_positionals(
        &self,
        start: usize,
        args: &[Cps],
        pattern: &[char],
        positionals_left: &mut bool,
        namespace: &mut Namespace,
        seen: &mut Vec<String>,
        unrecognized: &mut Vec<Cps>,
    ) -> Result<usize, Exit> {
        if !*positionals_left {
            return Ok(start);
        }
        let selected = &pattern[start.min(pattern.len())..];
        let count = match self.spec.positional {
            Positional::Remainder => selected.len(),
            Positional::Subparsers => {
                let skip = selected.iter().take_while(|&&c| c == '-').count();
                if selected.get(skip) == Some(&'A') {
                    selected.len()
                } else {
                    return Ok(start);
                }
            }
            Positional::None => return Ok(start),
        };
        *positionals_left = false;
        let values: Vec<Cps> = args[start..start + count].to_vec();
        match self.spec.positional {
            Positional::Remainder => {
                seen.push("argv".to_owned());
                let words = values
                    .into_iter()
                    .map(|v| CodepointString::try_from_codepoints(v).expect("範囲内"))
                    .collect();
                namespace.values.insert("argv", NsValue::Words(words));
            }
            _ => {
                seen.push("command".to_owned());
                self.take_subparsers(values, namespace, unrecognized)?;
            }
        }
        Ok(start + count)
    }

    fn take_subparsers(
        &self,
        values: Vec<Cps>,
        namespace: &mut Namespace,
        unrecognized: &mut Vec<Cps>,
    ) -> Result<(), Exit> {
        let name = &values[0];
        let Some(sub) = self.subs.iter().find(|s| cps(s.name) == *name) else {
            let mut message = cps("invalid choice: ");
            message.extend(repr(name));
            message.extend(cps(" (choose from "));
            for (i, s) in self.subs.iter().enumerate() {
                if i > 0 {
                    message.extend(cps(", "));
                }
                message.extend(repr(&cps(s.name)));
            }
            message.push(0x29);
            return Err(self.argument_error("command", &message));
        };
        namespace
            .values
            .insert("command", NsValue::Name(sub.name.to_owned()));
        let engine = Engine {
            spec: sub,
            prog: format!("{} {}", self.prog, sub.name),
            names: Vec::new(),
            env: self.env,
            subs: &[],
        };
        let (sub_namespace, extras) = engine.parse_known(&values[1..])?;
        for (key, value) in sub_namespace.values {
            namespace.values.insert(key, value);
        }
        unrecognized.extend(extras);
        Ok(())
    }
}

/// 解析の結果。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Parsed {
    pub command: String,
    pub namespace: Namespace,
}

/// `parse_args(args)`。
pub fn parse_args(args: &[Cps], env: &ArgEnv) -> Result<Parsed, Exit> {
    let subs = subparsers();
    let top = top_spec();
    let names: Vec<&str> = subs.iter().map(|s| s.name).collect();
    let engine = Engine {
        spec: &top,
        prog: PROG.to_owned(),
        names,
        env,
        subs: &subs,
    };
    let (namespace, extras) = engine.parse_known(args)?;
    if !extras.is_empty() {
        let mut message = cps("unrecognized arguments: ");
        for (i, extra) in extras.iter().enumerate() {
            if i > 0 {
                message.push(0x20);
            }
            message.extend_from_slice(extra);
        }
        return Err(engine.error(&message));
    }
    let command = match namespace.get("command") {
        NsValue::Name(name) => name.clone(),
        _ => unreachable!("必須の位置引数が揃っている"),
    };
    Ok(Parsed { command, namespace })
}

/// 空白で区切った語の並び(`split()`)。`REMAINDER` の検査に使う。
pub fn words(text: &[u32]) -> Vec<Cps> {
    split_whitespace(text)
}

#[cfg(test)]
mod tests {
    use super::super::pystr::{decode_fs, encode_backslashreplace, encode_fs, to_string};
    use super::super::testing::{cps_of, fixture, from_hex, namespace_json};
    use super::*;

    fn env(columns: Option<&str>, terminal: usize) -> ArgEnv {
        ArgEnv {
            columns: columns.map(cps),
            terminal_columns: terminal,
            max_str_digits: 4300,
        }
    }

    fn run(args: &[&str], columns: Option<&str>) -> Result<Parsed, Exit> {
        let args: Vec<Cps> = args.iter().map(|a| cps(a)).collect();
        parse_args(&args, &env(columns, 80))
    }

    #[test]
    fn a_simple_command_parses() {
        let parsed = run(&["verify", "--state", "/s", "--expect-sha256=abc"], None).unwrap();
        assert_eq!(parsed.command, "verify");
        assert_eq!(
            parsed.namespace.value("state"),
            Some(&ArgValue::Str(CodepointString::from("/s")))
        );
        assert_eq!(parsed.namespace.value("current_inventory"), None);
    }

    #[test]
    fn missing_arguments_and_unknown_commands_exit_with_2() {
        let exit = run(&[], None).unwrap_err();
        assert_eq!(exit.code, 2);
        let stderr = exit
            .outputs
            .iter()
            .map(|(_, t)| to_string(t))
            .collect::<String>();
        assert!(stderr.ends_with(
            "environment-guard.py: error: the following arguments are required: command\n"
        ));
        let exit = run(&["nope"], None).unwrap_err();
        assert!(
            to_string(&exit.outputs[1].1).contains("argument command: invalid choice: 'nope' (choose from 'bootstrap', 'verify', 'exec', 'read', 'hook', 'verify-pair', 'host-settings', 'host-allowlist', 'host-paths')")
        );
    }

    fn expected_bytes(case: &serde_json::Value, name: &str) -> Vec<u8> {
        match case[name].as_str() {
            Some(text) => text.as_bytes().to_vec(),
            None => from_hex(case[format!("{name}_hex")].as_str().unwrap()),
        }
    }

    #[test]
    fn argparse_cases_match_python_3_10() {
        let cases = fixture!("argparse_cases.json");
        let cases = cases.as_array().unwrap();
        assert!(cases.len() > 1500, "{}", cases.len());
        let mut exits = 0;
        let mut successes = 0;
        for case in cases {
            let args: Vec<Cps> = case["args"]
                .as_array()
                .unwrap()
                .iter()
                .map(|hex| decode_fs(&from_hex(hex.as_str().unwrap())))
                .collect();
            let env = ArgEnv {
                columns: case["columns"].as_array().map(|_| cps_of(&case["columns"])),
                terminal_columns: 80,
                max_str_digits: 4300,
            };
            let label = format!("{} {}", case["args_text"], case["columns_text"]);
            match parse_args(&args, &env) {
                Ok(parsed) => {
                    successes += 1;
                    assert_eq!(case["exit"], serde_json::Value::Null, "{label}");
                    assert_eq!(namespace_json(&parsed), case["namespace"], "{label}");
                }
                Err(exit) => {
                    exits += 1;
                    let (mut out, mut err) = (Vec::new(), Vec::new());
                    for (stream, text) in &exit.outputs {
                        match stream {
                            StreamKind::Stdout => out.extend_from_slice(text),
                            StreamKind::Stderr => err.extend_from_slice(text),
                        }
                    }
                    assert_eq!(
                        i64::from(exit.code),
                        case["exit"].as_i64().unwrap(),
                        "{label}"
                    );
                    assert_eq!(
                        encode_fs(&out).unwrap(),
                        expected_bytes(case, "stdout"),
                        "{label}"
                    );
                    assert_eq!(
                        encode_backslashreplace(&err),
                        expected_bytes(case, "stderr"),
                        "{label}"
                    );
                }
            }
        }
        assert!(exits > 1000 && successes > 20, "{exits} {successes}");
    }
}
