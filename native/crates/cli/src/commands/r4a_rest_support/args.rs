//! Python 3.10 `argparse` の、R4A 5 本が使う範囲の移植。
//!
//! 対応する範囲: 値 0 個・1 個の optional(`-h/--help`・store・store_true)と、
//! 0〜1 個の位置引数(`nargs="?"`)、省略形(`allow_abbrev=True`)。
//! 文字は文字番号の列(`PyStr`)で扱い、`argparse.py` の分類順・エラーの順序をそのまま写す。

use super::chars::{self, IntParse, PyStr};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NArgs {
    /// 値 0 個。
    Zero,
    /// `nargs=None`(値 1 個)。
    One,
    /// `nargs="?"`。
    Optional,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Kind {
    Help,
    Store,
    StoreTrue,
}

#[derive(Clone, Debug)]
pub struct Action {
    pub option_strings: &'static [&'static str],
    pub dest: &'static str,
    pub nargs: NArgs,
    pub kind: Kind,
    pub help: Option<&'static str>,
}

impl Action {
    fn is_positional(&self) -> bool {
        self.option_strings.is_empty()
    }
}

/// 解析後の値。`List` は `=--` で `--` が除かれた空の list を表す。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Value {
    None,
    Str(PyStr),
    List(Vec<PyStr>),
    Bool(bool),
}

#[derive(Clone, Debug)]
pub struct Parser {
    pub prog: &'static str,
    pub description: Option<&'static str>,
    pub actions: Vec<Action>,
}

/// 幅の決定に使う環境。`terminal` は stdout の端末の (列, 行)。取れなければ `None`。
#[derive(Clone, Debug, Default)]
pub struct Env {
    pub columns: Option<PyStr>,
    pub lines: Option<PyStr>,
    pub terminal: Option<(i64, i64)>,
    /// `PYTHONINTMAXSTRDIGITS` の実効値(既定 4300・0 は無制限)。
    pub int_max_digits: usize,
    /// `PYTHONOPTIMIZE` の水準。2 以上では docstring が消え、`description=__doc__` が `None` になる。
    pub optimize: i32,
}

/// 解析が終わらずに終了する結果。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Exit {
    /// `--help`: stdout へ 1 回書いて終了 0。
    Help { text: PyStr },
    /// エラー: stderr へ usage と本文を別々に書いて終了 2。
    Error {
        usage: PyStr,
        message: PyStr,
        origin: ErrorOrigin,
        /// `ArgumentError` を捕まえて `error()` へ進んだときの、元の例外の本文と投げた位置。
        cause: Option<(PyStr, CauseSite)>,
    },
}

/// `ArgumentError` を投げた `argparse.py` の位置(例外の連鎖の表示に使う)。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CauseSite {
    /// `_match_argument` の `expected one argument`。
    ExpectedOne,
    /// 短縮名の束ねの途中の `ignored explicit argument`。
    ExplicitBundled,
    /// 値を取らない長い名前への `ignored explicit argument`。
    ExplicitLong,
}

/// `error()` を呼んだ `argparse.py` の位置(呼出履歴の差を決める)。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorOrigin {
    /// `parse_args` の `unrecognized arguments`。
    Extra,
    /// `parse_known_args` が捕まえた `ArgumentError`。
    Argument,
    /// `_parse_optional` の `ambiguous option`。
    Ambiguous,
}

pub type Namespace = Vec<(&'static str, Value)>;

fn lit(text: &str) -> PyStr {
    chars::from_str(text)
}

fn starts_with(text: &[u32], prefix: &[u32]) -> bool {
    text.len() >= prefix.len() && &text[..prefix.len()] == prefix
}

/// `^-\d+$|^-\d*\.\d+$`(`\d` は Unicode の 10 進数字・`$` は末尾の LF の前にも一致)。
fn negative_number_like(text: &[u32]) -> bool {
    let mut end = text.len();
    if end > 0 && text[end - 1] == 10 {
        // `$` は最後の LF の直前で一致できる。LF なしの末尾でも一致する(下で両方を試す)。
        if negative_number_exact(&text[..end - 1]) {
            return true;
        }
    }
    end = text.len();
    negative_number_exact(&text[..end])
}

fn negative_number_exact(text: &[u32]) -> bool {
    if text.first() != Some(&45) {
        return false;
    }
    let rest = &text[1..];
    let digits = |s: &[u32]| s.iter().all(|&c| chars::decimal_value(c).is_some());
    if !rest.is_empty() && digits(rest) {
        return true;
    }
    match rest.iter().position(|&c| c == 46) {
        Some(dot) => digits(&rest[..dot]) && dot + 1 < rest.len() && digits(&rest[dot + 1..]),
        None => false,
    }
}

#[derive(Clone)]
struct OptionTuple {
    /// `actions` の添字。`None` は未知の optional。
    action: Option<usize>,
    option_string: PyStr,
    explicit_arg: Option<PyStr>,
}

struct Problem(Exit);

impl Parser {
    fn option_actions(&self) -> Vec<(PyStr, usize)> {
        let mut out = Vec::new();
        for (at, action) in self.actions.iter().enumerate() {
            for name in action.option_strings {
                out.push((lit(name), at));
            }
        }
        out
    }

    fn action_name(&self, at: usize) -> String {
        let action = &self.actions[at];
        if action.is_positional() {
            action.dest.to_owned()
        } else {
            action.option_strings.join("/")
        }
    }

    pub fn width(&self, env: &Env) -> i64 {
        let int = |value: &Option<PyStr>| match value {
            None => 0,
            Some(text) => match chars::parse_int(text, env.int_max_digits) {
                IntParse::Value(v) => {
                    i64::try_from(v).unwrap_or(if v < 0 { i64::MIN / 4 } else { i64::MAX / 4 })
                }
                IntParse::Huge(negative) => {
                    if negative {
                        i64::MIN / 4
                    } else {
                        i64::MAX / 4
                    }
                }
                IntParse::Invalid | IntParse::TooManyDigits => 0,
            },
        };
        let mut columns = int(&env.columns);
        let lines = int(&env.lines);
        if columns <= 0 || lines <= 0 {
            let size = env.terminal.unwrap_or((80, 24));
            if columns <= 0 {
                columns = size.0;
            }
        }
        columns - 2
    }

    fn usage_parts(&self) -> (Vec<String>, Vec<String>) {
        let mut optionals = Vec::new();
        let mut positionals = Vec::new();
        for action in &self.actions {
            if action.is_positional() {
                positionals.push(format!("[{}]", action.dest));
            } else {
                let first = action.option_strings[0];
                optionals.push(match action.nargs {
                    NArgs::Zero => format!("[{first}]"),
                    _ => format!("[{first} {}]", action.dest.to_uppercase()),
                });
            }
        }
        (optionals, positionals)
    }

    /// `format_usage()`。末尾は LF 1 個。
    pub fn format_usage(&self, env: &Env) -> PyStr {
        lit(&self.usage_body(self.width(env)))
    }

    fn usage_body(&self, width: i64) -> String {
        let prefix = "usage: ";
        let prog = self.prog;
        let (optionals, positionals) = self.usage_parts();
        let all: Vec<String> = optionals
            .iter()
            .chain(positionals.iter())
            .cloned()
            .collect();
        let action_usage = all.join(" ");
        let usage = if action_usage.is_empty() {
            prog.to_owned()
        } else {
            format!("{prog} {action_usage}")
        };
        let text_width = width;
        let mut usage = usage;
        if (prefix.len() + usage.len()) as i64 > text_width {
            let get_lines = |parts: &[String], indent: &str, with_prefix: bool| -> Vec<String> {
                let mut lines: Vec<String> = Vec::new();
                let mut line: Vec<String> = Vec::new();
                let mut line_len = if with_prefix {
                    prefix.len() as i64 - 1
                } else {
                    indent.len() as i64 - 1
                };
                for part in parts {
                    if line_len + 1 + part.len() as i64 > text_width && !line.is_empty() {
                        lines.push(format!("{indent}{}", line.join(" ")));
                        line.clear();
                        line_len = indent.len() as i64 - 1;
                    }
                    line.push(part.clone());
                    line_len += part.len() as i64 + 1;
                }
                if !line.is_empty() {
                    lines.push(format!("{indent}{}", line.join(" ")));
                }
                if with_prefix {
                    lines[0] = lines[0][indent.len()..].to_owned();
                }
                lines
            };
            let lines: Vec<String> =
                if (prefix.len() + prog.len()) as f64 <= 0.75 * text_width as f64 {
                    let indent = " ".repeat(prefix.len() + prog.len() + 1);
                    let mut collected;
                    if !optionals.is_empty() {
                        let mut first = vec![prog.to_owned()];
                        first.extend(optionals.iter().cloned());
                        collected = get_lines(&first, &indent, true);
                        collected.extend(get_lines(&positionals, &indent, false));
                    } else if !positionals.is_empty() {
                        let mut first = vec![prog.to_owned()];
                        first.extend(positionals.iter().cloned());
                        collected = get_lines(&first, &indent, true);
                    } else {
                        collected = vec![prog.to_owned()];
                    }
                    collected
                } else {
                    let indent = " ".repeat(prefix.len());
                    let mut parts = optionals.clone();
                    parts.extend(positionals.iter().cloned());
                    let mut collected = get_lines(&parts, &indent, false);
                    if collected.len() > 1 {
                        collected = get_lines(&optionals, &indent, false);
                        collected.extend(get_lines(&positionals, &indent, false));
                    }
                    let mut with_prog = vec![prog.to_owned()];
                    with_prog.extend(collected);
                    with_prog
                };
            usage = lines.join("\n");
        }
        format!("{prefix}{usage}\n")
    }

    /// `format_help()`(説明と options の節を持つ parser だけを対象にする)。
    pub fn format_help(&self, env: &Env) -> PyStr {
        let width = self.width(env);
        let mut out = self.usage_body(width);
        out.push('\n');
        if let Some(description) = self.description {
            let wrap_width = width.max(11) as usize;
            for line in wrap(&collapse_ascii_space(description), wrap_width) {
                out.push_str(&line);
                out.push('\n');
            }
            out.push('\n');
        }
        let max_help_position = 24.min((width - 20).max(4));
        let invocations: Vec<(String, Option<&'static str>)> = self
            .actions
            .iter()
            .filter(|action| !action.is_positional())
            .map(|action| {
                let name = match action.nargs {
                    NArgs::Zero => action.option_strings.join(", "),
                    _ => action
                        .option_strings
                        .iter()
                        .map(|s| format!("{s} {}", action.dest.to_uppercase()))
                        .collect::<Vec<_>>()
                        .join(", "),
                };
                (name, action.help)
            })
            .collect();
        let action_max = invocations
            .iter()
            .map(|(n, _)| n.len() + 2)
            .max()
            .unwrap_or(0) as i64;
        let help_position = (action_max + 2).min(max_help_position);
        let help_width = (width - help_position).max(11) as usize;
        let action_width = help_position - 4;
        out.push_str("options:\n");
        for (name, help) in invocations {
            let help_lines = help
                .map(|h| wrap(&collapse_ascii_space(h), help_width))
                .unwrap_or_default();
            if help_lines.is_empty() {
                out.push_str(&format!("  {name}\n"));
                continue;
            }
            let mut rest = &help_lines[..];
            if name.chars().count() as i64 <= action_width {
                out.push_str(&format!(
                    "  {name:<w$}  {}\n",
                    help_lines[0],
                    w = action_width as usize
                ));
                rest = &help_lines[1..];
            } else {
                out.push_str(&format!("  {name}\n"));
            }
            for line in rest {
                out.push_str(&format!("{}{line}\n", " ".repeat(help_position as usize)));
            }
        }
        lit(&out)
    }

    fn error(
        &self,
        env: &Env,
        message: PyStr,
        origin: ErrorOrigin,
        cause: Option<(PyStr, CauseSite)>,
    ) -> Problem {
        let mut text = lit(self.prog);
        text.extend(lit(": error: "));
        text.extend(message);
        text.push(10);
        Problem(Exit::Error {
            usage: self.format_usage(env),
            message: text,
            origin,
            cause,
        })
    }

    fn argument_error(&self, env: &Env, at: usize, message: PyStr, site: CauseSite) -> Problem {
        let mut text = lit(&format!("argument {}: ", self.action_name(at)));
        text.extend(message);
        self.error(env, text.clone(), ErrorOrigin::Argument, Some((text, site)))
    }

    fn parse_optional(
        &self,
        env: &Env,
        map: &[(PyStr, usize)],
        arg: &[u32],
    ) -> Result<Option<OptionTuple>, Problem> {
        if arg.is_empty() || arg[0] != 45 {
            return Ok(None);
        }
        if let Some((_, at)) = map.iter().find(|(name, _)| name == arg) {
            return Ok(Some(OptionTuple {
                action: Some(*at),
                option_string: arg.to_vec(),
                explicit_arg: None,
            }));
        }
        if arg.len() == 1 {
            return Ok(None);
        }
        if let Some(eq) = arg.iter().position(|&c| c == 61) {
            let (name, explicit) = (&arg[..eq], &arg[eq + 1..]);
            if let Some((_, at)) = map.iter().find(|(n, _)| n == name) {
                return Ok(Some(OptionTuple {
                    action: Some(*at),
                    option_string: name.to_vec(),
                    explicit_arg: Some(explicit.to_vec()),
                }));
            }
        }
        let tuples = self.option_tuples(map, arg);
        if tuples.len() > 1 {
            let matches: Vec<String> = tuples
                .iter()
                .map(|t| chars_to_string(&t.option_string))
                .collect();
            let mut message = lit("ambiguous option: ");
            message.extend_from_slice(arg);
            message.extend(lit(&format!(" could match {}", matches.join(", "))));
            return Err(self.error(env, message, ErrorOrigin::Ambiguous, None));
        }
        if tuples.len() == 1 {
            return Ok(tuples.into_iter().next());
        }
        if negative_number_like(arg) {
            return Ok(None);
        }
        if arg.contains(&32) {
            return Ok(None);
        }
        Ok(Some(OptionTuple {
            action: None,
            option_string: arg.to_vec(),
            explicit_arg: None,
        }))
    }

    fn option_tuples(&self, map: &[(PyStr, usize)], arg: &[u32]) -> Vec<OptionTuple> {
        let mut result = Vec::new();
        if arg[0] == 45 && arg.get(1) == Some(&45) {
            let (prefix, explicit) = match arg.iter().position(|&c| c == 61) {
                Some(eq) => (&arg[..eq], Some(arg[eq + 1..].to_vec())),
                None => (arg, None),
            };
            for (name, at) in map {
                if starts_with(name, prefix) {
                    result.push(OptionTuple {
                        action: Some(*at),
                        option_string: name.clone(),
                        explicit_arg: explicit.clone(),
                    });
                }
            }
        } else if arg[0] == 45 && arg.get(1) != Some(&45) {
            let short_prefix = &arg[..2];
            let short_explicit = arg[2..].to_vec();
            for (name, at) in map {
                if name == short_prefix {
                    result.push(OptionTuple {
                        action: Some(*at),
                        option_string: name.clone(),
                        explicit_arg: Some(short_explicit.clone()),
                    });
                } else if starts_with(name, arg) {
                    result.push(OptionTuple {
                        action: Some(*at),
                        option_string: name.clone(),
                        explicit_arg: None,
                    });
                }
            }
        }
        result
    }

    /// `parse_args()`。余剰の引数は `unrecognized arguments` のエラーにする。
    pub fn parse(&self, env: &Env, argv: &[PyStr]) -> Result<Namespace, Exit> {
        self.parse_inner(env, argv).map_err(|Problem(exit)| exit)
    }

    fn parse_inner(&self, env: &Env, argv: &[PyStr]) -> Result<Namespace, Problem> {
        let map = self.option_actions();
        let mut namespace: Namespace = Vec::new();
        for action in &self.actions {
            if action.kind == Kind::Help {
                continue;
            }
            let default = match action.kind {
                Kind::StoreTrue => Value::Bool(false),
                _ => Value::None,
            };
            namespace.push((action.dest, default));
        }

        // 全 argv の分類('O' = optional・'A' = 引数・'-' = 区切り)。
        let mut indices: Vec<(usize, OptionTuple)> = Vec::new();
        let mut pattern: Vec<u8> = Vec::new();
        let mut at = 0;
        while at < argv.len() {
            if argv[at] == [45, 45] {
                pattern.push(b'-');
                pattern.extend(std::iter::repeat_n(b'A', argv.len() - at - 1));
                break;
            }
            match self.parse_optional(env, &map, &argv[at])? {
                None => pattern.push(b'A'),
                Some(tuple) => {
                    indices.push((at, tuple));
                    pattern.push(b'O');
                }
            }
            at += 1;
        }

        let mut extras: Vec<PyStr> = Vec::new();
        let mut positionals: Vec<usize> = (0..self.actions.len())
            .filter(|&i| self.actions[i].is_positional())
            .collect();
        let max_option_index = indices.last().map(|(i, _)| *i);
        let mut start: usize = 0;

        while let Some(max_index) = max_option_index.filter(|m| start <= *m) {
            let _ = max_index;
            let next_option = indices
                .iter()
                .map(|(i, _)| *i)
                .filter(|&i| i >= start)
                .min()
                .expect("an option index remains");
            if start != next_option {
                let end = self.consume_positionals(
                    env,
                    &mut positionals,
                    &pattern,
                    argv,
                    start,
                    &mut namespace,
                )?;
                if end > start {
                    start = end;
                    continue;
                }
                start = end;
            }
            if !indices.iter().any(|(i, _)| *i == start) {
                extras.extend(argv[start..next_option].iter().cloned());
                start = next_option;
            }
            start = self.consume_optional(
                env,
                &indices,
                &pattern,
                argv,
                start,
                &mut namespace,
                &mut extras,
            )?;
        }
        let stop =
            self.consume_positionals(env, &mut positionals, &pattern, argv, start, &mut namespace)?;
        extras.extend(argv[stop..].iter().cloned());

        if !extras.is_empty() {
            let mut message = lit("unrecognized arguments: ");
            for (n, extra) in extras.iter().enumerate() {
                if n > 0 {
                    message.push(32);
                }
                message.extend_from_slice(extra);
            }
            return Err(self.error(env, message, ErrorOrigin::Extra, None));
        }
        Ok(namespace)
    }

    /// 位置引数の貪欲な一致(`nargs="?"` だけなので `(-*A?-*)`)。
    fn consume_positionals(
        &self,
        env: &Env,
        positionals: &mut Vec<usize>,
        pattern: &[u8],
        argv: &[PyStr],
        start: usize,
        namespace: &mut Namespace,
    ) -> Result<usize, Problem> {
        let mut start = start;
        let selected = &pattern[start.min(pattern.len())..];
        // 先頭から順に、残りの位置引数をできるだけ多く一致させる(`-*A?-*` の連結)。
        let mut counts = Vec::new();
        let mut cursor = 0;
        for _ in 0..positionals.len() {
            let mut end = cursor;
            while selected.get(end) == Some(&b'-') {
                end += 1;
            }
            if selected.get(end) == Some(&b'A') {
                end += 1;
            }
            while selected.get(end) == Some(&b'-') {
                end += 1;
            }
            counts.push(end - cursor);
            cursor = end;
        }
        let used = counts.len();
        for (n, count) in counts.iter().enumerate() {
            let action = positionals[n];
            let mut args: Vec<PyStr> = argv[start..start + count].to_vec();
            start += count;
            self.take_action(env, action, &mut args, namespace)?;
        }
        positionals.drain(..used);
        Ok(start)
    }

    #[allow(clippy::too_many_arguments)]
    fn consume_optional(
        &self,
        env: &Env,
        indices: &[(usize, OptionTuple)],
        pattern: &[u8],
        argv: &[PyStr],
        start_index: usize,
        namespace: &mut Namespace,
        extras: &mut Vec<PyStr>,
    ) -> Result<usize, Problem> {
        let tuple = indices
            .iter()
            .find(|(i, _)| *i == start_index)
            .map(|(_, t)| t.clone())
            .expect("an option tuple exists");
        let mut action = tuple.action;
        let mut option_string = tuple.option_string;
        let mut explicit_arg = tuple.explicit_arg;
        let map = self.option_actions();
        let mut action_tuples: Vec<(usize, Vec<PyStr>)> = Vec::new();
        let stop;
        loop {
            let Some(index) = action else {
                extras.push(argv[start_index].clone());
                return Ok(start_index + 1);
            };
            let spec = &self.actions[index];
            if let Some(explicit) = explicit_arg.clone() {
                let arg_count = match spec.nargs {
                    NArgs::Zero => 0,
                    _ => 1,
                };
                if arg_count == 0 && option_string.get(1) != Some(&45) && !explicit.is_empty() {
                    action_tuples.push((index, Vec::new()));
                    option_string = vec![option_string[0], explicit[0]];
                    let new_explicit = if explicit.len() > 1 {
                        Some(explicit[1..].to_vec())
                    } else {
                        None
                    };
                    if let Some((_, next)) = map.iter().find(|(n, _)| *n == option_string) {
                        action = Some(*next);
                        explicit_arg = new_explicit;
                    } else {
                        let mut message = lit("ignored explicit argument ");
                        message.extend(chars::repr(&explicit));
                        return Err(self.argument_error(
                            env,
                            index,
                            message,
                            CauseSite::ExplicitBundled,
                        ));
                    }
                } else if arg_count == 1 {
                    stop = start_index + 1;
                    action_tuples.push((index, vec![explicit]));
                    break;
                } else {
                    let mut message = lit("ignored explicit argument ");
                    message.extend(chars::repr(&explicit));
                    return Err(self.argument_error(env, index, message, CauseSite::ExplicitLong));
                }
            } else {
                let begin = start_index + 1;
                let selected = &pattern[begin.min(pattern.len())..];
                let arg_count = match spec.nargs {
                    NArgs::Zero => 0,
                    NArgs::One => {
                        if selected.first() == Some(&b'A') {
                            1
                        } else {
                            return Err(self.argument_error(
                                env,
                                index,
                                lit("expected one argument"),
                                CauseSite::ExpectedOne,
                            ));
                        }
                    }
                    NArgs::Optional => usize::from(selected.first() == Some(&b'A')),
                };
                stop = begin + arg_count;
                action_tuples.push((index, argv[begin..stop].to_vec()));
                break;
            }
        }
        for (index, mut args) in action_tuples {
            self.take_action(env, index, &mut args, namespace)?;
        }
        Ok(stop)
    }

    /// `_get_values` と action の実行。
    fn take_action(
        &self,
        env: &Env,
        index: usize,
        args: &mut Vec<PyStr>,
        namespace: &mut Namespace,
    ) -> Result<(), Problem> {
        let action = &self.actions[index];
        if let Some(pos) = args.iter().position(|a| a == &[45, 45]) {
            args.remove(pos);
        }
        match action.kind {
            Kind::Help => Err(Problem(Exit::Help {
                text: self.format_help(env),
            })),
            Kind::StoreTrue => {
                set(namespace, action.dest, Value::Bool(true));
                Ok(())
            }
            Kind::Store => {
                let value = if args.is_empty() && action.nargs == NArgs::Optional {
                    Value::None
                } else if args.len() == 1 {
                    Value::Str(args[0].clone())
                } else {
                    Value::List(args.clone())
                };
                set(namespace, action.dest, value);
                Ok(())
            }
        }
    }
}

fn set(namespace: &mut Namespace, dest: &'static str, value: Value) {
    if let Some(slot) = namespace.iter_mut().find(|(name, _)| *name == dest) {
        slot.1 = value;
    }
}

pub fn get<'a>(namespace: &'a Namespace, dest: &str) -> &'a Value {
    &namespace
        .iter()
        .find(|(name, _)| *name == dest)
        .expect("destination exists")
        .1
}

fn chars_to_string(text: &[u32]) -> String {
    text.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

/// ASCII の空白列(`\s+`・`re.ASCII`)を 1 個の空白にして両端を除く。
fn collapse_ascii_space(text: &str) -> String {
    text.split([' ', '\t', '\n', '\r', '\x0b', '\x0c'])
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// `textwrap.wrap(text, width)`(空白だけを境界にする。固定の説明文だけが対象)。
fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut chunks: Vec<Vec<char>> = Vec::new();
    for (n, word) in text.split(' ').enumerate() {
        if n > 0 {
            chunks.push(vec![' ']);
        }
        if !word.is_empty() {
            chunks.push(word.chars().collect());
        }
    }
    chunks.reverse();
    let mut lines = Vec::new();
    while !chunks.is_empty() {
        let mut current: Vec<Vec<char>> = Vec::new();
        let mut current_len = 0;
        if chunks
            .last()
            .is_some_and(|c| c.first().is_none_or(|ch| *ch == ' '))
            && !lines.is_empty()
        {
            chunks.pop();
        }
        while let Some(chunk) = chunks.last() {
            if current_len + chunk.len() <= width {
                current_len += chunk.len();
                current.push(chunks.pop().expect("chunk"));
            } else {
                break;
            }
        }
        if let Some(chunk) = chunks.pop_if(|c| c.len() > width) {
            let space_left = if width < 1 { 1 } else { width - current_len };
            current.push(chunk[..space_left].to_vec());
            chunks.push(chunk[space_left..].to_vec());
            current_len += space_left;
        }
        let _ = current_len;
        if current
            .last()
            .is_some_and(|c| c.first().is_none_or(|ch| *ch == ' '))
        {
            current.pop();
        }
        if !current.is_empty() {
            lines.push(current.iter().flatten().collect::<String>());
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> Parser {
        Parser {
            prog: "resolve-task-store.py",
            description: Some(
                "解析済みの profile 値からタスクの置き場を選ぶ。YAML 自体は呼出側で解析する。",
            ),
            actions: vec![
                Action {
                    option_strings: &["-h", "--help"],
                    dest: "help",
                    nargs: NArgs::Zero,
                    kind: Kind::Help,
                    help: Some("show this help message and exit"),
                },
                Action {
                    option_strings: &["--value-json"],
                    dest: "value_json",
                    nargs: NArgs::One,
                    kind: Kind::Store,
                    help: Some("YAML 解析後の task_store の JSON 値。省略時は file"),
                },
            ],
        }
    }

    fn argv(items: &[&str]) -> Vec<PyStr> {
        items.iter().map(|s| lit(s)).collect()
    }

    fn env() -> Env {
        Env {
            int_max_digits: 4300,
            ..Env::default()
        }
    }

    #[test]
    fn store_accepts_value_forms() {
        let parser = store();
        let ns = parser.parse(&env(), &argv(&["--value-json", "x"])).unwrap();
        assert_eq!(get(&ns, "value_json"), &Value::Str(lit("x")));
        let ns = parser.parse(&env(), &argv(&["--value-json=--"])).unwrap();
        assert_eq!(get(&ns, "value_json"), &Value::List(vec![]));
        let ns = parser.parse(&env(), &argv(&["--val=a=b"])).unwrap();
        assert_eq!(get(&ns, "value_json"), &Value::Str(lit("a=b")));
    }

    #[test]
    fn store_error_messages() {
        let parser = store();
        let message = |items: &[&str]| match parser.parse(&env(), &argv(items)) {
            Err(Exit::Error { message, .. }) => chars_to_string(&message),
            other => panic!("{other:?}"),
        };
        assert_eq!(
            message(&["--bad"]),
            "resolve-task-store.py: error: unrecognized arguments: --bad\n"
        );
        assert_eq!(
            message(&["--value-json"]),
            "resolve-task-store.py: error: argument --value-json: expected one argument\n"
        );
        assert_eq!(
            message(&["-hbad"]),
            "resolve-task-store.py: error: argument -h/--help: ignored explicit argument 'bad'\n"
        );
        assert_eq!(
            message(&["--=x"]),
            "resolve-task-store.py: error: ambiguous option: --=x could match --help, --value-json\n"
        );
        assert!(matches!(
            parser.parse(&env(), &argv(&["-hh"])),
            Err(Exit::Help { .. })
        ));
    }

    #[test]
    fn help_text_at_default_width() {
        let text = chars_to_string(&store().format_help(&env()));
        assert_eq!(
            text,
            "usage: resolve-task-store.py [-h] [--value-json VALUE_JSON]\n\n解析済みの profile 値からタスクの置き場を選ぶ。YAML 自体は呼出側で解析する。\n\noptions:\n  -h, --help            show this help message and exit\n  --value-json VALUE_JSON\n                        YAML 解析後の task_store の JSON 値。省略時は file\n"
        );
    }
}
