//! 旧版 `loop-supervisor.py` の `main`(`:110-148`)の写し。
//! - `main_logic` は OS の操作を [`Ops`] で受け、出力の文と終了値を返す(差し替えの比較が照らす)。
//! - 書込みは呼出元([`run`])が旧 Python の書き手で行う。

use super::check_links::pyio::Which;
use super::check_links::traceback as tb;
use super::ops::{Class, OpenFlags, Ops, PyErr};
use super::r4q_python::argparse::{self, Outcome};
use super::r4q_python::exception::Filename;
use super::r4q_python::json;
use super::r4q_python::legacy::{self, Script, Site, WriteStep, uncaught};
use super::r4q_python::value::PyValue;
use super::runtime::{self, Ended, Python, Start};
use super::supervise::{self, LaunchMode, SuperviseError, returncode};

/// `print(..., file=sys.stderr)` の 97 の文の頭。
pub const RECOVERY_PREFIX: &str = "子プロセスの回収を確認できない: ";
/// `--check` の失敗の文。
pub const CHECK_FAILED: &str = "子プロセスの監督に必要な Linux subreaper・pidfd を使えない";

/// 旧版の main の結果(書く文と終了値)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MainResult {
    /// 何も書かずに終わる。
    Exit(u8),
    /// argparse の `-h`(標準出力の文と終了 0)。
    Help(Vec<u32>),
    /// argparse の誤り(usage と `<prog>: error: …` を書いて終了 2)。
    Usage(argparse::ParseError),
    /// `print(text, file=sys.stderr)` の後に終わる。
    Message { text: Vec<u32>, code: u8 },
    /// 既定の SIGINT の処理の KeyboardInterrupt(呼出履歴の途中の行は比べない。2026-11-11 22:31 の決定)。
    KeyboardInterrupt,
    /// 捕まえない例外。
    Uncaught {
        site: Site,
        type_name: &'static str,
        message: String,
    },
}

fn chars(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

/// `except (OSError, ValueError, RuntimeError, subprocess.SubprocessError)`。
fn caught(error: &PyErr) -> bool {
    matches!(
        error.class,
        Class::Os(_) | Class::Value(_) | Class::Runtime(_) | Class::Subprocess(_)
    )
}

fn uncaught_error(site: Site, error: &PyErr) -> MainResult {
    MainResult::Uncaught {
        site,
        type_name: error.type_name(),
        message: error.message.clone(),
    }
}

/// `--check` の確かめ(setup と自分への pidfd と signal 0)。
fn check<O: Ops>(ops: &mut O) -> MainResult {
    let result = (|| -> Result<(), Failure> {
        match supervise::setup(ops) {
            Ok(()) => {}
            Err(SuperviseError::KeyboardInterrupt) => return Err(Failure::KeyboardInterrupt),
            Err(SuperviseError::Raise(error)) => return Err(Failure::Py(error)),
            Err(other) => unreachable!("setup の誤りは例外か KeyboardInterrupt だけ: {other:?}"),
        }
        let pid = ops.getpid();
        let fd = ops
            .pidfd_open(pid)
            .map_err(|failure| PyErr::os(&failure, None))?;
        let sent = ops
            .pidfd_send_signal(fd, 0)
            .map_err(|failure| Failure::Py(PyErr::os(&failure, None)));
        match ops.close(fd) {
            Ok(()) => sent,
            Err(failure) => Err(Failure::Py(PyErr::os(&failure, None))),
        }
    })();
    match result {
        Ok(()) => MainResult::Exit(0),
        Err(Failure::KeyboardInterrupt) => MainResult::KeyboardInterrupt,
        Err(Failure::Uncaught(site, error)) => uncaught_error(site, &error),
        Err(Failure::Py(error))
            if matches!(
                error.class,
                Class::Os(_) | Class::Value(_) | Class::Runtime(_)
            ) =>
        {
            MainResult::Message {
                text: chars(CHECK_FAILED),
                code: 97,
            }
        }
        Err(Failure::Py(error)) => uncaught_error(Site::LoopSupervisorDeadline, &error),
    }
}

/// argparse の値(float か、`--opt=--` の空の list)。
fn float_of(value: &PyValue) -> Option<f64> {
    match value {
        PyValue::Float(number) => Some(*number),
        _ => None,
    }
}

/// 旧版の main の本体(引数は `sys.argv[1:]`)。
pub fn main_logic<O: Ops>(
    ops: &mut O,
    args: &[Vec<u32>],
    context: &argparse::Context<'_>,
) -> MainResult {
    if args.len() == 1 && args[0] == chars("--check") {
        return check(ops);
    }
    let namespace = match argparse::parse(&argparse::LOOP_SUPERVISOR, args, context) {
        Outcome::Parsed(namespace) => namespace,
        Outcome::Help(text) => return MainResult::Help(text),
        Outcome::Error(error) => return MainResult::Usage(error),
    };
    // parse_args の後の区切り(argparse の途中で受けた SIGINT もここで KeyboardInterrupt になる)。
    if ops.keyboard_interrupt() {
        return MainResult::KeyboardInterrupt;
    }
    let get = |name: &str| {
        namespace
            .iter()
            .find(|(dest, _)| *dest == name)
            .map(|(_, value)| value.clone())
            .unwrap_or(PyValue::None)
    };
    let (result, nonce, timeout, grace, command) = (
        get("result"),
        get("nonce"),
        get("timeout"),
        get("grace"),
        get("command"),
    );
    let mut command = match command {
        PyValue::List(items) => items,
        _ => Vec::new(),
    };
    if command
        .first()
        .is_some_and(|first| matches!(first, PyValue::Str(text) if *text == chars("--")))
    {
        command.remove(0);
    }
    let command: Vec<Vec<u32>> = command
        .into_iter()
        .filter_map(|item| match item {
            PyValue::Str(text) => Some(text),
            _ => None,
        })
        .collect();
    // if not command or args.timeout <= 0 or args.grace < 0: raise ValueError(...)
    let invalid = || Failure::Py(PyErr::value("コマンドと正の timeout、非負の grace が必要"));
    let type_error =
        |site: Site, message: &str| Failure::Uncaught(site, PyErr::new(Class::Type, message));
    let outcome: Result<(), Failure> = (|| {
        if command.is_empty() {
            return Err(invalid());
        }
        let Some(timeout) = float_of(&timeout) else {
            return Err(type_error(
                Site::LoopSupervisorDeadline,
                uncaught::LIST_LE_INT,
            ));
        };
        if timeout <= 0.0 {
            return Err(invalid());
        }
        let Some(grace) = float_of(&grace) else {
            return Err(type_error(
                Site::LoopSupervisorDeadline,
                uncaught::LIST_LT_INT,
            ));
        };
        if grace < 0.0 {
            return Err(invalid());
        }
        let PyValue::Str(path) = &result else {
            return Err(type_error(Site::LoopSupervisorOpen, uncaught::OPEN_LIST));
        };
        let path = super::ops::fsencode(path)?;
        let fd = ops
            .open(&path, OpenFlags::Create)
            .map_err(|failure| PyErr::os(&failure, Some(Filename::from_raw(&path))))?;
        // os.open が返った所(fd に入れる前。try の外なので閉じない)。
        if ops.keyboard_interrupt() {
            return Err(Failure::KeyboardInterrupt);
        }
        let written = write_record(
            ops,
            fd,
            &command,
            timeout,
            grace,
            &nonce,
            context.max_str_digits,
        );
        // finally: os.close(fd)。閉じる誤りは前の誤りを置き換える。
        match ops.close(fd) {
            Ok(()) => written,
            Err(failure) => Err(Failure::Py(PyErr::os(&failure, None))),
        }
    })();
    match outcome {
        Ok(()) => MainResult::Exit(0),
        Err(Failure::Py(error)) if caught(&error) => MainResult::Message {
            text: [chars(RECOVERY_PREFIX), chars(&error.message)].concat(),
            code: 97,
        },
        Err(Failure::Py(error)) => uncaught_error(Site::LoopSupervisorDeadline, &error),
        Err(Failure::Uncaught(site, error)) => uncaught_error(site, &error),
        Err(Failure::KeyboardInterrupt) => MainResult::KeyboardInterrupt,
    }
}

/// main の中の誤り(捕まえうる例外か、場所の決まった捕まえない例外か、KeyboardInterrupt)。
enum Failure {
    Py(PyErr),
    Uncaught(Site, PyErr),
    KeyboardInterrupt,
}

impl From<PyErr> for Failure {
    fn from(error: PyErr) -> Self {
        Failure::Py(error)
    }
}

/// supervise と記録の書き込み(`try:` の中)。
fn write_record<O: Ops>(
    ops: &mut O,
    fd: i32,
    command: &[Vec<u32>],
    timeout: f64,
    grace: f64,
    nonce: &PyValue,
    max_str_digits: usize,
) -> Result<(), Failure> {
    let supervised = match supervise::supervise(
        ops,
        command,
        timeout,
        grace,
        LaunchMode::Propagate,
        max_str_digits,
    ) {
        Ok(supervised) => supervised,
        Err(SuperviseError::Raise(error)) => return Err(Failure::Py(error)),
        Err(SuperviseError::Uncaught { inner, error }) => {
            return Err(Failure::Uncaught(
                Site::LoopSupervisorSupervise(inner),
                error,
            ));
        }
        Err(SuperviseError::KeyboardInterrupt) => return Err(Failure::KeyboardInterrupt),
        // loop-supervisor は起動の失敗を包まない(LaunchMode::Propagate)。
        Err(SuperviseError::LaunchFailure) => unreachable!("LaunchMode::Propagate"),
    };
    let record = PyValue::Dict(vec![
        (PyValue::str("clean"), PyValue::Bool(true)),
        (
            PyValue::str("returncode"),
            PyValue::int(i64::from(returncode(supervised.status))),
        ),
        (
            PyValue::str("timed_out"),
            PyValue::Bool(supervised.timed_out),
        ),
        (
            PyValue::str("signal"),
            PyValue::int(i64::from(supervised.signal)),
        ),
        (PyValue::str("nonce"), nonce.clone()),
    ]);
    let mut raw = record_json(&record, true, max_str_digits).into_bytes();
    raw.push(b'\n');
    let count = ops
        .write(fd, &raw)
        .map_err(|failure| PyErr::os(&failure, None))?;
    if count != raw.len() {
        return Err(Failure::Py(PyErr::runtime("回収結果を書けない")));
    }
    ops.fsync(fd).map_err(|failure| PyErr::os(&failure, None))?;
    Ok(())
}

/// 記録の JSON(鍵は str だけ、値は bool・小さな int・str か list なので、dumps は失敗しない)。
pub fn record_json(record: &PyValue, sort_keys: bool, max_str_digits: usize) -> String {
    json::dumps(record, sort_keys, max_str_digits).expect("記録の鍵は str で、値は小さい")
}

/// 旧スクリプトの `<module>` のフレーム(KeyboardInterrupt と、print の失敗の呼出履歴に使う)。
pub fn module_frame(script: Script) -> tb::Frame {
    let frame = script.module();
    tb::Frame {
        file: frame.file,
        line: frame.line,
        name: frame.name,
        source: frame.source,
    }
}

/// 結果を旧 Python の書き手で書き、終わり方を返す(loop-supervisor と check-process の main で共通)。
/// - `-h`: argparse の `print_help()` は sys.stdout へ書く(None なら `_print_message` が sys.stderr にする)。
/// - 引数の誤り: `print_usage(sys.stderr)`(None なら sys.stdout)の後に、`exit(2, message)` が sys.stderr へ
///   書く。書く流れが None なら `AttributeError`。書く途中の例外は argparse の呼出履歴([`legacy::argparse_chain`])。
/// - 97 の文: `print(…, file=sys.stderr)`(None なら sys.stdout、どちらも None なら書かない)。
pub fn emit(python: &mut Python, result: MainResult, script: Script) -> Ended {
    match result {
        MainResult::Exit(code) => Ended::Code(code),
        MainResult::KeyboardInterrupt => runtime::keyboard_interrupt(module_frame(script)),
        MainResult::Uncaught {
            site,
            type_name,
            message,
        } => Ended::Raised(vec![runtime::traceback(Some(site), type_name, message)]),
        MainResult::Help(text) => {
            let target = python.first_open(&which(legacy::HELP_STREAMS));
            match python.argparse_write(target, &text) {
                Ok(()) => Ended::Code(0),
                Err(exception) => Ended::Raised(runtime::chain(legacy::argparse_chain(
                    script,
                    None,
                    WriteStep::Help,
                    r4q_exception(exception),
                ))),
            }
        }
        MainResult::Usage(error) => {
            let raised = |step: WriteStep, exception: tb::Exception| {
                Ended::Raised(runtime::chain(legacy::argparse_chain(
                    script,
                    Some(&error.site),
                    step,
                    r4q_exception(exception),
                )))
            };
            let target = python.first_open(&which(legacy::USAGE_STREAMS));
            if let Err(exception) = python.argparse_write(target, &error.usage) {
                return raised(WriteStep::Usage, exception);
            }
            let target = python.first_open(&which(legacy::MESSAGE_STREAMS));
            if let Err(exception) = python.argparse_write(target, &error.message) {
                return raised(WriteStep::Message, exception);
            }
            Ended::Code(2)
        }
        MainResult::Message { text, code } => match python.print_stderr(&text) {
            Ok(()) => Ended::Code(code),
            Err(error) => Ended::Raised(vec![tb::Traceback {
                frames: vec![module_frame(script)],
                exception: Python::io_exception(error),
            }]),
        },
    }
}

fn which(streams: &[legacy::Stream]) -> Vec<Which> {
    streams
        .iter()
        .map(|stream| match stream {
            legacy::Stream::Stdout => Which::Stdout,
            legacy::Stream::Stderr => Which::Stderr,
        })
        .collect()
}

fn r4q_exception(exception: tb::Exception) -> super::r4q_python::traceback::Exception {
    super::r4q_python::traceback::Exception {
        type_name: exception.type_name,
        value: exception.value,
    }
}

/// `python3 loop-supervisor.py <args…>` と同じ動作をして、終了値を返す。
pub fn run(args: &[std::ffi::OsString]) -> u8 {
    use std::os::unix::ffi::OsStrExt;
    let mut python = match runtime::start() {
        Start::Ready(python) => python,
        Start::Exit(code) => return code,
    };
    if python.interrupted() {
        return python.finish(runtime::keyboard_interrupt(module_frame(
            Script::LoopSupervisor,
        )));
    }
    let args: Vec<Vec<u32>> = args
        .iter()
        .map(|arg| super::r4q_python::code_points(arg.as_bytes()))
        .collect();
    let columns_env =
        std::env::var_os("COLUMNS").map(|value| super::r4q_python::code_points(value.as_bytes()));
    let columns = argparse::terminal_columns(
        columns_env.as_deref(),
        dev_workflow_platform::r4q_process::stdout_terminal_columns(),
        python.max_str_digits,
    );
    let doc = python.has_doc.then_some(legacy::LOOP_SUPERVISOR_DOC);
    let context = argparse::Context {
        width: columns - 2,
        description: doc,
        max_str_digits: python.max_str_digits,
    };
    let mut ops = super::ops::Real::new();
    let result = main_logic(&mut ops, &args, &context);
    if python.interrupted() {
        return python.finish(runtime::keyboard_interrupt(module_frame(
            Script::LoopSupervisor,
        )));
    }
    let ended = emit(&mut python, result, Script::LoopSupervisor);
    python.finish(ended)
}
