//! R4X-1専用。implement-agent の検査専用入口が使う OS の操作を、安全な関数として置く。
//!
//! - 起動の前の記録(`remember_startup_state`)は、検査専用入口の初期化の表からだけ呼ばれる。
//!   製品の実行ファイルはこの関数を参照しないので、`main` の前に何も走らない。
//! - R4X-1 の OS の呼び出し(`libc` の関数と `unsafe`)は、このファイルの安全な関数の中だけに置く。
//!   例: 信号の扱いと mask・`raise`・fd の種類と書き込み(`fstat`・`fcntl`・`BorrowedFd::borrow_raw`)・
//!   環境の読み取り(`environ`)・利用者の番号とログインのシェル(`getuid`・`geteuid`・`getpwuid`)・
//!   `faccessat`・道具の起動(`fork`・`dup2`・`execve`・`waitpid`・`pipe`)・Mac の `newlocale`。
//!   各所に安全の理由を書く。
#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::CString;
use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};

/// 信号の扱い。exec で受け継げるのは既定と無視だけ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Default,
    Ignore,
    Handler,
}

impl Disposition {
    fn from_raw(value: libc::sighandler_t) -> Self {
        if value == libc::SIG_DFL {
            Self::Default
        } else if value == libc::SIG_IGN {
            Self::Ignore
        } else {
            Self::Handler
        }
    }

    fn raw(self) -> libc::sighandler_t {
        match self {
            Self::Ignore => libc::SIG_IGN,
            Self::Default | Self::Handler => libc::SIG_DFL,
        }
    }
}

pub const SIGHUP: i32 = libc::SIGHUP;
pub const SIGINT: i32 = libc::SIGINT;
pub const SIGTERM: i32 = libc::SIGTERM;
pub const SIGPIPE: i32 = libc::SIGPIPE;
pub const SIGXFSZ: i32 = libc::SIGXFSZ;
pub const SIGCHLD: i32 = libc::SIGCHLD;
pub const SIGKILL: i32 = libc::SIGKILL;
pub const SIGSTOP: i32 = libc::SIGSTOP;

/// 旧版の罠の信号(517〜519 行。`trap 'on_signal TERM 143' TERM` など)の順。
pub const TRAP_SIGNALS: [i32; 3] = [libc::SIGTERM, libc::SIGHUP, libc::SIGINT];

/// bash 5.2.21 が EXIT の罠を置いたとき(152 行)に処理を置く終わりの信号のうち、罠の 3 つと、
/// 自分の誤りで届く信号(SIGILL など)を除いたもの(`sig.c` の `terminating_signals`)。
/// 起動時に無視なら置かない。
pub const TERMINATING_SIGNALS: [i32; 8] = [
    libc::SIGPIPE,
    libc::SIGALRM,
    libc::SIGXCPU,
    libc::SIGXFSZ,
    libc::SIGVTALRM,
    libc::SIGPROF,
    libc::SIGUSR1,
    libc::SIGUSR2,
];

/// 呼び出し側が区別するエラー番号。
pub mod errno {
    pub const EINTR: i32 = libc::EINTR;
    pub const ENOENT: i32 = libc::ENOENT;
    pub const EACCES: i32 = libc::EACCES;
    pub const ENOTDIR: i32 = libc::ENOTDIR;
    pub const EIO: i32 = libc::EIO;
    pub const EPIPE: i32 = libc::EPIPE;
    pub const ENOSPC: i32 = libc::ENOSPC;
    pub const EFBIG: i32 = libc::EFBIG;
    pub const EBADF: i32 = libc::EBADF;
    pub const ENOEXEC: i32 = libc::ENOEXEC;
    pub const ENAMETOOLONG: i32 = libc::ENAMETOOLONG;
    pub const EINVAL: i32 = libc::EINVAL;
}

// ---- 起動の前の記録 ----

static RECORDED: AtomicBool = AtomicBool::new(false);
/// 起動時に無視されていた信号(ビット 1 << 番号。1〜31)。
static IGNORED_AT_START: AtomicU32 = AtomicU32::new(0);
/// 起動時の止め(ビット 1 << 番号。1〜63)。
static BLOCKED_AT_START: AtomicU64 = AtomicU64::new(0);

/// 引き継いだ信号の扱いと止めを記録する。Rust の実行時は `main` の前に `SIGPIPE` を無視にするので、
/// 検査専用入口の初期化の表(Linux は `.init_array`、Mac は `__DATA,__mod_init_func`)からだけ呼ぶ。
/// 確保もプロセス全体の変更もしない。
pub extern "C" fn remember_startup_state() {
    let mut ignored = 0u32;
    for number in 1..32 {
        if number == libc::SIGKILL || number == libc::SIGSTOP {
            continue;
        }
        // SAFETY: 受け取り先を 0 で埋め、今の扱いを読むだけ(新しい扱いは渡さない)。
        let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: 新しい設定を渡さず、古い設定だけを受け取る。
        if unsafe { libc::sigaction(number, std::ptr::null(), &mut old) } == 0
            && old.sa_sigaction == libc::SIG_IGN
        {
            ignored |= 1 << number;
        }
    }
    IGNORED_AT_START.store(ignored, Ordering::SeqCst);
    BLOCKED_AT_START.store(current_mask(), Ordering::SeqCst);
    RECORDED.store(true, Ordering::SeqCst);
}

fn current_mask() -> u64 {
    // SAFETY: sigset_t を 0 で埋め、今の mask だけを受け取る。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        if libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut set) != 0 {
            return 0;
        }
        let mut bits = 0u64;
        for number in 1..64 {
            if libc::sigismember(&set, number) == 1 {
                bits |= 1 << number;
            }
        }
        bits
    }
}

/// 記録した、起動時の扱い(記録が無ければ `None`。製品の実行ファイルなど)。
pub fn inherited_disposition(number: i32) -> Option<Disposition> {
    if !RECORDED.load(Ordering::SeqCst) || !(1..32).contains(&number) {
        return None;
    }
    if IGNORED_AT_START.load(Ordering::SeqCst) & (1 << number) != 0 {
        Some(Disposition::Ignore)
    } else {
        Some(Disposition::Default)
    }
}

/// 記録した起動時の止め(記録が無ければ今の止め)。
pub fn blocked_at_start() -> u64 {
    if RECORDED.load(Ordering::SeqCst) {
        BLOCKED_AT_START.load(Ordering::SeqCst)
    } else {
        current_mask()
    }
}

// ---- 信号 ----

fn set_action(number: libc::c_int, handler: libc::sighandler_t) -> io::Result<()> {
    // SAFETY: sigaction を 0 で埋めてから必要な欄だけ設定する。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = handler;
    // bash 5.2.21 の sig.c の set_signal_handler と同じく、SA_RESTART を付けない(flags 0)。
    action.sa_flags = 0;
    // SAFETY: `action.sa_mask` は上で確保した有効な sigset_t。
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 有効な sigaction を渡し、古い設定は受け取らない。
    if unsafe { libc::sigaction(number, &action, std::ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 今の扱い。
pub fn disposition(number: i32) -> io::Result<Disposition> {
    // SAFETY: 受け取り先を 0 で埋め、今の扱いを読むだけ。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい設定を渡さず、古い設定だけを受け取る。
    if unsafe { libc::sigaction(number, std::ptr::null(), &mut old) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Disposition::from_raw(old.sa_sigaction))
}

/// 扱いを既定か無視にする(`Handler` は既定として扱う)。
pub fn set_disposition(number: i32, disposition: Disposition) -> io::Result<()> {
    set_action(number, disposition.raw())
}

/// 受けた信号の印(ビット 1 << 番号)。
static PENDING: AtomicU32 = AtomicU32::new(0);

extern "C" fn record_signal(number: libc::c_int) {
    if (0..32).contains(&number) {
        PENDING.fetch_or(1u32 << number, Ordering::SeqCst);
    }
}

/// 渡した信号に「受けたことを記録するだけ」の処理を設定する。
pub fn install_recorder(number: i32) -> io::Result<()> {
    let handler: extern "C" fn(libc::c_int) = record_signal;
    set_action(number, handler as libc::sighandler_t)
}

/// 受けた信号の印。
pub fn pending() -> u32 {
    PENDING.load(Ordering::SeqCst)
}

/// 渡した信号の記録だけを消す。
pub fn clear_pending(number: i32) {
    if (0..32).contains(&number) {
        PENDING.fetch_and(!(1u32 << number), Ordering::SeqCst);
    }
}

/// 記録を全部消す。
pub fn clear_all_pending() {
    PENDING.store(0, Ordering::SeqCst);
}

/// 検査で、信号を受けたことにする(単体テストの差し込み点から使う)。
pub fn simulate_signal(number: i32) {
    record_signal(number);
}

/// 既定の扱いで自分へ信号を送って終わる。止めていれば外す。信号で終わらなかったときだけ戻る。
pub fn die_by_signal(number: i32) {
    let _ = set_action(number, libc::SIG_DFL);
    // SAFETY: sigset_t を 0 で埋め、その番号だけを入れて止めを外す。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        if libc::sigemptyset(&mut set) == 0 && libc::sigaddset(&mut set, number) == 0 {
            libc::pthread_sigmask(libc::SIG_UNBLOCK, &set, std::ptr::null_mut());
        }
        libc::raise(number);
    }
}

/// 引き継いだ止めから `SIGCHLD` だけを外す(bash 5.2.21 の `initialize_shell_signals`)。
pub fn unblock_child_signal() -> io::Result<()> {
    // SAFETY: sigset_t を 0 で埋め、SIGCHLD だけを入れて止めを外す。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        if libc::sigemptyset(&mut set) != 0 || libc::sigaddset(&mut set, libc::SIGCHLD) != 0 {
            return Err(io::Error::last_os_error());
        }
        let code = libc::pthread_sigmask(libc::SIG_UNBLOCK, &set, std::ptr::null_mut());
        if code != 0 {
            return Err(io::Error::from_raw_os_error(code));
        }
    }
    Ok(())
}

// ---- fd ----

/// fd の `st_blksize`(glibc の stdio の溜め場所の大きさを決める値)。
pub fn fd_blksize(fd: RawFd) -> Option<i64> {
    // SAFETY: 受け取り先を 0 で埋め、fstat の結果を受ける。
    let mut info: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: 有効な受け取り先を渡す。
    if unsafe { libc::fstat(fd, &mut info) } != 0 {
        return None;
    }
    #[allow(clippy::unnecessary_cast)]
    Some(info.st_blksize as i64)
}

/// 1 回の `write`(`EINTR` も失敗として返す。glibc の stdio は `EINTR` を再試行しない)。
pub fn write_once(fd: BorrowedFd<'_>, data: &[u8]) -> io::Result<usize> {
    rustix::io::write(fd, data).map_err(|error| io::Error::from_raw_os_error(error.raw_os_error()))
}

/// 1 回の `read`(`EINTR` も失敗として返す)。
pub fn read_once(fd: BorrowedFd<'_>, data: &mut [u8]) -> io::Result<usize> {
    rustix::io::read(fd, data).map_err(|error| io::Error::from_raw_os_error(error.raw_os_error()))
}

/// `fd` 番の fd を、使う間だけ借りる。閉じた番号なら `None`。
pub fn borrow_fd(fd: RawFd) -> Option<BorrowedFd<'static>> {
    // SAFETY: 0〜2 番は Rust の実行時が開いたままにする(閉じた 0〜2 は /dev/null で開き直す)。
    // この部品は 0〜2 番を閉じないので、プロセスの終わりまで有効。ほかの番号は呼び出し側が
    // 開いたままにする fd だけを渡す。
    let borrowed = unsafe { BorrowedFd::borrow_raw(fd) };
    rustix::io::fcntl_getfd(borrowed).ok().map(|_| borrowed)
}

/// bash の `BASH_XTRACEFD` の確かめ: 開いていなければ `None`、読み取り専用なら `Some(false)`。
pub fn xtrace_fd(fd: i32) -> Option<bool> {
    // SAFETY: fcntl は整数だけを使う。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return None;
    }
    Some(flags & libc::O_ACCMODE != libc::O_RDONLY)
}

// ---- 利用者と環境 ----

/// 実の uid の利用者のログインシェル(`getpwuid`)。bash が PATH で自分を見つけられないときの `BASH`。
pub fn login_shell() -> Option<Vec<u8>> {
    // SAFETY: getpwuid は静的な領域を返す。ほかのスレッドが無い起動時に呼び、すぐ複製する。
    unsafe {
        let entry = libc::getpwuid(libc::getuid());
        if entry.is_null() || (*entry).pw_shell.is_null() {
            return None;
        }
        Some(
            std::ffi::CStr::from_ptr((*entry).pw_shell)
                .to_bytes()
                .to_vec(),
        )
    }
}

/// 実の uid と実効の uid。
pub fn user_ids() -> (u32, u32) {
    // SAFETY: getuid・geteuid は失敗しない。
    unsafe { (libc::getuid(), libc::geteuid()) }
}

/// 自分の pid。
pub fn own_pid() -> i32 {
    // SAFETY: getpid は失敗しない。
    unsafe { libc::getpid() }
}

/// プロセスの環境の項目を、並びのまま生のバイト列で返す(`=` の無い項目も含む)。
pub fn raw_environment() -> Vec<Vec<u8>> {
    let mut entries = Vec::new();
    // SAFETY: 起動直後(ほかのスレッドが環境を書き換えない時)に、NULL で終わる配列を読むだけ。
    unsafe {
        let mut cursor = environ_pointer();
        if cursor.is_null() {
            return entries;
        }
        while !(*cursor).is_null() {
            entries.push(std::ffi::CStr::from_ptr(*cursor).to_bytes().to_vec());
            cursor = cursor.add(1);
        }
    }
    entries
}

#[cfg(target_os = "macos")]
unsafe fn environ_pointer() -> *const *const libc::c_char {
    // SAFETY: _NSGetEnviron は常に有効なポインタを返す。
    unsafe { *libc::_NSGetEnviron() as *const *const libc::c_char }
}

#[cfg(not(target_os = "macos"))]
unsafe fn environ_pointer() -> *const *const libc::c_char {
    unsafe extern "C" {
        static environ: *const *const libc::c_char;
    }
    // SAFETY: libc が持つ環境の配列の先頭を読む。
    unsafe { environ }
}

/// 実効の uid での可否(bash の `test -r`・`-w`・`-x` と同じ `faccessat(AT_EACCESS)`)。
pub fn effective_access(path: &[u8], mode: Access) -> bool {
    let Ok(path) = CString::new(path) else {
        return false;
    };
    let mode = match mode {
        Access::Read => libc::R_OK,
        Access::Write => libc::W_OK,
        Access::Execute => libc::X_OK,
    };
    // SAFETY: NUL で終わるパスを渡す。
    unsafe { libc::faccessat(libc::AT_FDCWD, path.as_ptr(), mode, libc::AT_EACCESS) == 0 }
}

/// `effective_access` の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    Read,
    Write,
    Execute,
}

// ---- 道具の起動 ----

/// 子の stdout の向き。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildOut {
    /// 親の fd 1 を受け継ぐ。
    Inherit,
    /// パイプへ向ける(コマンド置換)。
    Pipe,
}

/// 子の stderr の向き。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildErr {
    /// 親の fd 2 を受け継ぐ。
    Inherit,
    /// `2> /dev/null`(`O_WRONLY|O_CREAT|O_TRUNC`・0666)。
    DevNull,
}

/// 起動の指定(argv・環境はプログラムの外で作る)。
pub struct Spawn<'a> {
    /// execve に渡すパス。
    pub path: &'a [u8],
    pub argv: &'a [Vec<u8>],
    /// `名前=値` の列。
    pub env: &'a [Vec<u8>],
    pub stdout: ChildOut,
    pub stderr: ChildErr,
}

/// 起動した子。
pub struct Child {
    pub pid: i32,
    /// `ChildOut::Pipe` のときの読み口。
    pub stdout: Option<OwnedFd>,
}

/// 子の終わり方。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildStatus {
    Exited(i32),
    /// (番号, core を残したか)。
    Signaled(i32, bool),
}

fn c_string(bytes: &[u8]) -> io::Result<CString> {
    CString::new(bytes).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))
}

/// bash の子の起動(`make_child` と `shell_execve`)。子では、起動時の信号の扱いと止め(`SIGCHLD` を除く)へ
/// 戻してから execve する。execve が失敗したら、`ENOENT` なら 127、ほかは 126 で終わる(bash の
/// `EX_NOTFOUND`・`EX_NOEXEC`)。fork の前に引数を全部 C の文字列にする(fork の後は確保しない)。
pub fn spawn(spec: &Spawn<'_>) -> io::Result<Child> {
    let path = c_string(spec.path)?;
    let argv = spec
        .argv
        .iter()
        .map(|a| c_string(a))
        .collect::<io::Result<Vec<_>>>()?;
    let env = spec
        .env
        .iter()
        .map(|e| c_string(e))
        .collect::<io::Result<Vec<_>>>()?;
    let mut argv_ptr: Vec<*const libc::c_char> = argv.iter().map(|a| a.as_ptr()).collect();
    argv_ptr.push(std::ptr::null());
    let mut env_ptr: Vec<*const libc::c_char> = env.iter().map(|e| e.as_ptr()).collect();
    env_ptr.push(std::ptr::null());
    let dev_null = c"/dev/null";
    // 子へ渡す信号の扱いと止め(記録が無ければ今の値)。
    let mut defaults = [Disposition::Default; 32];
    for (number, slot) in defaults.iter_mut().enumerate() {
        let number = number as i32;
        if number == 0 || number == libc::SIGKILL || number == libc::SIGSTOP {
            continue;
        }
        *slot = inherited_disposition(number)
            .unwrap_or_else(|| disposition(number).unwrap_or(Disposition::Default));
        if *slot == Disposition::Handler {
            *slot = Disposition::Default;
        }
    }
    let mask = blocked_at_start() & !(1u64 << libc::SIGCHLD);
    // SAFETY: sigset_t を 0 で埋め、記録した番号を入れる。
    let child_mask = unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        for number in 1..64 {
            if mask & (1 << number) != 0 {
                libc::sigaddset(&mut set, number);
            }
        }
        set
    };
    let mut pipe_fds = [-1 as libc::c_int; 2];
    if spec.stdout == ChildOut::Pipe {
        // SAFETY: 2 つの fd を受ける配列を渡す。
        if unsafe { libc::pipe(pipe_fds.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    // SAFETY: このプロセスはスレッドを 1 つしか持たない。子は下の非同期信号安全な呼出しだけを行い、
    // 確保をしない(引数は fork の前に用意した)。
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        let error = io::Error::last_os_error();
        if spec.stdout == ChildOut::Pipe {
            // SAFETY: 上で開いた 2 つの fd を閉じる。
            unsafe {
                libc::close(pipe_fds[0]);
                libc::close(pipe_fds[1]);
            }
        }
        return Err(error);
    }
    if pid == 0 {
        // SAFETY: fork の後の子。非同期信号安全な呼出しだけを行い、戻らない。
        unsafe {
            if spec.stdout == ChildOut::Pipe {
                libc::close(pipe_fds[0]);
                if pipe_fds[1] != 1 {
                    libc::dup2(pipe_fds[1], 1);
                    libc::close(pipe_fds[1]);
                }
            }
            if spec.stderr == ChildErr::DevNull {
                let fd = libc::open(
                    dev_null.as_ptr(),
                    libc::O_WRONLY | libc::O_CREAT | libc::O_TRUNC,
                    0o666 as libc::c_uint,
                );
                if fd < 0 {
                    libc::_exit(1);
                }
                if fd != 2 {
                    libc::dup2(fd, 2);
                    libc::close(fd);
                }
            }
            for (number, value) in defaults.iter().enumerate() {
                let number = number as i32;
                if number == 0 || number == libc::SIGKILL || number == libc::SIGSTOP {
                    continue;
                }
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = value.raw();
                libc::sigemptyset(&mut action.sa_mask);
                libc::sigaction(number, &action, std::ptr::null_mut());
            }
            libc::pthread_sigmask(libc::SIG_SETMASK, &child_mask, std::ptr::null_mut());
            libc::execve(path.as_ptr(), argv_ptr.as_ptr(), env_ptr.as_ptr());
            let code = if errno_value() == libc::ENOENT {
                127
            } else {
                126
            };
            libc::_exit(code);
        }
    }
    let stdout = if spec.stdout == ChildOut::Pipe {
        // SAFETY: 書き口は子へ渡したので閉じ、読み口は所有する(上で開いた fd)。
        unsafe {
            libc::close(pipe_fds[1]);
            Some(OwnedFd::from_raw_fd(pipe_fds[0]))
        }
    } else {
        None
    };
    Ok(Child { pid, stdout })
}

#[cfg(target_os = "macos")]
fn errno_value() -> i32 {
    // SAFETY: __error はこのスレッドの errno の場所を返す。
    unsafe { *libc::__error() }
}

#[cfg(not(target_os = "macos"))]
fn errno_value() -> i32 {
    // SAFETY: __errno_location はこのスレッドの errno の場所を返す。
    unsafe { *libc::__errno_location() }
}

/// 1 回の `waitpid(pid, 0)`(`EINTR` はそのまま返す)。
pub fn wait_once(pid: i32) -> io::Result<ChildStatus> {
    let mut status: libc::c_int = 0;
    // SAFETY: 受け取り先を渡す。
    let result = unsafe { libc::waitpid(pid, &mut status, 0) };
    if result < 0 {
        return Err(io::Error::last_os_error());
    }
    if libc::WIFSIGNALED(status) {
        Ok(ChildStatus::Signaled(
            libc::WTERMSIG(status),
            libc::WCOREDUMP(status),
        ))
    } else {
        Ok(ChildStatus::Exited(libc::WEXITSTATUS(status)))
    }
}

/// 読み口を閉じる(所有を放す)。
pub fn close(fd: OwnedFd) {
    drop(fd);
}

/// 所有する fd の番号(表示用)。
pub fn raw(fd: &OwnedFd) -> RawFd {
    fd.as_raw_fd()
}

// ---- 地域設定(Mac) ----

#[cfg(target_os = "macos")]
unsafe extern "C" {
    /// libSystem の `nl_langinfo_l`(`libc` の crate に無いので宣言する)。
    fn nl_langinfo_l(item: libc::nl_item, locale: libc::locale_t) -> *mut libc::c_char;
}

/// Mac の `newlocale(LC_CTYPE_MASK, name)` と `nl_langinfo_l(CODESET)`。入っていなければ `None`。
#[cfg(target_os = "macos")]
pub fn mac_codeset(name: &[u8]) -> Option<Vec<u8>> {
    let name = CString::new(name).ok()?;
    // SAFETY: NUL で終わる名前と null の基を渡す。結果は下で解放する。
    let locale =
        unsafe { libc::newlocale(libc::LC_CTYPE_MASK, name.as_ptr(), std::ptr::null_mut()) };
    if locale.is_null() {
        return None;
    }
    // SAFETY: 有効な locale を渡す。戻り値はその locale の中の文字列で、解放の前に複製する。
    unsafe {
        let text = nl_langinfo_l(libc::CODESET, locale);
        let out = if text.is_null() {
            None
        } else {
            Some(std::ffi::CStr::from_ptr(text).to_bytes().to_vec())
        };
        libc::freelocale(locale);
        out
    }
}

#[cfg(test)]
mod tests {
    //! 信号・fd を変える検査は、このテスト実行物を子として起動し、その中だけで行う。
    use super::*;
    use std::process::Command;

    const CHILD: &str = "R4X1_PLATFORM_CHILD";

    fn in_child(name: &str) -> bool {
        if std::env::var(CHILD).as_deref() == Ok(name) {
            return true;
        }
        let binary = std::env::current_exe().expect("test binary");
        let filter = format!("implement_agent::tests::{name}");
        let output = Command::new(&binary)
            .env(CHILD, name)
            .args([
                filter.as_str(),
                "--exact",
                "--nocapture",
                "--test-threads=1",
            ])
            .output()
            .expect("run child test");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            output.status.success() && stdout.contains("CHILD-OK"),
            "{name}: {stdout}\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
        false
    }

    #[test]
    fn recorder_marks_and_clears() {
        if !in_child("recorder_marks_and_clears") {
            return;
        }
        clear_all_pending();
        for number in TRAP_SIGNALS {
            install_recorder(number).unwrap();
        }
        // SAFETY: 自分へ信号を送る(処理は記録するだけ)。
        unsafe { libc::raise(libc::SIGHUP) };
        assert_eq!(pending(), 1 << SIGHUP);
        clear_pending(SIGHUP);
        assert_eq!(pending(), 0);
        simulate_signal(SIGINT);
        assert_eq!(pending(), 1 << SIGINT);
        clear_all_pending();
        println!("CHILD-OK");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn spawned_child_gets_start_dispositions_and_exit_codes() {
        if !in_child("spawned_child_gets_start_dispositions_and_exit_codes") {
            return;
        }
        // 起動時の状態: SIGPIPE は既定、SIGUSR1 は止めていない。記録の後に変えても、子は記録した状態で始まる。
        set_disposition(SIGPIPE, Disposition::Default).unwrap();
        remember_startup_state();
        set_disposition(SIGPIPE, Disposition::Ignore).unwrap();
        install_recorder(libc::SIGUSR2).unwrap();
        // SAFETY: この子のテスト実行物の中だけで SIGUSR1 を止める。
        unsafe {
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigaddset(&mut set, libc::SIGUSR1);
            libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
        }
        let argv = vec![
            b"sh".to_vec(),
            b"-c".to_vec(),
            b"grep -E '^Sig(Ign|Blk|Cgt)' /proc/$$/status; exit 7".to_vec(),
        ];
        let child = spawn(&Spawn {
            path: b"/bin/sh",
            argv: &argv,
            env: &[b"PATH=/usr/bin:/bin".to_vec()],
            stdout: ChildOut::Pipe,
            stderr: ChildErr::DevNull,
        })
        .unwrap();
        let mut out = Vec::new();
        let read = child.stdout.as_ref().unwrap();
        let mut buffer = [0u8; 256];
        loop {
            match read_once(read.as_fd_borrowed(), &mut buffer) {
                Ok(0) => break,
                Ok(n) => out.extend_from_slice(&buffer[..n]),
                Err(error) if error.raw_os_error() == Some(errno::EINTR) => continue,
                Err(error) => panic!("{error}"),
            }
        }
        assert_eq!(wait_once(child.pid).unwrap(), ChildStatus::Exited(7));
        let text = String::from_utf8_lossy(&out).into_owned();
        let field = |name: &str| -> u64 {
            let line = text
                .lines()
                .find(|l| l.starts_with(name))
                .unwrap_or_else(|| panic!("{name}: {text}"));
            u64::from_str_radix(line.split_whitespace().nth(1).unwrap(), 16).unwrap()
        };
        assert_eq!(field("SigIgn:") & (1 << (SIGPIPE - 1)), 0, "{text}");
        assert_eq!(field("SigBlk:") & (1 << (libc::SIGUSR1 - 1)), 0, "{text}");
        let missing = spawn(&Spawn {
            path: b"/nonexistent/tool",
            argv: &argv,
            env: &[],
            stdout: ChildOut::Inherit,
            stderr: ChildErr::DevNull,
        })
        .unwrap();
        assert_eq!(wait_once(missing.pid).unwrap(), ChildStatus::Exited(127));
        println!("CHILD-OK");
    }

    #[cfg(target_os = "linux")]
    trait AsFdBorrowed {
        fn as_fd_borrowed(&self) -> BorrowedFd<'_>;
    }

    #[cfg(target_os = "linux")]
    impl AsFdBorrowed for OwnedFd {
        fn as_fd_borrowed(&self) -> BorrowedFd<'_> {
            use std::os::fd::AsFd;
            self.as_fd()
        }
    }
}
