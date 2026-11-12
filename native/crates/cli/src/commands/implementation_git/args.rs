//! 旧版の`build_parser()`と、Python 3.10.11のargparseの解析(`parse_args`)。
//!
//! 解析の手順は`argparse.py`の`parse_known_args`・`_parse_known_args`・`_parse_optional`・
//! `_get_option_tuples`・`consume_optional`・`consume_positionals`・`_get_values`の順と同じにする。
//! 引数は解析の前に厳密なUTF-8と確かめてあるので、Rustの文字列で持つ。長さはコードポイントで数える。
//!
//! 値の形も旧版に合わせる。`--x=--`の形では、argparseが`--`を外して空のリストを入れる
//! (`Arg::EmptyList`)。旧版はその値で後の処理を続け、属性や型の例外になる。

use super::super::r4g_support::pyint::{self, PyInt};
use super::super::r4g_support::text::{self, PyText, from_str};

/// 旧版のパーサー(全体と、2段のsubparsersの各入口)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ParserId {
    Top,
    Prepare,
    Run,
    Status,
    Diff,
    Log,
    Show,
    LsFiles,
}

/// 値を入れる先(`dest`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dest {
    Cwd,
    Accept,
    Expect,
    Format,
    Untracked,
    Cached,
    Base,
    Target,
    Context,
    FromRev,
    ToRev,
    Limit,
    Rev,
    File,
    FileMode,
    Path,
}

/// 動作の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// `-h`/`--help`(`nargs=0`。helpを出して終了0)。
    Help,
    /// 旧版の`Once`(`nargs=None`。重複を拒む)。
    Once,
    /// 旧版の`Once`で`nargs=0`(`--cached`)。
    Flag,
    /// `action="append"`(`--path`)。
    Append,
}

/// 旧版の`add_argument`の1つ。
pub struct OptionSpec {
    pub strings: &'static [&'static str],
    pub dest: Option<Dest>,
    pub kind: Kind,
    pub required: bool,
    pub choices: Option<&'static [&'static str]>,
    /// `type=int`。
    pub int: bool,
    /// `dest.upper()`(既定のmetavar)。
    pub metavar: &'static str,
    pub help: &'static str,
}

/// `add_subparsers(dest=..., title=...)`と`add_parser(name, help=...)`。
pub struct SubSpec {
    pub dest: &'static str,
    pub title: &'static str,
    pub choices: &'static [(&'static str, &'static str, ParserId)],
}

/// 1つのパーサー。
pub struct ParserSpec {
    pub prog: &'static str,
    pub description: Option<&'static str>,
    /// `_actions`の順(先頭は`-h`)。subparsersは最後に置く(`subparsers`)。
    pub options: &'static [OptionSpec],
    pub subparsers: Option<SubSpec>,
}

const HELP: OptionSpec = OptionSpec {
    strings: &["-h", "--help"],
    dest: None,
    kind: Kind::Help,
    required: false,
    choices: None,
    int: false,
    metavar: "",
    help: "使い方を表示して終了",
};

const fn once(
    name: &'static str,
    dest: Dest,
    metavar: &'static str,
    help: &'static str,
) -> OptionSpec {
    OptionSpec {
        strings: string_list(name),
        dest: Some(dest),
        kind: Kind::Once,
        required: false,
        choices: None,
        int: false,
        metavar,
        help,
    }
}

const fn string_list(name: &'static str) -> &'static [&'static str] {
    match name.as_bytes() {
        b"--cwd" => &["--cwd"],
        b"--accept" => &["--accept"],
        b"--expect-context-sha256" => &["--expect-context-sha256"],
        b"--format" => &["--format"],
        b"--untracked" => &["--untracked"],
        b"--cached" => &["--cached"],
        b"--base" => &["--base"],
        b"--target" => &["--target"],
        b"--context" => &["--context"],
        b"--from" => &["--from"],
        b"--to" => &["--to"],
        b"--limit" => &["--limit"],
        b"--rev" => &["--rev"],
        b"--file" => &["--file"],
        b"--mode" => &["--mode"],
        _ => &["--path"],
    }
}

const fn required(spec: OptionSpec) -> OptionSpec {
    OptionSpec {
        required: true,
        ..spec
    }
}

const fn choices(spec: OptionSpec, values: &'static [&'static str]) -> OptionSpec {
    OptionSpec {
        choices: Some(values),
        ..spec
    }
}

const fn int(spec: OptionSpec) -> OptionSpec {
    OptionSpec { int: true, ..spec }
}

const PATH: OptionSpec = OptionSpec {
    strings: &["--path"],
    dest: Some(Dest::Path),
    kind: Kind::Append,
    required: false,
    choices: None,
    int: false,
    metavar: "PATH",
    help: "ルート相対のリテラルパス。反復可能",
};

const CWD: OptionSpec = required(once("--cwd", Dest::Cwd, "CWD", "本体リポジトリのルート"));
const ACCEPT: OptionSpec = once(
    "--accept",
    Dest::Accept,
    "ACCEPT",
    "最後に利用者が承認した一覧の値",
);

const PREPARE: &[OptionSpec] = &[HELP, CWD, ACCEPT];
const RUN: &[OptionSpec] = &[
    HELP,
    CWD,
    ACCEPT,
    required(once(
        "--expect-context-sha256",
        Dest::Expect,
        "EXPECT_CONTEXT_SHA256",
        "委託元から受け取った64桁の照合値",
    )),
];
const STATUS: &[OptionSpec] = &[
    HELP,
    choices(
        once(
            "--format",
            Dest::Format,
            "FORMAT",
            "表示形式（既定: short）",
        ),
        &["short", "porcelain"],
    ),
    choices(
        once(
            "--untracked",
            Dest::Untracked,
            "UNTRACKED",
            "未追跡の表示（既定: normal）",
        ),
        &["normal", "all", "no"],
    ),
    PATH,
];
const DIFF: &[OptionSpec] = &[
    HELP,
    OptionSpec {
        kind: Kind::Flag,
        ..once("--cached", Dest::Cached, "CACHED", "index と比較")
    },
    once("--base", Dest::Base, "BASE", "比較元の commit"),
    once(
        "--target",
        Dest::Target,
        "TARGET",
        "比較先の commit（--base が必要）",
    ),
    choices(
        once(
            "--format",
            Dest::Format,
            "FORMAT",
            "差分の形式（既定: patch）",
        ),
        &["patch", "stat", "name-only", "name-status"],
    ),
    int(once(
        "--context",
        Dest::Context,
        "CONTEXT",
        "前後の行数 0〜100（既定: 3）",
    )),
    PATH,
];
const LOG: &[OptionSpec] = &[
    HELP,
    once(
        "--from",
        Dest::FromRev,
        "FROM_REV",
        "表示から除く側の commit",
    ),
    once(
        "--to",
        Dest::ToRev,
        "TO_REV",
        "履歴をたどる起点（既定: HEAD）",
    ),
    int(once(
        "--limit",
        Dest::Limit,
        "LIMIT",
        "表示件数 1〜1000（既定: 20）",
    )),
    choices(
        once(
            "--format",
            Dest::Format,
            "FORMAT",
            "履歴の形式（既定: oneline）",
        ),
        &["oneline", "fuller"],
    ),
    PATH,
];
const SHOW: &[OptionSpec] = &[
    HELP,
    once("--rev", Dest::Rev, "REV", "表示する commit（既定: HEAD）"),
    choices(
        once(
            "--format",
            Dest::Format,
            "FORMAT",
            "表示形式（既定: patch。--file と併用不可）",
        ),
        &["patch", "stat", "name-only"],
    ),
    once(
        "--file",
        Dest::File,
        "FILE",
        "commit 内の単一ファイル。リンクは辿らない",
    ),
];
const LS_FILES: &[OptionSpec] = &[
    HELP,
    choices(
        once(
            "--mode",
            Dest::FileMode,
            "FILE_MODE",
            "対象（既定: tracked。untracked は ignore 済みを除く）",
        ),
        &["tracked", "untracked"],
    ),
    PATH,
];

/// 旧版の`EXIT_HELP`(各パーサーのepilog)。
pub const EXIT_HELP: &str = "終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。";

pub fn spec(id: ParserId) -> ParserSpec {
    match id {
        ParserId::Top => ParserSpec {
            prog: "implementation-git.py",
            description: Some("承認済みフィルターを無効化する、5操作専用の Git 読取り入口。"),
            options: &[HELP],
            subparsers: Some(SubSpec {
                dest: "mode",
                title: "入口",
                choices: &[
                    (
                        "prepare",
                        "委託直前の確認と承認情報の JSON 出力",
                        ParserId::Prepare,
                    ),
                    ("run", "承認情報を照合して指定した操作を実行", ParserId::Run),
                ],
            }),
        },
        ParserId::Prepare => ParserSpec {
            prog: "implementation-git.py prepare",
            description: None,
            options: PREPARE,
            subparsers: None,
        },
        ParserId::Run => ParserSpec {
            prog: "implementation-git.py run",
            description: None,
            options: RUN,
            subparsers: Some(SubSpec {
                dest: "operation",
                title: "読取り操作",
                choices: &[
                    ("status", "変更したファイルを表示", ParserId::Status),
                    (
                        "diff",
                        "作業ツリー・index・履歴の差分を表示",
                        ParserId::Diff,
                    ),
                    ("log", "履歴を表示（既定: HEAD の先頭20件）", ParserId::Log),
                    ("show", "1 commit またはその1ファイルを表示", ParserId::Show),
                    (
                        "ls-files",
                        "記録済みか未記録のファイルを表示",
                        ParserId::LsFiles,
                    ),
                ],
            }),
        },
        ParserId::Status => leaf("implementation-git.py run status", STATUS),
        ParserId::Diff => leaf("implementation-git.py run diff", DIFF),
        ParserId::Log => leaf("implementation-git.py run log", LOG),
        ParserId::Show => leaf("implementation-git.py run show", SHOW),
        ParserId::LsFiles => leaf("implementation-git.py run ls-files", LS_FILES),
    }
}

fn leaf(prog: &'static str, options: &'static [OptionSpec]) -> ParserSpec {
    ParserSpec {
        prog,
        description: None,
        options,
        subparsers: None,
    }
}

/// 文字の値(`str`か、`--x=--`で入る空のリスト)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Arg {
    Str(String),
    EmptyList,
}

/// `type=int`の値(`int`か、空のリスト)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Number {
    Int(PyInt),
    EmptyList,
}

/// 解析の結果(旧版の`Namespace`)。`None`は旧版の`None`。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Namespace {
    pub mode: Option<String>,
    pub operation: Option<String>,
    pub cwd: Option<Arg>,
    pub accept: Option<Arg>,
    pub expect: Option<Arg>,
    pub format: Option<Arg>,
    pub untracked: Option<Arg>,
    pub cached: bool,
    pub base: Option<Arg>,
    pub target: Option<Arg>,
    pub context: Option<Number>,
    pub from_rev: Option<Arg>,
    pub to_rev: Option<Arg>,
    pub limit: Option<Number>,
    pub rev: Option<Arg>,
    pub file: Option<Arg>,
    pub file_mode: Option<Arg>,
    pub path: Vec<Arg>,
}

/// 解析が止まる理由。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Stop {
    /// `parser.error(message)`(旧版の`Parser.error`が`ERROR [input] 引数が不正: ...`の`Failure`にする)。
    Error(PyText),
    /// 旧版の`Once`の重複(`ERROR [input] オプションが重複: <option_string>`)。
    Duplicate(String),
    /// `-h`/`--help`。そのパーサーのhelpを出して終了0。
    Help(ParserId),
}

/// `ArgumentError`(そのパーサーの`parse_known_args`が`error(str(err))`にする)。
struct ArgumentError {
    name: Option<String>,
    message: PyText,
}

impl ArgumentError {
    fn text(&self) -> PyText {
        match &self.name {
            None => self.message.clone(),
            Some(name) => {
                let mut out = from_str(&format!("argument {name}: "));
                out.extend_from_slice(&self.message);
                out
            }
        }
    }
}

/// 動作の番号(`options`の添字か、subparsers)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Action {
    Option(usize),
    Subparsers,
}

/// `_parse_optional`の結果(動作・使った名前・`=`の後の値)。
#[derive(Clone, Debug)]
struct OptionTuple {
    action: Option<usize>,
    option_string: String,
    explicit: Option<String>,
}

fn chars(text: &str) -> usize {
    text.chars().count()
}

fn repr(text: &str) -> PyText {
    text::repr(&from_str(text))
}

fn action_name(spec: &ParserSpec, action: Action) -> String {
    match action {
        Action::Option(index) => spec.options[index].strings.join("/"),
        Action::Subparsers => spec
            .subparsers
            .as_ref()
            .map(|sub| sub.dest)
            .unwrap_or("")
            .to_string(),
    }
}

fn lookup(spec: &ParserSpec, name: &str) -> Option<usize> {
    spec.options
        .iter()
        .position(|option| option.strings.contains(&name))
}

/// `_negative_number_matcher`(`^-\d+$|^-\d*\.\d+$`。`$`は最後の改行の前にも一致する)。
fn looks_negative(arg: &str) -> bool {
    let body = arg.strip_suffix('\n').unwrap_or(arg);
    let Some(rest) = body.strip_prefix('-') else {
        return false;
    };
    let digit = |c: char| text::decimal(u32::from(c)).is_some();
    let all_digits = !rest.is_empty() && rest.chars().all(digit);
    let fraction = match rest.split_once('.') {
        Some((head, tail)) => {
            head.chars().all(digit) && !tail.is_empty() && tail.chars().all(digit)
        }
        None => false,
    };
    all_digits || fraction
}

struct Parser<'a> {
    id: ParserId,
    spec: ParserSpec,
    max_digits: u64,
    ns: &'a mut Namespace,
}

/// `parse_known_args`。戻り値は解析しなかった引数。
fn parse_known_args(
    id: ParserId,
    args: &[String],
    ns: &mut Namespace,
    max_digits: u64,
) -> Result<Vec<String>, Stop> {
    let spec = spec(id);
    set_defaults(id, ns);
    let mut parser = Parser {
        id,
        spec,
        max_digits,
        ns,
    };
    let mut unrecognized = Vec::new();
    let mut extras = match parser.parse_inner(args, &mut unrecognized) {
        Ok(extras) => extras,
        Err(Inner::Argument(error)) => return Err(Stop::Error(error.text())),
        Err(Inner::Stop(stop)) => return Err(stop),
    };
    extras.extend(unrecognized);
    Ok(extras)
}

/// 既定値(`parse_known_args`の最初。そのパーサーの`dest`へ入れる)。
fn set_defaults(id: ParserId, ns: &mut Namespace) {
    let text = |value: &str| Some(Arg::Str(value.to_string()));
    let number = |value: u32| {
        Some(Number::Int(PyInt {
            negative: false,
            digits: value.to_string(),
        }))
    };
    match id {
        ParserId::Top => ns.mode = None,
        ParserId::Prepare => {
            ns.cwd = None;
            ns.accept = None;
        }
        ParserId::Run => {
            ns.cwd = None;
            ns.accept = None;
            ns.expect = None;
            ns.operation = None;
        }
        ParserId::Status => {
            ns.format = text("short");
            ns.untracked = text("normal");
            ns.path = Vec::new();
        }
        ParserId::Diff => {
            ns.cached = false;
            ns.base = None;
            ns.target = None;
            ns.format = text("patch");
            ns.context = number(3);
            ns.path = Vec::new();
        }
        ParserId::Log => {
            ns.from_rev = None;
            ns.to_rev = text("HEAD");
            ns.limit = number(20);
            ns.format = text("oneline");
            ns.path = Vec::new();
        }
        ParserId::Show => {
            ns.rev = text("HEAD");
            ns.format = None;
            ns.file = None;
        }
        ParserId::LsFiles => {
            ns.file_mode = text("tracked");
            ns.path = Vec::new();
        }
    }
}

enum Inner {
    Argument(ArgumentError),
    Stop(Stop),
}

impl From<Stop> for Inner {
    fn from(stop: Stop) -> Self {
        Self::Stop(stop)
    }
}

impl Parser<'_> {
    fn error(message: &str) -> Inner {
        Inner::Stop(Stop::Error(from_str(message)))
    }

    fn argument_error(&self, action: Action, message: PyText) -> Inner {
        Inner::Argument(ArgumentError {
            name: Some(action_name(&self.spec, action)),
            message,
        })
    }

    /// `_parse_optional`。`None`は位置引数。
    fn parse_optional(&self, arg: &str) -> Result<Option<OptionTuple>, Inner> {
        if arg.is_empty() || !arg.starts_with('-') {
            return Ok(None);
        }
        if let Some(index) = lookup(&self.spec, arg) {
            return Ok(Some(OptionTuple {
                action: Some(index),
                option_string: arg.to_string(),
                explicit: None,
            }));
        }
        if chars(arg) == 1 {
            return Ok(None);
        }
        if let Some((name, explicit)) = arg.split_once('=')
            && let Some(index) = lookup(&self.spec, name)
        {
            return Ok(Some(OptionTuple {
                action: Some(index),
                option_string: name.to_string(),
                explicit: Some(explicit.to_string()),
            }));
        }
        let tuples = self.option_tuples(arg);
        if tuples.len() > 1 {
            let names: Vec<&str> = tuples
                .iter()
                .map(|tuple| tuple.option_string.as_str())
                .collect();
            return Err(Self::error(&format!(
                "ambiguous option: {arg} could match {}",
                names.join(", ")
            )));
        }
        if let Some(tuple) = tuples.into_iter().next() {
            return Ok(Some(tuple));
        }
        if looks_negative(arg) {
            return Ok(None);
        }
        if arg.contains(' ') {
            return Ok(None);
        }
        Ok(Some(OptionTuple {
            action: None,
            option_string: arg.to_string(),
            explicit: None,
        }))
    }

    /// `_get_option_tuples`(`allow_abbrev=False`なので、`--`で始まる名前は候補を作らない)。
    fn option_tuples(&self, arg: &str) -> Vec<OptionTuple> {
        let mut result = Vec::new();
        let second = arg.chars().nth(1);
        if second == Some('-') {
            return result;
        }
        let short: String = arg.chars().take(2).collect();
        let short_explicit: String = arg.chars().skip(2).collect();
        for (index, option) in self.spec.options.iter().enumerate() {
            for name in option.strings {
                if *name == short {
                    result.push(OptionTuple {
                        action: Some(index),
                        option_string: (*name).to_string(),
                        explicit: Some(short_explicit.clone()),
                    });
                } else if name.starts_with(arg) {
                    result.push(OptionTuple {
                        action: Some(index),
                        option_string: (*name).to_string(),
                        explicit: None,
                    });
                }
            }
        }
        result
    }

    /// `_match_argument`(任意の引数。`nargs=None`は`(A)`、`nargs=0`は`()`)。
    fn match_argument(&self, index: usize, pattern: &str) -> Result<usize, Inner> {
        match self.spec.options[index].kind {
            Kind::Help | Kind::Flag => Ok(0),
            Kind::Once | Kind::Append => {
                if pattern.starts_with('A') {
                    Ok(1)
                } else {
                    Err(self
                        .argument_error(Action::Option(index), from_str("expected one argument")))
                }
            }
        }
    }

    fn parse_inner(
        &mut self,
        args: &[String],
        unrecognized: &mut Vec<String>,
    ) -> Result<Vec<String>, Inner> {
        // 引数の型の並び('O'・'A'・'-')。`--`の後はすべて'A'。
        let mut options: Vec<Option<OptionTuple>> = vec![None; args.len()];
        let mut pattern = String::new();
        let mut index = 0;
        while index < args.len() {
            if args[index] == "--" {
                pattern.push('-');
                for _ in index + 1..args.len() {
                    pattern.push('A');
                }
                break;
            }
            match self.parse_optional(&args[index])? {
                None => pattern.push('A'),
                Some(tuple) => {
                    options[index] = Some(tuple);
                    pattern.push('O');
                }
            }
            index += 1;
        }
        let pattern: Vec<char> = pattern.chars().collect();
        let option_indices: Vec<usize> =
            (0..args.len()).filter(|i| options[*i].is_some()).collect();
        let mut seen: Vec<Action> = Vec::new();
        let mut once_seen: Vec<Dest> = Vec::new();
        let mut positionals: Vec<Action> = if self.spec.subparsers.is_some() {
            vec![Action::Subparsers]
        } else {
            Vec::new()
        };
        let mut extras: Vec<String> = Vec::new();
        let mut start = 0;
        let max_option = option_indices.last().copied();
        while max_option.is_some_and(|max| start <= max) {
            let next = *option_indices
                .iter()
                .find(|i| **i >= start)
                .expect("the loop runs while an option remains");
            if start != next {
                let end = self.consume_positionals(
                    args,
                    &pattern,
                    start,
                    &mut positionals,
                    &mut seen,
                    unrecognized,
                )?;
                if end > start {
                    start = end;
                    continue;
                }
                start = end;
            }
            if options[start].is_none() {
                extras.extend_from_slice(&args[start..next]);
                start = next;
            }
            start = self.consume_optional(
                args,
                &pattern,
                start,
                options[start].clone().expect("an option is at this index"),
                &mut seen,
                &mut once_seen,
                &mut extras,
            )?;
        }
        let stop = self.consume_positionals(
            args,
            &pattern,
            start,
            &mut positionals,
            &mut seen,
            unrecognized,
        )?;
        extras.extend_from_slice(&args[stop..]);
        let missing: Vec<String> = self
            .spec
            .options
            .iter()
            .enumerate()
            .filter(|(index, option)| option.required && !seen.contains(&Action::Option(*index)))
            .map(|(index, _)| action_name(&self.spec, Action::Option(index)))
            .collect();
        if !missing.is_empty() {
            return Err(Self::error(&format!(
                "the following arguments are required: {}",
                missing.join(", ")
            )));
        }
        Ok(extras)
    }

    #[allow(clippy::too_many_arguments)]
    fn consume_optional(
        &mut self,
        args: &[String],
        pattern: &[char],
        start: usize,
        tuple: OptionTuple,
        seen: &mut Vec<Action>,
        once_seen: &mut Vec<Dest>,
        extras: &mut Vec<String>,
    ) -> Result<usize, Inner> {
        let OptionTuple {
            mut action,
            mut option_string,
            mut explicit,
        } = tuple;
        let mut tuples: Vec<(usize, Vec<String>, String)> = Vec::new();
        let stop;
        loop {
            let Some(index) = action else {
                extras.push(args[start].clone());
                return Ok(start + 1);
            };
            match explicit.take() {
                Some(value) => {
                    let count = self.match_argument(index, "A")?;
                    let second = option_string.chars().nth(1);
                    if count == 0 && second != Some('-') && !value.is_empty() {
                        tuples.push((index, Vec::new(), option_string.clone()));
                        let first: char = value.chars().next().expect("not empty");
                        let rest: String = value.chars().skip(1).collect();
                        option_string = format!("-{first}");
                        match lookup(&self.spec, &option_string) {
                            Some(next) => {
                                action = Some(next);
                                explicit = if rest.is_empty() { None } else { Some(rest) };
                            }
                            None => {
                                return Err(
                                    self.argument_error(Action::Option(index), ignored(&value))
                                );
                            }
                        }
                    } else if count == 1 {
                        stop = start + 1;
                        tuples.push((index, vec![value], option_string.clone()));
                        break;
                    } else {
                        return Err(self.argument_error(Action::Option(index), ignored(&value)));
                    }
                }
                None => {
                    let selected: String = pattern[start + 1..].iter().collect();
                    let count = self.match_argument(index, &selected)?;
                    stop = start + 1 + count;
                    tuples.push((index, args[start + 1..stop].to_vec(), option_string.clone()));
                    break;
                }
            }
        }
        for (index, values, name) in tuples {
            self.take_option(index, values, &name, seen, once_seen)?;
        }
        Ok(stop)
    }

    /// `take_action`と`_get_values`と、その動作。
    fn take_option(
        &mut self,
        index: usize,
        mut values: Vec<String>,
        option_string: &str,
        seen: &mut Vec<Action>,
        once_seen: &mut Vec<Dest>,
    ) -> Result<(), Inner> {
        seen.push(Action::Option(index));
        let options: &'static [OptionSpec] = self.spec.options;
        let option = &options[index];
        if let Some(position) = values.iter().position(|value| value == "--") {
            values.remove(position);
        }
        match option.kind {
            Kind::Help => Err(Inner::Stop(Stop::Help(self.id))),
            Kind::Flag => {
                let dest = option.dest.expect("flag has a dest");
                if once_seen.contains(&dest) {
                    return Err(Inner::Stop(Stop::Duplicate(option_string.to_string())));
                }
                once_seen.push(dest);
                self.ns.cached = true;
                Ok(())
            }
            Kind::Once | Kind::Append => {
                let dest = option.dest.expect("option has a dest");
                let value = match values.as_slice() {
                    [] => Value::EmptyList,
                    [single] => self.convert(index, single)?,
                    _ => unreachable!("nargs=None takes one argument"),
                };
                if option.kind == Kind::Once {
                    if once_seen.contains(&dest) {
                        return Err(Inner::Stop(Stop::Duplicate(option_string.to_string())));
                    }
                    once_seen.push(dest);
                }
                self.store(dest, option.kind, value);
                Ok(())
            }
        }
    }

    /// `_get_value`(`type=int`)と`_check_value`(choices)。
    fn convert(&self, index: usize, value: &str) -> Result<Value, Inner> {
        let options: &'static [OptionSpec] = self.spec.options;
        let option = &options[index];
        if option.int {
            return match pyint::parse(&from_str(value), self.max_digits) {
                Ok(number) => Ok(Value::Int(number)),
                Err(_) => {
                    let mut message = from_str("invalid int value: ");
                    message.extend(repr(value));
                    Err(self.argument_error(Action::Option(index), message))
                }
            };
        }
        if let Some(choices) = option.choices
            && !choices.contains(&value)
        {
            return Err(self.argument_error(Action::Option(index), invalid_choice(value, choices)));
        }
        Ok(Value::Str(value.to_string()))
    }

    fn store(&mut self, dest: Dest, kind: Kind, value: Value) {
        let ns = &mut *self.ns;
        if kind == Kind::Append {
            ns.path.push(value.into_arg());
            return;
        }
        match dest {
            Dest::Context => ns.context = Some(value.into_number()),
            Dest::Limit => ns.limit = Some(value.into_number()),
            _ => {
                let arg = Some(value.into_arg());
                match dest {
                    Dest::Cwd => ns.cwd = arg,
                    Dest::Accept => ns.accept = arg,
                    Dest::Expect => ns.expect = arg,
                    Dest::Format => ns.format = arg,
                    Dest::Untracked => ns.untracked = arg,
                    Dest::Base => ns.base = arg,
                    Dest::Target => ns.target = arg,
                    Dest::FromRev => ns.from_rev = arg,
                    Dest::ToRev => ns.to_rev = arg,
                    Dest::Rev => ns.rev = arg,
                    Dest::File => ns.file = arg,
                    Dest::FileMode => ns.file_mode = arg,
                    Dest::Cached | Dest::Context | Dest::Limit | Dest::Path => {
                        unreachable!("handled above")
                    }
                }
            }
        }
    }

    /// `consume_positionals`(位置引数はsubparsersの1つだけ。型は`(-*A[-AO]*)`)。
    fn consume_positionals(
        &mut self,
        args: &[String],
        pattern: &[char],
        start: usize,
        positionals: &mut Vec<Action>,
        seen: &mut Vec<Action>,
        unrecognized: &mut Vec<String>,
    ) -> Result<usize, Inner> {
        if positionals.is_empty() {
            return Ok(start);
        }
        let rest = &pattern[start..];
        let dashes = rest.iter().take_while(|c| **c == '-').count();
        if rest.get(dashes) != Some(&'A') {
            return Ok(start);
        }
        positionals.clear();
        seen.push(Action::Subparsers);
        let values = &args[start..];
        let sub = self
            .spec
            .subparsers
            .as_ref()
            .expect("positional is the subparsers");
        let name = values[0].as_str();
        let Some((_, _, target)) = sub.choices.iter().find(|(choice, _, _)| *choice == name) else {
            let names: Vec<&str> = sub.choices.iter().map(|(choice, _, _)| *choice).collect();
            return Err(self.argument_error(Action::Subparsers, invalid_choice(name, &names)));
        };
        match self.id {
            ParserId::Top => self.ns.mode = Some(name.to_string()),
            _ => self.ns.operation = Some(name.to_string()),
        }
        let sub_extras = parse_known_args(*target, &values[1..], self.ns, self.max_digits)?;
        unrecognized.extend(sub_extras);
        Ok(args.len())
    }
}

/// 型を変換した値。
enum Value {
    Str(String),
    Int(PyInt),
    EmptyList,
}

impl Value {
    fn into_arg(self) -> Arg {
        match self {
            Self::Str(text) => Arg::Str(text),
            Self::EmptyList => Arg::EmptyList,
            Self::Int(_) => unreachable!("only int options take int values"),
        }
    }

    fn into_number(self) -> Number {
        match self {
            Self::Int(number) => Number::Int(number),
            Self::EmptyList => Number::EmptyList,
            Self::Str(_) => unreachable!("int options always convert"),
        }
    }
}

fn ignored(value: &str) -> PyText {
    let mut message = from_str("ignored explicit argument ");
    message.extend(repr(value));
    message
}

fn invalid_choice(value: &str, choices: &[&str]) -> PyText {
    let mut message = from_str("invalid choice: ");
    message.extend(repr(value));
    message.extend(from_str(" (choose from "));
    for (index, choice) in choices.iter().enumerate() {
        if index > 0 {
            message.extend(from_str(", "));
        }
        message.extend(repr(choice));
    }
    message.push(u32::from(b')'));
    message
}

/// `build_parser().parse_args(argv)`。
pub fn parse_args(argv: &[String], max_digits: u64) -> Result<Namespace, Stop> {
    let mut ns = Namespace::default();
    let extras = parse_known_args(ParserId::Top, argv, &mut ns, max_digits)?;
    if !extras.is_empty() {
        return Err(Stop::Error(from_str(&format!(
            "unrecognized arguments: {}",
            extras.join(" ")
        ))));
    }
    Ok(ns)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(values: &[&str]) -> Result<Namespace, Stop> {
        let argv: Vec<String> = values.iter().map(|value| value.to_string()).collect();
        parse_args(&argv, 4300)
    }

    fn error(values: &[&str]) -> String {
        match parse(values) {
            Err(Stop::Error(text)) => text.iter().map(|c| char::from_u32(*c).unwrap()).collect(),
            other => panic!("{values:?}: {other:?}"),
        }
    }

    #[test]
    fn errors_follow_argparse_in_order() {
        let h = "a".repeat(64);
        assert_eq!(
            error(&["run", "--cwd", "d", "status"]),
            "the following arguments are required: --expect-context-sha256"
        );
        assert_eq!(
            error(&["bogus"]),
            "argument mode: invalid choice: 'bogus' (choose from 'prepare', 'run')"
        );
        assert_eq!(
            error(&[
                "run",
                "--cwd",
                "d",
                "--expect-context-sha256",
                &h,
                "status",
                "--format",
                "patch"
            ]),
            "argument --format: invalid choice: 'patch' (choose from 'short', 'porcelain')"
        );
        assert_eq!(
            error(&["prepare", "--cwd", "d", "status"]),
            "unrecognized arguments: status"
        );
        assert_eq!(
            error(&[
                "run",
                "--cwd",
                "d",
                "--expect-context-sha256",
                &h,
                "diff",
                "--context",
                "x"
            ]),
            "argument --context: invalid int value: 'x'"
        );
        assert_eq!(
            error(&["-hx"]),
            "argument -h/--help: ignored explicit argument 'x'"
        );
        assert_eq!(
            error(&["prepare", "--cwd"]),
            "argument --cwd: expected one argument"
        );
        assert_eq!(
            error(&["-- ", "run"]),
            "argument mode: invalid choice: '-- ' (choose from 'prepare', 'run')"
        );
        assert_eq!(
            error(&["--", "run"]),
            "argument mode: invalid choice: '--' (choose from 'prepare', 'run')"
        );
    }

    #[test]
    fn values_follow_argparse() {
        let ns = parse(&["prepare", "--cwd=--"]).unwrap();
        assert_eq!(ns.cwd, Some(Arg::EmptyList));
        let ns = parse(&["prepare", "--cwd", "-1"]).unwrap();
        assert_eq!(ns.cwd, Some(Arg::Str("-1".into())));
        assert!(matches!(parse(&["-hh"]), Err(Stop::Help(ParserId::Top))));
        assert!(matches!(
            parse(&["run", "-h", "--x"]),
            Err(Stop::Help(ParserId::Run))
        ));
        assert_eq!(
            parse(&["prepare", "--cwd", "a", "--cwd=b"]),
            Err(Stop::Duplicate("--cwd".into()))
        );
    }
}
