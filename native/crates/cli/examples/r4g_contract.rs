#![forbid(unsafe_code)]
//! R4G専用の検査入口。製品の登録表を使わず、旧版との比較にだけ使う。
//!
//! 形: `r4g_contract [入口の指定]... <コマンド> [コマンドの引数]...`
//! 入口の指定(`--r4g-`で始まる)は、旧版の起動の形に無い値を検査のときだけ渡す口である。
//! 製品のコマンドには接続しないので、製品の引数・環境変数から保護を外す口にはならない。
//! - `--r4g-script-name <名前>`: reviews-dirの旧スクリプトの呼出名(旧版の`$0`)。
//! - `--r4g-script-file <パス>`: implementation-gitの旧版の`__file__`(`implementation-git.py`の場所)。
//!   事前検査は旧版と同じく、その兄弟の`diff-snapshot.sh`(`Path(__file__).with_name(...)`)にする。
//!   子へ渡る環境には混ぜない。事前検査のスクリプトの名前を自分の引数に持たないのは、既存テストの
//!   記録の代用品が、親の引数に`diff-snapshot.sh`があるかで事前検査の中のGitを見分けるため。
//! - `--r4g-output-fault <stdout|stderr>:<通すバイト数>:<errno>`: 出力の故障(型による差し替え)。
//! - `--r4g-clock <秒>,<秒>,...`: pendingの`time.monotonic()`の値の並び(旧版の包みと同じ値)。
//! - `--r4g-pending-fault <JSON>`: pendingのOSの呼出しへの故障・呼出しの記録・決めた時点の信号・
//!   わざと候補を開く対照(型[`pending_faults::FaultSystem`]による差し替え)。形は旧版の包み
//!   `native/ci/r4g_fixtures/pending_wrapper.py`と同じ。
//!
//! - `--r4g-deadlines <秒>,<秒>`: implementation-gitの事前検査とGitの期限(旧版の定数を書き換えた
//!   コピーと同じ値。既存テストの`PRECHECK_TIMEOUT`・`GIT_TIMEOUT`の書き換えに当たる)。
//! - `--r4g-mutation operation-env`: implementation-gitの読取りの子へ、filterを入れない環境を渡す変異
//!   (既存テストの変異のコピーに当たる。比較器が変異を検出できることの確かめに使う)。
//!
//! pendingは単位4、reviews-dirは単位3、implementation-gitは単位5で接続した。
//! 起動前の記録のマクロを展開するのは、この入口だけである。

// 土台の部品のうち、単位3・5のコマンドだけが使うものは、接続までは使われない。
#[allow(dead_code)]
#[path = "../src/commands/r4g_support/mod.rs"]
mod r4g_support;

#[path = "../src/commands/pending_implementation/mod.rs"]
mod pending_implementation;

#[path = "../src/commands/reviews_dir/mod.rs"]
mod reviews_dir;

#[path = "../src/commands/implementation_git/mod.rs"]
mod implementation_git;

dev_workflow_platform::r4g_record_startup_state!();

use dev_workflow_platform::r4g::startup::{self, StartupState};
use std::ffi::{OsStr, OsString};
use std::os::unix::ffi::OsStrExt;

/// 比べる3本のコマンド。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CommandName {
    PendingImplementation,
    ReviewsDir,
    ImplementationGit,
}

impl CommandName {
    fn parse(value: &OsStr) -> Option<Self> {
        match value.as_bytes() {
            b"pending-implementation" => Some(Self::PendingImplementation),
            b"reviews-dir" => Some(Self::ReviewsDir),
            b"implementation-git" => Some(Self::ImplementationGit),
            _ => None,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::PendingImplementation => "pending-implementation",
            Self::ReviewsDir => "reviews-dir",
            Self::ImplementationGit => "implementation-git",
        }
    }
}

/// 出力の故障の指定。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct OutputFault {
    stream: r4g_support::python_io::Stream,
    limit: usize,
    errno: i32,
}

/// 入口だけが受ける値。
#[derive(Debug, Default, PartialEq)]
struct EntryOptions {
    script_name: Option<OsString>,
    script_file: Option<OsString>,
    output_faults: Vec<OutputFault>,
    clock: Option<Vec<f64>>,
    pending_fault: Option<Vec<u8>>,
    /// implementation-gitの事前検査とGitの期限(旧版の既存テストのコピーの書き換えに当たる注入)。
    deadlines: Option<(implementation_git::Deadline, implementation_git::Deadline)>,
    /// implementation-gitの検査専用の変異(`operation-env`)。
    mutation_operation_env: bool,
}

/// 入口が渡す、コマンドの実行に要るもの。各コマンドは単位3〜5でこれを受ける。
#[allow(dead_code)]
struct Context {
    startup: StartupState,
    options: EntryOptions,
    args: Vec<OsString>,
}

#[derive(Debug, PartialEq, Eq)]
enum EntryError {
    MissingCommand,
    UnknownCommand,
    BadOption(String),
    OptionForAnotherCommand(&'static str),
}

/// 入口の指定を、最初のコマンド名まで読む。コマンドの引数はそのまま渡す。
fn parse_entry(
    args: &[OsString],
) -> Result<(EntryOptions, CommandName, Vec<OsString>), EntryError> {
    let mut options = EntryOptions::default();
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_bytes();
        if !arg.starts_with(b"--r4g-") {
            break;
        }
        let name = String::from_utf8_lossy(arg).into_owned();
        let value = args
            .get(index + 1)
            .ok_or_else(|| EntryError::BadOption(name.clone()))?;
        match arg {
            b"--r4g-script-name" if options.script_name.is_none() => {
                options.script_name = Some(value.clone());
            }
            b"--r4g-script-file" if options.script_file.is_none() => {
                options.script_file = Some(value.clone());
            }
            b"--r4g-output-fault" => {
                let fault = parse_fault(value.as_bytes())
                    .ok_or_else(|| EntryError::BadOption(name.clone()))?;
                if options
                    .output_faults
                    .iter()
                    .any(|other| other.stream == fault.stream)
                {
                    return Err(EntryError::BadOption(name));
                }
                options.output_faults.push(fault);
            }
            b"--r4g-pending-fault" if options.pending_fault.is_none() => {
                if pending_faults::Spec::parse(value.as_bytes()).is_none() {
                    return Err(EntryError::BadOption(name));
                }
                options.pending_fault = Some(value.as_bytes().to_vec());
            }
            b"--r4g-clock" if options.clock.is_none() => {
                let values = std::str::from_utf8(value.as_bytes())
                    .ok()
                    .and_then(|text| {
                        text.split(',')
                            .map(|part| part.parse::<f64>().ok().filter(|v| v.is_finite()))
                            .collect::<Option<Vec<_>>>()
                    })
                    .filter(|values| !values.is_empty())
                    .ok_or_else(|| EntryError::BadOption(name.clone()))?;
                options.clock = Some(values);
            }
            b"--r4g-deadlines" if options.deadlines.is_none() => {
                options.deadlines = Some(
                    parse_deadlines(value.as_bytes())
                        .ok_or_else(|| EntryError::BadOption(name.clone()))?,
                );
            }
            b"--r4g-mutation" if !options.mutation_operation_env => {
                if value.as_bytes() != b"operation-env" {
                    return Err(EntryError::BadOption(name));
                }
                options.mutation_operation_env = true;
            }
            _ => return Err(EntryError::BadOption(name)),
        }
        index += 2;
    }
    let command = match args.get(index) {
        None => return Err(EntryError::MissingCommand),
        Some(value) => CommandName::parse(value).ok_or(EntryError::UnknownCommand)?,
    };
    if options.script_name.is_some() && command != CommandName::ReviewsDir {
        return Err(EntryError::OptionForAnotherCommand("--r4g-script-name"));
    }
    if options.script_file.is_some() && command != CommandName::ImplementationGit {
        return Err(EntryError::OptionForAnotherCommand("--r4g-script-file"));
    }
    if options.clock.is_some() && command != CommandName::PendingImplementation {
        return Err(EntryError::OptionForAnotherCommand("--r4g-clock"));
    }
    if options.pending_fault.is_some() && command != CommandName::PendingImplementation {
        return Err(EntryError::OptionForAnotherCommand("--r4g-pending-fault"));
    }
    if options.deadlines.is_some() && command != CommandName::ImplementationGit {
        return Err(EntryError::OptionForAnotherCommand("--r4g-deadlines"));
    }
    if options.mutation_operation_env && command != CommandName::ImplementationGit {
        return Err(EntryError::OptionForAnotherCommand("--r4g-mutation"));
    }
    Ok((options, command, args[index + 1..].to_vec()))
}

/// `<事前検査の秒>,<Gitの秒>`。各値は旧版の定数に書く数の字面(`0.05`・`1`など)。
fn parse_deadlines(
    value: &[u8],
) -> Option<(implementation_git::Deadline, implementation_git::Deadline)> {
    let text = std::str::from_utf8(value).ok()?;
    let (first, second) = text.split_once(',')?;
    // 受け付けるのは、Pythonの str(数) と同じ表記だけ(整数は先頭の0なし、小数は repr と同じ字面)。
    let one = |part: &str| {
        let digits = |t: &str| !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit());
        let normalized = match part.split_once('.') {
            None => digits(part) && !part.starts_with('0'),
            Some((whole, fraction)) => {
                digits(whole)
                    && digits(fraction)
                    && part
                        .parse::<f64>()
                        .is_ok_and(|value| implementation_git::float_repr(value) == part)
            }
        };
        if !normalized {
            return None;
        }
        let seconds: f64 = part.parse().ok()?;
        (seconds.is_finite() && seconds > 0.0).then(|| implementation_git::Deadline {
            seconds,
            text: part.to_string(),
        })
    };
    Some((one(first)?, one(second)?))
}

fn parse_fault(value: &[u8]) -> Option<OutputFault> {
    let text = std::str::from_utf8(value).ok()?;
    let mut parts = text.split(':');
    let stream = match parts.next()? {
        "stdout" => r4g_support::python_io::Stream::Stdout,
        "stderr" => r4g_support::python_io::Stream::Stderr,
        _ => return None,
    };
    let limit = parts.next()?.parse().ok()?;
    let errno: i32 = parts.next()?.parse().ok()?;
    (parts.next().is_none() && errno > 0).then_some(OutputFault {
        stream,
        limit,
        errno,
    })
}

/// 入口の誤り・未接続の診断と終了値。旧版の出力と区別できる固定の文にする。
const USAGE: &[u8] = "r4g_contract: 検査対象のコマンドを指定してください(pending-implementation・reviews-dir・implementation-git)\n".as_bytes();

fn entry_failure(error: &EntryError) -> (Vec<u8>, u8) {
    match error {
        EntryError::MissingCommand | EntryError::UnknownCommand => (USAGE.to_vec(), 2),
        EntryError::BadOption(name) => (
            format!("r4g_contract: 入口の指定が不正: {name}\n").into_bytes(),
            2,
        ),
        EntryError::OptionForAnotherCommand(name) => (
            format!("r4g_contract: {name} はこのコマンドに使えません\n").into_bytes(),
            2,
        ),
    }
}

/// コマンドへ振り分ける。pendingは自分で標準出力と標準エラーへ書き、終了値を返す。
fn dispatch(context: Context, command: CommandName) -> (Vec<u8>, u8) {
    match command {
        CommandName::PendingImplementation => (Vec::new(), python::run_pending(context)),
        CommandName::ReviewsDir => match context.options.script_name.clone() {
            // 旧版の `$0`(使い方の sed の引数と、bash 自身の診断の先頭)。入口だけが受ける。
            Some(script) => reviews_dir::main(
                script.as_bytes().to_vec(),
                context
                    .args
                    .iter()
                    .map(|arg| arg.as_bytes().to_vec())
                    .collect(),
            ),
            None => (
                "r4g_contract: reviews-dir には --r4g-script-name が要ります\n"
                    .as_bytes()
                    .to_vec(),
                2,
            ),
        },
        CommandName::ImplementationGit => match context.options.script_file {
            // 旧版の兄弟の diff-snapshot.sh に当たる場所。入口だけが受け、子の環境へ混ぜない。
            Some(_) => (Vec::new(), python::run_implementation_git(context)),
            None => (
                format!(
                    "r4g_contract: {} には --r4g-script-file が要ります\n",
                    command.name()
                )
                .into_bytes(),
                2,
            ),
        },
    }
}

/// 旧Python 3.10の起動と終了の枠(`python3 <script>`)。
mod python {
    use super::implementation_git as ig;
    use super::r4g_support::python_io::{FaultRaw, PythonStdio, Raw, StandardRaw, Stream};
    use super::r4g_support::python_startup as startup_py;
    use super::r4g_support::text::from_str;
    use super::r4g_support::{Clock, MonotonicClock, ScriptedClock};
    use super::{Context, pending_faults, pending_implementation as pending};
    use dev_workflow_platform::r4g::{Errno, fs, process, startup};
    use std::os::fd::AsFd;
    use std::os::unix::ffi::OsStrExt;

    /// 起動の時点のfd 2へ直接書く(旧PythonのCの`stderr`)。失敗は表に出さない。
    fn raw_stderr(bytes: &[u8]) {
        let mut rest = bytes;
        while !rest.is_empty() {
            match fs::write(std::io::stderr().as_fd(), rest, &mut || false) {
                Ok(0) | Err(_) => return,
                Ok(count) => rest = &rest[count..],
            }
        }
    }

    /// `Current thread`の値(実行ごとに変わる値。比較では位置と形式だけを見る)。
    fn thread_value() -> usize {
        let marker = 0u8;
        &marker as *const u8 as usize
    }

    /// 旧Pythonの`os.environ`の元(`environ`の順。`=`の前が名前。同じ名前は最初の値を読む)。
    fn environ_of(entries: &[Vec<u8>]) -> Vec<(Vec<u8>, Vec<u8>)> {
        entries
            .iter()
            .filter_map(|entry| {
                let index = entry.iter().position(|byte| *byte == b'=')?;
                Some((entry[..index].to_vec(), entry[index + 1..].to_vec()))
            })
            .collect()
    }

    fn raw(stream: Stream, context: &Context) -> Box<dyn Raw> {
        let fd = match stream {
            Stream::Stdout => 1,
            Stream::Stderr => 2,
        };
        match context
            .options
            .output_faults
            .iter()
            .find(|fault| fault.stream == stream)
        {
            Some(fault) => Box::new(FaultRaw::new(
                StandardRaw { fd },
                fault.limit,
                Errno::from_raw_os_error(fault.errno),
            )),
            None => Box::new(StandardRaw { fd }),
        }
    }

    /// 旧Pythonの起動(`main`の前まで)の結果。
    struct Booted {
        stdio: PythonStdio,
        /// 起動の時点の`environ`(並びのまま。`=`の無い項目も含む)。
        entries: Vec<Vec<u8>>,
        environ: Vec<(Vec<u8>, Vec<u8>)>,
        coercion: startup_py::Coercion,
        config: startup_py::PythonConfig,
    }

    /// 旧Pythonの起動の順: 地域設定の強制の警告・環境の設定・信号・標準入出力・Cのままの警告。
    /// 起動で止まるときは、その診断を書いた後の終了値を返す。
    fn boot(context: &Context) -> Result<Booted, u8> {
        // 起動の時点で閉じていたfdは、標準ライブラリが開いた /dev/null のまま使う(2026-11-12 08:55 の決定)。
        let states = startup_py::standard_states();
        let entries = process::environ_entries();
        let environ = environ_of(&entries);
        // 起動の前の処理: 地域設定の強制の警告(CのstderrでFD 2へ直接)。
        let coercion = startup_py::coercion(&environ);
        if let startup_py::Coercion::Coerce { warn: true } = coercion
            && let Some(warning) = startup_py::coercion_warning(&coercion)
        {
            raw_stderr(warning.as_bytes());
        }
        let config = match startup_py::config(&environ) {
            Ok(config) => config,
            Err(failure) => {
                raw_stderr(&failure.diagnostic(thread_value()));
                return Err(1);
            }
        };
        let _ = startup::install_python_signals();
        let raws = [raw(Stream::Stdout, context), raw(Stream::Stderr, context)];
        let mut stdio = match startup_py::stdio(config, states, raws) {
            Ok(stdio) => stdio,
            Err(failure) => {
                raw_stderr(&failure.diagnostic(thread_value()));
                return Err(1);
            }
        };
        // 起動の後の、LC_CTYPE が C のままの警告(sys.stderr へ書く)。
        if let startup_py::Coercion::None { warn: true } = coercion
            && let Some(warning) = startup_py::coercion_warning(&coercion)
        {
            let _ = stdio.stderr.write(&from_str(warning));
        }
        Ok(Booted {
            stdio,
            entries,
            environ,
            coercion,
            config,
        })
    }

    fn argv(context: &Context) -> Vec<Vec<u8>> {
        context
            .args
            .iter()
            .map(|arg| arg.as_bytes().to_vec())
            .collect()
    }

    /// pendingを旧Pythonの起動の順で動かす。`KeyboardInterrupt`で終わるときは戻らない。
    pub fn run_pending(context: Context) -> u8 {
        let mut booted = match boot(&context) {
            Ok(booted) => booted,
            Err(code) => return code,
        };
        let argv = argv(&context);
        let clock: Box<dyn Clock> = match context.options.clock.clone() {
            Some(values) => Box::new(ScriptedClock::new(values).expect("checked at entry")),
            None => Box::new(MonotonicClock::default()),
        };
        let real = pending::system::RealSystem::new(clock, booted.environ.clone());
        let stdio = &mut booted.stdio;
        let ending = match context
            .options
            .pending_fault
            .as_deref()
            .and_then(pending_faults::Spec::parse)
        {
            Some(spec) => {
                let mut sys = pending_faults::FaultSystem::new(real, spec);
                pending::main(&argv, &mut sys, stdio)
            }
            None => {
                let mut sys = real;
                pending::main(&argv, &mut sys, stdio)
            }
        };
        match ending {
            pending::Ending::Exit(code) => stdio.finalize(code),
            pending::Ending::Interrupt => {
                pending::print_keyboard_interrupt(stdio);
                let _ = stdio.finalize(1);
                startup::exit_by_python_interrupt()
            }
        }
    }

    /// 旧Pythonの地域設定の強制が`setenv("LC_CTYPE", "C.UTF-8", 1)`で変えた`environ`
    /// (最初の`LC_CTYPE=`の項目を置き換え、無ければ末尾に足す)。
    fn coerced_entries(entries: &[Vec<u8>], coercion: &startup_py::Coercion) -> Vec<Vec<u8>> {
        let mut entries = entries.to_vec();
        if let startup_py::Coercion::Coerce { .. } = coercion {
            let value = format!("LC_CTYPE={}", startup_py::COERCED_LOCALE).into_bytes();
            match entries
                .iter()
                .position(|entry| entry.starts_with(b"LC_CTYPE="))
            {
                Some(index) => entries[index] = value,
                None => entries.push(value),
            }
        }
        entries
    }

    /// implementation-gitを旧Pythonの起動の順で動かす。`KeyboardInterrupt`で終わるときは戻らない。
    pub fn run_implementation_git(context: Context) -> u8 {
        let mut booted = match boot(&context) {
            Ok(booted) => booted,
            Err(code) => return code,
        };
        let options = &context.options;
        let settings = ig::Settings {
            precheck_script: ig::path::sibling(
                options
                    .script_file
                    .as_ref()
                    .expect("checked at entry")
                    .as_bytes(),
                b"diff-snapshot.sh",
            ),
            precheck: options
                .deadlines
                .as_ref()
                .map_or_else(ig::Deadline::precheck, |pair| pair.0.clone()),
            git: options
                .deadlines
                .as_ref()
                .map_or_else(ig::Deadline::git, |pair| pair.1.clone()),
            operation_clean_env: options.mutation_operation_env,
        };
        let runtime = ig::Runtime {
            environ: process::PythonEnviron::from_entries(coerced_entries(
                &booted.entries,
                &booted.coercion,
            )),
            int_max_str_digits: booted.config.int_max_str_digits,
        };
        let argv = argv(&context);
        let stdio = &mut booted.stdio;
        match ig::main(&argv, &settings, &runtime, stdio) {
            ig::Ending::Exit(code) => stdio.finalize(code),
            ig::Ending::Interrupt => {
                let _ = stdio.finalize(1);
                startup::exit_by_python_interrupt()
            }
        }
    }
}

/// pendingの検査専用の差し替え(型による注入)。旧版の包み`pending_wrapper.py`と同じ形の指定を受ける。
mod pending_faults {
    use super::pending_implementation::system::{Exc, Fd, RealSystem, System};
    use dev_workflow_platform::r4g::fs::Stat;
    use dev_workflow_platform::r4g::{Errno, OFlags, startup};
    use serde_json::Value;
    use std::io::Write;

    /// 故障の中身。
    #[derive(Clone, Debug)]
    enum Action {
        Errno(i32),
        Unexpected(String),
        /// 本物の結果の欄へ足す値と、`mode`へ排他的論理和で当てる値。
        Change {
            ino: i128,
            uid: i128,
            size: i128,
            mtime_ns: i128,
            ctime_ns: i128,
            mode_xor: u32,
        },
    }

    /// 故障の規則1つ。条件に合う呼出しを1から数え、`calls`の番目か`from`以降で当てる(どちらも無ければ毎回)。
    #[derive(Clone, Debug)]
    struct Rule {
        op: String,
        path: Option<Vec<u8>>,
        last: Option<Vec<u8>>,
        dir: Option<bool>,
        ident: Option<(i128, i128)>,
        calls: Option<Vec<u64>>,
        from: Option<u64>,
        action: Action,
        count: u64,
        /// 故障を当てた回数(記録の`hits`へ書く。0回の規則は比較器が失敗にする)。
        applied: u64,
    }

    /// 規則を当てられる呼出しの名前(旧版の包みと同じ)。
    const KNOWN_OPS: [&str; 9] = [
        "stat", "fstat", "open", "close", "read", "readlink", "scandir", "getcwd", "clock",
    ];

    /// 指定の全体。
    #[derive(Clone, Debug)]
    pub struct Spec {
        trace: Option<std::path::PathBuf>,
        hits: Option<std::path::PathBuf>,
        rules: Vec<Rule>,
        signal_at_clock: Option<(u64, i32)>,
        overlap_unexpected: Option<String>,
        open_candidates: Option<String>,
    }

    fn hex(value: &Value) -> Option<Vec<u8>> {
        let text = value.as_str()?;
        if !text.len().is_multiple_of(2) {
            return None;
        }
        (0..text.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(text.get(index..index + 2)?, 16).ok())
            .collect()
    }

    fn int(value: Option<&Value>) -> Option<i128> {
        match value {
            None => Some(0),
            Some(value) => value.as_i64().map(i128::from),
        }
    }

    impl Spec {
        /// JSONの指定を読む。知らない欄・型の違いは`None`(入口の誤り)。
        pub fn parse(text: &[u8]) -> Option<Self> {
            let value: Value = serde_json::from_slice(text).ok()?;
            let object = value.as_object()?;
            let known = [
                "trace",
                "hits",
                "rules",
                "signal_at_clock",
                "overlap_unexpected",
                "open_candidates",
            ];
            if object.keys().any(|key| !known.contains(&key.as_str())) {
                return None;
            }
            let mut rules = Vec::new();
            for rule in object
                .get("rules")
                .map_or(Some(&Vec::new()), Value::as_array)?
            {
                let rule = rule.as_object()?;
                let fields = [
                    "op",
                    "path_hex",
                    "last_hex",
                    "dir",
                    "ident",
                    "calls",
                    "from",
                    "errno",
                    "unexpected",
                    "change",
                ];
                if rule.keys().any(|key| !fields.contains(&key.as_str())) {
                    return None;
                }
                let action = match (
                    rule.get("errno"),
                    rule.get("unexpected"),
                    rule.get("change"),
                ) {
                    (Some(errno), None, None) => {
                        Action::Errno(i32::try_from(errno.as_i64()?).ok()?)
                    }
                    (None, Some(name), None) => Action::Unexpected(name.as_str()?.to_string()),
                    (None, None, Some(change)) => {
                        let change = change.as_object()?;
                        Action::Change {
                            ino: int(change.get("ino"))?,
                            uid: int(change.get("uid"))?,
                            size: int(change.get("size"))?,
                            mtime_ns: int(change.get("mtime_ns"))?,
                            ctime_ns: int(change.get("ctime_ns"))?,
                            mode_xor: u32::try_from(int(change.get("mode_xor"))?).ok()?,
                        }
                    }
                    _ => return None,
                };
                let ident = match rule.get("ident") {
                    None => None,
                    Some(value) => {
                        let pair = value.as_array()?;
                        if pair.len() != 2 {
                            return None;
                        }
                        Some((i128::from(pair[0].as_u64()?), i128::from(pair[1].as_u64()?)))
                    }
                };
                rules.push(Rule {
                    op: Some(rule.get("op")?.as_str()?)
                        .filter(|op| KNOWN_OPS.contains(op))?
                        .to_string(),
                    path: rule
                        .get("path_hex")
                        .map(hex)
                        .map_or(Some(None), |v| v.map(Some))?,
                    last: rule
                        .get("last_hex")
                        .map(hex)
                        .map_or(Some(None), |v| v.map(Some))?,
                    dir: rule
                        .get("dir")
                        .map(Value::as_bool)
                        .map_or(Some(None), |v| v.map(Some))?,
                    ident,
                    calls: match rule.get("calls") {
                        None => None,
                        Some(calls) => Some(
                            calls
                                .as_array()?
                                .iter()
                                .map(Value::as_u64)
                                .collect::<Option<Vec<_>>>()?,
                        ),
                    },
                    from: rule
                        .get("from")
                        .map(Value::as_u64)
                        .map_or(Some(None), |v| v.map(Some))?,
                    action,
                    count: 0,
                    applied: 0,
                });
            }
            let signal_at_clock = match object.get("signal_at_clock") {
                None => None,
                Some(value) => {
                    let value = value.as_object()?;
                    Some((
                        value.get("call")?.as_u64()?,
                        i32::try_from(value.get("signal")?.as_i64()?).ok()?,
                    ))
                }
            };
            let open_candidates = match object.get("open_candidates") {
                None => None,
                Some(value) => {
                    let mode = value.as_str()?;
                    if mode != "relative" && mode != "absolute" {
                        return None;
                    }
                    Some(mode.to_string())
                }
            };
            Some(Self {
                trace: match object.get("trace") {
                    None => None,
                    Some(value) => Some(std::path::PathBuf::from(value.as_str()?)),
                },
                hits: match object.get("hits") {
                    None => None,
                    Some(value) => Some(std::path::PathBuf::from(value.as_str()?)),
                },
                rules,
                signal_at_clock,
                overlap_unexpected: match object.get("overlap_unexpected") {
                    None => None,
                    Some(value) => Some(value.as_str()?.to_string()),
                },
                open_candidates,
            })
        }
    }

    /// 旧版の`os`の呼出しの前後で、規則の故障を当て、呼出しを記録する。
    pub struct FaultSystem {
        inner: RealSystem,
        spec: Spec,
        clock_calls: u64,
    }

    fn hex_text(bytes: &[u8]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    fn optional(value: Option<Fd>) -> String {
        value.map_or("null".to_string(), |fd| fd.to_string())
    }

    fn stat_result(result: &Result<Stat, Exc>) -> String {
        match result {
            Ok(stat) => format!(
                "[\"ok\",[{},{},{},{},{},{},{}]]",
                stat.dev, stat.ino, stat.mode, stat.uid, stat.size, stat.mtime_ns, stat.ctime_ns
            ),
            Err(error) => failure(error),
        }
    }

    fn failure(error: &Exc) -> String {
        match error {
            Exc::Os(errno) => format!("[\"err\",{}]", errno.raw_os_error()),
            Exc::Unexpected(name) => format!("[\"exc\",\"{name}\"]"),
            Exc::Scan(_) | Exc::Interrupt => "[\"exc\",\"other\"]".to_string(),
        }
    }

    fn plain<T>(result: &Result<T, Exc>, ok: impl Fn(&T) -> String) -> String {
        match result {
            Ok(value) => format!("[\"ok\",{}]", ok(value)),
            Err(error) => failure(error),
        }
    }

    impl Drop for FaultSystem {
        /// 規則ごとの当てた回数を書く(旧版の包みと同じ形の JSON の並び)。
        fn drop(&mut self) {
            if let Some(path) = &self.spec.hits {
                let counts: Vec<String> = self
                    .spec
                    .rules
                    .iter()
                    .map(|rule| rule.applied.to_string())
                    .collect();
                let _ = std::fs::write(path, format!("[{}]\n", counts.join(",")));
            }
        }
    }

    impl FaultSystem {
        pub fn new(inner: RealSystem, spec: Spec) -> Self {
            Self {
                inner,
                spec,
                clock_calls: 0,
            }
        }

        /// 1行の記録を足す(開いて足して閉じる。記録のfdを持ち続けない)。割込みは記録しない。
        fn log(&self, line: String, interrupted: bool) {
            let Some(path) = &self.spec.trace else {
                return;
            };
            if interrupted {
                return;
            }
            if let Ok(mut file) = std::fs::OpenOptions::new()
                .append(true)
                .create(true)
                .open(path)
            {
                let _ = file.write_all(format!("{line}\n").as_bytes());
            }
        }

        /// 条件に合う規則を数え、当てる故障を返す(最初に当たった規則)。
        fn hit(
            &mut self,
            op: &str,
            dir: Option<Fd>,
            path: Option<&[u8]>,
            ident: Option<(i128, i128)>,
        ) -> Option<Action> {
            let mut chosen = None;
            for rule in &mut self.spec.rules {
                if rule.op != op {
                    continue;
                }
                if let Some(want) = &rule.path
                    && path != Some(want.as_slice())
                {
                    continue;
                }
                if let Some(want) = &rule.last {
                    let last = path.and_then(|path| path.rsplit(|byte| *byte == b'/').next());
                    if last != Some(want.as_slice()) {
                        continue;
                    }
                }
                if let Some(want) = rule.dir
                    && want != dir.is_some()
                {
                    continue;
                }
                if rule.ident.is_some() && rule.ident != ident {
                    continue;
                }
                rule.count += 1;
                let due = match (&rule.calls, rule.from) {
                    (Some(calls), _) => calls.contains(&rule.count),
                    (None, Some(from)) => rule.count >= from,
                    (None, None) => true,
                };
                if due && chosen.is_none() {
                    rule.applied += 1;
                    chosen = Some(rule.action.clone());
                }
            }
            chosen
        }

        fn raise(action: &Action) -> Option<Exc> {
            match action {
                Action::Errno(errno) => Some(Exc::Os(Errno::from_raw_os_error(*errno))),
                Action::Unexpected(name) => Some(Exc::Unexpected(name.clone())),
                Action::Change { .. } => None,
            }
        }

        fn apply(action: &Action, mut stat: Stat) -> Stat {
            if let Action::Change {
                ino,
                uid,
                size,
                mtime_ns,
                ctime_ns,
                mode_xor,
            } = action
            {
                stat.ino += ino;
                stat.uid += uid;
                stat.size += size;
                stat.mtime_ns += mtime_ns;
                stat.ctime_ns += ctime_ns;
                stat.mode ^= mode_xor;
            }
            stat
        }

        /// わざと候補を開く対照(観測の手段の陽性の対照)。記録にも故障にも数えない。
        fn open_candidate(&mut self, dir: Fd, name: &[u8]) {
            let flags = OFlags::RDONLY | OFlags::NONBLOCK;
            let opened = match self.spec.open_candidates.as_deref() {
                Some("relative") => self.inner.open(Some(dir), name, flags),
                Some("absolute") => {
                    let link = format!("/proc/self/fd/{dir}");
                    match self.inner.readlink(link.as_bytes()) {
                        Ok(mut base) => {
                            base.push(b'/');
                            base.extend_from_slice(name);
                            self.inner.open(None, &base, flags)
                        }
                        Err(error) => Err(error),
                    }
                }
                _ => return,
            };
            if let Ok(fd) = opened {
                let _ = self.inner.close(fd);
            }
        }
    }

    impl System for FaultSystem {
        fn stat(&mut self, dir: Option<Fd>, path: &[u8]) -> Result<Stat, Exc> {
            if let Some(dir) = dir
                && path.starts_with(b"guard-")
            {
                self.open_candidate(dir, path);
            }
            let action = self.hit("stat", dir, Some(path), None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self
                    .inner
                    .stat(dir, path)
                    .map(|stat| action.as_ref().map_or(stat, |a| Self::apply(a, stat))),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"stat\",{},\"{}\",{}]",
                    optional(dir),
                    hex_text(path),
                    stat_result(&result)
                ),
                interrupted,
            );
            result
        }

        fn fstat(&mut self, fd: Fd) -> Result<Stat, Exc> {
            let real = self.inner.fstat(fd);
            let ident = real.as_ref().ok().map(|stat| (stat.dev, stat.ino));
            let result = match real {
                Ok(stat) => match self.hit("fstat", None, None, ident) {
                    Some(action) => match Self::raise(&action) {
                        Some(error) => Err(error),
                        None => Ok(Self::apply(&action, stat)),
                    },
                    None => Ok(stat),
                },
                Err(error) => Err(error),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!("[\"fstat\",{fd},{}]", stat_result(&result)),
                interrupted,
            );
            result
        }

        fn open(&mut self, dir: Option<Fd>, path: &[u8], flags: OFlags) -> Result<Fd, Exc> {
            let action = self.hit("open", dir, Some(path), None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.open(dir, path, flags),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"open\",{},\"{}\",{},{}]",
                    optional(dir),
                    hex_text(path),
                    flags.bits(),
                    plain(&result, |fd| fd.to_string())
                ),
                interrupted,
            );
            result
        }

        fn close(&mut self, fd: Fd) -> Result<(), Exc> {
            let action = self.hit("close", None, None, None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.close(fd),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"close\",{fd},{}]",
                    plain(&result, |_| "null".to_string())
                ),
                interrupted,
            );
            result
        }

        fn read(&mut self, fd: Fd, length: usize) -> Result<Vec<u8>, Exc> {
            let action = self.hit("read", None, None, None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.read(fd, length),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"read\",{fd},{length},{}]",
                    plain(&result, |data| data.len().to_string())
                ),
                interrupted,
            );
            result
        }

        fn readlink(&mut self, path: &[u8]) -> Result<Vec<u8>, Exc> {
            let action = self.hit("readlink", None, Some(path), None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.readlink(path),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"readlink\",\"{}\",{}]",
                    hex_text(path),
                    plain(&result, |target| format!("\"{}\"", hex_text(target)))
                ),
                interrupted,
            );
            result
        }

        fn scandir(&mut self, fd: Fd, limit: usize) -> Result<(Vec<Vec<u8>>, Option<Exc>), Exc> {
            let action = self.hit("scandir", None, None, None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.scandir(fd, limit),
            };
            let interrupted = matches!(result, Err(Exc::Interrupt));
            self.log(
                format!(
                    "[\"scandir\",{fd},{}]",
                    plain(&result, |_| "null".to_string())
                ),
                interrupted,
            );
            result
        }

        fn getcwd(&mut self) -> Result<Vec<u8>, Exc> {
            let action = self.hit("getcwd", None, None, None);
            let result = match action.as_ref().and_then(Self::raise) {
                Some(error) => Err(error),
                None => self.inner.getcwd(),
            };
            let interrupted = result == Err(Exc::Interrupt);
            self.log(
                format!(
                    "[\"getcwd\",{}]",
                    plain(&result, |cwd| format!("\"{}\"", hex_text(cwd)))
                ),
                interrupted,
            );
            result
        }

        fn monotonic(&mut self) -> Result<f64, Exc> {
            self.clock_calls += 1;
            self.log("[\"clock\"]".to_string(), false);
            if let Some((call, signal)) = self.spec.signal_at_clock
                && call == self.clock_calls
            {
                let _ = startup::signal_self(signal);
            }
            // 時計の失敗の規則(旧版の包みでは time.monotonic の OSError)。
            if let Some(action) = self.hit("clock", None, None, None)
                && let Some(error) = Self::raise(&action)
            {
                return Err(error);
            }
            self.inner.monotonic()
        }

        fn environ(&self, name: &[u8]) -> Option<Vec<u8>> {
            self.inner.environ(name)
        }

        fn is_overlap_reason(&mut self, reason: Option<&'static str>) -> Result<bool, Exc> {
            if let Some(name) = &self.spec.overlap_unexpected {
                return Err(Exc::Unexpected(name.clone()));
            }
            self.inner.is_overlap_reason(reason)
        }

        fn check_interrupt(&mut self) -> Result<(), Exc> {
            self.inner.check_interrupt()
        }
    }
}

fn write_stderr(bytes: &[u8]) {
    use std::os::fd::AsFd;
    let mut rest = bytes;
    while !rest.is_empty() {
        match dev_workflow_platform::r4g::fs::write(std::io::stderr().as_fd(), rest, &mut || false)
        {
            Ok(0) | Err(_) => return,
            Ok(count) => rest = &rest[count..],
        }
    }
}

fn run(
    args: Vec<OsString>,
    recorded: Result<StartupState, startup::StartupError>,
) -> (Vec<u8>, u8) {
    let startup = match recorded {
        Ok(state) => state,
        Err(_) => {
            return (
                "r4g_contract: 起動前の記録がありません\n"
                    .as_bytes()
                    .to_vec(),
                70,
            );
        }
    };
    match parse_entry(&args) {
        Err(error) => entry_failure(&error),
        Ok((options, command, args)) => dispatch(
            Context {
                startup,
                options,
                args,
            },
            command,
        ),
    }
}

fn main() -> std::process::ExitCode {
    let args = std::env::args_os().skip(1).collect();
    let (stderr, code) = run(args, startup::recorded());
    write_stderr(&stderr);
    std::process::ExitCode::from(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&[u8]]) -> Vec<OsString> {
        values
            .iter()
            .map(|value| OsStr::from_bytes(value).to_os_string())
            .collect()
    }

    #[test]
    fn entry_options_stop_at_the_command_and_keep_command_arguments_raw() {
        let (options, command, rest) = parse_entry(&args(&[
            b"--r4g-script-name",
            b"/old/reviews-dir.sh",
            b"--r4g-output-fault",
            b"stdout:3:32",
            b"reviews-dir",
            b"--r4g-clock",
            b"\xff",
        ]))
        .unwrap();
        assert_eq!(command, CommandName::ReviewsDir);
        assert_eq!(
            options.script_name,
            Some(OsString::from("/old/reviews-dir.sh"))
        );
        assert_eq!(
            options.output_faults,
            [OutputFault {
                stream: r4g_support::python_io::Stream::Stdout,
                limit: 3,
                errno: 32
            }]
        );
        // コマンドの後の --r4g- はコマンドの引数として、そのまま渡す。
        assert_eq!(rest, args(&[b"--r4g-clock", b"\xff"]));
        let (options, command, _) = parse_entry(&args(&[
            b"--r4g-clock",
            b"0,1.5,5",
            b"pending-implementation",
        ]))
        .unwrap();
        assert_eq!(command, CommandName::PendingImplementation);
        assert_eq!(options.clock, Some(vec![0.0, 1.5, 5.0]));
        let (options, command, _) = parse_entry(&args(&[
            b"--r4g-script-file",
            b"/x/diff-snapshot.sh",
            b"implementation-git",
        ]))
        .unwrap();
        assert_eq!(command, CommandName::ImplementationGit);
        assert!(options.script_file.is_some());
    }

    #[test]
    fn entry_errors_are_fixed_and_options_belong_to_one_command() {
        let cases: [(&[&[u8]], EntryError); 10] = [
            (&[], EntryError::MissingCommand),
            (&[b"probe"], EntryError::UnknownCommand),
            (
                &[b"--r4g-script-name"],
                EntryError::BadOption("--r4g-script-name".into()),
            ),
            (
                &[b"--r4g-unknown", b"x", b"reviews-dir"],
                EntryError::BadOption("--r4g-unknown".into()),
            ),
            (
                &[b"--r4g-output-fault", b"stdout:x:32", b"reviews-dir"],
                EntryError::BadOption("--r4g-output-fault".into()),
            ),
            (
                &[
                    b"--r4g-output-fault",
                    b"stdout:1:32",
                    b"--r4g-output-fault",
                    b"stdout:2:32",
                    b"reviews-dir",
                ],
                EntryError::BadOption("--r4g-output-fault".into()),
            ),
            (
                &[b"--r4g-clock", b"1,nan", b"pending-implementation"],
                EntryError::BadOption("--r4g-clock".into()),
            ),
            (
                &[
                    b"--r4g-script-name",
                    b"a",
                    b"--r4g-script-name",
                    b"b",
                    b"reviews-dir",
                ],
                EntryError::BadOption("--r4g-script-name".into()),
            ),
            (
                &[b"--r4g-script-name", b"a", b"implementation-git"],
                EntryError::OptionForAnotherCommand("--r4g-script-name"),
            ),
            (
                &[b"--r4g-clock", b"1", b"reviews-dir"],
                EntryError::OptionForAnotherCommand("--r4g-clock"),
            ),
        ];
        for (values, expected) in cases {
            assert_eq!(
                parse_entry(&args(values)).err(),
                Some(expected),
                "{values:?}"
            );
        }
        assert_eq!(
            entry_failure(&EntryError::MissingCommand),
            (USAGE.to_vec(), 2)
        );
    }

    #[test]
    fn pending_fault_specs_are_checked_at_the_entry() {
        let good = br#"{"trace":"/t","rules":[{"op":"stat","last_hex":"6775617264","from":2,"change":{"ino":1}},{"op":"open","path_hex":"2e2e","errno":38}],"signal_at_clock":{"call":3,"signal":2},"overlap_unexpected":"RuntimeError","open_candidates":"relative"}"#;
        assert!(pending_faults::Spec::parse(good).is_some());
        for bad in [
            &br#"{"unknown":1}"#[..],
            br#"{"rules":[{"op":"stat"}]}"#,
            br#"{"rules":[{"op":"stat","errno":2,"unexpected":"X"}]}"#,
            br#"{"rules":[{"op":"stat","path_hex":"zz","errno":2}]}"#,
            br#"{"open_candidates":"both"}"#,
            b"not json",
        ] {
            assert!(pending_faults::Spec::parse(bad).is_none(), "{bad:?}");
        }
        assert_eq!(
            parse_entry(&args(&[b"--r4g-pending-fault", b"{}", b"reviews-dir"])).err(),
            Some(EntryError::OptionForAnotherCommand("--r4g-pending-fault"))
        );
        assert_eq!(
            parse_entry(&args(&[
                b"--r4g-pending-fault",
                b"[]",
                b"pending-implementation"
            ]))
            .err(),
            Some(EntryError::BadOption("--r4g-pending-fault".into()))
        );
    }

    #[test]
    fn the_frame_requires_the_startup_record_and_entry_values() {
        let state = startup::recorded().expect("この入口のテストにも起動前の記録がある");
        // implementation-git は事前検査の場所を入口の指定で受ける。無ければ入口の誤り。
        let (stderr, code) = run(args(&[b"implementation-git", b"run"]), Ok(state));
        assert_eq!(code, 2);
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            "r4g_contract: implementation-git には --r4g-script-file が要ります\n"
        );
        assert!(parse_deadlines(b"0.05,30").is_some());
        assert!(parse_deadlines(b"1.0,30").is_some());
        for bad in [
            &b"0.05"[..],
            b"x,1",
            b"1,",
            b"-1,1",
            b"1e3,1",
            b"0,1",
            b"0.050,1",
            b"007,1",
            b"1.,1",
            b".5,1",
        ] {
            assert!(parse_deadlines(bad).is_none(), "{bad:?}");
        }
        assert_eq!(
            parse_entry(&args(&[b"--r4g-deadlines", b"1,1", b"reviews-dir"])).err(),
            Some(EntryError::OptionForAnotherCommand("--r4g-deadlines"))
        );
        assert_eq!(
            parse_entry(&args(&[b"--r4g-mutation", b"other", b"implementation-git"])).err(),
            Some(EntryError::BadOption("--r4g-mutation".into()))
        );
        // reviews-dir は旧版の $0 を入口の指定で受ける。無ければ入口の誤り。
        let (stderr, code) = run(args(&[b"reviews-dir", b"ensure"]), Ok(state));
        assert_eq!(code, 2);
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            "r4g_contract: reviews-dir には --r4g-script-name が要ります\n"
        );
        let (stderr, code) = run(args(&[]), Err(startup::StartupError::NotRecorded));
        assert_eq!(code, 70);
        assert_eq!(
            String::from_utf8(stderr).unwrap(),
            "r4g_contract: 起動前の記録がありません\n"
        );
        let mut clock = r4g_support::ScriptedClock::new(vec![1.0, 2.5]).unwrap();
        use r4g_support::Clock;
        assert_eq!([clock.now(), clock.now(), clock.now()], [1.0, 2.5, 2.5]);
        assert!(r4g_support::ScriptedClock::new(vec![]).is_none());
    }
}
