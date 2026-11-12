//! 旧版の 2 本の parser(本家 3.10.11 の `Lib/argparse.py`)の解釈・usage・help・誤りの文。
//! - 再現する範囲は、2 本の parser の定義に要るものだけ: 長い選択肢と `-h`、省略形と曖昧な省略形の誤り、
//!   `--opt=値`、繰り返し(後の値が勝つ)、`store_true`・`type=int`・`type=float`・`required`・
//!   `nargs=REMAINDER`、`--`、負の数の形、usage と help の折り返し、`error:` の文。
//! - 文字列は符号位置の列(argv は surrogateescape で復号した値)で持つ。標準エラーへ書くときは
//!   `value::encode_backslashreplace`、標準出力は `value::encode_surrogateescape` で符号化する。
//! - 幅は `shutil.get_terminal_size().columns - 2`([`terminal_columns`])。

use super::number;
use super::value::{self, PyValue};

/// 選択肢の値の変換(`type=`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Convert {
    Str,
    Int,
    Float,
}

/// 引数の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// `-h`・`--help`。
    Help,
    /// `action='store_true'`。
    StoreTrue,
    /// 値を 1 つ取る選択肢(`nargs=None`)。
    Store(Convert),
    /// 位置引数 `nargs=argparse.REMAINDER`。
    Remainder,
}

/// `add_argument` 1 つ。
#[derive(Clone, Copy, Debug)]
pub struct Action {
    /// 選択肢の字面(位置引数は空)。
    pub options: &'static [&'static str],
    pub dest: &'static str,
    pub kind: Kind,
    pub required: bool,
    pub help: Option<&'static str>,
}

/// `ArgumentParser`。`actions` は `add_argument` の順(最初は `-h`)。
#[derive(Clone, Copy, Debug)]
pub struct Parser {
    pub prog: &'static str,
    pub actions: &'static [Action],
}

const HELP: Action = Action {
    options: &["-h", "--help"],
    dest: "help",
    kind: Kind::Help,
    required: false,
    help: Some("show this help message and exit"),
};

const fn optional(
    option: &'static [&'static str],
    dest: &'static str,
    kind: Kind,
    required: bool,
) -> Action {
    Action {
        options: option,
        dest,
        kind,
        required,
        help: None,
    }
}

const COMMAND: Action = Action {
    options: &[],
    dest: "command",
    kind: Kind::Remainder,
    required: true,
    help: None,
};

/// 旧版 `check-process.py:311-318`。
pub const CHECK_PROCESS: Parser = Parser {
    prog: "check-process.py",
    actions: &[
        HELP,
        optional(
            &["--anchor-fd"],
            "anchor_fd",
            Kind::Store(Convert::Int),
            false,
        ),
        optional(&["--linux"], "linux", Kind::StoreTrue, false),
        optional(&["--result"], "result", Kind::Store(Convert::Str), false),
        optional(
            &["--timeout"],
            "timeout",
            Kind::Store(Convert::Float),
            false,
        ),
        optional(&["--grace"], "grace", Kind::Store(Convert::Float), false),
        optional(&["--nonce"], "nonce", Kind::Store(Convert::Str), true),
        COMMAND,
    ],
};

/// 旧版 `loop-supervisor.py:123-128`。
pub const LOOP_SUPERVISOR: Parser = Parser {
    prog: "loop-supervisor.py",
    actions: &[
        HELP,
        optional(&["--result"], "result", Kind::Store(Convert::Str), true),
        optional(&["--nonce"], "nonce", Kind::Store(Convert::Str), true),
        optional(&["--timeout"], "timeout", Kind::Store(Convert::Float), true),
        optional(&["--grace"], "grace", Kind::Store(Convert::Float), true),
        COMMAND,
    ],
};

/// 幅などの、引数のほかに結果を変える値。
#[derive(Clone, Copy, Debug)]
pub struct Context<'a> {
    /// `HelpFormatter` の幅(`shutil.get_terminal_size().columns - 2`)。
    pub width: i64,
    /// `ArgumentParser(description=__doc__)`(`PYTHONOPTIMIZE` が 2 以上なら None)。
    pub description: Option<&'a str>,
    /// `int()` の桁の上限(`sys.get_int_max_str_digits()`。0 は上限なし)。
    pub max_str_digits: usize,
}

/// `parse_args` の結果。
#[derive(Clone, Debug)]
pub enum Outcome {
    /// `vars(args)`(`add_argument` の順。`help` は無い)。
    Parsed(Vec<(&'static str, PyValue)>),
    /// `-h`:標準出力へ書く文と終了 0。
    Help(Vec<u32>),
    /// 誤り:標準エラーへ書く文(usage と `<prog>: error: …`)と終了 2。
    Error(Vec<u32>),
}

/// `shutil.get_terminal_size().columns`。`COLUMNS` を `int()` で読み、0 以下か読めなければ標準出力の端末の幅
/// (`stdout_columns`。端末でなければ None で、80)。
pub fn terminal_columns(
    columns_env: Option<&[u32]>,
    stdout_columns: Option<u16>,
    max_str_digits: usize,
) -> i64 {
    const LIMIT: i64 = 1_000_000_000_000;
    let from_env = columns_env
        .and_then(|text| number::int_from_str(text, max_str_digits).ok())
        .map(|digits| {
            let (negative, magnitude) = match digits.strip_prefix('-') {
                Some(rest) => (true, rest.to_owned()),
                None => (false, digits),
            };
            let value = if magnitude.len() > 13 {
                LIMIT
            } else {
                magnitude.parse::<i64>().unwrap_or(LIMIT).min(LIMIT)
            };
            if negative { -value } else { value }
        })
        .unwrap_or(0);
    if from_env > 0 {
        return from_env;
    }
    // 3.10.11 は端末の幅が 0 でも 0 を使う(`size.columns or fallback` ではない)。端末でなければ 80。
    stdout_columns.map_or(80, i64::from)
}

fn chars(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn starts_with(text: &[u32], prefix: &[u32]) -> bool {
    text.len() >= prefix.len() && text[..prefix.len()] == *prefix
}

const DASH: u32 = b'-' as u32;
const EQUALS: u32 = b'=' as u32;
const SPACE: u32 = b' ' as u32;

/// `_negative_number_matcher`(`^-\d+$|^-\d*\.\d+$`。`\d` は Unicode の 10 進数字、`$` は最後の改行の前も)。
fn looks_negative(text: &[u32]) -> bool {
    let body = match text.strip_suffix(&[u32::from(b'\n')]) {
        Some(body) => body,
        None => text,
    };
    let candidates: &[&[u32]] = if body.len() == text.len() {
        &[text]
    } else {
        &[text, body]
    };
    candidates.iter().any(|candidate| {
        let Some(rest) = candidate.strip_prefix(&[DASH]) else {
            return false;
        };
        let digit = |code: &u32| super::decimal(*code).is_some();
        if !rest.is_empty() && rest.iter().all(digit) {
            return true;
        }
        match rest.iter().position(|&code| code == u32::from(b'.')) {
            Some(dot) => {
                rest[..dot].iter().all(digit)
                    && dot + 1 < rest.len()
                    && rest[dot + 1..].iter().all(digit)
            }
            None => false,
        }
    })
}

/// `_parse_optional` の結果の 1 つ(選択肢の番号・字面・`=` の後の値)。番号が None は知らない選択肢。
#[derive(Clone, Debug)]
struct OptionTuple {
    action: Option<usize>,
    option: Vec<u32>,
    explicit: Option<Vec<u32>>,
}

struct Engine<'a> {
    parser: &'a Parser,
    context: &'a Context<'a>,
    /// `_option_string_actions` の順(選択肢の字面と番号)。
    strings: Vec<(Vec<u32>, usize)>,
}

enum Stop {
    Help,
    Error(Vec<u32>),
}

impl Engine<'_> {
    fn find(&self, option: &[u32]) -> Option<usize> {
        self.strings
            .iter()
            .find(|(text, _)| text == option)
            .map(|(_, index)| *index)
    }

    fn name(&self, index: usize) -> String {
        let action = &self.parser.actions[index];
        if action.options.is_empty() {
            action.dest.to_owned()
        } else {
            action.options.join("/")
        }
    }

    fn argument_error(&self, index: usize, message: &str) -> Stop {
        self.error(&chars(&format!("argument {}: {message}", self.name(index))))
    }

    fn error(&self, message: &[u32]) -> Stop {
        let mut out = chars(&format_usage(self.parser, self.context.width));
        out.extend(chars(&format!("{}: error: ", self.parser.prog)));
        out.extend_from_slice(message);
        out.push(u32::from(b'\n'));
        Stop::Error(out)
    }

    fn option_tuples(&self, text: &[u32]) -> Vec<OptionTuple> {
        let mut result = Vec::new();
        if text[1] == DASH {
            let (prefix, explicit) = match text.iter().position(|&code| code == EQUALS) {
                Some(at) => (&text[..at], Some(text[at + 1..].to_vec())),
                None => (text, None),
            };
            for (option, index) in &self.strings {
                if starts_with(option, prefix) {
                    result.push(OptionTuple {
                        action: Some(*index),
                        option: option.clone(),
                        explicit: explicit.clone(),
                    });
                }
            }
        } else {
            let short = &text[..2];
            let short_explicit = text[2..].to_vec();
            for (option, index) in &self.strings {
                if option == short {
                    result.push(OptionTuple {
                        action: Some(*index),
                        option: option.clone(),
                        explicit: Some(short_explicit.clone()),
                    });
                } else if starts_with(option, text) {
                    result.push(OptionTuple {
                        action: Some(*index),
                        option: option.clone(),
                        explicit: None,
                    });
                }
            }
        }
        result
    }

    /// `_parse_optional`。位置引数なら Ok(None)。曖昧な省略形は誤り。
    fn parse_optional(&self, text: &[u32]) -> Result<Option<OptionTuple>, Stop> {
        if text.is_empty() || text[0] != DASH {
            return Ok(None);
        }
        if let Some(index) = self.find(text) {
            return Ok(Some(OptionTuple {
                action: Some(index),
                option: text.to_vec(),
                explicit: None,
            }));
        }
        if text.len() == 1 {
            return Ok(None);
        }
        if let Some(at) = text.iter().position(|&code| code == EQUALS)
            && let Some(index) = self.find(&text[..at])
        {
            return Ok(Some(OptionTuple {
                action: Some(index),
                option: text[..at].to_vec(),
                explicit: Some(text[at + 1..].to_vec()),
            }));
        }
        let tuples = self.option_tuples(text);
        if tuples.len() > 1 {
            let matches: Vec<String> = tuples
                .iter()
                .map(|tuple| value::text(&tuple.option))
                .collect();
            let mut message = chars("ambiguous option: ");
            message.extend_from_slice(text);
            message.extend(chars(&format!(" could match {}", matches.join(", "))));
            return Err(self.error(&message));
        }
        if let Some(tuple) = tuples.into_iter().next() {
            return Ok(Some(tuple));
        }
        if looks_negative(text) {
            return Ok(None);
        }
        if text.contains(&SPACE) {
            return Ok(None);
        }
        Ok(Some(OptionTuple {
            action: None,
            option: text.to_vec(),
            explicit: None,
        }))
    }

    /// `_get_value`。
    fn convert(&self, index: usize, text: &[u32]) -> Result<PyValue, Stop> {
        let kind = self.parser.actions[index].kind;
        let convert = match kind {
            Kind::Store(convert) => convert,
            _ => Convert::Str,
        };
        let failure = |type_name: &str| {
            let mut message = chars(&format!("invalid {type_name} value: "));
            message.extend(value::str_repr(text));
            let mut full = chars(&format!("argument {}: ", self.name(index)));
            full.extend(message);
            self.error(&full)
        };
        match convert {
            Convert::Str => Ok(PyValue::Str(text.to_vec())),
            Convert::Int => number::int_from_str(text, self.context.max_str_digits)
                .map(PyValue::Int)
                .map_err(|_| failure("int")),
            Convert::Float => number::float_from_str(text)
                .map(PyValue::Float)
                .map_err(|_| failure("float")),
        }
    }
}

/// `parser.parse_args(args)`。
pub fn parse(parser: &Parser, args: &[Vec<u32>], context: &Context<'_>) -> Outcome {
    let mut strings = Vec::new();
    for (index, action) in parser.actions.iter().enumerate() {
        for option in action.options {
            strings.push((chars(option), index));
        }
    }
    let engine = Engine {
        parser,
        context,
        strings,
    };
    match run(&engine, args) {
        Ok(namespace) => Outcome::Parsed(namespace),
        Err(Stop::Help) => Outcome::Help(chars(&format_help(
            parser,
            context.width,
            context.description,
        ))),
        Err(Stop::Error(text)) => Outcome::Error(text),
    }
}

/// 解釈の途中の状態(`_parse_known_args` の局所の値)。
struct State<'e> {
    engine: &'e Engine<'e>,
    args: &'e [Vec<u32>],
    namespace: Vec<(&'static str, PyValue)>,
    seen: Vec<bool>,
    /// 位置引数(REMAINDER)を消費したか(`positionals[:] = …`)。
    positional_done: bool,
}

impl State<'_> {
    /// `take_action`(`_get_values` と、選択肢ごとの動作)。
    fn take(&mut self, index: usize, strings: &[Vec<u32>]) -> Result<(), Stop> {
        self.seen[index] = true;
        let action = &self.engine.parser.actions[index];
        let mut strings: Vec<Vec<u32>> = strings.to_vec();
        if action.kind != Kind::Remainder
            && let Some(at) = strings.iter().position(|text| *text == [DASH, DASH])
        {
            strings.remove(at);
        }
        let values = match action.kind {
            Kind::Remainder => PyValue::List(strings.into_iter().map(PyValue::Str).collect()),
            Kind::Store(_) if strings.len() == 1 => self.engine.convert(index, &strings[0])?,
            _ => {
                let mut items = Vec::new();
                for text in &strings {
                    items.push(self.engine.convert(index, text)?);
                }
                PyValue::List(items)
            }
        };
        let value = match action.kind {
            Kind::Help => return Err(Stop::Help),
            Kind::StoreTrue => PyValue::Bool(true),
            Kind::Store(_) | Kind::Remainder => values,
        };
        if let Some(entry) = self
            .namespace
            .iter_mut()
            .find(|(dest, _)| *dest == action.dest)
        {
            entry.1 = value;
        }
        Ok(())
    }

    /// `consume_positionals`(REMAINDER の位置引数は、残りの全部を取る)。
    fn consume_positionals(&mut self, start: usize) -> Result<usize, Stop> {
        let positional = self
            .engine
            .parser
            .actions
            .iter()
            .position(|action| action.options.is_empty());
        match positional {
            Some(index) if !self.positional_done => {
                self.positional_done = true;
                let args = self.args;
                self.take(index, &args[start..])?;
                Ok(args.len())
            }
            _ => Ok(start),
        }
    }
}

fn ignored_explicit(engine: &Engine<'_>, action: usize, explicit: &[u32]) -> Stop {
    let mut message = chars("ignored explicit argument ");
    message.extend(value::str_repr(explicit));
    engine.argument_error(action, &value::text(&message))
}

fn run(engine: &Engine<'_>, args: &[Vec<u32>]) -> Result<Vec<(&'static str, PyValue)>, Stop> {
    let parser = engine.parser;
    // 既定値(help は SUPPRESS なので入れない)。
    let namespace: Vec<(&'static str, PyValue)> = parser
        .actions
        .iter()
        .filter(|action| action.kind != Kind::Help)
        .map(|action| {
            let default = if action.kind == Kind::StoreTrue {
                PyValue::Bool(false)
            } else {
                PyValue::None
            };
            (action.dest, default)
        })
        .collect();
    // 字面の種類の並び(O・A・-)と、選択肢の位置。
    let mut pattern: Vec<u8> = Vec::with_capacity(args.len());
    let mut options: Vec<(usize, OptionTuple)> = Vec::new();
    for (index, text) in args.iter().enumerate() {
        if *text == [DASH, DASH] {
            pattern.push(b'-');
            pattern.resize(args.len(), b'A');
            break;
        }
        match engine.parse_optional(text)? {
            None => pattern.push(b'A'),
            Some(tuple) => {
                options.push((index, tuple));
                pattern.push(b'O');
            }
        }
    }
    let mut state = State {
        engine,
        args,
        namespace,
        seen: vec![false; parser.actions.len()],
        positional_done: false,
    };
    let mut extras: Vec<Vec<u32>> = Vec::new();
    let option_at = |at: usize| {
        options
            .iter()
            .find(|(index, _)| *index == at)
            .map(|(_, tuple)| tuple.clone())
    };
    let mut start = 0usize;
    let max_option = options.iter().map(|(index, _)| *index).max();
    while let Some(max) = max_option
        && start <= max
    {
        let next = options
            .iter()
            .map(|(index, _)| *index)
            .filter(|&index| index >= start)
            .min()
            .expect("max 以下の選択肢がある");
        if start != next {
            let end = state.consume_positionals(start)?;
            if end > start {
                start = end;
                continue;
            }
            start = end;
        }
        if option_at(start).is_none() {
            extras.extend(args[start..next].iter().cloned());
            start = next;
        }
        // consume_optional
        let OptionTuple {
            action,
            mut option,
            mut explicit,
        } = option_at(start).expect("選択肢の位置");
        let Some(mut action) = action else {
            extras.push(args[start].clone());
            start += 1;
            continue;
        };
        let mut queue: Vec<(usize, Vec<Vec<u32>>)> = Vec::new();
        let stop = loop {
            let takes_value = matches!(parser.actions[action].kind, Kind::Store(_));
            match explicit.take() {
                Some(explicit_value) => {
                    if !takes_value && option[1] != DASH && !explicit_value.is_empty() {
                        queue.push((action, Vec::new()));
                        let short = vec![option[0], explicit_value[0]];
                        let rest = explicit_value[1..].to_vec();
                        match engine.find(&short) {
                            Some(found) => {
                                action = found;
                                option = short;
                                explicit = if rest.is_empty() { None } else { Some(rest) };
                            }
                            None => return Err(ignored_explicit(engine, action, &explicit_value)),
                        }
                    } else if takes_value {
                        queue.push((action, vec![explicit_value]));
                        break start + 1;
                    } else {
                        return Err(ignored_explicit(engine, action, &explicit_value));
                    }
                }
                None => {
                    let first = start + 1;
                    let count = if !takes_value {
                        0
                    } else if pattern.get(first) == Some(&b'A') {
                        1
                    } else {
                        return Err(engine.argument_error(action, "expected one argument"));
                    };
                    queue.push((action, args[first..first + count].to_vec()));
                    break first + count;
                }
            }
        };
        for (index, strings) in queue {
            state.take(index, &strings)?;
        }
        start = stop;
    }
    let stop = state.consume_positionals(start)?;
    extras.extend(args[stop..].iter().cloned());
    let required: Vec<String> = parser
        .actions
        .iter()
        .enumerate()
        .filter(|(index, action)| !state.seen[*index] && action.required)
        .map(|(index, _)| engine.name(index))
        .collect();
    if !required.is_empty() {
        return Err(engine.error(&chars(&format!(
            "the following arguments are required: {}",
            required.join(", ")
        ))));
    }
    if !extras.is_empty() {
        let mut message = chars("unrecognized arguments: ");
        for (index, text) in extras.iter().enumerate() {
            if index > 0 {
                message.push(SPACE);
            }
            message.extend_from_slice(text);
        }
        return Err(engine.error(&message));
    }
    Ok(state.namespace)
}

fn metavar(action: &Action) -> String {
    action.dest.to_uppercase()
}

/// `_format_action_invocation`。
fn invocation(action: &Action) -> String {
    match action.kind {
        Kind::Remainder => action.dest.to_owned(),
        Kind::Help | Kind::StoreTrue => action.options.join(", "),
        Kind::Store(_) => action
            .options
            .iter()
            .map(|option| format!("{option} {}", metavar(action)))
            .collect::<Vec<_>>()
            .join(", "),
    }
}

/// `_format_actions_usage` の 1 つ分を、折り返しの部品(`part_regexp` の findall)に分けたもの。
fn usage_parts(action: &Action) -> Vec<String> {
    let part = match action.kind {
        Kind::Remainder => return vec!["...".to_owned()],
        Kind::Help | Kind::StoreTrue => action.options[0].to_owned(),
        Kind::Store(_) => format!("{} {}", action.options[0], metavar(action)),
    };
    if action.required {
        part.split(' ').map(str::to_owned).collect()
    } else {
        vec![format!("[{part}]")]
    }
}

fn length(text: &str) -> i64 {
    text.chars().count() as i64
}

/// `HelpFormatter._format_usage`(`usage: ` から 2 個の改行まで)。
fn usage_block(parser: &Parser, width: i64) -> String {
    let prefix = "usage: ";
    let prog = parser.prog;
    let optionals: Vec<&Action> = parser
        .actions
        .iter()
        .filter(|a| !a.options.is_empty())
        .collect();
    let positionals: Vec<&Action> = parser
        .actions
        .iter()
        .filter(|a| a.options.is_empty())
        .collect();
    let opt_parts: Vec<String> = optionals
        .iter()
        .flat_map(|action| usage_parts(action))
        .collect();
    let pos_parts: Vec<String> = positionals
        .iter()
        .flat_map(|action| usage_parts(action))
        .collect();
    let action_usage: Vec<String> = opt_parts.iter().chain(&pos_parts).cloned().collect();
    let mut usage = format!("{prog} {}", action_usage.join(" "));
    let text_width = width;
    if length(prefix) + length(&usage) > text_width {
        let get_lines = |parts: &[String], indent: &str, prefix: Option<&str>| -> Vec<String> {
            let mut lines = Vec::new();
            let mut line: Vec<&str> = Vec::new();
            let mut line_len = match prefix {
                Some(prefix) => length(prefix) - 1,
                None => length(indent) - 1,
            };
            for part in parts {
                if line_len + 1 + length(part) > text_width && !line.is_empty() {
                    lines.push(format!("{indent}{}", line.join(" ")));
                    line.clear();
                    line_len = length(indent) - 1;
                }
                line.push(part);
                line_len += length(part) + 1;
            }
            if !line.is_empty() {
                lines.push(format!("{indent}{}", line.join(" ")));
            }
            if prefix.is_some() && !lines.is_empty() {
                lines[0] = lines[0][indent.len()..].to_owned();
            }
            lines
        };
        let lines: Vec<String> = if 4 * (length(prefix) + length(prog)) <= 3 * text_width {
            let indent = " ".repeat((length(prefix) + length(prog) + 1) as usize);
            let mut first: Vec<String> = vec![prog.to_owned()];
            if !opt_parts.is_empty() {
                first.extend(opt_parts.iter().cloned());
                let mut lines = get_lines(&first, &indent, Some(prefix));
                lines.extend(get_lines(&pos_parts, &indent, None));
                lines
            } else if !pos_parts.is_empty() {
                first.extend(pos_parts.iter().cloned());
                get_lines(&first, &indent, Some(prefix))
            } else {
                vec![prog.to_owned()]
            }
        } else {
            let indent = " ".repeat(length(prefix) as usize);
            let parts: Vec<String> = opt_parts.iter().chain(&pos_parts).cloned().collect();
            let mut lines = get_lines(&parts, &indent, None);
            if lines.len() > 1 {
                lines = get_lines(&opt_parts, &indent, None);
                lines.extend(get_lines(&pos_parts, &indent, None));
            }
            let mut all = vec![prog.to_owned()];
            all.extend(lines);
            all
        };
        usage = lines.join("\n");
    }
    format!("{prefix}{usage}\n\n")
}

/// `str.strip() == ''`(Python の空白だけか)。
fn blank(chunk: &[char]) -> bool {
    chunk.iter().all(|&c| {
        let code = u32::from(c);
        (code < 128 && (number::ascii_space(code as u8) || (0x1c..=0x1f).contains(&code)))
            || number::unicode_space_above_ascii(code)
    })
}

/// `textwrap.wrap(text, width)`(text は空白 1 つで区切った、`-` と tab を含まない文。`HelpFormatter` が
/// 空白をまとめた後の文がこの形になる)。
fn wrap(text: &str, width: i64) -> Vec<String> {
    debug_assert!(!text.contains('-') && !text.contains('\t'));
    let mut chunks: Vec<Vec<char>> = Vec::new();
    for (index, word) in text.split(' ').enumerate() {
        if index > 0 {
            chunks.push(vec![' ']);
        }
        if !word.is_empty() {
            chunks.push(word.chars().collect());
        }
    }
    chunks.reverse();
    let mut lines: Vec<String> = Vec::new();
    while !chunks.is_empty() {
        let mut line: Vec<Vec<char>> = Vec::new();
        let mut line_len: i64 = 0;
        if !lines.is_empty() && chunks.last().is_some_and(|chunk| blank(chunk)) {
            chunks.pop();
        }
        while let Some(chunk) = chunks.last() {
            let size = chunk.len() as i64;
            if line_len + size <= width {
                line_len += size;
                line.push(chunks.pop().expect("最後の部品"));
            } else {
                break;
            }
        }
        if let Some(chunk) = chunks.last()
            && chunk.len() as i64 > width
        {
            let space_left = if width < 1 { 1 } else { width - line_len };
            let end = (space_left.max(0) as usize).min(chunk.len());
            let head = chunk[..end].to_vec();
            let tail = chunk[end..].to_vec();
            line.push(head);
            *chunks.last_mut().expect("最後の部品") = tail;
        }
        if line.last().is_some_and(|chunk| blank(chunk)) {
            line.pop();
        }
        if !line.is_empty() {
            lines.push(line.concat().into_iter().collect());
        }
    }
    lines
}

/// `_whitespace_matcher.sub(' ', text).strip()`(ASCII の空白の並びを 1 つの空白にする)。
fn normalize(text: &str) -> String {
    let mut out = String::new();
    let mut space = false;
    for c in text.chars() {
        if c.is_ascii() && number::ascii_space(c as u8) {
            space = true;
        } else {
            if space && !out.is_empty() {
                out.push(' ');
            }
            space = false;
            out.push(c);
        }
    }
    let python_space = |c: char| blank(&[c]);
    out.trim_matches(python_space).to_owned()
}

/// `_format_action`(位置は `current_indent` = 2)。
fn action_block(action: &Action, width: i64, help_position: i64) -> String {
    let indent = 2usize;
    let help_width = (width - help_position).max(11);
    let action_width = help_position - indent as i64 - 2;
    let header = invocation(action);
    let mut out = String::new();
    let pad = |n: i64| " ".repeat(n.max(0) as usize);
    match action.help {
        None => {
            out.push_str(&format!("{}{header}\n", pad(indent as i64)));
        }
        Some(help) => {
            let indent_first = if length(&header) <= action_width {
                let fill = action_width - length(&header);
                out.push_str(&format!("{}{header}{}  ", pad(indent as i64), pad(fill)));
                0
            } else {
                out.push_str(&format!("{}{header}\n", pad(indent as i64)));
                help_position
            };
            let lines = wrap(&normalize(help), help_width);
            if let Some((first, rest)) = lines.split_first() {
                out.push_str(&format!("{}{first}\n", pad(indent_first)));
                for line in rest {
                    out.push_str(&format!("{}{line}\n", pad(help_position)));
                }
            }
        }
    }
    out
}

/// `long_break_matcher.sub('\n\n', help)` と `help.strip('\n') + '\n'`。
fn finish(text: &str) -> String {
    let mut out = String::new();
    let mut newlines = 0;
    for c in text.chars() {
        if c == '\n' {
            newlines += 1;
            continue;
        }
        if newlines > 0 {
            out.push_str(&"\n".repeat(if newlines >= 3 { 2 } else { newlines }));
            newlines = 0;
        }
        out.push(c);
    }
    if newlines > 0 {
        out.push_str(&"\n".repeat(if newlines >= 3 { 2 } else { newlines }));
    }
    let trimmed = out.trim_matches('\n');
    format!("{trimmed}\n")
}

/// `parser.format_usage()`。
pub fn format_usage(parser: &Parser, width: i64) -> String {
    finish(&format!("\n{}\n", usage_block(parser, width)))
}

/// `parser.format_help()`。
pub fn format_help(parser: &Parser, width: i64, description: Option<&str>) -> String {
    let max_help_position = 24.min((width - 20).max(4));
    let action_max_length = parser
        .actions
        .iter()
        .map(|action| length(&invocation(action)) + 2)
        .max()
        .unwrap_or(0);
    let help_position = (action_max_length + 2).min(max_help_position);
    let mut root = usage_block(parser, width);
    if let Some(description) = description {
        let text_width = width.max(11);
        root.push_str(&wrap(&normalize(description), text_width).join("\n"));
        root.push_str("\n\n");
    }
    let sections: [(&str, Vec<&Action>); 2] = [
        (
            "positional arguments",
            parser
                .actions
                .iter()
                .filter(|a| a.options.is_empty())
                .collect(),
        ),
        (
            "options",
            parser
                .actions
                .iter()
                .filter(|a| !a.options.is_empty())
                .collect(),
        ),
    ];
    for (title, actions) in sections {
        let items: String = actions
            .iter()
            .map(|action| action_block(action, width, help_position))
            .collect();
        if !items.is_empty() {
            root.push_str(&format!("\n{title}:\n{items}\n"));
        }
    }
    finish(&format!("\n{root}\n"))
}
