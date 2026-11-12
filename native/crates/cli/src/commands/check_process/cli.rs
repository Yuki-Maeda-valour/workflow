//! 旧版 `check-process.py` の監督の内部 CLI(`main`・`anchor`・`linux_worker`。`:261-333`)の写し。
//! - linux worker は loop-supervisor の `supervise` を使い、コマンドの起動の失敗だけを旧版の linux worker に
//!   合わせる(`LaunchFailure` の後に `reap({})` で不在を確かめて 127)。
//! - anchor は実際の OS だけで動く(TERM・INT・HUP では終わらず、KILL でだけ終わる)。

use super::loop_supervisor::cli::{MainResult, emit, module_frame, record_json};
use super::loop_supervisor::ops::{Class, OpenFlags, Ops, PyErr, Real};
use super::loop_supervisor::r4q_python::argparse::{self, Outcome};
use super::loop_supervisor::r4q_python::exception::Filename;
use super::loop_supervisor::r4q_python::legacy::{self, Script, Site, uncaught};
use super::loop_supervisor::r4q_python::value::PyValue;
use super::loop_supervisor::runtime::{self, Start};
use super::loop_supervisor::supervise::{
    self, LaunchMode, SIGHUP, SIGINT, SIGTERM, SuperviseError, returncode,
};
use dev_workflow_platform::r4q_process as os;

/// 97 の固定の文。
pub const RECOVERY_LINE: &str = "品質プロセスの回収を確認できない";

fn chars(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

/// `except (OSError, ValueError, RuntimeError, RecoveryError)`。
fn caught(error: &PyErr) -> bool {
    matches!(
        error.class,
        Class::Os(_) | Class::Value(_) | Class::Runtime(_) | Class::Recovery | Class::Interrupted
    )
}

/// main の中の誤り。
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

/// anchor を実際の OS で動かす依頼(main の `try:` の中で動かす)。
#[derive(Clone, Debug)]
pub struct AnchorRequest {
    pub fd: PyValue,
    pub nonce: PyValue,
    pub argv: Vec<Vec<u32>>,
}

/// main の結果(anchor のときは、呼出元が実際の OS で動かす)。
#[derive(Clone, Debug)]
pub enum CheckProcessResult {
    Done(MainResult),
    Anchor(AnchorRequest),
}

fn value_of(namespace: &[(&'static str, PyValue)], name: &str) -> PyValue {
    namespace
        .iter()
        .find(|(dest, _)| *dest == name)
        .map(|(_, value)| value.clone())
        .unwrap_or(PyValue::None)
}

/// 旧版の main の本体(引数は `sys.argv[1:]`)。
pub fn main_logic<O: Ops>(
    ops: &mut O,
    args: &[Vec<u32>],
    context: &argparse::Context<'_>,
) -> CheckProcessResult {
    let namespace = match argparse::parse(&argparse::CHECK_PROCESS, args, context) {
        Outcome::Parsed(namespace) => namespace,
        Outcome::Help(text) => return CheckProcessResult::Done(MainResult::Help(text)),
        Outcome::Error(error) => return CheckProcessResult::Done(MainResult::Usage(error)),
    };
    // parse_args の後の区切り。
    if ops.keyboard_interrupt() {
        return CheckProcessResult::Done(MainResult::KeyboardInterrupt);
    }
    let command = match value_of(&namespace, "command") {
        PyValue::List(items) => items,
        _ => Vec::new(),
    };
    let mut argv: Vec<Vec<u32>> = command
        .into_iter()
        .filter_map(|item| match item {
            PyValue::Str(text) => Some(text),
            _ => None,
        })
        .collect();
    if argv.first().is_some_and(|first| *first == chars("--")) {
        argv.remove(0);
    }
    if argv.is_empty() {
        // raise ValueError('コマンドが空')(`--linux` の分岐より前)。
        return CheckProcessResult::Done(MainResult::Message {
            text: chars(RECOVERY_LINE),
            code: 97,
        });
    }
    if !matches!(value_of(&namespace, "linux"), PyValue::Bool(true)) {
        return CheckProcessResult::Anchor(AnchorRequest {
            fd: value_of(&namespace, "anchor_fd"),
            nonce: value_of(&namespace, "nonce"),
            argv,
        });
    }
    let done = match linux_worker(ops, &namespace, &argv, context.max_str_digits) {
        Ok(()) => MainResult::Exit(0),
        Err(Failure::Py(error)) if caught(&error) => MainResult::Message {
            text: chars(RECOVERY_LINE),
            code: 97,
        },
        Err(Failure::Py(error)) => MainResult::Uncaught {
            site: Site::CheckProcessDeadline,
            type_name: error.type_name(),
            message: error.message,
        },
        Err(Failure::Uncaught(site, error)) => MainResult::Uncaught {
            site,
            type_name: error.type_name(),
            message: error.message,
        },
        Err(Failure::KeyboardInterrupt) => MainResult::KeyboardInterrupt,
    };
    CheckProcessResult::Done(done)
}

/// `linux_worker(args, argv)`(期限の検査を含む)。
fn linux_worker<O: Ops>(
    ops: &mut O,
    namespace: &[(&'static str, PyValue)],
    argv: &[Vec<u32>],
    max_str_digits: usize,
) -> Result<(), Failure> {
    let invalid = || Failure::Py(PyErr::value("期限が不正"));
    let list_error = || {
        Failure::Uncaught(
            Site::CheckProcessDeadline,
            PyErr::new(Class::Type, uncaught::LIST_AS_REAL),
        )
    };
    let timeout = value_of(namespace, "timeout");
    let grace = value_of(namespace, "grace");
    // args.timeout is None or args.grace is None or not isfinite(timeout) or timeout <= 0
    //     or not isfinite(grace) or grace < 0
    if matches!(timeout, PyValue::None) || matches!(grace, PyValue::None) {
        return Err(invalid());
    }
    let PyValue::Float(timeout) = timeout else {
        return Err(list_error());
    };
    if !timeout.is_finite() || timeout <= 0.0 {
        return Err(invalid());
    }
    let PyValue::Float(grace) = grace else {
        return Err(list_error());
    };
    if !grace.is_finite() || grace < 0.0 {
        return Err(invalid());
    }
    let path = match value_of(namespace, "result") {
        PyValue::Str(path) => super::loop_supervisor::ops::fsencode(&path)?,
        PyValue::None => {
            return Err(Failure::Uncaught(
                Site::CheckProcessOpen,
                PyErr::new(Class::Type, uncaught::OPEN_NONE),
            ));
        }
        _ => {
            return Err(Failure::Uncaught(
                Site::CheckProcessOpen,
                PyErr::new(Class::Type, uncaught::OPEN_LIST),
            ));
        }
    };
    let nonce = value_of(namespace, "nonce");
    // loop-supervisor.py を読み込む処理(importlib)の区切り。
    if ops.keyboard_interrupt() {
        return Err(Failure::KeyboardInterrupt);
    }
    let fd = ops
        .open(&path, OpenFlags::Create)
        .map_err(|failure| PyErr::os(&failure, Some(Filename::from_raw(&path))))?;
    // os.open が返った所(try の外なので閉じない)。
    if ops.keyboard_interrupt() {
        return Err(Failure::KeyboardInterrupt);
    }
    let written = worker_body(ops, fd, argv, timeout, grace, &nonce, max_str_digits);
    match ops.close(fd) {
        Ok(()) => written,
        Err(failure) => Err(Failure::Py(PyErr::os(&failure, None))),
    }
}

/// linux worker の `try:` の中。
fn worker_body<O: Ops>(
    ops: &mut O,
    fd: i32,
    argv: &[Vec<u32>],
    timeout: f64,
    grace: f64,
    nonce: &PyValue,
    max_str_digits: usize,
) -> Result<(), Failure> {
    let mut record =
        match supervise::supervise(ops, argv, timeout, grace, LaunchMode::Wrap, max_str_digits) {
            Ok(supervised) => vec![
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
            ],
            Err(SuperviseError::LaunchFailure) => {
                if !supervise::reap(ops, &mut Default::default())? {
                    return Err(Failure::Py(PyErr::recovery(
                        "起動失敗後に子プロセスの不在を確認できない",
                    )));
                }
                vec![
                    (PyValue::str("clean"), PyValue::Bool(true)),
                    (PyValue::str("returncode"), PyValue::int(127)),
                    (PyValue::str("timed_out"), PyValue::Bool(false)),
                    (
                        PyValue::str("signal"),
                        PyValue::int(i64::from(ops.latest_signal())),
                    ),
                ]
            }
            Err(SuperviseError::Raise(error)) => return Err(Failure::Py(error)),
            Err(SuperviseError::KeyboardInterrupt) => return Err(Failure::KeyboardInterrupt),
            Err(SuperviseError::Uncaught { inner, error }) => {
                return Err(Failure::Uncaught(Site::CheckProcessSupervise(inner), error));
            }
        };
    record.push((PyValue::str("nonce"), nonce.clone()));
    let mut raw = record_json(&PyValue::Dict(record), false, max_str_digits).into_bytes();
    raw.push(b'\n');
    let count = ops
        .write(fd, &raw)
        .map_err(|failure| PyErr::os(&failure, None))?;
    if count != raw.len() {
        return Err(Failure::Py(PyErr::recovery("回収結果を書けない")));
    }
    ops.fsync(fd).map_err(|failure| PyErr::os(&failure, None))?;
    Ok(())
}

/// `anchor(fd, nonce, argv)`(実際の OS)。戻るのは誤りのときだけ(成功すれば KILL まで pause を続ける)。
pub fn anchor(request: &AnchorRequest, max_str_digits: usize) -> MainResult {
    for number in [SIGTERM, SIGINT, SIGHUP] {
        let _ = os::set_handler(number, os::Handler::Nothing);
    }
    // os.set_inheritable(fd, False): None と list は TypeError、C の int の外は OverflowError。
    let fd = match &request.fd {
        PyValue::Int(digits) => match digits
            .parse::<i64>()
            .ok()
            .and_then(|value| i32::try_from(value).ok())
        {
            Some(fd) => fd,
            None => {
                return MainResult::Uncaught {
                    site: Site::CheckProcessAnchor,
                    type_name: "OverflowError",
                    message: uncaught::C_INT_OVERFLOW.to_owned(),
                };
            }
        },
        PyValue::List(_) => {
            return MainResult::Uncaught {
                site: Site::CheckProcessAnchor,
                type_name: "TypeError",
                message: uncaught::LIST_AS_INTEGER.to_owned(),
            };
        }
        _ => {
            return MainResult::Uncaught {
                site: Site::CheckProcessAnchor,
                type_name: "TypeError",
                message: uncaught::NONE_AS_INTEGER.to_owned(),
            };
        }
    };
    let recovery = || MainResult::Message {
        text: chars(RECOVERY_LINE),
        code: 97,
    };
    let Ok(fd) = os::inherited_fd(fd).and_then(|fd| os::set_inheritable(fd, false).map(|()| fd))
    else {
        return recovery();
    };
    // subprocess.Popen(argv, close_fds=True) と process.wait()。起動の OSError は 127。
    let rc = match launch_and_wait(&request.argv) {
        Ok(code) => code,
        Err(error) if error.errno().is_some() => 127,
        Err(_) => return recovery(),
    };
    let notice = PyValue::Dict(vec![
        (PyValue::str("nonce"), request.nonce.clone()),
        (PyValue::str("returncode"), PyValue::int(i64::from(rc))),
    ]);
    let mut raw = record_json(&notice, false, max_str_digits).into_bytes();
    raw.push(b'\n');
    if os::retry_eintr(|| os::write(fd, &raw)).is_err() {
        return recovery();
    }
    loop {
        os::pause();
    }
}

/// `Popen(argv, close_fds=True).wait()`(ECHILD は 0)。
fn launch_and_wait(argv: &[Vec<u32>]) -> Result<i32, PyErr> {
    let raw: Vec<Vec<u8>> = argv
        .iter()
        .map(|arg| super::loop_supervisor::ops::fsencode(arg))
        .collect::<Result<_, _>>()?;
    let path = std::env::var_os("PATH").map(std::os::unix::ffi::OsStringExt::into_vec);
    let candidates = os::exec_candidates(&raw[0], path.as_deref());
    let executables: Vec<&[u8]> = candidates.iter().map(Vec::as_slice).collect();
    let args: Vec<&[u8]> = raw.iter().map(Vec::as_slice).collect();
    let pid = os::spawn(&os::Spawn {
        argv: &args,
        executables: &executables,
        env: None,
        cwd: None,
        stdin: None,
        stdout: None,
        stderr: None,
        pass_fds: &[],
        new_session: false,
    })
    .map_err(|error| super::loop_supervisor::ops::spawn_error(error, &raw[0], None))?;
    loop {
        match os::wait_pid(pid, true) {
            Ok(os::PidWait::Exited(termination)) => {
                let raw = match termination {
                    os::Termination::Exited(code) => i32::from(code) << 8,
                    os::Termination::Signaled(number) => number,
                    os::Termination::Unknown(raw) => raw,
                };
                return super::loop_supervisor::ops::exit_code(raw);
            }
            Ok(os::PidWait::NoChild) => return Ok(0),
            Ok(os::PidWait::Running) => {}
            Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => {}
            Err(error) => {
                return Err(PyErr::os(
                    &super::loop_supervisor::ops::OsFailure::from_errno(
                        error.raw_os_error().unwrap_or(0),
                    ),
                    None,
                ));
            }
        }
    }
}

/// `python3 check-process.py <args…>` と同じ動作をして、終了値を返す。
pub fn run(args: &[std::ffi::OsString]) -> u8 {
    use std::os::unix::ffi::OsStrExt;
    let mut python = match runtime::start() {
        Start::Ready(python) => python,
        Start::Exit(code) => return code,
    };
    if python.interrupted() {
        return python.finish(runtime::keyboard_interrupt(module_frame(
            Script::CheckProcess,
        )));
    }
    let args: Vec<Vec<u32>> = args
        .iter()
        .map(|arg| super::loop_supervisor::r4q_python::code_points(arg.as_bytes()))
        .collect();
    let columns_env = std::env::var_os("COLUMNS")
        .map(|value| super::loop_supervisor::r4q_python::code_points(value.as_bytes()));
    let columns = argparse::terminal_columns(
        columns_env.as_deref(),
        os::stdout_terminal_columns(),
        python.max_str_digits,
    );
    let doc = python.has_doc.then_some(legacy::CHECK_PROCESS_DOC);
    let context = argparse::Context {
        width: columns - 2,
        description: doc,
        max_str_digits: python.max_str_digits,
    };
    let mut ops = Real::new();
    let result = match main_logic(&mut ops, &args, &context) {
        CheckProcessResult::Done(result) => result,
        CheckProcessResult::Anchor(request) => {
            if python.interrupted() {
                return python.finish(runtime::keyboard_interrupt(module_frame(
                    Script::CheckProcess,
                )));
            }
            anchor(&request, python.max_str_digits)
        }
    };
    if python.interrupted() {
        return python.finish(runtime::keyboard_interrupt(module_frame(
            Script::CheckProcess,
        )));
    }
    let ended = emit(&mut python, result, Script::CheckProcess);
    python.finish(ended)
}
