//! 旧版 `loop-supervisor.py` の `setup`・`identity`・`own_children`・`signal_owned`・`reap`・`supervise`
//! (`:22-107`)の写し。check-process の linux worker からも呼ぶ。
//! - OS の操作と時計は [`Ops`] で受ける(製品は本物、差し替えは検査の中だけ)。
//! - 回収した子の終わり方は C0c の [`ChildStatus`] で持つ。
//! - 1 本のスレッドで動かす(`waitpid(-1)` と PDEATHSIG の前提)。

use super::ops::{Class, Handler, Ops, OsFailure, Platform, PyErr};
use super::r4q_python::legacy::{InSupervise, uncaught};
use super::r4q_python::number;
use dev_workflow_core::ExitCode;
use dev_workflow_core::process::ChildStatus;
use std::collections::HashMap;

pub const SIGHUP: i32 = 1;
pub const SIGINT: i32 = 2;
pub const SIGKILL: i32 = 9;
pub const SIGTERM: i32 = 15;
const ESRCH: i32 = 3;
const ENOENT: i32 = 2;
const ECHILD: i32 = 10;
/// `PR_SET_CHILD_SUBREAPER`・`PR_SET_PDEATHSIG`。
const PR_SET_CHILD_SUBREAPER: i32 = 36;
const PR_SET_PDEATHSIG: i32 = 1;

/// supervise の誤り。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SuperviseError {
    /// 旧版の `except` が捕まえうる例外(OSError・ValueError・RuntimeError など)。
    Raise(PyErr),
    /// 旧版が捕まえない例外(呼出履歴と終了 1)。
    Uncaught { inner: InSupervise, error: PyErr },
    /// check-process の linux worker の `LaunchFailure`(コマンドの `Popen` が OSError)。
    LaunchFailure,
    /// 既定の SIGINT の処理の KeyboardInterrupt(setup が SIGINT を置き換える前に受けた)。
    KeyboardInterrupt,
}

impl From<PyErr> for SuperviseError {
    fn from(error: PyErr) -> Self {
        SuperviseError::Raise(error)
    }
}

/// supervise の結果(旧版の `{'clean': True, 'returncode', 'timed_out', 'signal': STOP}`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Supervised {
    pub status: ChildStatus,
    pub timed_out: bool,
    pub signal: i32,
}

/// `ChildStatus` から旧版の returncode(信号は負の番号)。
pub fn returncode(status: ChildStatus) -> i32 {
    match status {
        ChildStatus::Exited(code) => i32::from(code.as_u8()),
        ChildStatus::Signaled(number) => -number,
    }
}

fn child_status(code: i32) -> ChildStatus {
    if code >= 0 {
        ChildStatus::Exited(ExitCode::new(code as u8))
    } else {
        ChildStatus::Signaled(-code)
    }
}

/// コマンドの起動の失敗の扱い。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LaunchMode {
    /// loop-supervisor: OSError のまま。
    Propagate,
    /// check-process の linux worker: OSError を `LaunchFailure` にする。
    Wrap,
}

/// `setup()`。SIGINT の処理を置き換えるまでは、旧版の呼出しが返った所(Python の処理の区切り)ごとに
/// KeyboardInterrupt の印を見る。
pub fn setup<O: Ops>(ops: &mut O) -> Result<(), SuperviseError> {
    let interrupted = |ops: &mut O| {
        if ops.keyboard_interrupt() {
            Err(SuperviseError::KeyboardInterrupt)
        } else {
            Ok(())
        }
    };
    if ops.platform() != Platform::Linux || !ops.has_pidfd() {
        return Err(PyErr::runtime("Linux subreaper と pidfd が必要").into());
    }
    interrupted(ops)?;
    let parent = ops.getppid();
    interrupted(ops)?;
    if ops.prctl(PR_SET_CHILD_SUBREAPER, 1) != 0 {
        interrupted(ops)?;
        let errno = ops.get_errno();
        interrupted(ops)?;
        return Err(prctl_error(errno, "subreaper を設定できない").into());
    }
    interrupted(ops)?;
    for number in [SIGTERM, SIGINT, SIGHUP] {
        ops.set_handler(number, Handler::Interrupted);
        if number == SIGTERM {
            // SIGINT を置き換える直前の区切り。
            interrupted(ops)?;
        }
    }
    if ops.prctl(PR_SET_PDEATHSIG, SIGTERM) != 0 {
        let errno = ops.get_errno();
        return Err(prctl_error(errno, "親の終了通知を設定できない").into());
    }
    if parent == 1 || ops.getppid() != parent {
        ops.set_latest_signal(SIGTERM);
    }
    Ok(())
}

/// `OSError(errno, text)`。
fn prctl_error(errno: i32, text: &str) -> PyErr {
    PyErr::os(
        &OsFailure {
            errno,
            text: text.to_owned(),
        },
        None,
    )
}

/// str.split() の区切り(Python の空白)。
fn python_space(code: u32) -> bool {
    (code < 128 && (number::ascii_space(code as u8) || (0x1c..=0x1f).contains(&code)))
        || number::unicode_space_above_ascii(code)
}

/// `[int(value) for value in text.split()]`(旧版 `own_children` の解釈。段階 5 も使う純粋な関数)。
/// 各 pid は Python の int の 10 進の字面。int にならない字面は ValueError。
pub fn parse_children(text: &[u32], max_str_digits: usize) -> Result<Vec<String>, PyErr> {
    let mut pids = Vec::new();
    for word in text
        .split(|&code| python_space(code))
        .filter(|word| !word.is_empty())
    {
        pids.push(number::int_from_str(word, max_str_digits).map_err(PyErr::value)?);
    }
    Ok(pids)
}

/// `own_children()`(読取りは platform の `read_own_children`、解釈は [`parse_children`])。
fn own_children<O: Ops>(ops: &mut O, max_str_digits: usize) -> Result<Vec<String>, PyErr> {
    let pid = ops.getpid();
    let text = ops.read_children(pid)?;
    parse_children(&text, max_str_digits)
}

/// [`parse_identity`] の誤り(IndexError は旧版が捕まえない例外)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IdentityError {
    /// ValueError(`subsection not found`・int にならない欄)と、読取りの OSError。
    Raise(PyErr),
    /// `fields[1]`・`fields[19]` の `IndexError`。
    Index,
}

/// `raw[raw.rindex(b')') + 2:].split()` の `(int(fields[1]), int(fields[19]))`(旧版 `identity` の解釈。
/// ppid と starttime の 10 進の字面。段階 5 も使う純粋な関数)。
pub fn parse_identity(
    raw: &[u8],
    max_str_digits: usize,
) -> Result<(String, String), IdentityError> {
    let close = raw
        .iter()
        .rposition(|&byte| byte == b')')
        .ok_or_else(|| IdentityError::Raise(PyErr::value("subsection not found")))?;
    let rest = raw.get(close + 2..).unwrap_or_default();
    let fields: Vec<&[u8]> = rest
        .split(|&byte| matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
        .filter(|field| !field.is_empty())
        .collect();
    let field = |index: usize| fields.get(index).copied().ok_or(IdentityError::Index);
    let ppid = number::int_from_bytes(field(1)?, max_str_digits)
        .map_err(|message| IdentityError::Raise(PyErr::value(message)))?;
    let starttime = number::int_from_bytes(field(19)?, max_str_digits)
        .map_err(|message| IdentityError::Raise(PyErr::value(message)))?;
    Ok((ppid, starttime))
}

/// `identity(pid)`(読取りは platform の `read_proc_stat`、解釈は [`parse_identity`])。
fn identity<O: Ops>(
    ops: &mut O,
    pid: &str,
    max_str_digits: usize,
) -> Result<(String, String), IdentityError> {
    let raw = ops.read_stat(pid).map_err(IdentityError::Raise)?;
    parse_identity(&raw, max_str_digits)
}

/// `os.pidfd_open(pid)` の pid の変換(pid_t。C の int の外は OverflowError の文)。
pub fn pidfd_pid(pid: &str) -> Result<i32, &'static str> {
    match pid.parse::<i64>() {
        Ok(value) if value > i64::from(i32::MAX) => Err("signed integer is greater than maximum"),
        Ok(value) if value < i64::from(i32::MIN) => Err("signed integer is less than minimum"),
        Ok(value) => Ok(value as i32),
        Err(_) => Err("Python int too large to convert to C long"),
    }
}

/// pid の 1 つへの `try:` の中(`signal_owned` の本体)。
fn signal_one<O: Ops>(
    ops: &mut O,
    pid: &str,
    number: i32,
    max_str_digits: usize,
) -> Result<(), SuperviseError> {
    let lift = |error: IdentityError, inner: InSupervise| match error {
        IdentityError::Raise(error) => SuperviseError::Raise(error),
        IdentityError::Index => SuperviseError::Uncaught {
            inner,
            error: PyErr::new(Class::Index, uncaught::INDEX_OUT_OF_RANGE),
        },
    };
    let before = identity(ops, pid, max_str_digits)
        .map_err(|error| lift(error, InSupervise::IdentityBefore))?;
    if before.0 != ops.getpid().to_string() {
        return Err(PyErr::runtime("子プロセスの所有関係が変わった").into());
    }
    // C の int の外の pid は `os.pidfd_open` の OverflowError(旧版は捕まえない。読んだ一覧が壊れたとき)。
    let pid_number = pidfd_pid(pid).map_err(|message| SuperviseError::Uncaught {
        inner: InSupervise::PidfdOpen,
        error: PyErr::new(Class::Overflow, message),
    })?;
    let fd = ops
        .pidfd_open(pid_number)
        .map_err(|failure| PyErr::os(&failure, None))?;
    let inner = (|| -> Result<(), SuperviseError> {
        let after = identity(ops, pid, max_str_digits)
            .map_err(|error| lift(error, InSupervise::IdentityAfter))?;
        if before != after {
            return Err(PyErr::runtime("子プロセスの starttime が変わった").into());
        }
        ops.pidfd_send_signal(fd, number)
            .map_err(|failure| PyErr::os(&failure, None))?;
        Ok(())
    })();
    // finally: os.close(fd)。閉じる誤りは、前の誤りを置き換える。
    match ops.close(fd) {
        Ok(()) => inner,
        Err(failure) => Err(PyErr::os(&failure, None).into()),
    }
}

/// `signal_owned(number)`。ProcessLookupError と FileNotFoundError の pid だけを飛ばす。
pub fn signal_owned<O: Ops>(
    ops: &mut O,
    number: i32,
    max_str_digits: usize,
) -> Result<(), SuperviseError> {
    for pid in own_children(ops, max_str_digits)? {
        match signal_one(ops, &pid, number, max_str_digits) {
            Ok(()) => {}
            Err(SuperviseError::Raise(error))
                if matches!(error.errno(), Some(ESRCH) | Some(ENOENT)) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// `reap(statuses)`。ECHILD のときだけ真(所有する生きたプロセスが無い)。
pub fn reap<O: Ops>(ops: &mut O, statuses: &mut HashMap<i32, ChildStatus>) -> Result<bool, PyErr> {
    loop {
        match ops.waitpid_any() {
            Err(failure) if failure.errno == ECHILD => return Ok(true),
            Err(failure) => return Err(PyErr::os(&failure, None)),
            Ok((0, _)) => return Ok(false),
            Ok((pid, status)) => {
                let code = super::ops::exit_code(status)?;
                statuses.insert(pid, child_status(code));
            }
        }
    }
}

/// `supervise(argv, timeout, grace)`。
pub fn supervise<O: Ops>(
    ops: &mut O,
    argv: &[Vec<u32>],
    timeout: f64,
    grace: f64,
    mode: LaunchMode,
    max_str_digits: usize,
) -> Result<Supervised, SuperviseError> {
    setup(ops)?;
    if ops.latest_signal() != 0 {
        return Err(PyErr::runtime("起動前に親が終了した").into());
    }
    let pid = match ops.spawn(argv) {
        Ok(pid) => pid,
        Err(error) if mode == LaunchMode::Wrap && error.errno().is_some() => {
            return Err(SuperviseError::LaunchFailure);
        }
        Err(error) => return Err(error.into()),
    };
    let mut statuses: HashMap<i32, ChildStatus> = HashMap::new();
    let started = ops.monotonic();
    let mut timed_out = false;
    while !statuses.contains_key(&pid) && ops.latest_signal() == 0 {
        reap(ops, &mut statuses)?;
        if statuses.contains_key(&pid) {
            break;
        }
        if ops.monotonic() - started >= timeout {
            timed_out = true;
            break;
        }
        ops.sleep(0.02)?;
    }
    // 生きている親を止めると、その子プロセスはここへ移る。二重 fork も同じ。
    let term_end = ops.monotonic() + grace;
    let hard_end = term_end + 5.0;
    while !reap(ops, &mut statuses)? {
        let number = if ops.monotonic() < term_end {
            SIGTERM
        } else {
            SIGKILL
        };
        signal_owned(ops, number, max_str_digits)?;
        if ops.monotonic() >= hard_end {
            return Err(PyErr::runtime("子プロセスの不在を期限内に証明できない").into());
        }
        ops.sleep(0.02)?;
    }
    let Some(&status) = statuses.get(&pid) else {
        return Err(SuperviseError::Uncaught {
            inner: InSupervise::Returncode,
            error: PyErr::new(Class::Key, pid.to_string()),
        });
    };
    Ok(Supervised {
        status,
        timed_out,
        signal: ops.latest_signal(),
    })
}
