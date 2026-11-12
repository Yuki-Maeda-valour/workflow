//! R4Q専用。品質コマンドの監督(旧版 check-process.py・loop-supervisor.py の Rust 版)が使う OS の操作を、
//! 安全な関数として置く。
//!
//! - 各関数はシステムコールを 1 回だけ呼ぶ(子の起動 `spawn` は除く)。`EINTR` を繰り返すかは、旧版が呼ぶ
//!   Python の関数ごとに違う(PEP 475)ので、呼出元が決める。繰り返すときは [`retry_eintr`] を使う。
//! - 子の起動は std の `Command` を使わない(std は子の信号の mask を空にし、ENOEXEC で sh を起動し、
//!   SIGCHLD を無視した起動では exec の失敗で panic する)。CPython 3.10.11 の `_posixsubprocess` の
//!   `child_exec` と同じ順で、fork と exec の誤りを親へ返すパイプを使って起動する。
//! - `unsafe` はこのファイルだけに置く。各所に安全の理由を書く。
#![deny(unsafe_op_in_unsafe_fn)]

use crate::process::Status;
use rustix::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use rustix::fs::{self, Mode, OFlags, SeekFrom};
use std::ffi::CString;
use std::io;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::Duration;

/// 呼出元が区別するエラー番号。
pub mod errno {
    pub const EPERM: i32 = libc::EPERM;
    pub const ENOENT: i32 = libc::ENOENT;
    pub const ESRCH: i32 = libc::ESRCH;
    pub const EINTR: i32 = libc::EINTR;
    pub const EIO: i32 = libc::EIO;
    pub const E2BIG: i32 = libc::E2BIG;
    pub const ENOEXEC: i32 = libc::ENOEXEC;
    pub const EBADF: i32 = libc::EBADF;
    pub const ECHILD: i32 = libc::ECHILD;
    pub const EAGAIN: i32 = libc::EAGAIN;
    pub const EACCES: i32 = libc::EACCES;
    pub const EEXIST: i32 = libc::EEXIST;
    pub const ENOTDIR: i32 = libc::ENOTDIR;
    pub const EISDIR: i32 = libc::EISDIR;
    pub const EINVAL: i32 = libc::EINVAL;
    pub const EMFILE: i32 = libc::EMFILE;
    pub const EFBIG: i32 = libc::EFBIG;
    pub const ENOSPC: i32 = libc::ENOSPC;
    pub const EPIPE: i32 = libc::EPIPE;
    pub const ELOOP: i32 = libc::ELOOP;
    pub const ENOSYS: i32 = libc::ENOSYS;
}

pub const SIGHUP: i32 = libc::SIGHUP;
pub const SIGINT: i32 = libc::SIGINT;
pub const SIGQUIT: i32 = libc::SIGQUIT;
pub const SIGKILL: i32 = libc::SIGKILL;
pub const SIGUSR1: i32 = libc::SIGUSR1;
pub const SIGUSR2: i32 = libc::SIGUSR2;
pub const SIGPIPE: i32 = libc::SIGPIPE;
pub const SIGTERM: i32 = libc::SIGTERM;
pub const SIGCHLD: i32 = libc::SIGCHLD;
pub const SIGSTOP: i32 = libc::SIGSTOP;
pub const SIGCONT: i32 = libc::SIGCONT;
pub const SIGXFSZ: i32 = libc::SIGXFSZ;

fn rustix_error(error: rustix::io::Errno) -> io::Error {
    io::Error::from_raw_os_error(error.raw_os_error())
}

fn last_error() -> io::Error {
    io::Error::last_os_error()
}

/// `EINTR` のあいだ同じ呼出しを繰り返す(Python が C の中で自動で繰り返す操作の写しに使う)。
pub fn retry_eintr<T>(mut call: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    loop {
        match call() {
            Err(error) if error.raw_os_error() == Some(libc::EINTR) => continue,
            other => return other,
        }
    }
}

// ---- プロセスの番号 ----

/// `getpid()`。
pub fn getpid() -> i32 {
    // SAFETY: getpid は引数を取らず、失敗しない。
    unsafe { libc::getpid() }
}

/// `getppid()`。
pub fn getppid() -> i32 {
    // SAFETY: getppid は引数を取らず、失敗しない。
    unsafe { libc::getppid() }
}

// ---- Linux の subreaper・親の終了通知・pidfd ----

/// `prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)`(旧版 `loop-supervisor.py:27`)。
#[cfg(target_os = "linux")]
pub fn set_child_subreaper() -> io::Result<()> {
    // SAFETY: prctl へ整数だけを渡す。可変長の引数なので unsigned long にそろえる。
    let code = unsafe {
        libc::prctl(
            libc::PR_SET_CHILD_SUBREAPER,
            1 as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
        )
    };
    if code == 0 { Ok(()) } else { Err(last_error()) }
}

/// `prctl(PR_SET_PDEATHSIG, number, 0, 0, 0)`(旧版 `loop-supervisor.py:31`)。
#[cfg(target_os = "linux")]
pub fn set_parent_death_signal(number: i32) -> io::Result<()> {
    // SAFETY: prctl へ整数だけを渡す。
    let code = unsafe {
        libc::prctl(
            libc::PR_SET_PDEATHSIG,
            number as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
        )
    };
    if code == 0 { Ok(()) } else { Err(last_error()) }
}

/// `pidfd_open(pid, 0)`(Python の `os.pidfd_open`)。返す fd は exec の時に閉じる。
#[cfg(target_os = "linux")]
pub fn pidfd_open(pid: i32) -> io::Result<OwnedFd> {
    // SAFETY: syscall へ整数だけを渡す。
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::c_int, 0 as libc::c_uint) };
    if fd < 0 {
        return Err(last_error());
    }
    let fd = RawFd::try_from(fd).map_err(|_| io::Error::from_raw_os_error(libc::EBADF))?;
    // SAFETY: pidfd_open が返した、ほかに持ち主のいない新しい fd。
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// `pidfd_send_signal(pidfd, number, NULL, 0)`(Python の `signal.pidfd_send_signal`)。
#[cfg(target_os = "linux")]
pub fn pidfd_send_signal(pidfd: BorrowedFd<'_>, number: i32) -> io::Result<()> {
    // SAFETY: syscall へ整数と null の siginfo を渡す。
    let code = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            pidfd.as_raw_fd(),
            number as libc::c_int,
            std::ptr::null::<libc::siginfo_t>(),
            0 as libc::c_uint,
        )
    };
    if code == 0 { Ok(()) } else { Err(last_error()) }
}

// ---- 回収 ----

/// 回収した子の終わり方。`Unknown` は止まった子などの、終わりでない状態(`waitstatus_to_exitcode` が
/// 拒む値。`WUNTRACED` を付けないので、ふつうは返らない)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Termination {
    Exited(u8),
    Signaled(i32),
    Unknown(i32),
}

impl Termination {
    fn from_raw(status: libc::c_int) -> Self {
        if libc::WIFEXITED(status) {
            Termination::Exited(libc::WEXITSTATUS(status) as u8)
        } else if libc::WIFSIGNALED(status) {
            Termination::Signaled(libc::WTERMSIG(status))
        } else {
            Termination::Unknown(status)
        }
    }

    /// C0c の公開の型の形(数の終わりか信号の終わりか)。`Unknown` は None。
    pub fn status(self) -> Option<Status> {
        match self {
            Termination::Exited(code) => Some(Status::Exited(code)),
            Termination::Signaled(number) => Some(Status::Signaled(number)),
            Termination::Unknown(_) => None,
        }
    }
}

/// `waitpid(-1, WNOHANG)` の結果(旧版 `loop-supervisor.py:70-79` の `reap` が分ける 3 通り)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reaped {
    /// 子を 1 つ回収した。
    Child { pid: i32, status: Termination },
    /// 回収できる子がまだ無い(戻り値 0)。
    NotYet,
    /// 回収する子が無い(`ECHILD`)。
    NoChildren,
}

/// `waitpid(-1, &status, WNOHANG)`(1 回)。`ECHILD` は `NoChildren`、ほかの誤りは `Err`。
pub fn wait_any_nohang() -> io::Result<Reaped> {
    let mut status: libc::c_int = 0;
    // SAFETY: `status` は有効な受け取り先。
    let pid = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
    if pid < 0 {
        let error = last_error();
        return if error.raw_os_error() == Some(libc::ECHILD) {
            Ok(Reaped::NoChildren)
        } else {
            Err(error)
        };
    }
    if pid == 0 {
        return Ok(Reaped::NotYet);
    }
    Ok(Reaped::Child {
        pid,
        status: Termination::from_raw(status),
    })
}

/// pid を指す `waitpid` の結果(旧版の `Popen.poll`・`wait` が分ける 3 通り)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PidWait {
    /// まだ動いている(`WNOHANG` で 0)。
    Running,
    /// 回収した。
    Exited(Termination),
    /// `ECHILD`(SIGCHLD を無視した起動で自動で回収された子など)。Popen は returncode 0 とする。
    NoChild,
}

/// `waitpid(pid, &status, block ? 0 : WNOHANG)`(1 回)。`ECHILD` は `NoChild`、ほかの誤りは `Err`。
pub fn wait_pid(pid: i32, block: bool) -> io::Result<PidWait> {
    let flags = if block { 0 } else { libc::WNOHANG };
    let mut status: libc::c_int = 0;
    // SAFETY: `status` は有効な受け取り先。
    let got = unsafe { libc::waitpid(pid, &mut status, flags) };
    if got < 0 {
        let error = last_error();
        return if error.raw_os_error() == Some(libc::ECHILD) {
            Ok(PidWait::NoChild)
        } else {
            Err(error)
        };
    }
    if got == 0 {
        return Ok(PidWait::Running);
    }
    Ok(PidWait::Exited(Termination::from_raw(status)))
}

// ---- プロセスグループへの信号 ----

/// `killpg` の結果(旧版 `check-process.py:123-154` が分ける 4 通り)。
#[derive(Debug)]
pub enum GroupSignal {
    Sent,
    /// `ESRCH`(Python の `ProcessLookupError`)。
    NoProcess,
    /// `EPERM`(Python の `PermissionError`)。不在とは扱わない。
    NotPermitted,
    /// ほかの誤り。
    Failed(io::Error),
}

impl GroupSignal {
    /// `killpg` の結果を分ける(`ESRCH`・`EPERM`・ほか)。
    pub fn from_result(result: io::Result<()>) -> Self {
        match result {
            Ok(()) => GroupSignal::Sent,
            Err(error) => match error.raw_os_error() {
                Some(libc::ESRCH) => GroupSignal::NoProcess,
                Some(libc::EPERM) => GroupSignal::NotPermitted,
                _ => GroupSignal::Failed(error),
            },
        }
    }
}

/// `killpg(pgid, number)`(1 回。Python の `os.killpg` は `EINTR` を繰り返さない)。
pub fn killpg(pgid: i32, number: i32) -> GroupSignal {
    // SAFETY: killpg は整数だけを使う。
    let result = if unsafe { libc::killpg(pgid, number) } == 0 {
        Ok(())
    } else {
        Err(last_error())
    };
    GroupSignal::from_result(result)
}

/// `kill(pid, number)`(1 回)。
pub fn kill(pid: i32, number: i32) -> io::Result<()> {
    // SAFETY: kill は整数だけを使う。
    if unsafe { libc::kill(pid, number) } == 0 {
        Ok(())
    } else {
        Err(last_error())
    }
}

// ---- パイプ・読める状態の待ち・pause ----

/// 読み口と書き口の順に、両端に exec の時に閉じる印の付いたパイプを作る(Python の `os.pipe`)。
pub fn pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let (reader, writer) = std::io::pipe()?;
    Ok((OwnedFd::from(reader), OwnedFd::from(writer)))
}

/// select が扱える fd の上限(`FD_SETSIZE`)。Python の `select.select` は、これ以上の fd を
/// `ValueError('filedescriptor out of range in select()')` で拒む。
pub const SELECT_LIMIT: RawFd = libc::FD_SETSIZE as RawFd;

/// select が扱える番号か(Python の `_PyIsSelectable_fd`。0 以上 `FD_SETSIZE` 未満)。
pub fn selectable(fd: RawFd) -> bool {
    (0..SELECT_LIMIT).contains(&fd)
}

/// `select_readable` の誤り。
#[derive(Debug)]
pub enum SelectError {
    /// fd が `SELECT_LIMIT` 以上(Python の `ValueError`)。select を呼ばない。
    OutOfRange,
    /// select の失敗(`EINTR` を含む)。
    Os(io::Error),
}

/// `select([fd], [], [], timeout)` を 1 回呼び、読めるか(終わりを含む)を返す。`None` は期限なし。
pub fn select_readable(fd: BorrowedFd<'_>, timeout: Option<Duration>) -> Result<bool, SelectError> {
    let raw = fd.as_raw_fd();
    if !selectable(raw) {
        return Err(SelectError::OutOfRange);
    }
    // SAFETY: fd_set を 0 で埋めてから FD_ZERO で初期化する。raw は 0 以上 FD_SETSIZE 未満。
    let mut set: libc::fd_set = unsafe { std::mem::zeroed() };
    // SAFETY: `set` は有効な fd_set。
    unsafe {
        libc::FD_ZERO(&mut set);
        libc::FD_SET(raw, &mut set);
    }
    let mut value = timeout.map(|limit| libc::timeval {
        tv_sec: limit.as_secs() as _,
        tv_usec: limit.subsec_micros() as _,
    });
    let pointer = value
        .as_mut()
        .map_or(std::ptr::null_mut(), |value| value as *mut libc::timeval);
    // SAFETY: 読みの集合だけを渡す。期限は null か有効な timeval。
    let ready = unsafe {
        libc::select(
            raw + 1,
            &mut set,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            pointer,
        )
    };
    if ready < 0 {
        return Err(SelectError::Os(last_error()));
    }
    // SAFETY: `set` は select が書いた有効な fd_set。raw は範囲内。
    Ok(ready > 0 && unsafe { libc::FD_ISSET(raw, &set) })
}

/// `pause()`。信号の処理が走ると戻る(Python の `signal.pause`)。
pub fn pause() {
    // SAFETY: pause は引数を取らない。戻り値はいつも -1(EINTR)。
    unsafe { libc::pause() };
}

// ---- 信号の処理 ----

static FIRST: AtomicI32 = AtomicI32::new(0);
static LATEST: AtomicI32 = AtomicI32::new(0);

extern "C" fn record_first(number: libc::c_int) {
    let _ = FIRST.compare_exchange(0, number, Ordering::SeqCst, Ordering::SeqCst);
}

extern "C" fn record_latest(number: libc::c_int) {
    LATEST.store(number, Ordering::SeqCst);
}

extern "C" fn do_nothing(_number: libc::c_int) {}

/// 信号に置く処理。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Handler {
    /// `SIG_DFL`。
    Default,
    /// `SIG_IGN`。
    Ignore,
    /// 最初の 1 つの番号だけを記録する(旧版 `StopSignals.receive`)。[`first_signal`] で読む。
    RecordFirst,
    /// 後の信号で番号を上書きする(旧版 `loop-supervisor.py` の `interrupted`)。[`latest_signal`] で読む。
    RecordLatest,
    /// 何もしない(旧版の anchor の `lambda *_: None`)。捕捉した処理なので、exec の後の子では既定に戻る。
    Nothing,
}

/// 置き換える前の処理。[`restore_handler`] で戻す。
#[derive(Clone, Copy)]
pub struct Previous {
    number: i32,
    action: libc::sigaction,
}

impl Previous {
    pub fn number(&self) -> i32 {
        self.number
    }
}

/// 今の処理の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Default,
    Ignore,
    Caught,
}

/// 処理を置き、前の処理を返す。CPython 3.10 の `PyOS_setsig` と同じく `SA_ONSTACK` だけを付け
/// (`SA_RESTART` は付けない)、`sa_mask` は空にする。
pub fn set_handler(number: i32, handler: Handler) -> io::Result<Previous> {
    let pointer: libc::sighandler_t = match handler {
        Handler::Default => libc::SIG_DFL,
        Handler::Ignore => libc::SIG_IGN,
        Handler::RecordFirst => record_first as extern "C" fn(libc::c_int) as libc::sighandler_t,
        Handler::RecordLatest => record_latest as extern "C" fn(libc::c_int) as libc::sighandler_t,
        Handler::Nothing => do_nothing as extern "C" fn(libc::c_int) as libc::sighandler_t,
    };
    // SAFETY: sigaction を 0 で埋めてから必要な欄だけ設定する。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = pointer;
    action.sa_flags = libc::SA_ONSTACK;
    // SAFETY: `action.sa_mask` は有効な sigset_t。
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err(last_error());
    }
    // SAFETY: 受け取り先を 0 で埋める。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 有効な sigaction を渡し、古い設定を受け取る。
    if unsafe { libc::sigaction(number, &action, &mut old) } != 0 {
        return Err(last_error());
    }
    Ok(Previous {
        number,
        action: old,
    })
}

/// 置き換える前の処理へ戻す。
pub fn restore_handler(previous: &Previous) -> io::Result<()> {
    // SAFETY: sigaction が返した有効な設定を渡す。
    if unsafe { libc::sigaction(previous.number, &previous.action, std::ptr::null_mut()) } != 0 {
        return Err(last_error());
    }
    Ok(())
}

/// 今の処理の詳細(種類と、`SA_RESTART`・`SA_ONSTACK` の有無と、`sa_mask` が空か)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HandlerInfo {
    pub disposition: Disposition,
    pub restart: bool,
    pub onstack: bool,
    pub empty_mask: bool,
}

/// 今の処理の詳細。
pub fn handler_info(number: i32) -> io::Result<HandlerInfo> {
    // SAFETY: 受け取り先を 0 で埋め、今の設定だけを読む。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい設定を渡さない。
    if unsafe { libc::sigaction(number, std::ptr::null(), &mut old) } != 0 {
        return Err(last_error());
    }
    let disposition = match old.sa_sigaction {
        libc::SIG_DFL => Disposition::Default,
        libc::SIG_IGN => Disposition::Ignore,
        _ => Disposition::Caught,
    };
    let empty_mask = (1..32).all(|other| {
        // SAFETY: `old.sa_mask` は sigaction が返した有効な sigset_t。
        unsafe { libc::sigismember(&old.sa_mask, other) != 1 }
    });
    Ok(HandlerInfo {
        disposition,
        restart: old.sa_flags & libc::SA_RESTART != 0,
        onstack: old.sa_flags & libc::SA_ONSTACK != 0,
        empty_mask,
    })
}

/// 今の処理の種類。
pub fn disposition(number: i32) -> io::Result<Disposition> {
    // SAFETY: 受け取り先を 0 で埋め、今の設定だけを読む。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい設定を渡さない。
    if unsafe { libc::sigaction(number, std::ptr::null(), &mut old) } != 0 {
        return Err(last_error());
    }
    Ok(match old.sa_sigaction {
        libc::SIG_DFL => Disposition::Default,
        libc::SIG_IGN => Disposition::Ignore,
        _ => Disposition::Caught,
    })
}

/// [`Handler::RecordFirst`] が記録した最初の番号(無ければ 0)。
pub fn first_signal() -> i32 {
    FIRST.load(Ordering::SeqCst)
}

/// [`Handler::RecordFirst`] の記録を消す(旧版 `StopSignals.__init__` の `number = 0`)。
pub fn clear_first_signal() {
    FIRST.store(0, Ordering::SeqCst);
}

/// [`Handler::RecordLatest`] が記録した最後の番号(無ければ 0)。
pub fn latest_signal() -> i32 {
    LATEST.load(Ordering::SeqCst)
}

/// [`Handler::RecordLatest`] の記録を書く(旧版 `setup` の `interrupted(signal.SIGTERM, None)`)。
pub fn set_latest_signal(number: i32) {
    LATEST.store(number, Ordering::SeqCst);
}

/// SIGPIPE と SIGXFSZ を無視にする(Python の起動時の `_PySignal_Init` と同じ順)。
pub fn ignore_pipe_and_file_size_signals() -> io::Result<()> {
    set_handler(libc::SIGPIPE, Handler::Ignore)?;
    set_handler(libc::SIGXFSZ, Handler::Ignore)?;
    Ok(())
}

/// 止めた信号の集合(`pthread_sigmask` の前の mask)。
#[derive(Clone, Copy)]
pub struct SignalMask(libc::sigset_t);

impl SignalMask {
    /// 番号が集合にあるか。
    pub fn contains(&self, number: i32) -> bool {
        // SAFETY: `self.0` は pthread_sigmask が返した有効な sigset_t。
        unsafe { libc::sigismember(&self.0, number) == 1 }
    }
}

fn signal_set(numbers: &[i32]) -> io::Result<libc::sigset_t> {
    // SAFETY: sigset_t を 0 で埋めてから sigemptyset で初期化する。
    let mut set: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: `set` は有効な sigset_t。
    if unsafe { libc::sigemptyset(&mut set) } != 0 {
        return Err(last_error());
    }
    for &number in numbers {
        // SAFETY: `set` は初期化済み。番号が不正なら EINVAL が返る。
        if unsafe { libc::sigaddset(&mut set, number) } != 0 {
            return Err(last_error());
        }
    }
    Ok(set)
}

fn change_mask(how: libc::c_int, set: Option<&libc::sigset_t>) -> io::Result<SignalMask> {
    // SAFETY: 受け取り先を 0 で埋める。
    let mut old: libc::sigset_t = unsafe { std::mem::zeroed() };
    let pointer = set.map_or(std::ptr::null(), |set| set as *const libc::sigset_t);
    // SAFETY: 有効な sigset_t か null を渡す。
    let code = unsafe { libc::pthread_sigmask(how, pointer, &mut old) };
    if code != 0 {
        return Err(io::Error::from_raw_os_error(code));
    }
    Ok(SignalMask(old))
}

/// 呼んだスレッドで信号を止め、止める前の mask を返す。
pub fn block_signals(numbers: &[i32]) -> io::Result<SignalMask> {
    let set = signal_set(numbers)?;
    change_mask(libc::SIG_BLOCK, Some(&set))
}

/// 呼んだスレッドの今の mask。
pub fn current_mask() -> io::Result<SignalMask> {
    change_mask(libc::SIG_BLOCK, None)
}

/// 呼んだスレッドの mask を戻す。
pub fn set_mask(mask: &SignalMask) -> io::Result<()> {
    change_mask(libc::SIG_SETMASK, Some(&mask.0)).map(|_| ())
}

/// `handle` のスレッドへ信号を送る(`pthread_kill`)。
///
/// 送り先は、まだ join していない `JoinHandle` を借りて指す。借りている間は join されないので、
/// スレッドが終わっていても `pthread_t` の記憶域は解放されない(POSIX は join するまで有効とする)。
// pthread_t の整数型は std と libc で同じ型のこともある(Linux)。
#[allow(clippy::unnecessary_cast)]
pub fn signal_thread<T>(handle: &std::thread::JoinHandle<T>, number: i32) -> io::Result<()> {
    use std::os::unix::thread::JoinHandleExt;
    let thread: libc::pthread_t = handle.as_pthread_t() as _;
    // SAFETY: 借りた JoinHandle のスレッドは join されていないので、pthread_t は有効。
    let code = unsafe { libc::pthread_kill(thread, number) };
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code))
    }
}

/// 呼んだスレッドだけへ信号を送る(`pthread_kill`)。
pub fn signal_current_thread(number: i32) -> io::Result<()> {
    // SAFETY: 自分のスレッドへ送る。番号は呼出元が渡す。
    let code = unsafe { libc::pthread_kill(libc::pthread_self(), number) };
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code))
    }
}

/// 標準出力(fd 1)の端末の幅(Python の `os.get_terminal_size(sys.__stdout__.fileno()).columns`)。
/// 端末でないか取得に失敗すれば None(`shutil.get_terminal_size` は 80 にする)。0 もそのまま返す。
pub fn stdout_terminal_columns() -> Option<u16> {
    rustix::termios::tcgetwinsize(std::io::stdout())
        .ok()
        .map(|size| size.ws_col)
}

// ---- 記録と出力のファイル ----

/// 記録のファイルを作る(旧版 `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, 0o600)`。Python は
/// `O_CLOEXEC` も足す)。
pub fn create_record(path: &[u8]) -> io::Result<OwnedFd> {
    fs::open(
        path,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
    )
    .map_err(rustix_error)
}

/// 記録を読むために開く(旧版 `read_result` の `O_RDONLY|O_NONBLOCK|O_NOFOLLOW`。`O_CLOEXEC` も足す)。
pub fn open_record(path: &[u8]) -> io::Result<OwnedFd> {
    fs::open(
        path,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(rustix_error)
}

/// 出力のファイルを開く(旧版 `Path.open('w+b')` の `O_RDWR|O_CREAT|O_TRUNC|O_CLOEXEC`、0o666。リンクを辿る)。
pub fn open_output(path: &[u8]) -> io::Result<OwnedFd> {
    fs::open(
        path,
        OFlags::RDWR | OFlags::CREATE | OFlags::TRUNC | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o666),
    )
    .map_err(rustix_error)
}

/// `/dev/null` を `O_RDWR|O_CLOEXEC` で開く(旧版の `Popen` の `stdin=DEVNULL`)。
pub fn open_dev_null() -> io::Result<OwnedFd> {
    fs::open("/dev/null", OFlags::RDWR | OFlags::CLOEXEC, Mode::empty()).map_err(rustix_error)
}

/// `fstat` のうち旧版が見る値。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileInfo {
    pub regular: bool,
    pub size: i64,
}

/// `fstat(fd)`。
// stat の整数型は OS で違う。取得値を損失なく持つ。
#[allow(clippy::unnecessary_cast)]
pub fn fstat(fd: BorrowedFd<'_>) -> io::Result<FileInfo> {
    let stat = fs::fstat(fd).map_err(rustix_error)?;
    Ok(FileInfo {
        regular: (stat.st_mode as u32) & 0o170000 == 0o100000,
        size: stat.st_size as i64,
    })
}

/// `read(fd, buffer)`(1 回)。
pub fn read(fd: BorrowedFd<'_>, buffer: &mut [u8]) -> io::Result<usize> {
    rustix::io::read(fd, buffer).map_err(rustix_error)
}

/// `write(fd, buffer)`(1 回)。
pub fn write(fd: BorrowedFd<'_>, buffer: &[u8]) -> io::Result<usize> {
    rustix::io::write(fd, buffer).map_err(rustix_error)
}

/// `fsync(fd)`。
pub fn fsync(fd: BorrowedFd<'_>) -> io::Result<()> {
    fs::fsync(fd).map_err(rustix_error)
}

/// `lseek(fd, 0, SEEK_SET)`。
pub fn seek_start(fd: BorrowedFd<'_>) -> io::Result<u64> {
    fs::seek(fd, SeekFrom::Start(0)).map_err(rustix_error)
}

/// 失敗を返す `close`。`EINTR` でも繰り返さない(Python の `os.close` と同じ)。
pub fn close(fd: OwnedFd) -> io::Result<()> {
    let raw = std::os::fd::IntoRawFd::into_raw_fd(fd);
    // SAFETY: `raw` は所有していた fd で、ここで手放した。以後この番号を使わない。
    if unsafe { libc::close(raw) } == 0 {
        Ok(())
    } else {
        Err(last_error())
    }
}

// ---- /proc の読取り(監督の所有の確かめ。段階 5 の loop.sh の Rust 版も使う) ----

/// `Path(…).read_bytes()` の失敗。旧版の OSError は、開く段の誤りにだけファイル名が付く(読む段の
/// `FileIO.readall` の誤りには付かない)。
#[derive(Debug)]
pub enum ProcReadError {
    Open(io::Error),
    Read(io::Error),
}

impl ProcReadError {
    pub fn errno(&self) -> i32 {
        match self {
            ProcReadError::Open(error) | ProcReadError::Read(error) => {
                error.raw_os_error().unwrap_or(0)
            }
        }
    }

    /// 旧版の例外にファイル名が付くか。
    pub fn has_filename(&self) -> bool {
        matches!(self, ProcReadError::Open(_))
    }
}

/// ファイルの全部を読む(`FileIO` の open〈`O_RDONLY|O_CLOEXEC`〉と `readall`。どちらも EINTR を繰り返す)。
fn read_whole(path: &[u8]) -> Result<Vec<u8>, ProcReadError> {
    let fd = retry_eintr(|| {
        fs::open(path, OFlags::RDONLY | OFlags::CLOEXEC, Mode::empty()).map_err(rustix_error)
    })
    .map_err(ProcReadError::Open)?;
    let mut out = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match retry_eintr(|| read(fd.as_fd(), &mut chunk)) {
            Ok(0) => return Ok(out),
            Ok(count) => out.extend_from_slice(&chunk[..count]),
            Err(error) => return Err(ProcReadError::Read(error)),
        }
    }
}

/// 旧版 `own_children` のパス(`/proc/self/task/<pid>/children`)。
pub fn own_children_path(pid: i32) -> String {
    format!("/proc/self/task/{pid}/children")
}

/// 旧版 `identity` のパス(`/proc/<pid>/stat`。`pid` は `children` から読んだ 10 進の字面)。
pub fn proc_stat_path(pid: &str) -> String {
    format!("/proc/{pid}/stat")
}

/// `Path(f'/proc/self/task/{os.getpid()}/children').read_text()` の読取り(復号は呼出元。
/// 旧版の `read_text` は UTF-8 の strict)。
pub fn read_own_children(pid: i32) -> Result<Vec<u8>, ProcReadError> {
    read_whole(own_children_path(pid).as_bytes())
}

/// `Path(f'/proc/{pid}/stat').read_bytes()`(旧版 `identity` の読取り。解釈は呼出元の純粋な関数)。
pub fn read_proc_stat(pid: &str) -> Result<Vec<u8>, ProcReadError> {
    read_whole(proc_stat_path(pid).as_bytes())
}

// ---- 乱数の /dev/urandom の段(CPython 3.10.11 `Python/bootstrap_hash.c` の `dev_urandom`) ----

/// `dev_urandom(buffer, size, raise=1)` の失敗。
#[derive(Debug)]
pub enum UrandomError {
    /// 開けない(ENOENT・ENXIO・ENODEV・EACCES)。旧版は `NotImplementedError('/dev/urandom (or equivalent) not found')`。
    NotFound,
    /// ほかの開く誤り(旧版は `/dev/urandom` のファイル名付きの OSError)。
    Open(io::Error),
    /// 開いた後の fstat の誤り(ファイル名の無い OSError)。
    Fstat(io::Error),
    /// 読む誤り(ファイル名の無い OSError)。
    Read(io::Error),
    /// 途中で終わった(`RuntimeError('Failed to read <残りのバイト数> bytes from /dev/urandom')`)。
    Eof(usize),
}

/// 開いた `/dev/urandom` の控え(CPython の `urandom_cache`。プロセスの間ずっと開いておく)。
static URANDOM: std::sync::Mutex<Option<(OwnedFd, libc::dev_t, libc::ino_t)>> =
    std::sync::Mutex::new(None);

/// `/dev/urandom` から `buffer` を満たす(getrandom・getentropy が ENOSYS・EPERM のときだけ呼ぶ)。
pub fn dev_urandom(buffer: &mut [u8]) -> Result<(), UrandomError> {
    let mut cache = URANDOM
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let stale = cache.as_ref().is_some_and(|(fd, dev, ino)| {
        fs::fstat(fd.as_fd()).map_or(true, |info| info.st_dev != *dev || info.st_ino != *ino)
    });
    if stale && let Some((fd, _, _)) = cache.take() {
        // 閉じない(ほかの用途で使われている番号かもしれない。CPython と同じ)。
        let _ = std::os::fd::IntoRawFd::into_raw_fd(fd);
    }
    if cache.is_none() {
        let fd = retry_eintr(|| {
            fs::open(
                "/dev/urandom",
                OFlags::RDONLY | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .map_err(rustix_error)
        })
        .map_err(|error| match error.raw_os_error() {
            Some(libc::ENOENT | libc::ENXIO | libc::ENODEV | libc::EACCES) => {
                UrandomError::NotFound
            }
            _ => UrandomError::Open(error),
        })?;
        let info =
            fs::fstat(fd.as_fd()).map_err(|error| UrandomError::Fstat(rustix_error(error)))?;
        *cache = Some((fd, info.st_dev, info.st_ino));
    }
    let Some((fd, _, _)) = cache.as_ref() else {
        return Err(UrandomError::Fstat(io::Error::from_raw_os_error(
            libc::EBADF,
        )));
    };
    let mut filled = 0usize;
    while filled < buffer.len() {
        match retry_eintr(|| read(fd.as_fd(), &mut buffer[filled..])) {
            Ok(0) => return Err(UrandomError::Eof(buffer.len() - filled)),
            Ok(count) => filled += count,
            Err(error) => return Err(UrandomError::Read(error)),
        }
    }
    Ok(())
}

/// anchor が `--anchor-fd` で受けた、起動した側から継いだ fd(旧版 `anchor(fd, nonce, argv)` の fd)を借りる。
/// - 使う所は check-process の anchor の通知の fd だけ(継がない設定にして、通知の行を 1 回書く)。
/// - 負の番号と開いていない番号は `EBADF`(旧版の `os.set_inheritable(fd, False)` と同じ errno)。
/// - このプロセスはこの fd を閉じない(anchor は KILL まで pause を続ける)ので、借用はプロセスの終わりまで有効。
pub fn inherited_fd(fd: RawFd) -> io::Result<BorrowedFd<'static>> {
    if fd < 0 {
        return Err(io::Error::from_raw_os_error(libc::EBADF));
    }
    // SAFETY: fcntl(F_GETFD) は番号の fd の印を読むだけで、記憶域に触れない。
    if unsafe { libc::fcntl(fd, libc::F_GETFD) } < 0 {
        return Err(last_error());
    }
    // SAFETY: 上で開いていることを確かめた。起動した側から継いだ fd で、このプロセスは閉じない。
    Ok(unsafe { BorrowedFd::borrow_raw(fd) })
}

/// fd の exec の時に閉じる印を変える(Python の `os.set_inheritable(fd, inheritable)`)。
pub fn set_inheritable(fd: BorrowedFd<'_>, inheritable: bool) -> io::Result<()> {
    set_cloexec(fd.as_raw_fd(), !inheritable)
}

// ---- 子の起動 ----

/// 実行ファイルの候補(旧版の `subprocess.Popen` の `_execute_child`)。`program` に `/` があれば
/// それだけ。無ければ PATH(`None` は `os.defpath` の `/bin:/usr/bin`)を `:` で分けた各要素と
/// `posixpath.join` で結んだもの(空の要素は作業フォルダからの相対)。
pub fn exec_candidates(program: &[u8], path: Option<&[u8]>) -> Vec<Vec<u8>> {
    if program.contains(&b'/') {
        return vec![program.to_vec()];
    }
    let path = path.unwrap_or(b"/bin:/usr/bin");
    path.split(|&byte| byte == b':')
        .map(|directory| {
            let mut joined = directory.to_vec();
            if !(directory.is_empty() || directory.ends_with(b"/")) {
                joined.push(b'/');
            }
            joined.extend_from_slice(program);
            joined
        })
        .collect()
}

/// 起動の要求。旧版の `subprocess.Popen(argv, close_fds=True, …)` の引数に当たる。
pub struct Spawn<'a> {
    /// 子の argv。
    pub argv: &'a [&'a [u8]],
    /// 順に `execve` を試す実行ファイル([`exec_candidates`] の結果)。
    pub executables: &'a [&'a [u8]],
    /// 子の環境(`(鍵, 値)` を渡した順)。`None` は起動した側の環境(`execv`)。
    pub env: Option<&'a [(&'a [u8], &'a [u8])]>,
    /// 子の作業フォルダ。`None` は起動した側のまま。
    pub cwd: Option<&'a [u8]>,
    /// 0・1・2 番の向け先。`None` は起動した側のまま。
    pub stdin: Option<BorrowedFd<'a>>,
    pub stdout: Option<BorrowedFd<'a>>,
    pub stderr: Option<BorrowedFd<'a>>,
    /// 同じ番号で子へ継がせる fd(`pass_fds`)。ほかの 3 以上の fd は閉じる。
    pub pass_fds: &'a [BorrowedFd<'a>],
    /// 子で `setsid` を呼ぶか(`start_new_session=True`)。
    pub new_session: bool,
}

/// 起動の失敗。
#[derive(Debug)]
pub enum SpawnError {
    /// 環境の鍵に `=` がある(旧版の `ValueError('illegal environment variable name')`)。fork しない。
    IllegalEnvironmentName,
    /// 実行ファイル・argv・環境・作業フォルダに NUL がある(旧版の `ValueError('embedded null byte')`)。
    /// fork しない。
    EmbeddedNull,
    /// 起動した側の OS の操作の失敗(パイプ・mask・fork・誤りのパイプの読み・失敗した子の回収)。
    Parent(io::Error),
    /// 子の準備か exec の失敗。`before_exec` は exec に届く前(旧版の `noexec`。ファイル名は作業フォルダ)、
    /// そうでなければ exec の失敗(ファイル名は argv[0])。失敗した子は回収済み。
    Child { errno: i32, before_exec: bool },
    /// 子が返した誤りの形が不正(旧版の `SubprocessError('Bad exception data from child: …')` に当たる)。
    BadChildData(Vec<u8>),
}

/// 親で用意した NUL 終わりの文字列の配列。子の中では読むだけ。
struct Strings {
    _owned: Vec<CString>,
    pointers: Vec<*const libc::c_char>,
}

impl Strings {
    fn new<I, T>(items: I) -> Result<Self, SpawnError>
    where
        I: IntoIterator<Item = T>,
        T: Into<Vec<u8>>,
    {
        let mut owned = Vec::new();
        for item in items {
            owned.push(CString::new(item).map_err(|_| SpawnError::EmbeddedNull)?);
        }
        let mut pointers: Vec<*const libc::c_char> =
            owned.iter().map(|item| item.as_ptr()).collect();
        pointers.push(std::ptr::null());
        Ok(Self {
            _owned: owned,
            pointers,
        })
    }

    fn len(&self) -> usize {
        self.pointers.len() - 1
    }
}

/// 子の中だけで使う値。親で作り、子の中では確保・解放・ロックをしない。
struct ChildPlan {
    executables: Strings,
    argv: Strings,
    envp: Option<Strings>,
    cwd: Option<CString>,
    stdin: RawFd,
    stdout: RawFd,
    stderr: RawFd,
    /// 3 以上で閉じない fd(pass_fds と誤りのパイプの書き口)。小さい順。
    keep: Vec<RawFd>,
    errpipe_read: RawFd,
    errpipe_write: RawFd,
    new_session: bool,
    /// Linux(旧版は vfork)では、起動の前の mask。子で処理を既定へ戻してから、この mask にする。
    child_mask: Option<libc::sigset_t>,
    /// Mac(旧版は `/dev/fd` を子で列挙する)では、親で列挙した fd。列挙できなければ None。
    #[cfg_attr(target_os = "linux", allow(dead_code))]
    enumerated: Option<Vec<RawFd>>,
    /// `sysconf(_SC_OPEN_MAX)`(取れなければ 256)。列挙できないときの上限。
    max_fd: libc::c_long,
}

fn set_cloexec(fd: RawFd, on: bool) -> io::Result<()> {
    // SAFETY: fcntl は整数だけを使う。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(last_error());
    }
    let wanted = if on {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    if wanted == flags {
        return Ok(());
    }
    // SAFETY: fcntl は整数だけを使う。
    if unsafe { libc::fcntl(fd, libc::F_SETFD, wanted) } < 0 {
        return Err(last_error());
    }
    Ok(())
}

/// 子の中の errno(確保しない)。
fn child_errno() -> i32 {
    io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// 子の中の `POSIX_CALL`。-1 なら errno を返す。
fn posix_call(code: libc::c_int) -> Result<libc::c_int, i32> {
    if code == -1 {
        Err(child_errno())
    } else {
        Ok(code)
    }
}

/// CPython の `_pos_int_from_ascii`(NUL 終わりの 10 進。数字でなければ None)。
#[cfg(target_os = "linux")]
fn fd_from_name(name: &[u8]) -> Option<RawFd> {
    let mut value: RawFd = 0;
    for &byte in name {
        if byte == 0 {
            break;
        }
        if !byte.is_ascii_digit() {
            return None;
        }
        value = value
            .wrapping_mul(10)
            .wrapping_add(RawFd::from(byte - b'0'));
    }
    Some(value)
}

impl ChildPlan {
    /// fork の後の子で呼ぶ。exec に成功すれば戻らない。失敗すれば親へ errno を書いて `_exit(255)`。
    fn run(&self) -> ! {
        let (errno, before_exec) = match self.prepare() {
            Err(errno) => (errno, true),
            Ok(()) => (self.exec(), false),
        };
        let mut report = [0u8; 5];
        report[..4].copy_from_slice(&errno.to_ne_bytes());
        report[4] = u8::from(before_exec);
        loop {
            // SAFETY: 親で作ったパイプの書き口へ、5 バイトの配列を書く。
            let written =
                unsafe { libc::write(self.errpipe_write, report.as_ptr().cast(), report.len()) };
            if written >= 0 || child_errno() != libc::EINTR {
                break;
            }
        }
        // SAFETY: exec の失敗の後、何も片付けずに終わる(std の終了処理を走らせない)。
        unsafe { libc::_exit(255) }
    }

    /// `child_exec` の exec の前(旧版の `reached_preexec` まで)。
    fn prepare(&self) -> Result<(), i32> {
        // make_inheritable: 継がせる fd の印を外す(誤りのパイプの書き口は exec の時に閉じる)。
        for &fd in &self.keep {
            if fd != self.errpipe_write {
                set_cloexec(fd, false).map_err(|error| error.raw_os_error().unwrap_or(0))?;
            }
        }
        // SAFETY: 子の中の、親の読み口の複製を閉じる。
        posix_call(unsafe { libc::close(self.errpipe_read) })?;
        let mut stdout = self.stdout;
        let mut stderr = self.stderr;
        if stdout == 0 {
            // SAFETY: dup は整数だけを使う。
            stdout = posix_call(unsafe { libc::dup(stdout) })?;
            set_cloexec(stdout, true).map_err(|error| error.raw_os_error().unwrap_or(0))?;
        }
        while stderr == 0 || stderr == 1 {
            // SAFETY: dup は整数だけを使う。
            stderr = posix_call(unsafe { libc::dup(stderr) })?;
            set_cloexec(stderr, true).map_err(|error| error.raw_os_error().unwrap_or(0))?;
        }
        for (source, target) in [(self.stdin, 0), (stdout, 1), (stderr, 2)] {
            if source == target {
                // dup2 が何もしない場合は、継がれるように印を外す(CPython と同じ)。
                set_cloexec(source, false).map_err(|error| error.raw_os_error().unwrap_or(0))?;
            } else if source != -1 {
                // SAFETY: dup2 は整数だけを使う。
                posix_call(unsafe { libc::dup2(source, target) })?;
            }
        }
        if let Some(cwd) = &self.cwd {
            // SAFETY: 親で作った NUL 終わりのパスを渡す。
            posix_call(unsafe { libc::chdir(cwd.as_ptr()) })?;
        }
        // _Py_RestoreSignals: SIGPIPE と SIGXFSZ を既定へ(失敗は無視する)。
        for number in [libc::SIGPIPE, libc::SIGXFSZ] {
            let _ = set_handler(number, Handler::Default);
        }
        if let Some(mask) = &self.child_mask {
            reset_signal_handlers(mask);
            // SAFETY: 親で取った有効な sigset_t。
            let code =
                unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, mask, std::ptr::null_mut()) };
            if code != 0 {
                return Err(code);
            }
        }
        if self.new_session {
            // SAFETY: setsid は引数を取らない。
            posix_call(unsafe { libc::setsid() })?;
        }
        self.close_open_fds();
        Ok(())
    }

    /// 実行ファイルの候補へ順に exec する。戻れば、報告する errno(最初の ENOENT・ENOTDIR でない誤り、
    /// 全部がそうなら最後の誤り)。
    fn exec(&self) -> i32 {
        let mut saved = 0;
        let mut last = 0;
        for &executable in &self.executables.pointers[..self.executables.len()] {
            match &self.envp {
                // SAFETY: 親で作った NUL 終わりの文字列と null 終わりの配列を渡す。
                Some(envp) => unsafe {
                    libc::execve(
                        executable,
                        self.argv.pointers.as_ptr(),
                        envp.pointers.as_ptr(),
                    );
                },
                // SAFETY: 同上。環境は起動した側の environ。
                None => unsafe {
                    libc::execv(executable, self.argv.pointers.as_ptr());
                },
            }
            last = child_errno();
            if last != libc::ENOENT && last != libc::ENOTDIR && saved == 0 {
                saved = last;
            }
        }
        if saved != 0 { saved } else { last }
    }

    fn kept(&self, fd: RawFd) -> bool {
        self.keep.binary_search(&fd).is_ok()
    }

    /// CPython の `_close_fds_by_brute_force`(3 から上限まで、残す fd のほかを閉じる)。
    fn close_by_brute_force(&self) {
        let end = RawFd::try_from(self.max_fd).unwrap_or(RawFd::MAX);
        let mut fd = 3;
        while fd <= end {
            if !self.kept(fd) {
                // SAFETY: 子の中で番号の fd を閉じる。開いていなければ EBADF で、無視する。
                unsafe { libc::close(fd) };
            }
            if fd == RawFd::MAX {
                break;
            }
            fd += 1;
        }
    }

    /// Linux: CPython の `_close_open_fds_safe`(`/proc/self/fd` を getdents64 で読み、閉じる)。
    #[cfg(target_os = "linux")]
    fn close_open_fds(&self) {
        // SAFETY: NUL 終わりの固定のパスを開く。
        let directory =
            unsafe { libc::open(c"/proc/self/fd".as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
        if directory == -1 {
            self.close_by_brute_force();
            return;
        }
        // struct linux_dirent64 1 つ分(8 バイト境界の 280 バイト)。
        let mut buffer = [0u64; 35];
        loop {
            // SAFETY: 書き込める領域とその長さを渡す。
            let bytes = unsafe {
                libc::syscall(
                    libc::SYS_getdents64,
                    directory,
                    buffer.as_mut_ptr(),
                    std::mem::size_of_val(&buffer),
                )
            };
            if bytes <= 0 {
                break;
            }
            let bytes = bytes as usize;
            // SAFETY: getdents64 が書いた先頭の `bytes` バイトを、バイト列として読む。
            let raw = unsafe { std::slice::from_raw_parts(buffer.as_ptr().cast::<u8>(), bytes) };
            let mut offset = 0;
            while offset + 19 <= bytes {
                let reclen = usize::from(u16::from_ne_bytes([raw[offset + 16], raw[offset + 17]]));
                if reclen == 0 || offset + reclen > bytes {
                    break;
                }
                let name = &raw[offset + 19..offset + reclen];
                if let Some(fd) = fd_from_name(name)
                    && fd != directory
                    && fd >= 3
                    && !self.kept(fd)
                {
                    // SAFETY: 子の中で番号の fd を閉じる。
                    unsafe { libc::close(fd) };
                }
                offset += reclen;
            }
        }
        // SAFETY: 上で開いた fd を閉じる。
        unsafe { libc::close(directory) };
    }

    /// Mac: 旧版は子で `/dev/fd` を opendir で列挙する。子では確保しないため、親で列挙した fd を閉じる
    /// (列挙できなければ上限まで)。
    #[cfg(not(target_os = "linux"))]
    fn close_open_fds(&self) {
        match &self.enumerated {
            Some(fds) => {
                for &fd in fds {
                    if fd >= 3 && !self.kept(fd) {
                        // SAFETY: 子の中で番号の fd を閉じる。
                        unsafe { libc::close(fd) };
                    }
                }
            }
            None => self.close_by_brute_force(),
        }
    }
}

/// CPython の `reset_signal_handlers`(vfork の子)。子の mask で止めない信号のうち、捕捉している処理を
/// 既定へ戻す(無視と既定はそのまま)。誤りは無視する。
fn reset_signal_handlers(child_mask: &libc::sigset_t) {
    // Linux の _NSIG は 65。
    for number in 1..65 {
        if number == libc::SIGKILL || number == libc::SIGSTOP {
            continue;
        }
        // SAFETY: 有効な sigset_t と番号を渡す。
        if unsafe { libc::sigismember(child_mask, number) } == 1 {
            continue;
        }
        // SAFETY: 受け取り先を 0 で埋める。
        let mut current: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: 新しい設定を渡さず、今の設定を受け取る。
        if unsafe { libc::sigaction(number, std::ptr::null(), &mut current) } == -1 {
            continue;
        }
        if current.sa_sigaction == libc::SIG_IGN || current.sa_sigaction == libc::SIG_DFL {
            continue;
        }
        // SAFETY: 0 で埋めた設定(SIG_DFL・flags 0・空の mask)を渡す。
        let default: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: 有効な sigaction を渡す。
        unsafe { libc::sigaction(number, &default, std::ptr::null_mut()) };
    }
}

/// CPython の `safe_get_max_fd`。
fn max_fd() -> libc::c_long {
    // SAFETY: sysconf は整数だけを使う。
    let value = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if value == -1 { 256 } else { value }
}

#[cfg(not(target_os = "linux"))]
fn enumerate_open_fds() -> Option<Vec<RawFd>> {
    let mut fds = Vec::new();
    for entry in std::fs::read_dir("/dev/fd").ok()? {
        let entry = entry.ok()?;
        if let Some(fd) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<RawFd>().ok())
        {
            fds.push(fd);
        }
    }
    Some(fds)
}

/// 誤りのパイプの書き口を 3 以上にする(旧版の `while errpipe_write < 3: os.dup(…)`)。
fn above_standard(fd: OwnedFd) -> io::Result<OwnedFd> {
    if fd.as_raw_fd() >= 3 {
        return Ok(fd);
    }
    // SAFETY: fcntl は整数だけを使う。3 以上の最小の番号へ、exec の時に閉じる印付きで複製する。
    let copy = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
    if copy < 0 {
        return Err(last_error());
    }
    drop(fd);
    // SAFETY: fcntl が返した、ほかに持ち主のいない新しい fd。
    Ok(unsafe { OwnedFd::from_raw_fd(copy) })
}

/// 子を起動して pid を返す(旧版の `subprocess.Popen` の `_execute_child` と `_posixsubprocess.fork_exec`)。
///
/// 順は旧版と同じ: 誤りのパイプ → 環境の鍵の `=` → 実行ファイル・argv・環境・作業フォルダの NUL →
/// (Linux)全部の信号を止める → fork → 親の mask を戻す → 書き口を閉じる → 誤りのパイプを終わりまで読む
/// (`EINTR` は繰り返す)→ 誤りがあれば子を `waitpid(pid, 0)` で回収(`ECHILD` は無視)して失敗を返す。
pub fn spawn(request: &Spawn<'_>) -> Result<i32, SpawnError> {
    let (errpipe_read, errpipe_write) = pipe().map_err(SpawnError::Parent)?;
    let errpipe_write = above_standard(errpipe_write).map_err(SpawnError::Parent)?;
    if let Some(env) = request.env
        && env.iter().any(|(key, _)| key.contains(&b'='))
    {
        return Err(SpawnError::IllegalEnvironmentName);
    }
    let executables = Strings::new(request.executables.iter().copied())?;
    let argv = Strings::new(request.argv.iter().copied())?;
    let envp = match request.env {
        Some(env) => Some(Strings::new(env.iter().map(|(key, value)| {
            let mut entry = key.to_vec();
            entry.push(b'=');
            entry.extend_from_slice(value);
            entry
        }))?),
        None => None,
    };
    let cwd = match request.cwd {
        Some(cwd) => Some(CString::new(cwd).map_err(|_| SpawnError::EmbeddedNull)?),
        None => None,
    };
    let mut keep: Vec<RawFd> = request
        .pass_fds
        .iter()
        .map(AsRawFd::as_raw_fd)
        .chain([errpipe_write.as_raw_fd()])
        .collect();
    keep.sort_unstable();
    keep.dedup();
    let raw = |fd: Option<BorrowedFd<'_>>| fd.map_or(-1, |fd| fd.as_raw_fd());
    #[cfg(target_os = "linux")]
    let enumerated = None;
    #[cfg(not(target_os = "linux"))]
    let enumerated = enumerate_open_fds();
    let stdin = raw(request.stdin);
    let stdout = raw(request.stdout);
    let stderr = raw(request.stderr);
    let max_fd = max_fd();
    // Linux(旧版は vfork)は、全部の信号を止めてから fork し、子で処理を既定へ戻してから元の mask にする。
    #[cfg(target_os = "linux")]
    let old_mask = {
        // SAFETY: sigset_t を 0 で埋めてから sigfillset で全部の信号にする。
        let mut all: libc::sigset_t = unsafe { std::mem::zeroed() };
        // SAFETY: `all` は有効な sigset_t。
        unsafe { libc::sigfillset(&mut all) };
        Some(change_mask(libc::SIG_BLOCK, Some(&all)).map_err(SpawnError::Parent)?)
    };
    #[cfg(not(target_os = "linux"))]
    let old_mask: Option<SignalMask> = None;
    let plan = ChildPlan {
        executables,
        argv,
        envp,
        cwd,
        stdin,
        stdout,
        stderr,
        keep,
        errpipe_read: errpipe_read.as_raw_fd(),
        errpipe_write: errpipe_write.as_raw_fd(),
        new_session: request.new_session,
        child_mask: old_mask.map(|mask| mask.0),
        enumerated,
        max_fd,
    };
    // SAFETY: fork の後の子では、親で用意した値を読み、非同期シグナル安全な呼出しだけを使って exec するか
    // `_exit` で終わる(`ChildPlan::run`)。確保・解放・ロックはしない。
    let pid = unsafe { libc::fork() };
    if pid == 0 {
        plan.run();
    }
    let fork_error = (pid == -1).then(last_error);
    if let Some(old) = &old_mask {
        let _ = set_mask(old);
    }
    drop(errpipe_write);
    if let Some(error) = fork_error {
        return Err(SpawnError::Parent(error));
    }
    let mut data = Vec::new();
    let mut chunk = [0u8; 64];
    loop {
        match retry_eintr(|| read(errpipe_read.as_fd(), &mut chunk)) {
            Ok(0) => break,
            Ok(count) => {
                data.extend_from_slice(&chunk[..count]);
                if data.len() > 50000 {
                    break;
                }
            }
            Err(error) => return Err(SpawnError::Parent(error)),
        }
    }
    drop(errpipe_read);
    if data.is_empty() {
        return Ok(pid);
    }
    match retry_eintr(|| wait_pid(pid, true)) {
        Ok(_) => {}
        Err(error) => return Err(SpawnError::Parent(error)),
    }
    if data.len() != 5 {
        return Err(SpawnError::BadChildData(data));
    }
    let errno = i32::from_ne_bytes([data[0], data[1], data[2], data[3]]);
    Err(SpawnError::Child {
        errno,
        before_exec: data[4] != 0,
    })
}
