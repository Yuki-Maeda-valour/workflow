//! 引数(R4E Task 1.4)。
//!
//! 旧版 `main` の `argparse.ArgumentParser(description=__doc__)`(CPython 3.10.11 の `argparse.py`)を、
//! 同じ順・同じ文で再現する。
//!
//! - 解析([`parse`]): `_parse_known_args` と同じく、各引数を `_parse_optional`(完全一致・`=`・短縮・
//!   単一文字の連結・負の数の形・空白)で分類し、選択肢と位置引数を交互に消費する。`inputs`
//!   (`nargs="+"`)は最初の塊だけを取り、後の位置引数は未知の引数になる。その後に必須の欠落、
//!   最後に未知の引数を調べる。重複した `-o` は最後が勝ち、`--source-root` は足していく。
//! - 出力: 誤りは `error()`(使い方の行を `sys.stderr`、`md_tables_to_xlsx.py: error: …`、終了 2)。
//!   `-h` は `print_help()` と `exit()`(終了 0)。`sys.stdout`・`sys.stderr` が `None`(起動時に閉じた
//!   fd)のときの `print_usage`・`print_help`・`_print_message` の切り替えと、書き込みの失敗の
//!   呼出履歴(例外の連鎖を含む)も同じにする。
//! - 書式: `HelpFormatter`(使い方の行の折り返し・段の位置)と `textwrap`(説明文と help の文の
//!   折り返し)。幅は `shutil.get_terminal_size()`(`COLUMNS` → 標準出力の端末の幅 → 80)から 2 を引く。
//!
//! 引数のバイトは surrogateescape で文字にして解析し(`explicit_arg[0]` などは文字で数える)、値は
//! 元のバイトへ戻して渡す(ファイル操作は元のバイトを使う)。
//!
//! 文字コードの設定は決定 22:24 のとおり UTF-8 に固定する(`PYTHONUTF8=1` の旧版と同じ)。

use super::errors::{ExcType, Frame, PyException, frames as script_frames, repr};
use super::pyio::{Outcome, PyStr, Runtime, Stream};
use super::{r4b_unicode, unicode13};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};

/// 呼出名(決定 2026-11-11 17:58・21:35 の固定名)。
pub const PROG: &str = "md_tables_to_xlsx.py";

/// 旧スクリプトの docstring(`description=__doc__`)。`PYTHONOPTIMIZE` が 2 以上なら無い。
pub const DOCSTRING: &str = "Markdown 文書内の表を xlsx(または CSV)へ変換する(export-doc 用)。\n\n使い方:\n  python3 md_tables_to_xlsx.py <input.md> [<input2.md> ...] -o <output.xlsx>\n\n- 1 表 1 シート。シート名は「文書名 + 直前の見出し」(Excel の 31 文字制約に収める)\n- openpyxl が無い環境では、出力名と同名のディレクトリへ CSV(BOM 付き UTF-8)で縮退出力する\n- 入力は読み取りのみ(変更しない)。同名出力は上書き(冪等)\n- --source-root <dir> で元の正本を保護する(繰返し可。省略時は入力の親)\n- 正本への出力・最終symlink・複数hardlink・特殊ファイルは書込前に拒否する\n- セル内の Markdown 装飾(**強調**・`コード`・[リンク](url)・<br>)はプレーンテキスト化する\n\n終了コード: 0 = 出力あり / 1 = 表が見つからない / 2 = 入力エラー・出力先の拒否\n";

const HELP_HELP: &str = "show this help message and exit";
const INPUTS_HELP: &str = "入力 Markdown ファイル";
const OUTPUT_HELP: &str = "出力 xlsx パス";
const SOURCE_ROOT_HELP: &str = "保護する元の正本ディレクトリ（繰返し可。省略時は入力の親）";

/// `argparse.ArgumentError`(表示は `argparse.ArgumentError`)。
pub const ARGUMENT_ERROR: ExcType = ExcType {
    module: "argparse",
    name: "ArgumentError",
    bases: &["Exception", "BaseException"],
};

// ---------------------------------------------------------------- 結果

/// 名前空間に置かれる 1 つの値。
///
/// 選択肢に `=` などで明示した値がちょうど `--` のとき、argparse の `_get_values` は `--` を取り除いて
/// 空のリストを置く(`-o=--` なら `args.output == []`)。旧版はその後の `Path([])` で `TypeError` になる。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    /// 文字列(引数の元のバイト)。
    Str(OsString),
    /// 空のリスト(`[]`)。
    EmptyList,
}

/// 解析した引数(`args.inputs`・`args.output`・`args.source_root`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Arguments {
    pub inputs: Vec<OsString>,
    pub output: Value,
    pub source_root: Vec<Value>,
}

/// `COLUMNS` と標準出力の端末の幅(`shutil.get_terminal_size` の入力)。
pub struct Width<'a> {
    /// 環境変数 `COLUMNS` の値(無ければ `None`)。
    pub columns: Option<Vec<u8>>,
    /// `os.get_terminal_size(sys.__stdout__.fileno()).columns`。取れなければ `None`。
    pub terminal: &'a dyn Fn() -> Option<u16>,
}

/// 旧版の `ap.parse_args()`。引数(呼出名を除く)を解析する。誤りと `-h` は、旧版と同じ出力を
/// `py` へ書いて、旧版の終わり方([`Outcome`])を返す。
pub fn parse(args: &[OsString], py: &mut Runtime) -> Result<Arguments, Outcome> {
    // `os.get_terminal_size` は読むだけなので、使わないときに先に読んでも結果は変わらない。
    let size = py.stdout_terminal_columns();
    let width = Width {
        columns: std::env::var_os("COLUMNS").map(OsStringExt::into_vec),
        terminal: &move || size,
    };
    parse_with(args, py, &width)
}

/// [`parse`] の `COLUMNS` と端末の幅を渡す形(試験用)。
pub fn parse_with(
    args: &[OsString],
    py: &mut Runtime,
    width: &Width,
) -> Result<Arguments, Outcome> {
    let strings = args
        .iter()
        .map(|arg| PyStr::from_os_bytes(arg.as_bytes()))
        .collect::<Vec<_>>();
    match parse_strings(&strings) {
        Ok(arguments) => Ok(arguments),
        Err(stop) => Err(act(py, width, stop)),
    }
}

// ---------------------------------------------------------------- 選択肢の表

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Help,
    Inputs,
    Output,
    SourceRoot,
}

impl Action {
    /// `_get_action_name`。
    fn name(self) -> &'static str {
        match self {
            Self::Help => "-h/--help",
            Self::Inputs => "inputs",
            Self::Output => "-o/--output",
            Self::SourceRoot => "--source-root",
        }
    }

    /// `nargs=0`(`-h`)か。ほかの選択肢は `nargs=None`(値 1 つ)。
    fn takes_no_value(self) -> bool {
        self == Self::Help
    }
}

/// `_option_string_actions` の登録の順(`-h`・`--help`・`-o`・`--output`・`--source-root`)。
const OPTIONS: [(&str, Action); 5] = [
    ("-h", Action::Help),
    ("--help", Action::Help),
    ("-o", Action::Output),
    ("--output", Action::Output),
    ("--source-root", Action::SourceRoot),
];

const DASH: u32 = '-' as u32;
const EQUALS: u32 = '=' as u32;
const SPACE: u32 = ' ' as u32;
const NEWLINE: u32 = '\n' as u32;
const DOT: u32 = '.' as u32;

fn chars(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn lookup(option: &[u32]) -> Option<Action> {
    OPTIONS
        .iter()
        .find(|(name, _)| chars(name) == option)
        .map(|(_, action)| *action)
}

// ---------------------------------------------------------------- 解析

/// `_parse_optional` が返す組(`action` が `None` なら未知の選択肢)。
#[derive(Clone, Debug)]
struct OptionTuple {
    action: Option<Action>,
    option: Vec<u32>,
    explicit: Option<Vec<u32>>,
}

/// 解析を止める理由(旧版の `print_help` か `error`)。
#[derive(Debug)]
enum Stop {
    /// `_HelpAction`(`print_help()` と `exit()`)。
    Help,
    /// `parser.error(message)`。
    Error { message: PyStr, site: ErrorSite },
}

/// `error()` を呼んだ所(書き込みに失敗したときの呼出履歴を決める)。
#[derive(Debug)]
enum ErrorSite {
    /// `parse_args` の未知の引数。
    Unrecognized,
    /// `_parse_known_args` の必須の欠落。
    Required,
    /// `_parse_optional` のあいまいな短縮。
    Ambiguous,
    /// `parse_known_args` が捕まえた `ArgumentError`(処理中の例外として連鎖する)。
    Argument(PyException),
}

/// Python の `re` の `$`(末尾か、末尾の改行 1 つの直前)まで、`body` が全体に一致するか。
fn full_match_with_dollar(text: &[u32], body: impl Fn(&[u32]) -> bool) -> bool {
    body(text) || text.last() == Some(&NEWLINE) && body(&text[..text.len() - 1])
}

/// `_negative_number_matcher`(`^-\d+$|^-\d*\.\d+$`。`\d` は Unicode の 10 進数字)。
fn looks_like_negative_number(text: &[u32]) -> bool {
    let digit = |code: &u32| r4b_unicode::decimal(*code).is_some();
    full_match_with_dollar(text, |text| {
        let Some((&first, rest)) = text.split_first() else {
            return false;
        };
        if first != DASH {
            return false;
        }
        if !rest.is_empty() && rest.iter().all(digit) {
            return true;
        }
        match rest.iter().position(|&code| code == DOT) {
            Some(dot) => {
                rest[..dot].iter().all(digit)
                    && dot + 1 < rest.len()
                    && rest[dot + 1..].iter().all(digit)
            }
            None => false,
        }
    })
}

/// `_get_option_tuples`(短縮と、単一文字の選択肢に続けた値)。
fn option_tuples(text: &[u32]) -> Vec<OptionTuple> {
    let mut result = Vec::new();
    if text[1] == DASH {
        let (prefix, explicit) = match text.iter().position(|&code| code == EQUALS) {
            Some(index) => (&text[..index], Some(text[index + 1..].to_vec())),
            None => (text, None),
        };
        for (name, action) in OPTIONS {
            if chars(name).starts_with(prefix) {
                result.push(OptionTuple {
                    action: Some(action),
                    option: chars(name),
                    explicit: explicit.clone(),
                });
            }
        }
    } else {
        let short = &text[..2];
        for (name, action) in OPTIONS {
            let name = chars(name);
            if name == short {
                result.push(OptionTuple {
                    action: Some(action),
                    option: name,
                    explicit: Some(text[2..].to_vec()),
                });
            } else if name.starts_with(text) {
                result.push(OptionTuple {
                    action: Some(action),
                    option: name,
                    explicit: None,
                });
            }
        }
    }
    result
}

/// `_parse_optional`。`Ok(None)` は位置引数。あいまいな短縮は、その場で `error()`。
fn parse_optional(text: &[u32]) -> Result<Option<OptionTuple>, Stop> {
    if text.first() != Some(&DASH) {
        return Ok(None);
    }
    if let Some(action) = lookup(text) {
        return Ok(Some(OptionTuple {
            action: Some(action),
            option: text.to_vec(),
            explicit: None,
        }));
    }
    if text.len() == 1 {
        return Ok(None);
    }
    if let Some(index) = text.iter().position(|&code| code == EQUALS)
        && let Some(action) = lookup(&text[..index])
    {
        return Ok(Some(OptionTuple {
            action: Some(action),
            option: text[..index].to_vec(),
            explicit: Some(text[index + 1..].to_vec()),
        }));
    }
    let tuples = option_tuples(text);
    if tuples.len() > 1 {
        let mut message = PyStr::from("ambiguous option: ");
        message.push_py(&PyStr::from_code_points(text.to_vec()));
        message.push_str(" could match ");
        for (index, tuple) in tuples.iter().enumerate() {
            if index > 0 {
                message.push_str(", ");
            }
            message.push_py(&PyStr::from_code_points(tuple.option.clone()));
        }
        return Err(Stop::Error {
            message,
            site: ErrorSite::Ambiguous,
        });
    }
    if let Some(tuple) = tuples.into_iter().next() {
        return Ok(Some(tuple));
    }
    if looks_like_negative_number(text) || text.contains(&SPACE) {
        return Ok(None);
    }
    Ok(Some(OptionTuple {
        action: None,
        option: text.to_vec(),
        explicit: None,
    }))
}

/// 引数の型(`arg_strings_pattern` の 1 文字)。
#[derive(Clone, Debug)]
enum Pattern {
    /// `A`(位置引数)。
    Argument,
    /// `O`(選択肢)。
    Optional(OptionTuple),
    /// `-`(`--`)。
    Separator,
}

impl Pattern {
    fn is_argument(&self) -> bool {
        matches!(self, Self::Argument)
    }
}

/// 名前空間(既定値は `source_root` の空のリストだけ)。
#[derive(Default)]
struct Namespace {
    inputs: Option<Vec<OsString>>,
    output: Option<Value>,
    source_root: Vec<Value>,
}

fn to_os(text: &[u32]) -> OsString {
    let bytes = PyStr::from_code_points(text.to_vec())
        .encode_surrogateescape()
        .expect("引数から作った文字はいつも元のバイトへ戻せる");
    OsString::from_vec(bytes)
}

/// `ArgumentError(action, message)` を、捕まえた所までの呼出履歴つきで作る。
fn argument_error(action: Action, message: PyStr, inner: &[Frame]) -> Stop {
    let mut text = PyStr::from(format!("argument {}: ", action.name()));
    text.push_py(&message);
    let mut stack = vec![frames::PARSE_KNOWN_ARGS, frames::PARSE_KNOWN_ARGS_CONSUMES];
    stack.extend_from_slice(inner);
    let exception = PyException::new(ARGUMENT_ERROR, text.clone()).called_from(&stack);
    Stop::Error {
        message: text,
        site: ErrorSite::Argument(exception),
    }
}

/// `'ignored explicit argument %r' % explicit_arg`。
fn ignored_explicit(explicit: &[u32]) -> PyStr {
    let mut message = PyStr::from("ignored explicit argument ");
    message.push_py(&repr(&PyStr::from_code_points(explicit.to_vec())));
    message
}

/// `_get_values` の `arg_strings.remove('--')`(最初の `--` だけを取り除く)。
fn without_first_separator<'a>(strings: &[&'a [u32]]) -> Vec<&'a [u32]> {
    let separator = chars("--");
    let mut values = strings.to_vec();
    if let Some(index) = values
        .iter()
        .position(|value| *value == separator.as_slice())
    {
        values.remove(index);
    }
    values
}

/// `_get_values`(`nargs=None` の選択肢): 1 つ残れば文字列、残らなければ空のリスト。
fn option_value(strings: &[&[u32]]) -> Value {
    match without_first_separator(strings).first() {
        Some(value) => Value::Str(to_os(value)),
        None => Value::EmptyList,
    }
}

/// `take_action`(`seen_actions` への追加と、各 action の `__call__`)。
fn take_action(
    namespace: &mut Namespace,
    seen: &mut Vec<Action>,
    action: Action,
    strings: &[&[u32]],
) -> Result<(), Stop> {
    seen.push(action);
    match action {
        Action::Help => Err(Stop::Help),
        Action::Inputs => {
            let values = without_first_separator(strings);
            namespace.inputs = Some(values.iter().map(|value| to_os(value)).collect());
            Ok(())
        }
        Action::Output => {
            namespace.output = Some(option_value(strings));
            Ok(())
        }
        Action::SourceRoot => {
            namespace.source_root.push(option_value(strings));
            Ok(())
        }
    }
}

/// `_parse_known_args` と `parse_args` の未知の引数の検査。
fn parse_strings(strings: &[PyStr]) -> Result<Arguments, Stop> {
    let texts = strings
        .iter()
        .map(PyStr::as_code_points)
        .collect::<Vec<_>>();
    let mut patterns = Vec::with_capacity(texts.len());
    let mut after_separator = false;
    for text in &texts {
        if after_separator {
            patterns.push(Pattern::Argument);
        } else if *text == chars("--").as_slice() {
            after_separator = true;
            patterns.push(Pattern::Separator);
        } else {
            patterns.push(match parse_optional(text)? {
                Some(tuple) => Pattern::Optional(tuple),
                None => Pattern::Argument,
            });
        }
    }
    let option_indices = patterns
        .iter()
        .enumerate()
        .filter(|(_, pattern)| matches!(pattern, Pattern::Optional(_)))
        .map(|(index, _)| index)
        .collect::<Vec<_>>();

    let mut namespace = Namespace::default();
    let mut seen = Vec::new();
    let mut extras: Vec<&[u32]> = Vec::new();
    let mut positionals_left = true;

    // consume_positionals: `inputs` の `(-*A[A-]*)` を先頭から当てる。
    let consume_positionals = |start: usize,
                               namespace: &mut Namespace,
                               seen: &mut Vec<Action>,
                               positionals_left: &mut bool|
     -> Result<usize, Stop> {
        if !*positionals_left {
            return Ok(start);
        }
        let mut end = start;
        while matches!(patterns.get(end), Some(Pattern::Separator)) {
            end += 1;
        }
        if !matches!(patterns.get(end), Some(Pattern::Argument)) {
            return Ok(start);
        }
        while matches!(
            patterns.get(end),
            Some(Pattern::Argument | Pattern::Separator)
        ) {
            end += 1;
        }
        *positionals_left = false;
        take_action(namespace, seen, Action::Inputs, &texts[start..end])?;
        Ok(end)
    };

    let max_option_index = option_indices.last().copied();
    let mut start = 0usize;
    while max_option_index.is_some_and(|max| start <= max) {
        let next_option = *option_indices
            .iter()
            .find(|&&index| index >= start)
            .expect("start は最後の選択肢の位置以下");
        if start != next_option {
            let end = consume_positionals(start, &mut namespace, &mut seen, &mut positionals_left)?;
            if end > start {
                start = end;
                continue;
            }
        }
        if !matches!(patterns[start], Pattern::Optional(_)) {
            extras.extend_from_slice(&texts[start..next_option]);
            start = next_option;
        }
        start = consume_optional(
            start,
            &texts,
            &patterns,
            &mut namespace,
            &mut seen,
            &mut extras,
        )?;
    }
    let stop = consume_positionals(start, &mut namespace, &mut seen, &mut positionals_left)?;
    extras.extend_from_slice(&texts[stop..]);

    let mut required = Vec::new();
    if !seen.contains(&Action::Inputs) {
        required.push(Action::Inputs.name());
    }
    if !seen.contains(&Action::Output) {
        required.push(Action::Output.name());
    }
    if !required.is_empty() {
        return Err(Stop::Error {
            message: PyStr::from(format!(
                "the following arguments are required: {}",
                required.join(", ")
            )),
            site: ErrorSite::Required,
        });
    }
    if !extras.is_empty() {
        let mut message = PyStr::from("unrecognized arguments: ");
        for (index, extra) in extras.iter().enumerate() {
            if index > 0 {
                message.push_str(" ");
            }
            message.push_py(&PyStr::from_code_points(extra.to_vec()));
        }
        return Err(Stop::Error {
            message,
            site: ErrorSite::Unrecognized,
        });
    }
    Ok(Arguments {
        inputs: namespace.inputs.expect("inputs は必須の検査を通った"),
        output: namespace.output.expect("output は必須の検査を通った"),
        source_root: namespace.source_root,
    })
}

/// `consume_optional`。次に調べる位置を返す。
fn consume_optional<'a>(
    start: usize,
    texts: &[&'a [u32]],
    patterns: &[Pattern],
    namespace: &mut Namespace,
    seen: &mut Vec<Action>,
    extras: &mut Vec<&'a [u32]>,
) -> Result<usize, Stop> {
    let Pattern::Optional(tuple) = &patterns[start] else {
        unreachable!("consume_optional は選択肢の位置だけで呼ぶ");
    };
    let mut action = tuple.action;
    let mut option = tuple.option.clone();
    let mut explicit = tuple.explicit.clone();
    let mut tuples: Vec<(Action, Vec<Vec<u32>>)> = Vec::new();
    let stop;
    loop {
        let Some(current) = action else {
            extras.push(texts[start]);
            return Ok(start + 1);
        };
        if let Some(value) = &explicit {
            // match_argument(action, 'A'): `-h` は 0 個、ほかは 1 個。
            if current.takes_no_value() {
                if option[1] != DASH && !value.is_empty() {
                    tuples.push((current, Vec::new()));
                    option = vec![option[0], value[0]];
                    let rest = value[1..].to_vec();
                    match lookup(&option) {
                        Some(next) => {
                            action = Some(next);
                            explicit = (!rest.is_empty()).then_some(rest);
                        }
                        None => {
                            return Err(argument_error(
                                current,
                                ignored_explicit(value),
                                &[frames::CONSUME_OPTIONAL_RAISES_SINGLE_DASH],
                            ));
                        }
                    }
                } else {
                    return Err(argument_error(
                        current,
                        ignored_explicit(value),
                        &[frames::CONSUME_OPTIONAL_RAISES_IGNORED],
                    ));
                }
            } else {
                stop = start + 1;
                tuples.push((current, vec![value.clone()]));
                break;
            }
        } else {
            let first = start + 1;
            let count = if current.takes_no_value() {
                0
            } else if patterns.get(first).is_some_and(Pattern::is_argument) {
                1
            } else {
                return Err(argument_error(
                    current,
                    PyStr::from("expected one argument"),
                    &[
                        frames::CONSUME_OPTIONAL_MATCHES,
                        frames::MATCH_ARGUMENT_RAISES,
                    ],
                ));
            };
            stop = first + count;
            tuples.push((
                current,
                texts[first..stop]
                    .iter()
                    .map(|text| text.to_vec())
                    .collect(),
            ));
            break;
        }
    }
    for (action, strings) in tuples {
        let strings = strings.iter().map(Vec::as_slice).collect::<Vec<_>>();
        take_action(namespace, seen, action, &strings)?;
    }
    Ok(stop)
}

// ---------------------------------------------------------------- 出力

/// `sys.<stream>`(`None` なら `None`)。
fn sys_file(py: &Runtime, stream: Stream) -> Option<Stream> {
    py.has(stream).then_some(stream)
}

/// `_print_message(message, file)`: `file` が `None` なら `sys.stderr` へ書く。それも `None` なら
/// `None.write` の `AttributeError`。
fn print_message(
    py: &mut Runtime,
    message: &PyStr,
    file: Option<Stream>,
) -> Result<(), PyException> {
    if message.is_empty() {
        return Ok(());
    }
    let result = match file.or_else(|| sys_file(py, Stream::Stderr)) {
        Some(stream) => py.write(stream, message),
        None => Err(PyException::none_has_no_attribute("write")),
    };
    result.map_err(|error| error.called_from(&[frames::PRINT_MESSAGE_WRITES]))
}

/// 解析を止めた後の旧版の動き(出力と終わり方)。
fn act(py: &mut Runtime, width: &Width, stop: Stop) -> Outcome {
    let outer = [script_frames::MODULE, frames::MAIN_PARSES_ARGS];
    match stop {
        Stop::Help => {
            // print_help(): file = sys.stdout(None なら _print_message が sys.stderr へ)。
            let text = PyStr::from(format_help(
                columns(width, py.config().int_max_str_digits_limit()),
                (!py.config().strips_docstrings()).then_some(DOCSTRING),
            ));
            let file = sys_file(py, Stream::Stdout);
            match print_message(py, &text, file) {
                Ok(()) => Outcome::Exit(0),
                Err(error) => Outcome::raised(error.called_from(&frames::help_stack(&outer))),
            }
        }
        Stop::Error { message, site } => {
            let (stack, context) = match site {
                ErrorSite::Unrecognized => {
                    (vec![outer[0], outer[1], frames::PARSE_ARGS_ERROR], None)
                }
                ErrorSite::Required => (
                    vec![
                        outer[0],
                        outer[1],
                        frames::PARSE_ARGS,
                        frames::PARSE_KNOWN_ARGS,
                        frames::PARSE_KNOWN_ARGS_REQUIRED,
                    ],
                    None,
                ),
                ErrorSite::Ambiguous => (
                    vec![
                        outer[0],
                        outer[1],
                        frames::PARSE_ARGS,
                        frames::PARSE_KNOWN_ARGS,
                        frames::PARSE_KNOWN_ARGS_CLASSIFIES,
                        frames::PARSE_OPTIONAL_ERROR,
                    ],
                    None,
                ),
                ErrorSite::Argument(context) => (
                    vec![
                        outer[0],
                        outer[1],
                        frames::PARSE_ARGS,
                        frames::PARSE_KNOWN_ARGS_ERROR,
                    ],
                    Some(context),
                ),
            };
            let raised = |error: PyException, inner: &[Frame]| {
                let mut frames = stack.clone();
                frames.extend_from_slice(inner);
                let error = error.called_from(&frames);
                Outcome::raised(match &context {
                    Some(context) => error.with_context(context.clone()),
                    None => error,
                })
            };
            let max_digits = py.config().int_max_str_digits_limit();
            // error(): print_usage(sys.stderr) → file が None なら sys.stdout。
            let usage = PyStr::from(format_usage(columns(width, max_digits)));
            let file = sys_file(py, Stream::Stderr).or_else(|| sys_file(py, Stream::Stdout));
            if let Err(error) = print_message(py, &usage, file) {
                return raised(error, &[frames::ERROR_PRINTS_USAGE, frames::PRINT_USAGE]);
            }
            // exit(2, message): _print_message(message, sys.stderr)。
            let mut text = PyStr::from(format!("{PROG}: error: "));
            text.push_py(&message);
            text.push_str("\n");
            let file = sys_file(py, Stream::Stderr);
            if let Err(error) = print_message(py, &text, file) {
                return raised(error, &[frames::ERROR_EXITS, frames::EXIT_PRINTS]);
            }
            Outcome::Exit(2)
        }
    }
}

// ---------------------------------------------------------------- 幅

/// `HelpFormatter` の幅: `shutil.get_terminal_size().columns - 2`。
///
/// `COLUMNS` を `int()` で読めて 0 より大きければその値、そうでなければ標準出力の端末の幅
/// (0 もそのまま)、取れなければ 80。
fn columns(width: &Width, max_digits: u32) -> i64 {
    let from_env = width
        .columns
        .as_deref()
        .and_then(|value| python_int(&PyStr::from_os_bytes(value), max_digits))
        .filter(|value| *value > 0);
    let columns = match from_env {
        Some(value) => value,
        // 3.10 の shutil は端末の幅の 0 もそのまま使う(`size.columns or fallback` は 3.11 から)。
        None => (width.terminal)().map_or(80, i64::from),
    };
    columns - 2
}

/// 幅の上限。これより広い幅は、どの行も折り返さないので結果が同じ(`int()` の大きな値をここで止める)。
const WIDTH_CAP: i64 = 1 << 40;

/// Python の `int(str)`(10 進)。読めなければ `None`(`ValueError`)。
///
/// `_PyUnicode_TransformDecimalAndSpaceToASCII` で ASCII 以外の空白を空白へ、10 進数字を ASCII の数字へ
/// 変え(どちらでもない文字で打ち切る)、`PyLong_FromString` と同じく前後の ASCII の空白・符号・
/// 数字の間の `_` 1 つを許す。数字が `max_digits`(0 は上限なし)を超えると `ValueError`。値は
/// [`WIDTH_CAP`] で止める。
fn python_int(text: &PyStr, max_digits: u32) -> Option<i64> {
    let mut ascii = Vec::with_capacity(text.len());
    let points = text.as_code_points();
    let non_ascii = points.iter().any(|&code| code >= 0x80);
    for &code in points {
        if !non_ascii || code < 127 {
            ascii.push(code as u8);
        } else if unicode13::is_space(code) {
            ascii.push(b' ');
        } else if let Some(digit) = r4b_unicode::decimal(code) {
            ascii.push(b'0' + digit);
        } else {
            ascii.push(b'?');
            break;
        }
    }
    let is_space = |byte: &u8| matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r');
    let mut rest = ascii.as_slice();
    while rest.first().is_some_and(is_space) {
        rest = &rest[1..];
    }
    let mut negative = false;
    if let Some(&sign) = rest.first()
        && (sign == b'+' || sign == b'-')
    {
        negative = sign == b'-';
        rest = &rest[1..];
    }
    if rest.first() == Some(&b'_') {
        return None;
    }
    let mut digits = 0u64;
    let mut value: i64 = 0;
    let mut previous_underscore = false;
    let mut index = 0;
    while index < rest.len() && (rest[index].is_ascii_digit() || rest[index] == b'_') {
        if rest[index] == b'_' {
            if previous_underscore {
                return None;
            }
            previous_underscore = true;
        } else {
            previous_underscore = false;
            digits += 1;
            value = value
                .saturating_mul(10)
                .saturating_add(i64::from(rest[index] - b'0'))
                .min(WIDTH_CAP);
        }
        index += 1;
    }
    if previous_underscore || digits == 0 {
        return None;
    }
    if digits > 640 && max_digits > 0 && digits > u64::from(max_digits) {
        return None;
    }
    let mut tail = &rest[index..];
    while tail.first().is_some_and(is_space) {
        tail = &tail[1..];
    }
    if !tail.is_empty() {
        return None;
    }
    Some(if negative { -value } else { value })
}

// ---------------------------------------------------------------- 書式

const USAGE_PREFIX: &str = "usage: ";
/// `_format_actions_usage` の選択肢と位置引数の部分(`part_regexp` で分けた後)。
const OPTIONAL_PARTS: [&str; 4] = ["[-h]", "-o", "OUTPUT", "[--source-root SOURCE_ROOT]"];
const POSITIONAL_PARTS: [&str; 2] = ["inputs", "[inputs ...]"];

/// `_format_usage` の `get_lines`。
fn usage_lines(
    parts: &[&str],
    indent: usize,
    prefix: Option<usize>,
    text_width: i64,
) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line: Vec<&str> = Vec::new();
    let mut line_len = prefix.unwrap_or(indent) as i64 - 1;
    for &part in parts {
        if line_len + 1 + part.len() as i64 > text_width && !line.is_empty() {
            lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
            line.clear();
            line_len = indent as i64 - 1;
        }
        line.push(part);
        line_len += part.len() as i64 + 1;
    }
    if !line.is_empty() {
        lines.push(format!("{}{}", " ".repeat(indent), line.join(" ")));
    }
    if prefix.is_some() {
        lines[0].drain(..indent);
    }
    lines
}

/// `_format_usage`(`usage: …` と空行)。
fn usage_section(width: i64) -> String {
    let prog_and_parts = [&[PROG][..], &OPTIONAL_PARTS[..], &POSITIONAL_PARTS[..]].concat();
    let usage = prog_and_parts.join(" ");
    let text_width = width;
    let body = if (USAGE_PREFIX.len() + usage.len()) as i64 > text_width {
        let lines = if 4 * (USAGE_PREFIX.len() + PROG.len()) as i64 <= 3 * text_width {
            let indent = USAGE_PREFIX.len() + PROG.len() + 1;
            let mut lines = usage_lines(
                &[&[PROG][..], &OPTIONAL_PARTS[..]].concat(),
                indent,
                Some(USAGE_PREFIX.len()),
                text_width,
            );
            lines.extend(usage_lines(&POSITIONAL_PARTS, indent, None, text_width));
            lines
        } else {
            let indent = USAGE_PREFIX.len();
            let parts = [&OPTIONAL_PARTS[..], &POSITIONAL_PARTS[..]].concat();
            let mut lines = usage_lines(&parts, indent, None, text_width);
            if lines.len() > 1 {
                lines = usage_lines(&OPTIONAL_PARTS, indent, None, text_width);
                lines.extend(usage_lines(&POSITIONAL_PARTS, indent, None, text_width));
            }
            let mut all = vec![PROG.to_owned()];
            all.extend(lines);
            all
        };
        lines.join("\n")
    } else {
        usage
    };
    format!("{USAGE_PREFIX}{body}\n\n")
}

/// `HelpFormatter.format_help` の仕上げ: 3 つ以上の改行を 2 つへ、前後の改行を除いて改行を 1 つ足す。
fn finish_help(text: &str) -> String {
    let mut collapsed = String::with_capacity(text.len());
    let mut newlines = 0;
    for character in text.chars() {
        if character == '\n' {
            newlines += 1;
            continue;
        }
        collapsed.push_str(&"\n".repeat(if newlines >= 3 { 2 } else { newlines }));
        newlines = 0;
        collapsed.push(character);
    }
    collapsed.push_str(&"\n".repeat(if newlines >= 3 { 2 } else { newlines }));
    format!("{}\n", collapsed.trim_matches('\n'))
}

/// `parser.format_usage()`(誤りの使い方の行)。
pub fn format_usage(width: i64) -> String {
    finish_help(&usage_section(width))
}

/// 1 つの action の段(`_format_action`。字下げは段の中の 2)。
fn action_section(header: &str, help: &str, width: i64) -> String {
    const CURRENT_INDENT: i64 = 2;
    const ACTION_MAX_LENGTH: i64 = "-o OUTPUT, --output OUTPUT".len() as i64 + CURRENT_INDENT;
    let max_help_position = 24.min((width - 20).max(4));
    let help_position = (ACTION_MAX_LENGTH + 2).min(max_help_position);
    let help_width = (width - help_position).max(11);
    let action_width = help_position - CURRENT_INDENT - 2;
    let mut text = String::new();
    let indent_first = if header.len() as i64 <= action_width {
        text.push_str(&format!(
            "  {header:<width$}  ",
            width = action_width as usize
        ));
        0
    } else {
        text.push_str(&format!("  {header}\n"));
        help_position as usize
    };
    let lines = textwrap::wrap(&split_lines_text(help), help_width as usize);
    text.push_str(&format!("{}{}\n", " ".repeat(indent_first), lines[0]));
    for line in &lines[1..] {
        text.push_str(&format!("{}{line}\n", " ".repeat(help_position as usize)));
    }
    text
}

/// `_whitespace_matcher.sub(' ', text).strip()`(`\s+` は ASCII の空白だけ、`strip` は `str.isspace`)。
fn split_lines_text(text: &str) -> Vec<u32> {
    let mut result = Vec::with_capacity(text.len());
    let mut in_space = false;
    for code in text.chars().map(u32::from) {
        if matches!(code, 0x20 | 0x09 | 0x0a | 0x0d | 0x0c | 0x0b) {
            if !in_space {
                result.push(SPACE);
            }
            in_space = true;
        } else {
            result.push(code);
            in_space = false;
        }
    }
    let start = result
        .iter()
        .position(|&code| !unicode13::is_space(code))
        .unwrap_or(result.len());
    let end = result
        .iter()
        .rposition(|&code| !unicode13::is_space(code))
        .map_or(start, |index| index + 1);
    result[start..end].to_vec()
}

/// `parser.format_help()`。`description` は docstring(`-OO` では `None`)。
pub fn format_help(width: i64, description: Option<&str>) -> String {
    let mut text = usage_section(width);
    if let Some(description) = description {
        let lines = textwrap::wrap(&split_lines_text(description), width.max(11) as usize);
        text.push_str(&lines.join("\n"));
        text.push_str("\n\n");
    }
    text.push_str("\npositional arguments:\n");
    text.push_str(&action_section("inputs", INPUTS_HELP, width));
    text.push_str("\n\noptions:\n");
    text.push_str(&action_section("-h, --help", HELP_HELP, width));
    text.push_str(&action_section(
        "-o OUTPUT, --output OUTPUT",
        OUTPUT_HELP,
        width,
    ));
    text.push_str(&action_section(
        "--source-root SOURCE_ROOT",
        SOURCE_ROOT_HELP,
        width,
    ));
    text.push('\n');
    finish_help(&text)
}

/// `textwrap.TextWrapper` の既定(`break_on_hyphens`・`break_long_words`・`drop_whitespace`)。
///
/// 語の分け方(`wordsep_re`)の `\w` は、固定の説明文と help の文に出る文字だけで正しい(試験で
/// その文の全部の文字を確かめる)。ほかの文字には使わない。
mod textwrap {
    use super::{r4b_unicode, unicode13};

    /// textwrap の `_whitespace`。
    fn is_whitespace(code: u32) -> bool {
        matches!(code, 0x09 | 0x0a | 0x0b | 0x0c | 0x0d | 0x20)
    }

    /// 固定の文の、ASCII 以外で `\w` に当たらない文字(Python の `re` で確かめた)。
    pub(super) const FIXED_TEXT_NON_WORD: [char; 7] = ['、', '。', '「', '」', '・', '（', '）'];

    /// `\w`(固定の文の文字だけで正しい)。
    fn is_word(code: u32) -> bool {
        if code < 0x80 {
            let byte = code as u8;
            return byte.is_ascii_alphanumeric() || byte == b'_';
        }
        !FIXED_TEXT_NON_WORD.iter().any(|&c| u32::from(c) == code)
    }

    /// `\d`(Unicode の 10 進数字)。
    fn is_digit(code: u32) -> bool {
        r4b_unicode::decimal(code).is_some()
    }

    /// `letter = [^\d\W]`。
    fn is_letter(code: u32) -> bool {
        is_word(code) && !is_digit(code)
    }

    /// `word_punct = [\w!"\'&.,?]`。
    fn is_word_punct(code: u32) -> bool {
        is_word(code)
            || matches!(
                char::from_u32(code),
                Some('!' | '"' | '\'' | '&' | '.' | ',' | '?')
            )
    }

    /// `text[at..]` の先頭の `-` の数。
    fn dashes(text: &[u32], at: usize) -> usize {
        text[at.min(text.len())..]
            .iter()
            .take_while(|&&code| code == u32::from('-'))
            .count()
    }

    /// `(?<=wp) -{2,} (?=\w)` が `at` から当たれば、その終わり。
    fn em_dash_at(text: &[u32], at: usize) -> Option<usize> {
        if at == 0 || !is_word_punct(text[at - 1]) {
            return None;
        }
        let count = dashes(text, at);
        (count >= 2 && text.get(at + count).is_some_and(|&code| is_word(code)))
            .then_some(at + count)
    }

    /// `wordsep_re` が `at` から当てる長さの終わり。
    fn match_at(text: &[u32], at: usize) -> usize {
        let letter = |index: usize| text.get(index).is_some_and(|&code| is_letter(code));
        let dash = |index: usize| text.get(index) == Some(&u32::from('-'));
        if is_whitespace(text[at]) {
            let mut end = at;
            while end < text.len() && is_whitespace(text[end]) {
                end += 1;
            }
            return end;
        }
        if let Some(end) = em_dash_at(text, at) {
            return end;
        }
        // %(nws)s+? を 1 文字ずつ伸ばし、後ろの 3 つの選択肢を順に試す。
        let mut q = at + 1;
        loop {
            // 1. ハイフンでつないだ語: -(?: (?<=lt{2}-) | (?<=lt-lt-)) (?= lt -? lt)
            if dash(q) {
                let behind = (q >= 2 && letter(q - 2) && letter(q - 1))
                    || (q >= 3 && letter(q - 3) && dash(q - 2) && letter(q - 1));
                let ahead = letter(q + 1) && (letter(q + 2) || (dash(q + 2) && letter(q + 3)));
                if behind && ahead {
                    return q + 1;
                }
            }
            // 2. 語の終わり: (?=ws|\Z)
            if q == text.len() || is_whitespace(text[q]) {
                return q;
            }
            // 3. em-dash の前: (?<=wp) (?=-{2,}\w)
            if is_word_punct(text[q - 1]) && em_dash_at(text, q).is_some() {
                return q;
            }
            q += 1;
        }
    }

    /// `TextWrapper._split`(`wordsep_re.split` の空でない部分)。
    pub fn chunks(text: &[u32]) -> Vec<Vec<u32>> {
        let mut result = Vec::new();
        let mut at = 0;
        while at < text.len() {
            let end = match_at(text, at);
            result.push(text[at..end].to_vec());
            at = end;
        }
        result
    }

    /// `chunk.strip() == ''`。
    fn is_blank(chunk: &[u32]) -> bool {
        chunk.iter().all(|&code| unicode13::is_space(code))
    }

    /// `textwrap.wrap(text, width)`(字下げなし。`width` は 1 以上)。
    pub fn wrap(text: &[u32], width: usize) -> Vec<String> {
        let mut chunks = chunks(text);
        chunks.reverse();
        let mut lines: Vec<String> = Vec::new();
        while !chunks.is_empty() {
            let mut line: Vec<Vec<u32>> = Vec::new();
            let mut length = 0usize;
            if !lines.is_empty() && chunks.last().is_some_and(|chunk| is_blank(chunk)) {
                chunks.pop();
            }
            while let Some(chunk) = chunks.last() {
                if length + chunk.len() <= width {
                    length += chunk.len();
                    line.push(chunks.pop().expect("直前に確かめた"));
                } else {
                    break;
                }
            }
            if let Some(chunk) = chunks.pop_if(|chunk| chunk.len() > width) {
                // _handle_long_word
                let space_left = if width < 1 { 1 } else { width - length };
                let mut end = space_left;
                if chunk.len() > space_left
                    && let Some(hyphen) = chunk[..space_left]
                        .iter()
                        .rposition(|&code| code == u32::from('-'))
                    && hyphen > 0
                    && chunk[..hyphen].iter().any(|&code| code != u32::from('-'))
                {
                    end = hyphen + 1;
                }
                line.push(chunk[..end].to_vec());
                chunks.push(chunk[end..].to_vec());
            }
            // 行末の空白の部分を落とす(`cur_len` はこの後に使わない)。
            if line.last().is_some_and(|chunk| is_blank(chunk)) {
                line.pop();
            }
            if !line.is_empty() {
                lines.push(
                    line.concat()
                        .into_iter()
                        .map(|code| char::from_u32(code).unwrap_or('\u{fffd}'))
                        .collect(),
                );
            }
        }
        lines
    }
}

// ---------------------------------------------------------------- 呼出履歴の段

/// argparse の段(本家 CPython 3.10.11 の `argparse.py` の行番号とソースの行)と、旧スクリプトの段。
pub mod frames {
    use super::super::errors::{Frame, frames::SCRIPT};

    const ARGPARSE: &str = "argparse.py";

    pub const MAIN_PARSES_ARGS: Frame =
        Frame::new(SCRIPT, 206, "main", "    args = ap.parse_args()");
    pub const PARSE_ARGS: Frame = Frame::new(
        ARGPARSE,
        1833,
        "parse_args",
        "        args, argv = self.parse_known_args(args, namespace)",
    );
    pub const PARSE_ARGS_ERROR: Frame = Frame::new(
        ARGPARSE,
        1836,
        "parse_args",
        "            self.error(msg % ' '.join(argv))",
    );
    pub const PARSE_KNOWN_ARGS: Frame = Frame::new(
        ARGPARSE,
        1866,
        "parse_known_args",
        "                namespace, args = self._parse_known_args(args, namespace)",
    );
    pub const PARSE_KNOWN_ARGS_ERROR: Frame = Frame::new(
        ARGPARSE,
        1869,
        "parse_known_args",
        "                self.error(str(err))",
    );
    pub const PARSE_KNOWN_ARGS_CLASSIFIES: Frame = Frame::new(
        ARGPARSE,
        1910,
        "_parse_known_args",
        "                option_tuple = self._parse_optional(arg_string)",
    );
    pub const PARSE_KNOWN_ARGS_CONSUMES: Frame = Frame::new(
        ARGPARSE,
        2079,
        "_parse_known_args",
        "            start_index = consume_optional(start_index)",
    );
    pub const PARSE_KNOWN_ARGS_REQUIRED: Frame = Frame::new(
        ARGPARSE,
        2107,
        "_parse_known_args",
        "            self.error(_('the following arguments are required: %s') %",
    );
    pub const CONSUME_OPTIONAL_RAISES_SINGLE_DASH: Frame = Frame::new(
        ARGPARSE,
        1987,
        "consume_optional",
        "                            raise ArgumentError(action, msg % explicit_arg)",
    );
    pub const CONSUME_OPTIONAL_RAISES_IGNORED: Frame = Frame::new(
        ARGPARSE,
        2001,
        "consume_optional",
        "                        raise ArgumentError(action, msg % explicit_arg)",
    );
    pub const CONSUME_OPTIONAL_MATCHES: Frame = Frame::new(
        ARGPARSE,
        2009,
        "consume_optional",
        "                    arg_count = match_argument(action, selected_patterns)",
    );
    pub const CONSUME_OPTIONAL_TAKES_ACTION: Frame = Frame::new(
        ARGPARSE,
        2019,
        "consume_optional",
        "                take_action(action, args, option_string)",
    );
    pub const TAKE_ACTION_CALLS: Frame = Frame::new(
        ARGPARSE,
        1943,
        "take_action",
        "                action(self, namespace, argument_values, option_string)",
    );
    pub const MATCH_ARGUMENT_RAISES: Frame = Frame::new(
        ARGPARSE,
        2174,
        "_match_argument",
        "            raise ArgumentError(action, msg)",
    );
    pub const PARSE_OPTIONAL_ERROR: Frame = Frame::new(
        ARGPARSE,
        2230,
        "_parse_optional",
        "            self.error(msg % args)",
    );
    pub const HELP_PRINTS: Frame =
        Frame::new(ARGPARSE, 1106, "__call__", "        parser.print_help()");
    pub const PRINT_USAGE: Frame = Frame::new(
        ARGPARSE,
        2562,
        "print_usage",
        "        self._print_message(self.format_usage(), file)",
    );
    pub const PRINT_HELP: Frame = Frame::new(
        ARGPARSE,
        2567,
        "print_help",
        "        self._print_message(self.format_help(), file)",
    );
    pub const PRINT_MESSAGE_WRITES: Frame = Frame::new(
        ARGPARSE,
        2573,
        "_print_message",
        "            file.write(message)",
    );
    pub const EXIT_PRINTS: Frame = Frame::new(
        ARGPARSE,
        2580,
        "exit",
        "            self._print_message(message, _sys.stderr)",
    );
    pub const ERROR_PRINTS_USAGE: Frame = Frame::new(
        ARGPARSE,
        2592,
        "error",
        "        self.print_usage(_sys.stderr)",
    );
    pub const ERROR_EXITS: Frame = Frame::new(
        ARGPARSE,
        2594,
        "error",
        "        self.exit(2, _('%(prog)s: error: %(message)s\\n') % args)",
    );

    /// `-h` の `print_help` までの段(外側の `outer` の後)。
    pub fn help_stack(outer: &[Frame]) -> Vec<Frame> {
        let mut stack = outer.to_vec();
        stack.extend_from_slice(&[
            PARSE_ARGS,
            PARSE_KNOWN_ARGS,
            PARSE_KNOWN_ARGS_CONSUMES,
            CONSUME_OPTIONAL_TAKES_ACTION,
            TAKE_ACTION_CALLS,
            HELP_PRINTS,
            PRINT_HELP,
        ]);
        stack
    }

    /// この単位の段のすべて(実物と照らす対象)。
    pub const ALL: &[Frame] = &[
        MAIN_PARSES_ARGS,
        PARSE_ARGS,
        PARSE_ARGS_ERROR,
        PARSE_KNOWN_ARGS,
        PARSE_KNOWN_ARGS_ERROR,
        PARSE_KNOWN_ARGS_CLASSIFIES,
        PARSE_KNOWN_ARGS_CONSUMES,
        PARSE_KNOWN_ARGS_REQUIRED,
        CONSUME_OPTIONAL_RAISES_SINGLE_DASH,
        CONSUME_OPTIONAL_RAISES_IGNORED,
        CONSUME_OPTIONAL_MATCHES,
        CONSUME_OPTIONAL_TAKES_ACTION,
        TAKE_ACTION_CALLS,
        MATCH_ARGUMENT_RAISES,
        PARSE_OPTIONAL_ERROR,
        HELP_PRINTS,
        PRINT_USAGE,
        PRINT_HELP,
        PRINT_MESSAGE_WRITES,
        EXIT_PRINTS,
        ERROR_PRINTS_USAGE,
        ERROR_EXITS,
    ];
}

#[cfg(test)]
mod tests {
    use super::super::errors::types;
    use super::super::pyio::fake::{FakeOs, Step};
    use super::*;
    use dev_workflow_platform::r4e::{Disposition, FdKind, FdState, StandardFd, StartupState};

    fn startup(stdout_open: bool, stderr_open: bool) -> StartupState {
        let fd = |open: bool| StandardFd {
            state: if open {
                FdState::Open(FdKind::Fifo)
            } else {
                FdState::Closed
            },
            python_check_errno: (!open).then_some(9),
        };
        StartupState {
            fds: [
                StandardFd {
                    state: FdState::Open(FdKind::CharacterDevice),
                    python_check_errno: None,
                },
                fd(stdout_open),
                fd(stderr_open),
            ],
            sigpipe: Some(Disposition::Default),
            sigint: Some(Disposition::Default),
        }
    }

    struct Run {
        result: Result<Arguments, ExitCode>,
        stdout: String,
        stderr: String,
    }

    use dev_workflow_core::ExitCode;

    fn run_with(
        args: &[&[u8]],
        columns: &str,
        streams: (bool, bool),
        env: &'static [(&'static str, &'static str)],
        os: FakeOs,
    ) -> Run {
        let state = startup(streams.0, streams.1);
        let lookup = |name: &str| {
            env.iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| value.as_bytes().to_vec())
        };
        let mut py = Runtime::start_with(&state, &lookup, Box::new(os.clone())).unwrap();
        let args = args
            .iter()
            .map(|arg| OsString::from_vec(arg.to_vec()))
            .collect::<Vec<_>>();
        let width = Width {
            columns: Some(columns.as_bytes().to_vec()),
            terminal: &|| None,
        };
        let result = match parse_with(&args, &mut py, &width) {
            Ok(arguments) => Ok(arguments),
            Err(outcome) => Err(py.finish(outcome)),
        };
        Run {
            result,
            stdout: os.written(Stream::Stdout),
            stderr: os.written(Stream::Stderr),
        }
    }

    fn run(args: &[&str]) -> Run {
        let args = args.iter().map(|arg| arg.as_bytes()).collect::<Vec<_>>();
        run_with(&args, "80", (true, true), &[], FakeOs::new())
    }

    const USAGE_80: &str = "usage: md_tables_to_xlsx.py [-h] -o OUTPUT [--source-root SOURCE_ROOT]\n                            inputs [inputs ...]\n";

    #[test]
    fn the_docstring_is_the_legacy_docstring() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py");
        let source = std::fs::read_to_string(path).unwrap();
        let start = source.find("\"\"\"").unwrap() + 3;
        let end = start + source[start..].find("\"\"\"").unwrap();
        assert_eq!(&source[start..end], DOCSTRING);
    }

    /// 誤りの最後の行は旧版の実測(CPython 3.10.11・COLUMNS=80)。
    #[test]
    fn errors_match_the_legacy_text() {
        let cases: &[(&[&str], &str)] = &[
            (
                &[],
                "the following arguments are required: inputs, -o/--output",
            ),
            (&["x"], "the following arguments are required: -o/--output"),
            (&["-o", "y"], "the following arguments are required: inputs"),
            (&["x", "-o"], "argument -o/--output: expected one argument"),
            (
                &["x", "--output"],
                "argument -o/--output: expected one argument",
            ),
            (
                &["x", "-o", "y", "--source-root"],
                "argument --source-root: expected one argument",
            ),
            (
                &["x", "-o", "--source-root", "r"],
                "argument -o/--output: expected one argument",
            ),
            (
                &["x", "-o", "y", "--bogus"],
                "unrecognized arguments: --bogus",
            ),
            (&["x", "-o", "y", "-x"], "unrecognized arguments: -x"),
            (
                &["--bogus"],
                "the following arguments are required: inputs, -o/--output",
            ),
            (
                &["--=x"],
                "ambiguous option: --=x could match --help, --output, --source-root",
            ),
            (&["x", "-o", "y", "z", "w"], "unrecognized arguments: z w"),
            (
                &["-hx"],
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (&["-h="], "argument -h/--help: ignored explicit argument ''"),
            (
                &["--help=1"],
                "argument -h/--help: ignored explicit argument '1'",
            ),
            (&["x", "-ho"], "argument -o/--output: expected one argument"),
            (
                &["x", "-o", "-1.xlsx"],
                "argument -o/--output: expected one argument",
            ),
            (
                &["x", "-o", "y", "--", "--"],
                "unrecognized arguments: -- --",
            ),
            (
                &["x", "-o", "y", "--b=c d"],
                "unrecognized arguments: --b=c d",
            ),
            (
                &["-hé"],
                "argument -h/--help: ignored explicit argument 'é'",
            ),
            (&["x", "-o", "y", "-é"], "unrecognized arguments: -é"),
            (
                &["a", "-o", "y", "b", "-o", "z", "c"],
                "unrecognized arguments: b c",
            ),
            (&["x", "-o", "y", "---"], "unrecognized arguments: ---"),
        ];
        for (args, last) in cases {
            let run = run(args);
            assert_eq!(
                run.result.as_ref().unwrap_err(),
                &ExitCode::new(2),
                "{args:?}"
            );
            assert_eq!(run.stdout, "", "{args:?}");
            assert_eq!(
                run.stderr,
                format!("{USAGE_80}md_tables_to_xlsx.py: error: {last}\n"),
                "{args:?}"
            );
        }
    }

    #[test]
    fn values_follow_argparse() {
        let str = |text: &str| Value::Str(OsString::from(text));
        let strings = |items: &[&str]| items.iter().map(OsString::from).collect::<Vec<_>>();
        let cases: &[(&[&str], Arguments)] = &[
            (
                &["-output", "y"],
                Arguments {
                    inputs: strings(&["y"]),
                    output: str("utput"),
                    source_root: vec![],
                },
            ),
            (
                &["x", "-o=--"],
                Arguments {
                    inputs: strings(&["x"]),
                    output: Value::EmptyList,
                    source_root: vec![],
                },
            ),
            (
                &["x", "-o", "y", "--source-root=--", "--s", "z", "--sour=w"],
                Arguments {
                    inputs: strings(&["x"]),
                    output: str("y"),
                    source_root: vec![Value::EmptyList, str("z"), str("w")],
                },
            ),
            (
                &["x", "-o", "a", "--out", "b", "-oc", "--output=d=e"],
                Arguments {
                    inputs: strings(&["x"]),
                    output: str("d=e"),
                    source_root: vec![],
                },
            ),
            (
                &["-o", "y", "--", "--", "-h"],
                Arguments {
                    inputs: strings(&["--", "-h"]),
                    output: str("y"),
                    source_root: vec![],
                },
            ),
            (
                &["-1", "-o", "-2.5", "--source-root", "-١٢"],
                Arguments {
                    inputs: strings(&["-1"]),
                    output: str("-2.5"),
                    source_root: vec![str("-١٢")],
                },
            ),
            (
                &[
                    "x",
                    "-o",
                    "-1\n",
                    "--source-root",
                    "-",
                    "--source-root",
                    "- 1",
                ],
                Arguments {
                    inputs: strings(&["x"]),
                    output: str("-1\n"),
                    source_root: vec![str("-"), str("- 1")],
                },
            ),
            (
                &["", "", "-o", ""],
                Arguments {
                    inputs: strings(&["", ""]),
                    output: str(""),
                    source_root: vec![],
                },
            ),
        ];
        for (args, expected) in cases {
            let run = run(args);
            assert_eq!(run.result.as_ref().unwrap(), expected, "{args:?}");
            assert_eq!((run.stdout.as_str(), run.stderr.as_str()), ("", ""));
        }
        // UTF-8 でないバイトは surrogateescape で文字にして解析し、値は元のバイトへ戻す。
        let run = run_with(
            &[b"\xff\xfe", b"-o\xc3", b"--source-root=\x80"],
            "80",
            (true, true),
            &[],
            FakeOs::new(),
        );
        let parsed = run.result.unwrap();
        assert_eq!(parsed.inputs, [OsString::from_vec(b"\xff\xfe".to_vec())]);
        assert_eq!(
            parsed.output,
            Value::Str(OsString::from_vec(b"\xc3".to_vec()))
        );
        assert_eq!(
            parsed.source_root,
            [Value::Str(OsString::from_vec(b"\x80".to_vec()))]
        );
        let run = run_with(&[b"-h\xff"], "80", (true, true), &[], FakeOs::new());
        assert_eq!(run.result.unwrap_err(), ExitCode::new(2));
        assert!(
            run.stderr
                .ends_with("error: argument -h/--help: ignored explicit argument '\\udcff'\n"),
            "{}",
            run.stderr
        );
    }

    /// `-h` は、前にある選択肢の誤りより後、後ろにある引数の誤りより先に働く(左から順に処理する)。
    #[test]
    fn help_is_taken_in_order() {
        for args in [
            &["-h"][..],
            &["--help"],
            &["--h"],
            &["-hh"],
            &["-hoX"],
            &["--bogus", "-h"],
            &["-h", "-o"],
            &["x", "-o", "y", "-h", "--bogus"],
        ] {
            let run = run(args);
            assert_eq!(
                run.result.as_ref().unwrap_err(),
                &ExitCode::new(0),
                "{args:?}"
            );
            assert!(run.stdout.starts_with(USAGE_80), "{args:?}");
            assert_eq!(run.stderr, "", "{args:?}");
        }
        let run = run(&["-o", "-h"]);
        assert_eq!(run.result.unwrap_err(), ExitCode::new(2));
    }

    /// 旧版の実測(CPython 3.10.11)の全文。
    #[test]
    fn help_and_usage_wrap_like_python() {
        let help_30 = "usage: md_tables_to_xlsx.py\n       [-h] -o OUTPUT\n       [--source-root SOURCE_ROOT]\n       inputs [inputs ...]\n\nMarkdown 文書内の表を xlsx(または\nCSV)へ変換する(export-doc 用)。\n使い方: python3\nmd_tables_to_xlsx.py\n<input.md> [<input2.md> ...]\n-o <output.xlsx> - 1 表 1\nシート。シート名は「文書名 +\n直前の見出し」(Excel の 31 文字制約に収める)\n- openpyxl\nが無い環境では、出力名と同名のディレクトリへ\nCSV(BOM 付き UTF-8)で縮退出力する - 入\n力は読み取りのみ(変更しない)。同名出力は上書き(冪等)\n- --source-root <dir>\nで元の正本を保護する(繰返し可。省略時は入力の親) - \n正本への出力・最終symlink・複数hardlink・\n特殊ファイルは書込前に拒否する - セル内の\nMarkdown 装飾(**強調**・`コード`・[リン\nク](url)・<br>)はプレーンテキスト化する\n終了コード: 0 = 出力あり / 1 =\n表が見つからない / 2 = 入力エラー・出力先の拒否\n\npositional arguments:\n  inputs\n        入力 Markdown ファイル\n\noptions:\n  -h, --help\n        show this help\n        message and exit\n  -o OUTPUT, --output OUTPUT\n        出力 xlsx パス\n  --source-root SOURCE_ROOT\n        保護する元の正本ディレクトリ（繰返し可。\n        省略時は入力の親）\n";
        assert_eq!(format_help(28, Some(DOCSTRING)), help_30);
        let help_10 = "usage: md_tables_to_xlsx.py\n       [-h]\n       -o\n       OUTPUT\n       [--source-root SOURCE_ROOT]\n       inputs\n       [inputs ...]\n\nMarkdown\n文書内の表を\nxlsx(または CS\nV)へ変換する(exp\nort-doc 用)。\n使い方:\npython3 md_\ntables_to_x\nlsx.py\n<input.md> \n[<input2.md\n> ...] -o <\noutput.xlsx\n> - 1 表 1 シ\nート。シート名は「文書\n名 + 直前の見出し」\n(Excel の 31\n文字制約に収める) -\nopenpyxl が無\nい環境では、出力名と同\n名のディレクトリへ\nCSV(BOM 付き \nUTF-\n8)で縮退出力する -\n入力は読み取りのみ(変\n更しない)。同名出力は\n上書き(冪等) -\n--source-\nroot <dir> \nで元の正本を保護する(\n繰返し可。省略時は入力\nの親) - 正本への出\n力・最終symlink\n・複数hardlink\n・特殊ファイルは書込前\nに拒否する -\nセル内の\nMarkdown 装飾\n(**強調**・`コー\nド`・[リンク](ur\nl)・<br>)はプレ\nーンテキスト化する\n終了コード: 0 =\n出力あり / 1 =\n表が見つからない /\n2 = 入力エラー・出\n力先の拒否\n\npositional arguments:\n  inputs\n    入力 Markdown\n    ファイル\n\noptions:\n  -h, --help\n    show this\n    help\n    message and\n    exit\n  -o OUTPUT, --output OUTPUT\n    出力 xlsx パス\n  --source-root SOURCE_ROOT\n    保護する元の正本ディレ\n    クトリ（繰返し可。省略\n    時は入力の親）\n";
        assert_eq!(format_help(8, Some(DOCSTRING)), help_10);
        let help_oo = "usage: md_tables_to_xlsx.py [-h] -o OUTPUT\n                            [--source-root SOURCE_ROOT]\n                            inputs [inputs ...]\n\npositional arguments:\n  inputs                入力 Markdown ファイル\n\noptions:\n  -h, --help            show this help message and exit\n  -o OUTPUT, --output OUTPUT\n                        出力 xlsx パス\n  --source-root SOURCE_ROOT\n                        保護する元の正本ディレクトリ（繰返し可。省略時は入力の親）\n";
        assert_eq!(format_help(58, None), help_oo);
        let usage_1 = "usage: md_tables_to_xlsx.py\n       [-h]\n       -o\n       OUTPUT\n       [--source-root SOURCE_ROOT]\n       inputs\n       [inputs ...]\n";
        assert_eq!(format_usage(-1), usage_1);
        assert_eq!(format_usage(78), USAGE_80);
        // 使い方の行が 1 行に収まる幅の前・等しい・後(`usage: ` と 83 文字で 90 文字)。
        assert_eq!(format_usage(89).lines().count(), 2);
        assert_eq!(format_usage(90).lines().count(), 1);
        assert_eq!(format_usage(91).lines().count(), 1);
        // 呼出名の後に続けるかどうかの境目(len(prefix) + len(prog) <= 0.75 * 幅 → 幅 36)。
        assert!(format_usage(35).starts_with("usage: md_tables_to_xlsx.py\n"));
        assert!(format_usage(36).starts_with("usage: md_tables_to_xlsx.py [-h]"));
    }

    /// `PYTHONOPTIMIZE=2` では docstring が消え、説明文が出ない。
    #[test]
    fn optimize_two_drops_the_description() {
        let run = run_with(
            &[b"-h"],
            "60",
            (true, true),
            &[("PYTHONOPTIMIZE", "2")],
            FakeOs::new(),
        );
        assert_eq!(run.stdout, format_help(58, None));
        let run = run_with(
            &[b"-h"],
            "60",
            (true, true),
            &[("PYTHONOPTIMIZE", "1")],
            FakeOs::new(),
        );
        assert_eq!(run.stdout, format_help(58, Some(DOCSTRING)));
    }

    /// Python の `TextWrapper()._split` の実測。
    #[test]
    fn textwrap_chunks_match_python() {
        let split = |text: &str| -> Vec<String> {
            textwrap::chunks(&chars(text))
                .into_iter()
                .map(|chunk| {
                    chunk
                        .iter()
                        .map(|&code| char::from_u32(code).unwrap())
                        .collect()
                })
                .collect()
        };
        let cases: &[(&str, &[&str])] = &[
            (
                "Look, goof-ball -- use the -b option!",
                &[
                    "Look,", " ", "goof-", "ball", " ", "--", " ", "use", " ", "the", " ", "-b",
                    " ", "option!",
                ],
            ),
            (
                "a-b-c ab-cd-ef x--y --z a---b 1-2 ab-1 ab-c-d-e",
                &[
                    "a-b-c", " ", "ab-", "cd-", "ef", " ", "x", "--", "y", " ", "--z", " ", "a",
                    "---", "b", " ", "1-2", " ", "ab-1", " ", "ab-", "c-", "d-e",
                ],
            ),
            ("export-doc 用)。", &["export-", "doc", " ", "用)。"]),
            (
                "保護する元の正本ディレクトリ（繰返し可。省略時は入力の親）",
                &["保護する元の正本ディレクトリ（繰返し可。省略時は入力の親）"],
            ),
        ];
        for (text, expected) in cases {
            assert_eq!(split(text), *expected, "{text}");
        }
        let docstring = split_lines_text(DOCSTRING);
        let docstring: String = docstring
            .iter()
            .map(|&code| char::from_u32(code).unwrap())
            .collect();
        let expected = [
            "Markdown",
            " ",
            "文書内の表を",
            " ",
            "xlsx(または",
            " ",
            "CSV)へ変換する(export-",
            "doc",
            " ",
            "用)。",
            " ",
            "使い方:",
            " ",
            "python3",
            " ",
            "md_tables_to_xlsx.py",
            " ",
            "<input.md>",
            " ",
            "[<input2.md>",
            " ",
            "...]",
            " ",
            "-o",
            " ",
            "<output.xlsx>",
            " ",
            "-",
            " ",
            "1",
            " ",
            "表",
            " ",
            "1",
            " ",
            "シート。シート名は「文書名",
            " ",
            "+",
            " ",
            "直前の見出し」(Excel",
            " ",
            "の",
            " ",
            "31",
            " ",
            "文字制約に収める)",
            " ",
            "-",
            " ",
            "openpyxl",
            " ",
            "が無い環境では、出力名と同名のディレクトリへ",
            " ",
            "CSV(BOM",
            " ",
            "付き",
            " ",
            "UTF-8)で縮退出力する",
            " ",
            "-",
            " ",
            "入力は読み取りのみ(変更しない)。同名出力は上書き(冪等)",
            " ",
            "-",
            " ",
            "--source-",
            "root",
            " ",
            "<dir>",
            " ",
            "で元の正本を保護する(繰返し可。省略時は入力の親)",
            " ",
            "-",
            " ",
            "正本への出力・最終symlink・複数hardlink・特殊ファイルは書込前に拒否する",
            " ",
            "-",
            " ",
            "セル内の",
            " ",
            "Markdown",
            " ",
            "装飾(**強調**・`コード`・[リンク](url)・<br>)はプレーンテキスト化する",
            " ",
            "終了コード:",
            " ",
            "0",
            " ",
            "=",
            " ",
            "出力あり",
            " ",
            "/",
            " ",
            "1",
            " ",
            "=",
            " ",
            "表が見つからない",
            " ",
            "/",
            " ",
            "2",
            " ",
            "=",
            " ",
            "入力エラー・出力先の拒否",
        ];
        assert_eq!(split(&docstring), expected);
    }

    /// 固定の文の ASCII 以外の文字は、Python の `\w` の判定(実測)と同じに分ける。
    #[test]
    fn fixed_text_characters_are_classified_like_python() {
        const WORD: &str = "あいかがきしすたつでとなにのはへまみめらりるをァィイエキクコシスセテデトドパフプラリルレンー上了付使保元先入内冪出制前力化収取可同名否境変字強拒換数文方時更書最本正殊無特環用略直省等約終縮繰表装複見親読調護込返退飾";
        for text in [
            DOCSTRING,
            HELP_HELP,
            INPUTS_HELP,
            OUTPUT_HELP,
            SOURCE_ROOT_HELP,
        ] {
            for character in text.chars().filter(|c| !c.is_ascii()) {
                let in_word = WORD.contains(character);
                let in_non_word = textwrap::FIXED_TEXT_NON_WORD.contains(&character);
                assert!(in_word != in_non_word, "{character}");
            }
        }
    }

    #[test]
    fn python_int_reads_like_python() {
        let cases: &[(&str, Option<i64>)] = &[
            ("80", Some(80)),
            ("+80", Some(80)),
            ("1_0", Some(10)),
            (" 50 ", Some(50)),
            ("\t50\n", Some(50)),
            ("\t\n5\x0b\x0c\r", Some(5)),
            ("0", Some(0)),
            ("-5", Some(-5)),
            ("-0", Some(0)),
            ("0050", Some(50)),
            ("٥٠", Some(50)),
            ("５０", Some(50)),
            ("٥_٠", Some(50)),
            ("5\u{85}", Some(5)),
            ("\u{3000}5", Some(5)),
            ("abc", None),
            ("", None),
            (" ", None),
            ("+", None),
            ("80.0", None),
            ("0x50", None),
            ("_50", None),
            ("50_", None),
            ("5__0", None),
            ("+_5", None),
            ("\x1c5", None),
            ("5\x7f", None),
            ("5 5", None),
        ];
        for (text, expected) in cases {
            assert_eq!(python_int(&PyStr::from(*text), 4300), *expected, "{text:?}");
        }
        assert_eq!(python_int(&PyStr::from_os_bytes(b"5\xff"), 4300), None);
        assert_eq!(
            python_int(&PyStr::from("99999999999999999999999"), 4300),
            Some(WIDTH_CAP)
        );
        // 桁数の上限(既定 4300。640 桁以下は数えない。0 は上限なし)。前・等しい・後。
        for (digits, limit, ok) in [
            (4300, 4300, true),
            (4301, 4300, false),
            (4301, 0, true),
            (700, 640, false),
            (640, 640, true),
            (641, 640, false),
        ] {
            let text = "1".repeat(digits);
            assert_eq!(
                python_int(&PyStr::from(text.as_str()), limit).is_some(),
                ok,
                "{digits} {limit}"
            );
        }
        let underscored = format!("{}1", "1_".repeat(4300));
        assert_eq!(python_int(&PyStr::from(underscored.as_str()), 4300), None);
    }

    #[test]
    fn columns_follow_shutil() {
        let width = |columns: Option<&str>, terminal: Option<u16>| {
            let terminal = move || terminal;
            columns_of(
                &Width {
                    columns: columns.map(|text| text.as_bytes().to_vec()),
                    terminal: &terminal,
                },
                4300,
            )
        };
        assert_eq!(width(None, None), 78);
        assert_eq!(width(Some("30"), Some(57)), 28);
        assert_eq!(width(None, Some(57)), 55);
        // 3.10 は端末の幅の 0 もそのまま使う。
        assert_eq!(width(None, Some(0)), -2);
        for invalid in ["0", "-5", "abc", "", "80.0"] {
            assert_eq!(width(Some(invalid), Some(57)), 55, "{invalid}");
            assert_eq!(width(Some(invalid), None), 78, "{invalid}");
        }
        assert_eq!(width(Some("1"), None), -1);
    }

    fn columns_of(width: &Width, max_digits: u32) -> i64 {
        columns(width, max_digits)
    }

    #[test]
    fn negative_numbers_use_unicode_digits_and_dollar() {
        for number in ["-1", "-١", "-١.٢", "-.٥", "-१२", "-１２.５", "-1\n"] {
            assert!(looks_like_negative_number(&chars(number)), "{number:?}");
        }
        for text in [
            "-", "-1.", "-1.xlsx", "-١x", "-1\r", "-1\n\n", "-1.2.3", "--1", "-a",
        ] {
            assert!(!looks_like_negative_number(&chars(text)), "{text:?}");
        }
    }

    /// 起動時に閉じた fd 1・fd 2(`sys.stdout`・`sys.stderr` が `None`)での argparse(旧版の実測)。
    #[test]
    fn closed_streams_follow_argparse() {
        let help = |streams| run_with(&[b"--help"], "80", streams, &[], FakeOs::new());
        let error = |args: &[&[u8]], streams| run_with(args, "80", streams, &[], FakeOs::new());
        // fd 1 が閉じた --help: 説明を標準エラーへ、終了 0。
        let run = help((false, true));
        assert_eq!(run.result.unwrap_err(), ExitCode::new(0));
        assert!(run.stderr.starts_with(USAGE_80));
        assert_eq!(run.stdout, "");
        // fd 2 だけが閉じた --help: ふつうどおり。
        let run = help((true, false));
        assert_eq!(run.result.unwrap_err(), ExitCode::new(0));
        assert!(run.stdout.starts_with(USAGE_80));
        // 両方が閉じた --help: 何も出さず終了 1。
        let run = help((false, false));
        assert_eq!(run.result.unwrap_err(), ExitCode::new(1));
        assert_eq!((run.stdout.as_str(), run.stderr.as_str()), ("", ""));
        // fd 2 が閉じた引数の誤りと必須の欠落: 使い方の行だけを標準出力へ、終了 1。
        for args in [&[&b"x"[..], b"-o", b"y", b"--bogus"][..], &[b"x"]] {
            let run = error(args, (true, false));
            assert_eq!(run.result.unwrap_err(), ExitCode::new(1));
            assert_eq!(run.stdout, USAGE_80);
            assert_eq!(run.stderr, "");
        }
        // 両方が閉じた引数の誤り: 何も出さず終了 1。
        let run = error(&[b"--bogus"], (false, false));
        assert_eq!(run.result.unwrap_err(), ExitCode::new(1));
        assert_eq!((run.stdout.as_str(), run.stderr.as_str()), ("", ""));
        // fd 1 だけが閉じた引数の誤り: ふつうどおり標準エラーへ、終了 2。
        let run = error(&[b"--bogus"], (false, true));
        assert_eq!(run.result.unwrap_err(), ExitCode::new(2));
        assert!(run.stderr.starts_with(USAGE_80));
        assert!(
            run.stderr
                .ends_with("error: the following arguments are required: inputs, -o/--output\n")
        );
    }

    fn raised(
        args: &[&[u8]],
        env: &'static [(&'static str, &'static str)],
        os: FakeOs,
    ) -> PyException {
        let state = startup(true, true);
        let lookup = |name: &str| {
            env.iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| value.as_bytes().to_vec())
        };
        let mut py = Runtime::start_with(&state, &lookup, Box::new(os)).unwrap();
        let args = args
            .iter()
            .map(|arg| OsString::from_vec(arg.to_vec()))
            .collect::<Vec<_>>();
        let width = Width {
            columns: Some(b"80".to_vec()),
            terminal: &|| None,
        };
        match parse_with(&args, &mut py, &width) {
            Err(Outcome::Raised(uncaught)) => uncaught.exception,
            _ => panic!("例外にならなかった"),
        }
    }

    fn places(frames: &[Frame]) -> Vec<(&'static str, u32)> {
        frames
            .iter()
            .map(|frame| (frame.file, frame.line))
            .collect()
    }

    /// 書き込みの失敗の呼出履歴は、旧版の実測の段(argparse.py の行番号)と同じ。
    #[test]
    fn write_failures_raise_through_the_argparse_frames() {
        let os = FakeOs::new();
        os.steps(Stream::Stdout, &[Step::Fail(28)]);
        let error = raised(&[b"--help"], &[("PYTHONUNBUFFERED", "1")], os);
        assert_eq!(error.kind, types::OS_ERROR);
        assert_eq!(
            places(&error.traceback),
            [
                ("md_tables_to_xlsx.py", 241),
                ("md_tables_to_xlsx.py", 206),
                ("argparse.py", 1833),
                ("argparse.py", 1866),
                ("argparse.py", 2079),
                ("argparse.py", 2019),
                ("argparse.py", 1943),
                ("argparse.py", 1106),
                ("argparse.py", 2567),
                ("argparse.py", 2573),
            ]
        );
        let failing = || {
            let os = FakeOs::new();
            os.steps(Stream::Stderr, &[Step::Fail(5)]);
            os
        };
        let usage_tail = [
            ("argparse.py", 2592),
            ("argparse.py", 2562),
            ("argparse.py", 2573),
        ];
        let head = [("md_tables_to_xlsx.py", 241), ("md_tables_to_xlsx.py", 206)];
        let error = raised(&[b"x", b"-o", b"y", b"z"], &[], failing());
        assert_eq!(
            places(&error.traceback),
            [&head[..], &[("argparse.py", 1836)], &usage_tail].concat()
        );
        assert!(error.context.is_none());
        let error = raised(&[b"x"], &[], failing());
        assert_eq!(
            places(&error.traceback),
            [
                &head[..],
                &[
                    ("argparse.py", 1833),
                    ("argparse.py", 1866),
                    ("argparse.py", 2107)
                ],
                &usage_tail
            ]
            .concat()
        );
        let error = raised(&[b"--=x"], &[], failing());
        assert_eq!(
            places(&error.traceback),
            [
                &head[..],
                &[
                    ("argparse.py", 1833),
                    ("argparse.py", 1866),
                    ("argparse.py", 1910),
                    ("argparse.py", 2230)
                ],
                &usage_tail
            ]
            .concat()
        );
        // ArgumentError は処理中の例外として連鎖する。
        for (args, inner) in [
            (
                &[&b"x"[..], b"-o"][..],
                &[("argparse.py", 2009), ("argparse.py", 2174)][..],
            ),
            (&[b"x", b"-ox", b"-hz"], &[("argparse.py", 1987)]),
            (&[b"x", b"--help=1"], &[("argparse.py", 2001)]),
        ] {
            let error = raised(args, &[], failing());
            assert_eq!(
                places(&error.traceback),
                [
                    &head[..],
                    &[("argparse.py", 1833), ("argparse.py", 1869)],
                    &usage_tail
                ]
                .concat()
            );
            let context = error.context.as_ref().expect("ArgumentError が連鎖する");
            assert_eq!(context.kind, ARGUMENT_ERROR);
            assert_eq!(
                places(&context.traceback),
                [&[("argparse.py", 1866), ("argparse.py", 2079)][..], inner].concat()
            );
        }
        // 2 つ目の書き込み(error: の行)の失敗は exit を通る。
        let os = FakeOs::new();
        os.steps(Stream::Stderr, &[Step::Accept(usize::MAX), Step::Fail(5)]);
        let error = raised(&[b"x", b"-o", b"y", b"z"], &[], os);
        assert_eq!(
            places(&error.traceback),
            [
                &head[..],
                &[
                    ("argparse.py", 1836),
                    ("argparse.py", 2594),
                    ("argparse.py", 2580),
                    ("argparse.py", 2573)
                ]
            ]
            .concat()
        );
    }

    /// 段の表は argparse.py(本家 CPython 3.10.11)と旧スクリプトの実物の行と同じ(旧スクリプトの段は
    /// ここで、標準ライブラリの段は検査入口の `R4E_CONTRACT_FRAMES` で確かめる)。
    #[test]
    fn script_frames_match_the_legacy_script() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py");
        let source = std::fs::read_to_string(path).unwrap();
        let lines = source.split('\n').collect::<Vec<_>>();
        let frame = frames::MAIN_PARSES_ARGS;
        assert_eq!(lines[frame.line as usize - 1], frame.source);
    }
}
