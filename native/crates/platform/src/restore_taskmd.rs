//! R4R専用。タスク本文の復元が使う OS の操作を、安全な関数として置く。
//!
//! - 各関数は 1 回だけシステムコールを呼ぶ。`EINTR` を再試行するかどうかは、旧 helper が
//!   呼ぶ Python の関数ごとに違うので、呼び出し側(cli の `restore_taskmd`)が決める。
//! - `unsafe` は、このファイルの信号・mask・`RLIMIT_FSIZE`・乱数・子の中の fd の処理・
//!   環境の書き換えだけに置く。各所に安全の理由を書く。
#![deny(unsafe_op_in_unsafe_fn)]

use rustix::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use rustix::fs::{self, AtFlags, Mode, OFlags, SeekFrom};
use std::ffi::OsStr;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};

/// `stat` の結果のうち、旧 helper が比べる値だけを損失なく持つ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stat {
    pub dev: u64,
    pub ino: u64,
    pub mode: u32,
    pub size: i64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
    pub blksize: i64,
}

/// 種類のビット(`S_IFMT`)。Linux と Mac で同じ値。
pub const S_IFMT: u32 = 0o170000;
pub const S_IFREG: u32 = 0o100000;
pub const S_IFDIR: u32 = 0o040000;
pub const S_IFLNK: u32 = 0o120000;

// stat の整数型は Linux と Mac で異なる。取得値を損失なく保存する。
#[allow(clippy::unnecessary_cast)]
impl Stat {
    fn from_raw(stat: fs::Stat) -> Self {
        Self {
            dev: stat.st_dev as u64,
            ino: stat.st_ino as u64,
            mode: stat.st_mode as u32,
            size: stat.st_size as i64,
            mtime_ns: stat.st_mtime as i128 * 1_000_000_000 + stat.st_mtime_nsec as i128,
            ctime_ns: stat.st_ctime as i128 * 1_000_000_000 + stat.st_ctime_nsec as i128,
            blksize: stat.st_blksize as i64,
        }
    }
}

/// 呼び出し側が区別するエラー番号。
pub mod errno {
    pub const EINTR: i32 = libc::EINTR;
    pub const ENOENT: i32 = libc::ENOENT;
    pub const ECHILD: i32 = libc::ECHILD;
    pub const ESRCH: i32 = libc::ESRCH;
    pub const EBADF: i32 = libc::EBADF;
    pub const EIO: i32 = libc::EIO;
    pub const EPIPE: i32 = libc::EPIPE;
    pub const ENOSPC: i32 = libc::ENOSPC;
    pub const EFBIG: i32 = libc::EFBIG;
    pub const EDQUOT: i32 = libc::EDQUOT;
    pub const EACCES: i32 = libc::EACCES;
    pub const ENOSYS: i32 = libc::ENOSYS;
    pub const EPERM: i32 = libc::EPERM;
    pub const ENOEXEC: i32 = libc::ENOEXEC;
}

fn errno(error: rustix::io::Errno) -> io::Error {
    io::Error::from_raw_os_error(error.raw_os_error())
}

/// `os.open('/', O_RDONLY | O_DIRECTORY | O_CLOEXEC)`。
pub fn open_root() -> io::Result<OwnedFd> {
    fs::open(
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 保持した親から、リンクを辿らずにフォルダを開く(`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`)。
pub fn open_directory_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 退避本文を開く(`O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`)。
pub fn open_read_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 排他作成(`O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`、mode 0600)。
pub fn create_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
    )
    .map_err(errno)
}

/// リンクを辿らない `fstatat`。
pub fn stat_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<Stat> {
    fs::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW)
        .map(Stat::from_raw)
        .map_err(errno)
}

/// リンクを辿る `stat`。
pub fn stat_path(path: &[u8]) -> io::Result<Stat> {
    fs::stat(path).map(Stat::from_raw).map_err(errno)
}

/// `lstat`。
pub fn lstat_path(path: &[u8]) -> io::Result<Stat> {
    fs::lstat(path).map(Stat::from_raw).map_err(errno)
}

/// `readlink`。
pub fn read_link(path: &[u8]) -> io::Result<Vec<u8>> {
    fs::readlink(path, Vec::new())
        .map(|value| value.into_bytes())
        .map_err(errno)
}

/// `access(path, X_OK)`。実の利用者の権限で確かめる。
pub fn access_executable(path: &[u8]) -> io::Result<()> {
    fs::access(path, fs::Access::EXEC_OK).map_err(errno)
}

pub fn mkdir_at(dir: BorrowedFd<'_>, name: &[u8], mode: u32) -> io::Result<()> {
    fs::mkdirat(dir, name, Mode::from_raw_mode(mode as fs::RawMode)).map_err(errno)
}

pub fn unlink_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<()> {
    fs::unlinkat(dir, name, AtFlags::empty()).map_err(errno)
}

pub fn rmdir_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<()> {
    fs::unlinkat(dir, name, AtFlags::REMOVEDIR).map_err(errno)
}

pub fn rename_at(
    old_dir: BorrowedFd<'_>,
    old: &[u8],
    new_dir: BorrowedFd<'_>,
    new: &[u8],
) -> io::Result<()> {
    fs::renameat(old_dir, old, new_dir, new).map_err(errno)
}

pub fn fstat(fd: BorrowedFd<'_>) -> io::Result<Stat> {
    fs::fstat(fd).map(Stat::from_raw).map_err(errno)
}

pub fn fchmod(fd: BorrowedFd<'_>, mode: u32) -> io::Result<()> {
    fs::fchmod(fd, Mode::from_raw_mode(mode as fs::RawMode)).map_err(errno)
}

pub fn ftruncate(fd: BorrowedFd<'_>, length: u64) -> io::Result<()> {
    fs::ftruncate(fd, length).map_err(errno)
}

/// `lseek(fd, 0, SEEK_SET)`。
pub fn seek_start(fd: BorrowedFd<'_>) -> io::Result<u64> {
    fs::seek(fd, SeekFrom::Start(0)).map_err(errno)
}

pub fn read(fd: BorrowedFd<'_>, buffer: &mut [u8]) -> io::Result<usize> {
    rustix::io::read(fd, buffer).map_err(errno)
}

pub fn write(fd: BorrowedFd<'_>, buffer: &[u8]) -> io::Result<usize> {
    rustix::io::write(fd, buffer).map_err(errno)
}

/// 失敗を返す `close`。`EINTR` でも再試行しない(旧 helper の `os.close` と同じ)。
pub fn close(fd: OwnedFd) -> io::Result<()> {
    let raw = std::os::fd::IntoRawFd::into_raw_fd(fd);
    // SAFETY: `raw` は所有していた fd で、ここで手放した。以後この番号を使わない。
    if unsafe { libc::close(raw) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// `/dev/null` を `O_RDWR|O_CLOEXEC` で 1 回だけ開く(旧 helper の `Popen` の `DEVNULL` と同じ)。
pub fn open_dev_null() -> io::Result<OwnedFd> {
    fs::open("/dev/null", OFlags::RDWR | OFlags::CLOEXEC, Mode::empty()).map_err(errno)
}

/// 読み口と書き口の順に、`O_CLOEXEC` の付いたパイプを作る。
pub fn pipe() -> io::Result<(OwnedFd, OwnedFd)> {
    let (reader, writer) = std::io::pipe()?;
    Ok((OwnedFd::from(reader), OwnedFd::from(writer)))
}

/// 読み口に読めるもの(終わりを含む)があるかを、最大 `timeout` だけ待つ(1 回)。
pub fn wait_readable(fd: BorrowedFd<'_>, timeout: std::time::Duration) -> io::Result<bool> {
    use rustix::event::{PollFd, PollFlags, Timespec, poll};
    let mut fds = [PollFd::new(&fd, PollFlags::IN)];
    let timeout = Timespec {
        tv_sec: timeout.as_secs() as _,
        tv_nsec: timeout.subsec_nanos() as _,
    };
    let ready = poll(&mut fds, Some(&timeout)).map_err(errno)?;
    if ready == 0 {
        return Ok(false);
    }
    // select の「読める」は、終わり(HUP)と異常(ERR)も含む。
    Ok(fds[0]
        .revents()
        .intersects(PollFlags::IN | PollFlags::HUP | PollFlags::ERR | PollFlags::NVAL))
}

// ---- 信号 ----

static RECORDED: AtomicI32 = AtomicI32::new(0);

/// 受けた信号を記録するだけの処理。最初の 1 つだけを残す。
extern "C" fn record_signal(number: libc::c_int) {
    let _ = RECORDED.compare_exchange(0, number, Ordering::SeqCst, Ordering::SeqCst);
}

/// 旧 helper の `SIGNALS` と同じ順(TERM・INT・HUP)。
pub const RECORDED_SIGNALS: [libc::c_int; 3] = [libc::SIGTERM, libc::SIGINT, libc::SIGHUP];

fn set_disposition(number: libc::c_int, handler: libc::sighandler_t) -> io::Result<()> {
    // SAFETY: sigaction を 0 で埋めてから必要な欄だけ設定する。`sa_mask` は空にする。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = handler;
    // CPython 3.10 の PyOS_setsig と同じく SA_ONSTACK だけ。SA_RESTART は付けない。
    action.sa_flags = libc::SA_ONSTACK;
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

/// `SIGXFSZ` を無視にする(Python の起動時と同じ)。部品の開始時に呼ぶ。
pub fn ignore_file_size_signal() -> io::Result<()> {
    set_disposition(libc::SIGXFSZ, libc::SIG_IGN)
}

/// 記録を消してから、TERM・INT・HUP に「最初の信号を記録するだけ」の処理を設定する。
pub fn install_recording_handlers() -> io::Result<()> {
    RECORDED.store(0, Ordering::SeqCst);
    let handler: extern "C" fn(libc::c_int) = record_signal;
    for number in RECORDED_SIGNALS {
        set_disposition(number, handler as libc::sighandler_t)?;
    }
    Ok(())
}

/// 記録した最初の信号。
pub fn recorded_signal() -> Option<i32> {
    match RECORDED.load(Ordering::SeqCst) {
        0 => None,
        number => Some(number),
    }
}

/// TERM・INT・HUP を既定(`SIG_DFL`)へ戻す。CPython の `_PySignal_Fini` と同じく、番号の小さい順
/// (HUP・INT・TERM)に戻し、失敗しても残りを戻す。
pub fn reset_recording_handlers() -> io::Result<()> {
    let mut result = Ok(());
    for number in [libc::SIGHUP, libc::SIGINT, libc::SIGTERM] {
        if let Err(error) = set_disposition(number, libc::SIG_DFL)
            && result.is_ok()
        {
            result = Err(error);
        }
    }
    result
}

/// 止める前の信号の mask。
#[derive(Clone, Copy)]
pub struct SignalMask(libc::sigset_t);

fn recorded_set() -> io::Result<libc::sigset_t> {
    // SAFETY: sigset_t を 0 で埋めてから sigemptyset で初期化する。
    let mut set: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: `set` は有効な sigset_t。
    if unsafe { libc::sigemptyset(&mut set) } != 0 {
        return Err(io::Error::last_os_error());
    }
    for number in RECORDED_SIGNALS {
        // SAFETY: `set` は初期化済みの sigset_t で、番号は有効な信号。
        if unsafe { libc::sigaddset(&mut set, number) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(set)
}

/// TERM・INT・HUP を止め、止める前の mask を返す(`pthread_sigmask(SIG_BLOCK, …)`)。
pub fn block_recorded_signals() -> io::Result<SignalMask> {
    let set = recorded_set()?;
    // SAFETY: sigset_t を 0 で埋めて、古い mask の受け取り先にする。
    let mut old: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: 有効な sigset_t を渡す。
    let code = unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut old) };
    if code != 0 {
        return Err(io::Error::from_raw_os_error(code));
    }
    Ok(SignalMask(old))
}

/// mask を戻す(`pthread_sigmask(SIG_SETMASK, …)`)。
pub fn restore_signal_mask(mask: &SignalMask) -> io::Result<()> {
    // SAFETY: `mask.0` は pthread_sigmask が返した有効な sigset_t。
    let code = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &mask.0, std::ptr::null_mut()) };
    if code != 0 {
        return Err(io::Error::from_raw_os_error(code));
    }
    Ok(())
}

fn current_mask() -> io::Result<libc::sigset_t> {
    // SAFETY: sigset_t を 0 で埋めて、今の mask の受け取り先にする。
    let mut current: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: 変更する集合を渡さず、今の mask だけを受け取る。
    let code = unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut current) };
    if code != 0 {
        return Err(io::Error::from_raw_os_error(code));
    }
    Ok(current)
}

/// 子へ信号を送る(`kill`。1 回)。
pub fn send_signal(pid: i32, number: i32) -> io::Result<()> {
    // SAFETY: kill は引数の整数だけを使う。
    if unsafe { libc::kill(pid, number) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// 呼んだスレッドだけへ信号を送る(`pthread_kill`)。止めた mask の確かめに使う。
pub fn signal_current_thread(number: i32) -> io::Result<()> {
    // SAFETY: 自分のスレッドへの送信。番号は呼び出し側が渡す信号番号。
    let code = unsafe { libc::pthread_kill(libc::pthread_self(), number) };
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code))
    }
}

pub const SIGTERM: i32 = libc::SIGTERM;
pub const SIGINT: i32 = libc::SIGINT;
pub const SIGHUP: i32 = libc::SIGHUP;
pub const SIGKILL: i32 = libc::SIGKILL;

// ---- 子の状態 ----

/// 子の状態。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WaitStatus {
    /// まだ動いている(`WNOHANG` で 0)。
    Running,
    /// 終わった。値は Python の `waitstatus_to_exitcode` と同じ(信号なら負の番号)。
    Exited(i32),
}

fn exit_code(status: libc::c_int) -> i32 {
    if libc::WIFEXITED(status) {
        libc::WEXITSTATUS(status)
    } else if libc::WIFSIGNALED(status) {
        -libc::WTERMSIG(status)
    } else {
        // 止まった子は WUNTRACED を付けないので返らない。
        status
    }
}

fn wait_once(pid: i32, flags: libc::c_int) -> io::Result<WaitStatus> {
    let mut status: libc::c_int = 0;
    // SAFETY: `status` は有効な受け取り先。
    let got = unsafe { libc::waitpid(pid, &mut status, flags) };
    if got < 0 {
        return Err(io::Error::last_os_error());
    }
    if got == 0 {
        return Ok(WaitStatus::Running);
    }
    Ok(WaitStatus::Exited(exit_code(status)))
}

/// `waitpid(pid, WNOHANG)`(1 回)。
pub fn wait_nohang(pid: i32) -> io::Result<WaitStatus> {
    wait_once(pid, libc::WNOHANG)
}

/// `waitpid(pid, 0)`(1 回)。
pub fn wait_blocking(pid: i32) -> io::Result<WaitStatus> {
    wait_once(pid, 0)
}

// ---- 乱数 ----

/// OS の乱数を最大 `buffer.len()` バイト読む(1 回)。Linux は `getrandom`(flags 0)、Mac は
/// `getentropy`(1 回に 256 バイトまで)。CPython 3.10.12 の `py_getrandom`・`py_getentropy` と同じ呼び方。
pub fn random_bytes(buffer: &mut [u8]) -> io::Result<usize> {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: `buffer` は書き込める領域で、長さを渡す。flags は 0(Python の os.urandom と同じ)。
        let got = unsafe {
            libc::syscall(
                libc::SYS_getrandom,
                buffer.as_mut_ptr(),
                buffer.len(),
                0 as libc::c_uint,
            )
        };
        if got < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(got as usize)
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        let length = buffer.len().min(256);
        // SAFETY: `buffer` の先頭から 256 バイト以下を書かせる。
        if unsafe { libc::getentropy(buffer.as_mut_ptr().cast(), length) } == 0 {
            Ok(length)
        } else {
            Err(io::Error::last_os_error())
        }
    }
}

/// CPython の `dev_urandom`(raise あり)と同じく、開いたまま保持する `/dev/urandom`。
static URANDOM: std::sync::Mutex<Option<(OwnedFd, u64, u64)>> = std::sync::Mutex::new(None);

/// `getrandom`・`getentropy` が ENOSYS・EPERM のときの切り替え先(CPython の `dev_urandom`)。
/// 開いた fd は閉じずに保持し、次の呼び出しで同じ実体か(device・inode)を確かめて使い回す。
/// 違えば、閉じずに忘れて開き直す。open・read の EINTR は再試行する。
pub fn dev_urandom(buffer: &mut [u8]) -> io::Result<()> {
    let mut cache = URANDOM
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let stale = match cache.as_ref() {
        Some((fd, dev, ino)) => {
            fstat(fd.as_fd()).map_or(true, |info| info.dev != *dev || info.ino != *ino)
        }
        None => false,
    };
    if stale && let Some((fd, _, _)) = cache.take() {
        // 閉じない(ほかの用途で使われている番号かもしれない。CPython と同じ)。
        let _ = std::os::fd::IntoRawFd::into_raw_fd(fd);
    }
    if cache.is_none() {
        let fd = loop {
            match fs::open(
                "/dev/urandom",
                OFlags::RDONLY | OFlags::CLOEXEC,
                Mode::empty(),
            ) {
                Err(error) if error == rustix::io::Errno::INTR => continue,
                other => break other.map_err(errno)?,
            }
        };
        let info = fstat(fd.as_fd())?;
        *cache = Some((fd, info.dev, info.ino));
    }
    let Some((fd, _, _)) = cache.as_ref() else {
        return Err(io::Error::from_raw_os_error(libc::EBADF));
    };
    let mut filled = 0usize;
    while filled < buffer.len() {
        match read(fd.as_fd(), buffer.get_mut(filled..).unwrap_or_default()) {
            Ok(0) => return Err(io::Error::other("Failed to read bytes from /dev/urandom")),
            Ok(count) => filled += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

// ---- 環境 ----

/// 子へ渡す `LC_CTYPE` の値(Linux。旧 helper の Python が PEP 538 で足す値)。
#[cfg(target_os = "linux")]
pub const COERCED_LC_CTYPE: &str = "C.UTF-8";

/// 旧 helper(Python 3.10)の PEP 538 と同じく、プロセスの環境の `LC_CTYPE` を書き換える。
/// 書き換えたら true。
///
/// - Linux(musl で組むので glibc の地域設定を知れない): `LC_ALL` が空で、`LC_CTYPE`(空なら
///   `LANG`)が空・`C`・`POSIX` なら `C.UTF-8` にする。
/// - Mac(旧 Python と同じ libc): CPython 3.10.12 の `_Py_LegacyLocaleDetected` と同じく、
///   `setlocale(LC_CTYPE, "")` の後の `setlocale(LC_CTYPE, NULL)` が `C` と等しいときだけ書き換える
///   (`POSIX` は含めない。Mac は `POSIX` を `C` の別名にしないことがある)。目標は `_Py_CoerceLegacyLocale`
///   と同じく `C.UTF-8`・`C.utf8`・`UTF-8` を `setlocale` で試し、`nl_langinfo(CODESET)` が空でない最初の
///   名前。プロセスの地域設定は子の環境に影響しないので、判定の後に元へ戻す。
///
/// 既にある `LC_CTYPE` は同じ位置で置き換え、無ければ末尾に足す(libc の `setenv` の動き)。
/// 子は親の環境をそのまま継ぐので、旧 helper と同じ並びで渡る。
///
/// 部品の開始時に 1 回だけ、ほかのスレッドが無い時に呼ぶ。
pub fn coerce_c_locale() -> bool {
    let nonempty = |name: &str| std::env::var_os(name).filter(|value| !value.is_empty());
    if nonempty("LC_ALL").is_some() {
        return false;
    }
    let Some(target) = legacy_locale_target() else {
        return false;
    };
    // SAFETY: edition 2024 では環境の書き換えが unsafe。部品の開始時に 1 回だけ、ほかのスレッドが
    // 環境を読み書きしない時に呼ぶ(呼び出し側の前提)。std の set_var は libc の setenv を
    // std の環境の鍵の中で呼ぶので、既にあれば同じ位置で置き換え、無ければ末尾に足す。
    unsafe { std::env::set_var("LC_CTYPE", target) };
    true
}

#[cfg(target_os = "linux")]
fn legacy_locale_target() -> Option<&'static str> {
    let nonempty = |name: &str| std::env::var_os(name).filter(|value| !value.is_empty());
    let value = nonempty("LC_CTYPE").or_else(|| nonempty("LANG"));
    let legacy = match value.as_deref() {
        None => true,
        Some(value) => value == OsStr::new("C") || value == OsStr::new("POSIX"),
    };
    legacy.then_some(COERCED_LC_CTYPE)
}

#[cfg(not(target_os = "linux"))]
fn legacy_locale_target() -> Option<&'static str> {
    use std::ffi::CStr;
    // SAFETY: 部品の開始時(ほかのスレッドが無い時)だけ呼ぶ。setlocale と nl_langinfo が返す
    // 文字列は、次の setlocale まで有効なので、すぐ複製するか比べ終える。最後に元の地域設定へ戻す。
    unsafe {
        let old = libc::setlocale(libc::LC_CTYPE, std::ptr::null());
        if old.is_null() {
            return None;
        }
        let old = CStr::from_ptr(old).to_owned();
        // _Py_SetLocaleFromEnv(LC_CTYPE) と _Py_LegacyLocaleDetected。
        libc::setlocale(libc::LC_CTYPE, c"".as_ptr());
        let current = libc::setlocale(libc::LC_CTYPE, std::ptr::null());
        let legacy = !current.is_null() && CStr::from_ptr(current) == c"C";
        let mut chosen = None;
        if legacy {
            // _TARGET_LOCALES の順。nl_langinfo(CODESET) が空なら使わない。
            for (name, text) in [
                (c"C.UTF-8", "C.UTF-8"),
                (c"C.utf8", "C.utf8"),
                (c"UTF-8", "UTF-8"),
            ] {
                if !libc::setlocale(libc::LC_CTYPE, name.as_ptr()).is_null() {
                    let codeset = libc::nl_langinfo(libc::CODESET);
                    if !codeset.is_null() && *codeset != 0 {
                        chosen = Some(text);
                        break;
                    }
                    libc::setlocale(libc::LC_CTYPE, c"".as_ptr());
                }
            }
        }
        libc::setlocale(libc::LC_CTYPE, old.as_ptr());
        chosen
    }
}

// ---- 子の起動 ----

/// 子の種類。
pub enum ChildKind<'a> {
    /// `--version` 用。stdout だけを `stdout` のパイプにし、0 番と 2 番は `/dev/null`。
    Version { stdout: BorrowedFd<'a> },
    /// コピー用。0〜2 番は `/dev/null`。`keep` の 2 本を同じ番号で継がせ、
    /// `RLIMIT_FSIZE` を soft・hard とも `file_size` にする。
    Copy {
        keep: [BorrowedFd<'a>; 2],
        file_size: u64,
    },
}

#[cfg(test)]
static FORCE_ENUMERATION: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static FORCE_NO_ENUMERATION: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static EXTRA_ENUMERATED: AtomicI32 = AtomicI32::new(-1);

/// 親で開いている fd を列挙する(Linux は `/proc/self/fd`、Mac は `/dev/fd`)。
/// 列挙に使ったフォルダの fd も入る(子では `EBADF` になり、無視する)。
fn enumerate_open_fds() -> io::Result<Vec<RawFd>> {
    #[cfg(test)]
    if FORCE_NO_ENUMERATION.load(Ordering::SeqCst) {
        return Err(io::Error::from_raw_os_error(libc::ENOENT));
    }
    let root = if cfg!(target_os = "linux") {
        "/proc/self/fd"
    } else {
        "/dev/fd"
    };
    let mut fds = Vec::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        if let Some(fd) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<RawFd>().ok())
            && fd >= 3
        {
            fds.push(fd);
        }
    }
    #[cfg(test)]
    {
        let extra = EXTRA_ENUMERATED.load(Ordering::SeqCst);
        if extra >= 3 {
            fds.push(extra);
        }
    }
    Ok(fds)
}

/// CPython の `safe_get_max_fd`(`sysconf(_SC_OPEN_MAX)`、取れなければ 256)。
fn open_max() -> RawFd {
    // SAFETY: sysconf は整数だけを使う。
    let value = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if value < 0 {
        256
    } else {
        RawFd::try_from(value).unwrap_or(RawFd::MAX)
    }
}

/// 親で用意した execv の argv。子の中では読むだけ。
struct Exec {
    program: std::ffi::CString,
    _args: Vec<std::ffi::CString>,
    pointers: Vec<*const libc::c_char>,
}

// SAFETY: `pointers` は同じ構造体が持つ CString の中身を指し、子の中で execv に渡して読むだけ。
// CString の中身はヒープにあり、構造体が動いても動かない。
unsafe impl Send for Exec {}
// SAFETY: 同上。書き換えない。
unsafe impl Sync for Exec {}

impl Exec {
    fn new(program: &[u8], args: &[&[u8]]) -> io::Result<Self> {
        let invalid = |_| io::Error::from_raw_os_error(libc::EINVAL);
        let program = std::ffi::CString::new(program).map_err(invalid)?;
        let mut owned = vec![program.clone()];
        for arg in args {
            owned.push(std::ffi::CString::new(*arg).map_err(invalid)?);
        }
        let mut pointers: Vec<*const libc::c_char> = owned.iter().map(|arg| arg.as_ptr()).collect();
        pointers.push(std::ptr::null());
        Ok(Self {
            program,
            _args: owned,
            pointers,
        })
    }
}

/// 子の中だけで動く準備。確保・ロック・ログを行わない。
struct ChildSetup {
    devnull: RawFd,
    stdout: RawFd,
    keep: Vec<RawFd>,
    file_size: Option<u64>,
    mask: libc::sigset_t,
    enumerated: Option<Vec<RawFd>>,
    open_max: RawFd,
    use_close_range: bool,
    exec: Exec,
}

fn last_error() -> io::Error {
    io::Error::last_os_error()
}

fn set_cloexec(fd: RawFd, on: bool) -> io::Result<()> {
    // SAFETY: fcntl は整数だけを使う。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(last_error());
    }
    let flags = if on {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    // SAFETY: fcntl は整数だけを使う。
    if unsafe { libc::fcntl(fd, libc::F_SETFD, flags) } < 0 {
        return Err(last_error());
    }
    Ok(())
}

/// 番号の fd に exec の時に閉じる印を付ける。開いていない番号(EBADF)は無視する。
fn mark_cloexec(fd: RawFd) -> io::Result<()> {
    match set_cloexec(fd, true) {
        Err(error) if error.raw_os_error() != Some(libc::EBADF) => Err(error),
        _ => Ok(()),
    }
}

fn redirect(source: RawFd, target: RawFd) -> io::Result<()> {
    if source == target {
        // dup2 が何もしない場合は、継がれるように印を外す(CPython の _posixsubprocess と同じ)。
        return set_cloexec(target, false);
    }
    loop {
        // SAFETY: dup2 は整数だけを使う。
        if unsafe { libc::dup2(source, target) } >= 0 {
            return Ok(());
        }
        let error = last_error();
        if error.raw_os_error() != Some(libc::EINTR) {
            return Err(error);
        }
    }
}

impl ChildSetup {
    /// fork の後、exec の前に子の中で呼ぶ。成功すれば execv から戻らない。
    fn run(&self) -> io::Result<()> {
        // 0〜2 番。CPython と同じく、dup2 の元が 0〜2 と重なる場合は先に別の番号へ逃がす必要が
        // あるが、Rust の実行時は閉じた 0〜2 を /dev/null で開き直すので、元は 3 以上になる。
        redirect(self.devnull, 0)?;
        redirect(self.stdout, 1)?;
        redirect(self.devnull, 2)?;
        // SIGPIPE と SIGXFSZ を既定に戻す(CPython の _Py_RestoreSignals と同じ)。
        for number in [libc::SIGPIPE, libc::SIGXFSZ] {
            // SAFETY: 既定の動作に戻すだけ。
            if unsafe { libc::signal(number, libc::SIG_DFL) } == libc::SIG_ERR {
                return Err(last_error());
            }
        }
        // 信号の mask は、起動の直前の親の mask にする。
        // SAFETY: `self.mask` は親で取得した有効な sigset_t。
        let code =
            unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &self.mask, std::ptr::null_mut()) };
        if code != 0 {
            return Err(io::Error::from_raw_os_error(code));
        }
        if let Some(size) = self.file_size {
            let limit = libc::rlimit {
                rlim_cur: size as libc::rlim_t,
                rlim_max: size as libc::rlim_t,
            };
            // SAFETY: `limit` は有効な rlimit。
            if unsafe { libc::setrlimit(libc::RLIMIT_FSIZE, &limit) } != 0 {
                return Err(last_error());
            }
        }
        // 3 以上の fd を exec の時に閉じる。std が起動の失敗を知らせるパイプを残すため、閉じない。
        // 順は close_range → 親で列挙した fd → 上限までの番号(CPython の _close_open_fds と同じ考え)。
        if !self.close_range() {
            match &self.enumerated {
                Some(fds) => {
                    for &fd in fds {
                        mark_cloexec(fd)?;
                    }
                }
                None => {
                    for fd in 3..=self.open_max {
                        mark_cloexec(fd)?;
                    }
                }
            }
        }
        for &fd in &self.keep {
            set_cloexec(fd, false)?;
        }
        // std の execvp は ENOEXEC で /bin/sh に切り替えるので、旧 helper と同じ execv を自分で呼ぶ。
        // SAFETY: `exec` の argv は親で用意した、NUL で終わる文字列と null で終わる配列。
        unsafe { libc::execv(self.exec.program.as_ptr(), self.exec.pointers.as_ptr()) };
        Err(last_error())
    }

    #[cfg(target_os = "linux")]
    fn close_range(&self) -> bool {
        if !self.use_close_range {
            return false;
        }
        // SAFETY: close_range は整数だけを使う。可変長の引数なので型を c_uint にそろえる。
        let code = unsafe {
            libc::syscall(
                libc::SYS_close_range,
                3 as libc::c_uint,
                u32::MAX as libc::c_uint,
                libc::CLOSE_RANGE_CLOEXEC as libc::c_uint,
            )
        };
        code == 0
    }

    #[cfg(not(target_os = "linux"))]
    fn close_range(&self) -> bool {
        let _ = self.use_close_range;
        false
    }
}

/// 子を起動して pid を返す。argv[0] は `program`。環境・作業フォルダ・プロセスグループは親のまま。
/// 子の準備(fd・信号・mask・`setrlimit`)と exec の失敗は、起動の失敗として返す。
pub fn spawn(
    program: &[u8],
    args: &[&[u8]],
    devnull: BorrowedFd<'_>,
    kind: ChildKind<'_>,
) -> io::Result<i32> {
    let mask = current_mask()?;
    // 列挙できないことだけで起動を失敗にしない(子で close_range が使えれば列挙は要らない)。
    let enumerated = enumerate_open_fds().ok();
    #[cfg(test)]
    let use_close_range = !FORCE_ENUMERATION.load(Ordering::SeqCst);
    #[cfg(not(test))]
    let use_close_range = true;
    let (stdout, keep, file_size) = match kind {
        ChildKind::Version { stdout } => (stdout.as_raw_fd(), Vec::new(), None),
        ChildKind::Copy { keep, file_size } => (
            devnull.as_raw_fd(),
            keep.iter().map(AsRawFd::as_raw_fd).collect(),
            Some(file_size),
        ),
    };
    let setup = ChildSetup {
        devnull: devnull.as_raw_fd(),
        stdout,
        keep,
        file_size,
        mask,
        enumerated,
        open_max: open_max(),
        use_close_range,
        exec: Exec::new(program, args)?,
    };
    let mut command = Command::new(OsStr::from_bytes(program));
    command
        .args(args.iter().map(|arg| OsStr::from_bytes(arg)))
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    // SAFETY: 閉包は fork の後の子の中で動く。親で用意した値を読み、非同期シグナル安全な
    // システムコール(dup2・signal・pthread_sigmask・setrlimit・syscall・fcntl・execv)だけを呼ぶ。
    // 確保・ロック・ログは行わない。
    unsafe {
        command.pre_exec(move || setup.run());
    }
    spawn_quietly(&mut command)
}

/// std の spawn は、子の起動の失敗を受けた後の waitpid が ECHILD(SIGCHLD を無視した起動)だと
/// panic する。旧 helper の subprocess はこの ECHILD を無視して子の失敗を返すので、panic も起動の
/// 失敗として返す。panic の表示は出さない。
fn spawn_quietly(command: &mut Command) -> io::Result<i32> {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(|_| {}));
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| command.spawn()));
    std::panic::set_hook(previous);
    match result {
        Ok(Ok(child)) => Ok(child.id() as i32),
        Ok(Err(error)) => Err(error),
        Err(_) => Err(io::Error::from_raw_os_error(libc::ECHILD)),
    }
}

#[cfg(test)]
mod tests {
    //! プロセス全体の設定(信号・mask・rlimit・環境)を変える検査は、このテスト実行物を
    //! 子として起動し、その中だけで行う(`in_child`)。
    use super::*;

    const CHILD: &str = "R4R_PLATFORM_CHILD";

    /// 子のテスト実行物の中なら true。親なら同じ名前の検査だけを子で流し、成功を確かめる。
    fn in_child(name: &str, env: Option<&[(&str, &str)]>) -> bool {
        if std::env::var(CHILD).as_deref() == Ok(name) {
            return true;
        }
        let binary = std::env::current_exe().expect("test binary");
        let filter = format!("restore_taskmd::tests::{name}");
        let tail = [
            filter.as_str(),
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ];
        let mut command = match env {
            // std の Command::env は環境を並べ替えるので、並びを保つ env(1) で渡す。
            Some(pairs) => {
                let mut command = Command::new("/usr/bin/env");
                command.arg("-i");
                command.args(pairs.iter().map(|(key, value)| format!("{key}={value}")));
                command.arg(format!("{CHILD}={name}"));
                command.arg(&binary);
                command
            }
            None => {
                let mut command = Command::new(&binary);
                command.env(CHILD, name);
                command
            }
        };
        command.args(tail);
        let output = command.output().expect("run child test");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            output.status.success() && stdout.contains("CHILD-OK"),
            "{name}: {stdout}\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
        false
    }

    fn sigaction_of(number: libc::c_int) -> libc::sigaction {
        // SAFETY: 受け取り先を 0 で埋め、現在の設定だけを読む。
        let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: 新しい設定を渡さず、古い設定を受け取る。
        assert_eq!(
            unsafe { libc::sigaction(number, std::ptr::null(), &mut old) },
            0
        );
        old
    }

    fn mask_is_empty(set: &libc::sigset_t) -> bool {
        // SAFETY: `set` は有効な sigset_t。番号は 1〜31 の通常の信号。
        (1..32).all(|n| unsafe { libc::sigismember(set, n) } == 0)
    }

    #[test]
    fn recording_handlers_use_onstack_without_restart_and_empty_mask() {
        if !in_child(
            "recording_handlers_use_onstack_without_restart_and_empty_mask",
            None,
        ) {
            return;
        }
        install_recording_handlers().unwrap();
        for number in RECORDED_SIGNALS {
            let action = sigaction_of(number);
            assert_ne!(action.sa_sigaction, libc::SIG_DFL);
            assert_ne!(action.sa_sigaction, libc::SIG_IGN);
            assert_ne!(action.sa_flags & libc::SA_ONSTACK, 0);
            assert_eq!(action.sa_flags & libc::SA_RESTART, 0);
            assert!(mask_is_empty(&action.sa_mask));
        }
        // 最初の信号だけを記録する。
        assert_eq!(recorded_signal(), None);
        signal_current_thread(SIGTERM).unwrap();
        signal_current_thread(SIGINT).unwrap();
        assert_eq!(recorded_signal(), Some(SIGTERM));
        // 止めた間に届いた信号は、戻した時に記録される。
        install_recording_handlers().unwrap();
        let mask = block_recorded_signals().unwrap();
        signal_current_thread(SIGHUP).unwrap();
        assert_eq!(recorded_signal(), None);
        restore_signal_mask(&mask).unwrap();
        assert_eq!(recorded_signal(), Some(SIGHUP));
        reset_recording_handlers().unwrap();
        for number in RECORDED_SIGNALS {
            assert_eq!(sigaction_of(number).sa_sigaction, libc::SIG_DFL);
        }
        ignore_file_size_signal().unwrap();
        assert_eq!(sigaction_of(libc::SIGXFSZ).sa_sigaction, libc::SIG_IGN);
        println!("CHILD-OK");
    }

    fn ctype_locale() -> Vec<u8> {
        // SAFETY: 今の LC_CTYPE の地域設定の名前を読むだけ。すぐ複製する。
        unsafe {
            let name = libc::setlocale(libc::LC_CTYPE, std::ptr::null());
            assert!(!name.is_null());
            std::ffi::CStr::from_ptr(name).to_bytes().to_vec()
        }
    }

    fn vars() -> Vec<String> {
        std::env::vars_os()
            .map(|(k, v)| format!("{}={}", k.to_string_lossy(), v.to_string_lossy()))
            .filter(|entry| !entry.starts_with(CHILD))
            .collect()
    }

    #[test]
    fn lc_all_set_changes_nothing() {
        if !in_child(
            "lc_all_set_changes_nothing",
            Some(&[("LANG", "C"), ("LC_ALL", "C")]),
        ) {
            return;
        }
        let before = (vars(), ctype_locale());
        assert!(!coerce_c_locale());
        assert_eq!((vars(), ctype_locale()), before);
        println!("CHILD-OK");
    }

    #[test]
    fn legacy_decision_restores_locale_and_appends_at_end() {
        // 値そのもの(Mac で書き換えるか・どの名前か)は、Mac の CI の旧 helper との比較が正本。
        // ここでは Python を起動しないで確かめられる性質だけを見る。
        if !in_child(
            "legacy_decision_restores_locale_and_appends_at_end",
            Some(&[("A", "1"), ("LANG", "C")]),
        ) {
            return;
        }
        let locale = ctype_locale();
        let changed = coerce_c_locale();
        assert_eq!(ctype_locale(), locale, "判定の後に地域設定が元に戻る");
        let after = vars();
        if changed {
            let last = after.last().unwrap();
            assert!(
                ["LC_CTYPE=C.UTF-8", "LC_CTYPE=C.utf8", "LC_CTYPE=UTF-8"].contains(&last.as_str()),
                "{last}"
            );
            assert_eq!(&after[..after.len() - 1], &["A=1", "LANG=C"]);
        } else {
            assert_eq!(after, ["A=1", "LANG=C"]);
        }
        if cfg!(target_os = "linux") {
            assert!(changed);
        }
        println!("CHILD-OK");
    }

    #[test]
    fn spawn_failures_after_child_setup_reach_the_parent() {
        if !in_child("spawn_failures_after_child_setup_reach_the_parent", None) {
            return;
        }
        let devnull = open_dev_null().unwrap();
        // exec の失敗(fd の処理の後)。
        let directory = std::env::temp_dir().join(format!("r4r-platform-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let plain = directory.join("not-executable");
        std::fs::write(&plain, b"#!/bin/sh\n").unwrap();
        let input = std::fs::File::open(&plain).unwrap();
        let output = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&plain)
            .unwrap();
        let keep = [input.as_fd(), output.as_fd()];
        let error = spawn(
            plain.as_os_str().as_bytes(),
            &[],
            devnull.as_fd(),
            ChildKind::Copy { keep, file_size: 9 },
        )
        .unwrap_err();
        assert_eq!(error.raw_os_error(), Some(libc::EACCES));
        // シバンの無い実行可能ファイルは ENOEXEC(execvp のように /bin/sh へ切り替えない)。
        let bare = directory.join("bare-script");
        std::fs::write(&bare, b"exit 0\n").unwrap();
        std::fs::set_permissions(&bare, std::os::unix::fs::PermissionsExt::from_mode(0o755))
            .unwrap();
        let error = spawn(
            bare.as_os_str().as_bytes(),
            &[b"--version"],
            devnull.as_fd(),
            ChildKind::Version {
                stdout: devnull.as_fd(),
            },
        )
        .unwrap_err();
        assert_eq!(error.raw_os_error(), Some(libc::ENOEXEC));
        // SIGCHLD を無視した起動でも、起動の失敗は panic せずに失敗として返る。
        // SAFETY: この子のテスト実行物の中だけの設定。
        unsafe { libc::signal(libc::SIGCHLD, libc::SIG_IGN) };
        assert!(
            spawn(
                bare.as_os_str().as_bytes(),
                &[],
                devnull.as_fd(),
                ChildKind::Copy { keep, file_size: 9 },
            )
            .is_err()
        );
        // SAFETY: 既定へ戻す。
        unsafe { libc::signal(libc::SIGCHLD, libc::SIG_DFL) };
        // setrlimit の失敗(ハードの上限が N 未満)。この子の中だけで上限を下げる。
        let limit = libc::rlimit {
            rlim_cur: 100,
            rlim_max: 100,
        };
        // SAFETY: 有効な rlimit を渡す。この子のテスト実行物の中だけの変更。
        assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_FSIZE, &limit) }, 0);
        let error = spawn(
            b"/bin/sh",
            &[b"-c", b"exit 0"],
            devnull.as_fd(),
            ChildKind::Copy {
                keep,
                file_size: 1000,
            },
        )
        .unwrap_err();
        assert!(error.raw_os_error().is_some());
        std::fs::remove_dir_all(&directory).unwrap();
        println!("CHILD-OK");
    }

    #[cfg(target_os = "linux")]
    mod linux {
        use super::*;
        use std::collections::BTreeMap;

        fn stopped(kind: ChildKind<'_>) -> i32 {
            let devnull = open_dev_null().unwrap();
            let pid = spawn(
                b"/bin/sh",
                &[b"-c", b"kill -STOP $$"],
                devnull.as_fd(),
                kind,
            )
            .unwrap();
            let mut status = 0;
            // SAFETY: 起動した子を止まるまで待つ。
            assert_eq!(
                unsafe { libc::waitpid(pid, &mut status, libc::WUNTRACED) },
                pid
            );
            assert!(libc::WIFSTOPPED(status));
            pid
        }

        fn finish(pid: i32) {
            send_signal(pid, SIGKILL).unwrap();
            let mut status = 0;
            // SAFETY: 子を回収する。
            assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
        }

        /// /proc/<pid>/fd の番号 → (行き先, fdinfo の flags)。
        fn fds(pid: i32) -> BTreeMap<i32, (String, u32)> {
            let mut result = BTreeMap::new();
            for entry in std::fs::read_dir(format!("/proc/{pid}/fd")).unwrap() {
                let entry = entry.unwrap();
                let fd: i32 = entry.file_name().to_str().unwrap().parse().unwrap();
                let target = std::fs::read_link(entry.path()).unwrap();
                let info = std::fs::read_to_string(format!("/proc/{pid}/fdinfo/{fd}")).unwrap();
                let flags = info
                    .lines()
                    .find_map(|line| line.strip_prefix("flags:"))
                    .map(|value| u32::from_str_radix(value.trim(), 8).unwrap())
                    .unwrap();
                result.insert(fd, (target.to_string_lossy().into_owned(), flags));
            }
            result
        }

        fn status(pid: i32, name: &str) -> u64 {
            let text = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
            let value = text
                .lines()
                .find_map(|line| line.strip_prefix(&format!("{name}:")))
                .unwrap();
            u64::from_str_radix(value.trim(), 16).unwrap()
        }

        fn bit(number: libc::c_int) -> u64 {
            1 << (number - 1)
        }

        fn check_copy_child(force_enumeration: bool, no_enumeration: bool) {
            FORCE_ENUMERATION.store(force_enumeration, Ordering::SeqCst);
            // 列挙できないとき(/proc が無いなど)は、上限までの番号に印を付ける。
            FORCE_NO_ENUMERATION.store(no_enumeration, Ordering::SeqCst);
            // 列挙の後に閉じた番号(子では EBADF)も入れる。
            EXTRA_ENUMERATED.store(1500, Ordering::SeqCst);
            let directory =
                std::env::temp_dir().join(format!("r4r-platform-copy-{}", std::process::id()));
            std::fs::create_dir_all(&directory).unwrap();
            let path = directory.join("file");
            std::fs::write(&path, b"x").unwrap();
            let input = std::fs::File::open(&path).unwrap();
            let output = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
                .unwrap();
            // 継承可能な余分な fd と、RLIMIT_NOFILE より大きい番号の fd。
            // SAFETY: 既存の fd の複製。番号は検査の中だけで使い、最後に閉じる。
            let extra = unsafe { libc::dup(input.as_raw_fd()) };
            assert!(extra >= 3);
            // soft の上限より大きい番号の fd(列挙できないときは CPython と同じく上限までしか見ないので、
            // その場合は上限より小さい番号にする)。
            let high = if no_enumeration { 60 } else { 900 };
            // SAFETY: high 番へ複製する(継承可能)。
            assert_eq!(unsafe { libc::dup2(input.as_raw_fd(), high) }, high);
            let mut nofile = libc::rlimit {
                rlim_cur: 0,
                rlim_max: 0,
            };
            // SAFETY: 受け取り先は有効な rlimit。
            assert_eq!(
                unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut nofile) },
                0
            );
            let lowered = libc::rlimit {
                rlim_cur: 64,
                rlim_max: nofile.rlim_max,
            };
            // SAFETY: この子のテスト実行物の中だけで soft を下げる。
            assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &lowered) }, 0);
            let pid = stopped(ChildKind::Copy {
                keep: [input.as_fd(), output.as_fd()],
                file_size: 1234,
            });
            let view = fds(pid);
            let expected: Vec<i32> = vec![0, 1, 2, input.as_raw_fd(), output.as_raw_fd()];
            assert_eq!(view.keys().copied().collect::<Vec<_>>(), {
                let mut sorted = expected.clone();
                sorted.sort();
                sorted
            });
            for fd in [0, 1, 2] {
                let (target, flags) = &view[&fd];
                assert_eq!(target, "/dev/null");
                assert_eq!(flags & libc::O_ACCMODE as u32, libc::O_RDWR as u32);
            }
            let cloexec = libc::O_CLOEXEC as u32;
            let (_, flags) = &view[&input.as_raw_fd()];
            assert_eq!(flags & libc::O_ACCMODE as u32, libc::O_RDONLY as u32);
            assert_eq!(flags & cloexec, 0);
            let (_, flags) = &view[&output.as_raw_fd()];
            assert_eq!(flags & libc::O_ACCMODE as u32, libc::O_RDWR as u32);
            assert_eq!(flags & cloexec, 0);
            let limits = std::fs::read_to_string(format!("/proc/{pid}/limits")).unwrap();
            let line = limits
                .lines()
                .find(|line| line.starts_with("Max file size"))
                .unwrap();
            let values: Vec<&str> = line.split_whitespace().collect();
            assert_eq!(&values[3..5], &["1234", "1234"]);
            let blocked = status(pid, "SigBlk");
            let ignored = status(pid, "SigIgn");
            assert_ne!(blocked & bit(libc::SIGUSR1), 0);
            assert_eq!(
                blocked & (bit(libc::SIGTERM) | bit(libc::SIGINT) | bit(libc::SIGHUP)),
                0
            );
            assert_ne!(ignored & bit(libc::SIGUSR2), 0);
            for number in [
                libc::SIGPIPE,
                libc::SIGXFSZ,
                libc::SIGTERM,
                libc::SIGINT,
                libc::SIGHUP,
            ] {
                assert_eq!(ignored & bit(number), 0, "{number}");
            }
            let cmdline = std::fs::read(format!("/proc/{pid}/cmdline")).unwrap();
            assert!(cmdline.starts_with(b"/bin/sh\0-c\0"));
            finish(pid);
            // SAFETY: 上で作った fd を閉じる。
            unsafe {
                libc::close(extra);
                libc::close(high);
            }
            // SAFETY: soft を元に戻す。
            assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &nofile) }, 0);
            std::fs::remove_dir_all(&directory).unwrap();
        }

        fn prepare_signals() {
            install_recording_handlers().unwrap();
            // SAFETY: この子のテスト実行物の中だけの設定。
            unsafe {
                libc::signal(libc::SIGUSR2, libc::SIG_IGN);
                libc::signal(libc::SIGPIPE, libc::SIG_IGN);
                libc::signal(libc::SIGXFSZ, libc::SIG_IGN);
                let mut set: libc::sigset_t = std::mem::zeroed();
                libc::sigemptyset(&mut set);
                libc::sigaddset(&mut set, libc::SIGUSR1);
                assert_eq!(
                    libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut()),
                    0
                );
            }
        }

        #[test]
        fn copy_child_gets_only_two_fds_rlimit_default_signals_and_mask() {
            if !in_child(
                "linux::copy_child_gets_only_two_fds_rlimit_default_signals_and_mask",
                None,
            ) {
                return;
            }
            prepare_signals();
            check_copy_child(false, false);
            check_copy_child(true, false);
            // close_range も列挙も使えない: 上限(soft)までの番号へ印を付ける。soft より大きい番号の
            // fd は CPython と同じく残るので、この検査では 900 番を使わない。
            check_copy_child(true, true);
            println!("CHILD-OK");
        }

        #[test]
        fn version_child_gets_pipe_stdout_and_dev_null_in_order() {
            if !in_child(
                "linux::version_child_gets_pipe_stdout_and_dev_null_in_order",
                None,
            ) {
                return;
            }
            prepare_signals();
            let devnull = open_dev_null().unwrap();
            let (reader, writer) = pipe().unwrap();
            // /dev/null の次とその次がパイプの番号(間に fd を開かない)。
            assert_eq!(reader.as_raw_fd(), devnull.as_raw_fd() + 1);
            assert_eq!(writer.as_raw_fd(), devnull.as_raw_fd() + 2);
            let flags = fs::fcntl_getfl(&devnull).unwrap();
            assert!(flags.contains(OFlags::RDWR));
            assert!(
                rustix::io::fcntl_getfd(&devnull)
                    .unwrap()
                    .contains(rustix::io::FdFlags::CLOEXEC)
            );
            drop(devnull);
            let pid = stopped(ChildKind::Version {
                stdout: writer.as_fd(),
            });
            let view = fds(pid);
            assert_eq!(view.keys().copied().collect::<Vec<_>>(), vec![0, 1, 2]);
            assert_eq!(view[&0].0, "/dev/null");
            assert!(view[&1].0.starts_with("pipe:"));
            assert_eq!(view[&2].0, "/dev/null");
            finish(pid);
            println!("CHILD-OK");
        }

        fn environment_case(name: &str, env: &[(&str, &str)], expected: &[&str]) {
            if !in_child(name, Some(env)) {
                return;
            }
            coerce_c_locale();
            let vars: Vec<String> = std::env::vars_os()
                .map(|(k, v)| format!("{}={}", k.to_string_lossy(), v.to_string_lossy()))
                .filter(|entry| !entry.starts_with(CHILD))
                .collect();
            assert_eq!(vars, expected);
            let pid = stopped(ChildKind::Version {
                stdout: open_dev_null().unwrap().as_fd(),
            });
            let environ = std::fs::read(format!("/proc/{pid}/environ")).unwrap();
            finish(pid);
            let child: Vec<String> = environ
                .split(|c| *c == 0)
                .filter(|entry| !entry.is_empty())
                .map(|entry| String::from_utf8_lossy(entry).into_owned())
                .filter(|entry| !entry.starts_with(CHILD))
                .collect();
            assert_eq!(child, expected);
            println!("CHILD-OK");
        }

        #[test]
        fn lc_ctype_replaced_in_place() {
            environment_case(
                "linux::lc_ctype_replaced_in_place",
                &[("A", "1"), ("LC_CTYPE", "POSIX"), ("Z", "2")],
                &["A=1", "LC_CTYPE=C.UTF-8", "Z=2"],
            );
        }

        #[test]
        fn lc_ctype_appended_at_end() {
            environment_case(
                "linux::lc_ctype_appended_at_end",
                &[("Z", "1"), ("LANG", "C"), ("A", "2")],
                &["Z=1", "LANG=C", "A=2", "LC_CTYPE=C.UTF-8"],
            );
        }

        #[test]
        fn lc_all_or_real_locale_keeps_environment() {
            environment_case(
                "linux::lc_all_or_real_locale_keeps_environment",
                &[("LC_ALL", "C"), ("B", "1")],
                &["LC_ALL=C", "B=1"],
            );
        }

        #[test]
        fn utf8_lang_keeps_environment() {
            environment_case(
                "linux::utf8_lang_keeps_environment",
                &[("LANG", "en_US.UTF-8")],
                &["LANG=en_US.UTF-8"],
            );
        }
    }
}
