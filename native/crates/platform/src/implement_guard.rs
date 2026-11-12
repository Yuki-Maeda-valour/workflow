//! S3I-A専用。implement-guard の検査専用入口が使う OS の操作を、安全な関数として置く。
//!
//! - 起動の前の記録(`remember_startup_state`)は、検査専用入口の初期化の表からだけ呼ばれる。
//!   製品の実行ファイルはこの関数を参照しないので、`main` の前に何も走らない。
//! - 各関数は 1 回だけシステムコールを呼ぶ。`EINTR` の扱いは呼び出し側(cli の `implement_guard`)が
//!   旧 bash と同じに決める。
//! - S3I-A の OS の呼び出し(`libc` の関数と `unsafe`)は、このファイルの安全な関数の中だけに置く。
//!   例: 信号の扱いと mask・`raise`・umask・fd の差し替えと種類(`dup2`・`fstat`・`fcntl`・
//!   `BorrowedFd::borrow_raw`)・環境の読み取りと置き換え(`environ`・`clearenv`・`setenv`)・
//!   利用者の番号とログインのシェル(`getuid`・`geteuid`・`getpwuid`)・`faccessat`・`pthread_self`・
//!   Mac の `newlocale`。各所に安全の理由を書く(このほか、Mac の errno の文の `strerror_r` は
//!   R4R の `restore_taskmd.rs` の `strerror_text` にある)。
#![deny(unsafe_op_in_unsafe_fn)]

use std::ffi::CString;
use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicI32, AtomicU32, AtomicUsize, Ordering};

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

/// 旧版の罠の信号(`trap on_signal TERM INT HUP`)。
pub const TRAP_SIGNALS: [i32; 3] = [libc::SIGTERM, libc::SIGINT, libc::SIGHUP];

/// bash 5.2.21 が EXIT の罠を置いたときに処理を置く終わりの信号(`sig.c` の `terminating_signals` のうち、
/// 罠の 3 つと、自分の誤りで届く信号〈SIGILL など〉を除いたもの。起動時に無視なら置かない)。
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
    pub const EEXIST: i32 = libc::EEXIST;
    pub const ENOTDIR: i32 = libc::ENOTDIR;
    pub const EISDIR: i32 = libc::EISDIR;
    pub const ENOTEMPTY: i32 = libc::ENOTEMPTY;
    pub const EIO: i32 = libc::EIO;
    pub const EPIPE: i32 = libc::EPIPE;
    pub const ENOSPC: i32 = libc::ENOSPC;
    pub const EFBIG: i32 = libc::EFBIG;
    pub const EBADF: i32 = libc::EBADF;
    pub const ENAMETOOLONG: i32 = libc::ENAMETOOLONG;
    pub const ELOOP: i32 = libc::ELOOP;
    pub const EINVAL: i32 = libc::EINVAL;
    pub const EROFS: i32 = libc::EROFS;
    pub const EPERM: i32 = libc::EPERM;
    pub const ERANGE: i32 = libc::ERANGE;
    pub const EMLINK: i32 = libc::EMLINK;
    pub const EDQUOT: i32 = libc::EDQUOT;
    pub const ECHILD: i32 = libc::ECHILD;
    pub const ESRCH: i32 = libc::ESRCH;
    pub const EAGAIN: i32 = libc::EAGAIN;
    pub const EWOULDBLOCK: i32 = libc::EWOULDBLOCK;
    pub const EINPROGRESS: i32 = libc::EINPROGRESS;
    pub const EALREADY: i32 = libc::EALREADY;
    pub const ESHUTDOWN: i32 = libc::ESHUTDOWN;
    pub const ECONNABORTED: i32 = libc::ECONNABORTED;
    pub const ECONNRESET: i32 = libc::ECONNRESET;
    pub const ETIMEDOUT: i32 = libc::ETIMEDOUT;
    pub const ECONNREFUSED: i32 = libc::ECONNREFUSED;
}

// ---- 起動の前の記録 ----

const NOT_RECORDED: usize = usize::MAX;
static INHERITED_PIPE: AtomicUsize = AtomicUsize::new(NOT_RECORDED);

/// 引き継いだ `SIGPIPE` の扱いを記録する。Rust の実行時は `main` の前に `SIGPIPE` を無視にするので、
/// 検査専用入口の初期化の表(Linux は `.init_array`、Mac は `__DATA,__mod_init_func`)からだけ呼ぶ。
/// 確保もプロセス全体の変更もしない。
pub extern "C" fn remember_startup_state() {
    // SAFETY: 受け取り先を 0 で埋め、今の扱いを読むだけ(新しい扱いは渡さない)。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい設定を渡さず、古い設定だけを受け取る。
    if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut old) } == 0 {
        INHERITED_PIPE.store(old.sa_sigaction, Ordering::SeqCst);
    }
}

/// 記録した `SIGPIPE` の扱い。記録が無ければ `None`(製品の実行ファイルなど)。
pub fn inherited_pipe() -> Option<Disposition> {
    match INHERITED_PIPE.load(Ordering::SeqCst) {
        NOT_RECORDED => None,
        raw => Some(Disposition::from_raw(raw)),
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

/// 罠で受けた信号の印(ビット 1 << 番号)。
static PENDING: AtomicU32 = AtomicU32::new(0);
/// 最初に受けた信号の番号。
static FIRST: AtomicI32 = AtomicI32::new(0);

extern "C" fn record_trap(number: libc::c_int) {
    if (0..32).contains(&number) {
        PENDING.fetch_or(1u32 << number, Ordering::SeqCst);
    }
    let _ = FIRST.compare_exchange(0, number, Ordering::SeqCst, Ordering::SeqCst);
}

/// 渡した信号に「受けたことを記録するだけ」の処理を設定する(R4R の記録とは別)。
pub fn install_trap(number: i32) -> io::Result<()> {
    let handler: extern "C" fn(libc::c_int) = record_trap;
    set_action(number, handler as libc::sighandler_t)
}

/// 罠で受けた信号の印。
pub fn pending_traps() -> u32 {
    PENDING.load(Ordering::SeqCst)
}

/// 最初に受けた信号の番号。
pub fn first_trap() -> Option<i32> {
    match FIRST.load(Ordering::SeqCst) {
        0 => None,
        number => Some(number),
    }
}

/// 渡した信号の記録だけを消す。
pub fn clear_trap(number: i32) {
    if (0..32).contains(&number) {
        PENDING.fetch_and(!(1u32 << number), Ordering::SeqCst);
    }
}

/// 罠の記録を消す。
pub fn clear_traps() {
    PENDING.store(0, Ordering::SeqCst);
    FIRST.store(0, Ordering::SeqCst);
}

/// 検査で、罠の信号を受けたことにする(差し込み点から使う)。
pub fn simulate_trap(number: i32) {
    record_trap(number);
}

/// 既定の扱いで自分へ信号を送って終わる(`SIGINT`・`SIGPIPE`・`SIGXFSZ` の再現)。止めていれば外す。
/// 信号で終わらなかったときだけ戻る(呼び出し側が終了値を決める)。
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
/// `SIGCHLD` の扱い(既定か無視)は変えない。旧版では、引き継いだ無視が旧 helper と cp の子まで
/// 残る(実測)。
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

/// 今止めている信号のうち、1〜31 番の印(ビット 1 << 番号)。
pub fn blocked_signals() -> io::Result<u32> {
    // SAFETY: sigset_t を 0 で埋め、今の mask だけを受け取る。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        let code = libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut set);
        if code != 0 {
            return Err(io::Error::from_raw_os_error(code));
        }
        let mut bits = 0u32;
        for number in 1..32 {
            if libc::sigismember(&set, number) == 1 {
                bits |= 1 << number;
            }
        }
        Ok(bits)
    }
}

// ---- 起動の状態 ----

/// `exec </dev/null`。`/dev/null` を読み取り専用で開いて fd 0 へ移す(bash のリダイレクトと同じ
/// `O_RDONLY` の open と `dup2`。開いた元の fd は閉じる)。
pub fn redirect_stdin_to_dev_null() -> io::Result<()> {
    let path = c"/dev/null";
    // SAFETY: NUL で終わるパスを渡す。戻った fd はこの関数の中で閉じる。
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    if fd != 0 {
        // SAFETY: 開いた fd を 0 番へ複製する。
        let result = unsafe { libc::dup2(fd, 0) };
        let error = io::Error::last_os_error();
        // SAFETY: 上で開いた fd を閉じる(0 番とは別の番号)。
        unsafe { libc::close(fd) };
        if result < 0 {
            return Err(error);
        }
    }
    Ok(())
}

/// `umask 077`。
pub fn set_umask_077() {
    // SAFETY: umask は整数だけを使う。
    unsafe { libc::umask(0o077) };
}

/// fd の種類(`fstat`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FdKind {
    Directory,
    Regular,
    Other,
}

/// fd の種類を返す。開いていなければ `None`。
pub fn fd_kind(fd: RawFd) -> Option<FdKind> {
    // SAFETY: 受け取り先を 0 で埋め、fstat の結果を受ける。
    let mut info: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: 有効な受け取り先を渡す。
    if unsafe { libc::fstat(fd, &mut info) } != 0 {
        return None;
    }
    Some(match info.st_mode & libc::S_IFMT {
        libc::S_IFDIR => FdKind::Directory,
        libc::S_IFREG => FdKind::Regular,
        _ => FdKind::Other,
    })
}

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

/// `fd` 番の fd を、使う間だけ借りる。閉じた番号なら `None`。
pub fn borrow_fd(fd: RawFd) -> Option<BorrowedFd<'static>> {
    // SAFETY: 1・2 番は Rust の実行時が開いたままにする(閉じた 0〜2 は /dev/null で開き直す)。
    // この部品は 0〜2 番を閉じないので、プロセスの終わりまで有効。
    let borrowed = unsafe { BorrowedFd::borrow_raw(fd) };
    rustix::io::fcntl_getfd(borrowed).ok().map(|_| borrowed)
}

/// bash の `BASH_XTRACEFD` の確かめ: 開いていなければ `None`、開いているが書き込みで `fdopen` できない
/// (読み取り専用)なら `Some(false)`、使えるなら `Some(true)`。
pub fn xtrace_fd(fd: i32) -> Option<bool> {
    // SAFETY: fcntl は整数だけを使う。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return None;
    }
    Some(flags & libc::O_ACCMODE != libc::O_RDONLY)
}

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

// ---- 環境 ----

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

/// プロセスの環境を、渡した項目(`名前=値`)の集合に置き換える。既存の名前を全部消してから足す。
/// 部品の開始時か exec の時点に 1 回だけ、ほかのスレッドが無い時に呼ぶ。
/// 名前の無い項目・`=` の無い項目は足さない(旧版の bash は子へ渡さない)。
pub fn replace_environment(entries: &[Vec<u8>]) -> io::Result<()> {
    // 置き換える前に、名前と値の C 文字列を全部用意する(途中で失敗して半端にしない)。
    let mut prepared = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(eq) = entry.iter().position(|b| *b == b'=') else {
            continue;
        };
        if eq == 0 {
            continue;
        }
        let name = CString::new(entry.get(..eq).unwrap_or_default())
            .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
        let value = CString::new(entry.get(eq + 1..).unwrap_or_default())
            .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
        prepared.push((name, value));
    }
    clear_environment()?;
    for (name, value) in &prepared {
        // SAFETY: 呼び出し側の前提(ほかのスレッドが環境を読み書きしない)。NUL で終わる名前と値を渡し、
        // 名前に `=` は無い。setenv は複製を持つ。
        if unsafe { libc::setenv(name.as_ptr(), value.as_ptr(), 1) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn clear_environment() -> io::Result<()> {
    // SAFETY: 呼び出し側の前提(ほかのスレッドが環境を読み書きしない)。
    if unsafe { libc::clearenv() } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn clear_environment() -> io::Result<()> {
    // Mac には clearenv が無い。配列の先頭を NULL にして空にする(libc の配列は以後 setenv が作り直す)。
    // SAFETY: 呼び出し側の前提(ほかのスレッドが環境を読み書きしない)。_NSGetEnviron が指す配列の
    // 先頭の要素を書き換えるだけで、配列そのものは解放しない。
    unsafe {
        let pointer = *libc::_NSGetEnviron();
        if !pointer.is_null() {
            *pointer = std::ptr::null_mut();
        }
    }
    Ok(())
}

// ---- 確かめ ----

/// 実効の uid での読み取りの可否(bash の `test -r` と同じ `faccessat(AT_EACCESS)`)。
pub fn effective_readable(path: &[u8]) -> bool {
    effective_access(path, libc::R_OK)
}

/// 実効の uid での実行の可否(bash の `test -x`・`file_status` と同じ `faccessat(AT_EACCESS)`)。
pub fn effective_executable(path: &[u8]) -> bool {
    effective_access(path, libc::X_OK)
}

fn effective_access(path: &[u8], mode: libc::c_int) -> bool {
    let Ok(path) = CString::new(path) else {
        return false;
    };
    // SAFETY: NUL で終わるパスを渡す。
    unsafe { libc::faccessat(libc::AT_FDCWD, path.as_ptr(), mode, libc::AT_EACCESS) == 0 }
}

/// 今のスレッドの番号(`pthread_self`)。`Fatal Python error` の `Current thread` に出す値。
pub fn thread_id() -> u64 {
    // SAFETY: pthread_self は引数を取らず、失敗しない。
    #[allow(clippy::unnecessary_cast, clippy::useless_conversion)]
    let id = unsafe { libc::pthread_self() } as usize as u64;
    id
}

/// 実の uid と実効の uid。
pub fn user_ids() -> (u32, u32) {
    // SAFETY: getuid・geteuid は失敗しない。
    unsafe { (libc::getuid(), libc::geteuid()) }
}

/// fd が開いているか。
pub fn fd_is_open(fd: RawFd) -> bool {
    borrow_fd(fd).is_some()
}

/// 標準出力の fd の番号。
pub fn stdout_fd() -> RawFd {
    io::stdout().as_raw_fd()
}

// ---- 地域設定 ----

/// 旧 bash の `setlocale(LC_ALL, 値)` の結果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Locale {
    /// 入っている(警告を出さない)。
    Found,
    /// 入っていない。`errno` は 0。
    Missing,
    /// 入っていない。この `errno` が残る(bash は `: strerror(errno)` を足す)。
    Failed(i32),
}

/// `setlocale(LC_ALL, name)`(空でない名前)の結果。Linux は glibc 2.39 の規則をファイルから読んで
/// 決める(musl の setlocale はどの名前も受け付けるので使わない)。Mac は `newlocale` で決め、
/// プロセスの地域設定を変えない。
pub fn lc_all(name: &[u8], locpath: Option<&[u8]>) -> Locale {
    locale::lc_all(name, locpath)
}

#[cfg(target_os = "macos")]
mod locale {
    use super::Locale;
    pub fn lc_all(name: &[u8], _locpath: Option<&[u8]>) -> Locale {
        let Ok(name) = std::ffi::CString::new(name) else {
            return Locale::Missing;
        };
        // SAFETY: NUL で終わる名前と null の基を渡す。結果は下で解放する。errno を 0 にしてから呼ぶ。
        let found = unsafe {
            *libc::__error() = 0;
            libc::newlocale(libc::LC_ALL_MASK, name.as_ptr(), std::ptr::null_mut())
        };
        if found.is_null() {
            match std::io::Error::last_os_error().raw_os_error() {
                Some(0) | None => Locale::Missing,
                Some(code) => Locale::Failed(code),
            }
        } else {
            // SAFETY: 上の newlocale が返した値で、以後使わない。
            unsafe { libc::freelocale(found) };
            Locale::Found
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod locale {
    //! glibc 2.39 の `setlocale` の判定(`setlocale.c`・`findlocale.c`・`loadarchive.c`・
    //! `l10nflist.c`・`explodename.c`・`localealias.c`)を、ファイルを読んで再現する。
    use super::Locale;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    /// CI の Ubuntu 24.04 の glibc に組み込まれた場所。
    const DEFAULT_PATH: &[u8] = b"/usr/lib/locale";
    const ARCHIVE: &str = "/usr/lib/locale/locale-archive";
    const ALIAS: &str = "/usr/share/locale/locale.alias";
    const CATEGORIES: [&str; 12] = [
        "LC_CTYPE",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_MESSAGES",
        "LC_PAPER",
        "LC_NAME",
        "LC_ADDRESS",
        "LC_TELEPHONE",
        "LC_MEASUREMENT",
        "LC_IDENTIFICATION",
    ];
    const XPG_NORM_CODESET: u8 = 1;
    const XPG_CODESET: u8 = 2;
    const XPG_TERRITORY: u8 = 4;
    const XPG_MODIFIER: u8 = 8;

    pub fn lc_all(name: &[u8], locpath: Option<&[u8]>) -> Locale {
        // setlocale.c: 空でない LOCPATH は、保管庫(archive)の代わりに既定のフォルダより先に探す。
        let dirs = match locpath.filter(|p| !p.is_empty()) {
            Some(p) => {
                let mut dirs: Vec<Vec<u8>> = p
                    .split(|b| *b == b':')
                    .filter(|d| !d.is_empty())
                    .map(<[u8]>::to_vec)
                    .collect();
                dirs.push(DEFAULT_PATH.to_vec());
                Some(dirs)
            }
            None => None,
        };
        if !name.contains(&b';') {
            return match find(name, dirs.as_deref()) {
                Ok(()) => Locale::Found,
                Err(code) => code.map_or(Locale::Missing, Locale::Failed),
            };
        }
        // `CATEGORY=値;…` の形は、全部の種類を名指す必要がある。
        let mut values: [Option<&[u8]>; 12] = [None; 12];
        let mut rest = name;
        while let Some(eq) = rest.iter().position(|b| *b == b'=') {
            let key = rest.get(..eq).unwrap_or_default();
            let Some(index) = CATEGORIES.iter().position(|c| c.as_bytes() == key) else {
                return Locale::Failed(libc::EINVAL);
            };
            let value = rest.get(eq + 1..).unwrap_or_default();
            let slot = match values.get_mut(index) {
                Some(slot) => slot,
                None => return Locale::Failed(libc::EINVAL),
            };
            match value.iter().position(|b| *b == b';') {
                Some(end) => {
                    *slot = Some(value.get(..end).unwrap_or_default());
                    rest = value.get(end + 1..).unwrap_or_default();
                }
                None => {
                    *slot = Some(value);
                    break;
                }
            }
        }
        if values.iter().any(Option::is_none) {
            return Locale::Failed(libc::EINVAL);
        }
        // setlocale.c と同じく、最後の種類から読み込む。
        for value in values.iter().rev().flatten() {
            if let Err(code) = find(value, dirs.as_deref()) {
                return Locale::Failed(code.unwrap_or(libc::ENOENT));
            }
        }
        Locale::Found
    }

    /// 各種類の `_nl_find_locale`。見つからなければ `Err(None)`(errno 0)、名前が不正なら
    /// `Err(Some(EINVAL))`。
    fn find(name: &[u8], dirs: Option<&[Vec<u8>]>) -> Result<(), Option<i32>> {
        if name.is_empty() || name == b"C" || name == b"POSIX" {
            return Ok(());
        }
        if !valid(name) {
            return Err(Some(libc::EINVAL));
        }
        let (dirs, loc_name) = match dirs {
            None => {
                if in_archive(name) {
                    return Ok(());
                }
                let alias = expand_alias(name);
                if alias.as_deref().is_some_and(in_archive) {
                    return Ok(());
                }
                (
                    vec![DEFAULT_PATH.to_vec()],
                    alias.unwrap_or_else(|| name.to_vec()),
                )
            }
            Some(dirs) => (
                dirs.to_vec(),
                expand_alias(name).unwrap_or_else(|| name.to_vec()),
            ),
        };
        let candidates = l10nflist(&loc_name);
        let found = CATEGORIES.iter().all(|category| {
            dirs.iter().any(|dir| {
                candidates.iter().any(|candidate| {
                    let mut path = dir.clone();
                    path.push(b'/');
                    path.extend_from_slice(candidate);
                    path.push(b'/');
                    path.extend_from_slice(category.as_bytes());
                    category_file(&path, category)
                })
            })
        });
        if found { Ok(()) } else { Err(None) }
    }

    /// `_nl_load_locale`: 種類のファイルか、フォルダの中の `SYS_<種類>`。
    fn category_file(path: &[u8], category: &str) -> bool {
        let path = Path::new(std::ffi::OsStr::from_bytes(path));
        match std::fs::metadata(path) {
            Ok(m) if m.is_file() => std::fs::File::open(path).is_ok(),
            Ok(m) if m.is_dir() => {
                let inner = path.join(format!("SYS_{category}"));
                std::fs::metadata(&inner).is_ok_and(|m| m.is_file())
                    && std::fs::File::open(&inner).is_ok()
            }
            _ => false,
        }
    }

    /// findlocale.c の `valid_locale_name`。
    fn valid(name: &[u8]) -> bool {
        let n = name.len();
        if n > 255 || name.windows(4).any(|w| w == b"/../") || name == b".." {
            return false;
        }
        if n >= 3 && (name.starts_with(b"../") || name.ends_with(b"/..")) {
            return false;
        }
        !(name.contains(&b'/') && name.first() != Some(&b'/'))
    }

    /// l10nflist.c の `_nl_normalize_codeset`。
    pub(super) fn normalize(codeset: &[u8]) -> Vec<u8> {
        let digits_only = codeset
            .iter()
            .filter(|b| b.is_ascii_alphanumeric())
            .all(u8::is_ascii_digit);
        let mut out = if digits_only { b"iso".to_vec() } else { vec![] };
        for b in codeset {
            if b.is_ascii_alphabetic() {
                out.push(b.to_ascii_lowercase());
            } else if b.is_ascii_digit() {
                out.push(*b);
            }
        }
        out
    }

    /// explodename.c と、`_nl_make_l10nflist` が試す名前。
    pub(super) fn l10nflist(name: &[u8]) -> Vec<Vec<u8>> {
        let stop = |b: &u8| matches!(b, b'_' | b'.' | b'@');
        let lang_end = name.iter().position(stop).unwrap_or(name.len());
        if lang_end == 0 {
            return vec![name.to_vec()];
        }
        let language = name.get(..lang_end).unwrap_or_default();
        let mut cp = lang_end;
        let mut mask = 0;
        let mut territory: &[u8] = b"";
        let mut codeset: &[u8] = b"";
        let mut normalized: Vec<u8> = vec![];
        let mut modifier: &[u8] = b"";
        if name.get(cp) == Some(&b'_') {
            let start = cp + 1;
            let rest = name.get(start..).unwrap_or_default();
            cp = start
                + rest
                    .iter()
                    .position(|b| matches!(b, b'.' | b'@'))
                    .unwrap_or(rest.len());
            territory = name.get(start..cp).unwrap_or_default();
            mask |= XPG_TERRITORY;
        }
        if name.get(cp) == Some(&b'.') {
            let start = cp + 1;
            let rest = name.get(start..).unwrap_or_default();
            cp = start + rest.iter().position(|b| *b == b'@').unwrap_or(rest.len());
            codeset = name.get(start..cp).unwrap_or_default();
            mask |= XPG_CODESET;
            if !codeset.is_empty() {
                normalized = normalize(codeset);
                // ここでの strcmp は、名前の終わりまでの文字列と比べる。
                if normalized != name.get(start..).unwrap_or_default() {
                    mask |= XPG_NORM_CODESET;
                }
            }
        }
        if name.get(cp) == Some(&b'@') {
            modifier = name.get(cp + 1..).unwrap_or_default();
            if !modifier.is_empty() {
                mask |= XPG_MODIFIER;
            }
        }
        if territory.is_empty() {
            mask &= !XPG_TERRITORY;
        }
        if codeset.is_empty() {
            mask &= !XPG_CODESET;
        }
        let mut out = vec![];
        for subset in (0..=mask).rev() {
            if subset & !mask != 0 || (subset & XPG_CODESET != 0 && subset & XPG_NORM_CODESET != 0)
            {
                continue;
            }
            let mut candidate = language.to_vec();
            if subset & XPG_TERRITORY != 0 {
                candidate.push(b'_');
                candidate.extend_from_slice(territory);
            }
            if subset & XPG_CODESET != 0 {
                candidate.push(b'.');
                candidate.extend_from_slice(codeset);
            }
            if subset & XPG_NORM_CODESET != 0 {
                candidate.push(b'.');
                candidate.extend_from_slice(&normalized);
            }
            if subset & XPG_MODIFIER != 0 {
                candidate.push(b'@');
                candidate.extend_from_slice(modifier);
            }
            out.push(candidate);
        }
        out
    }

    /// loadarchive.c: 文字コード名を正規化した名前が、名前の表にあるか。
    fn in_archive(name: &[u8]) -> bool {
        let mut name = name.to_vec();
        if let Some(dot) = name.iter().position(|b| *b == b'.')
            && !matches!(name.get(dot + 1), Some(b'@') | None)
        {
            let tail = name.get(dot + 1..).unwrap_or_default();
            let end = tail
                .iter()
                .position(|b| *b == b'@')
                .map_or(name.len(), |p| dot + 1 + p);
            let normalized = normalize(name.get(dot + 1..end).unwrap_or_default());
            name = [
                name.get(..=dot).unwrap_or_default(),
                &normalized[..],
                name.get(end..).unwrap_or_default(),
            ]
            .concat();
        }
        let Ok(data) = std::fs::read(ARCHIVE) else {
            return false;
        };
        let word = |at: usize| -> Option<usize> {
            data.get(at..at + 4)
                .map(|b| u32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as usize)
        };
        // struct locarhead: magic, serial, namehash_offset, namehash_used, namehash_size, …
        if word(0) != Some(0xde02_0109) {
            return false;
        }
        let (Some(offset), Some(size)) = (word(8), word(16)) else {
            return false;
        };
        (0..size).any(|i| {
            // struct namehashent: hashval, name_offset, locrec_offset。
            let entry = offset + i * 12;
            match (word(entry + 4), word(entry + 8)) {
                (Some(at), Some(record)) if at != 0 && record != 0 => data
                    .get(at..)
                    .and_then(|s| s.iter().position(|b| *b == 0).and_then(|e| s.get(..e)))
                    .is_some_and(|stored| stored == name.as_slice()),
                _ => false,
            }
        })
    }

    /// localealias.c: 1 段だけ。別名は大文字と小文字を区別しない。
    fn expand_alias(name: &[u8]) -> Option<Vec<u8>> {
        let text = std::fs::read(ALIAS).ok()?;
        for line in text.split(|b| *b == b'\n') {
            let line = trim_start(line);
            if line.is_empty() || line.first() == Some(&b'#') {
                continue;
            }
            let end = line
                .iter()
                .position(|b| b.is_ascii_whitespace())
                .unwrap_or(line.len());
            let (alias, rest) = line.split_at(end);
            let rest = trim_start(rest);
            let value_end = rest
                .iter()
                .position(|b| b.is_ascii_whitespace())
                .unwrap_or(rest.len());
            let value = rest.get(..value_end).unwrap_or_default();
            if !value.is_empty() && alias.eq_ignore_ascii_case(name) {
                return Some(value.to_vec());
            }
        }
        None
    }

    fn trim_start(s: &[u8]) -> &[u8] {
        let start = s
            .iter()
            .position(|b| !b.is_ascii_whitespace())
            .unwrap_or(s.len());
        s.get(start..).unwrap_or_default()
    }
}

// ---- `rm -rf` の再現の走査(GNU rm 9.4 の fts と同じく、親の fd から辿る) ----

/// 走査で見つけた項目の種類(`d_type`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TreeKind {
    Directory,
    Other,
    Unknown,
}

/// リンクを辿らない `fstatat`・`fstat` の結果(フォルダか・device・inode)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TreeStat {
    pub directory: bool,
    pub dev: u64,
    pub ino: u64,
}

fn tree_errno(error: rustix::io::Errno) -> io::Error {
    io::Error::from_raw_os_error(error.raw_os_error())
}

// st_dev・st_ino の型は OS ごとに違う(Mac の st_dev は i32)。
#[allow(clippy::unnecessary_cast)]
fn tree_stat_of(stat: &rustix::fs::Stat) -> TreeStat {
    TreeStat {
        directory: rustix::fs::FileType::from_raw_mode(stat.st_mode as rustix::fs::RawMode)
            == rustix::fs::FileType::Directory,
        dev: stat.st_dev as u64,
        ino: stat.st_ino as u64,
    }
}

/// 親の fd(無ければ現在のフォルダ)から、リンクを辿らずに種類と番号を得る。
pub fn tree_lstat(parent: Option<BorrowedFd<'_>>, name: &[u8]) -> io::Result<TreeStat> {
    let flags = rustix::fs::AtFlags::SYMLINK_NOFOLLOW;
    let stat = match parent {
        Some(fd) => rustix::fs::statat(fd, name, flags),
        None => rustix::fs::statat(rustix::fs::CWD, name, flags),
    }
    .map_err(tree_errno)?;
    Ok(tree_stat_of(&stat))
}

/// 開いたフォルダの種類と番号。
pub fn tree_fstat(fd: BorrowedFd<'_>) -> io::Result<TreeStat> {
    rustix::fs::fstat(fd)
        .map(|stat| tree_stat_of(&stat))
        .map_err(tree_errno)
}

/// 親の fd(無ければ現在のフォルダ)から、リンクを辿らずにフォルダを開く
/// (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`)。
pub fn tree_open_dir(parent: Option<BorrowedFd<'_>>, name: &[u8]) -> io::Result<OwnedFd> {
    let flags = rustix::fs::OFlags::RDONLY
        | rustix::fs::OFlags::DIRECTORY
        | rustix::fs::OFlags::NOFOLLOW
        | rustix::fs::OFlags::CLOEXEC;
    let mode = rustix::fs::Mode::empty();
    match parent {
        Some(fd) => rustix::fs::openat(fd, name, flags, mode),
        None => rustix::fs::openat(rustix::fs::CWD, name, flags, mode),
    }
    .map_err(tree_errno)
}

/// 開いたフォルダの中身を、`readdir` の順に全部読む(`.` と `..` は除く)。開き直さずに、fd を複製して
/// 読む(`Dir::read_from` は `openat(fd, ".")` で開き直すので、読めるが検索できないフォルダ〈mode 400・600〉で
/// 失敗する。GNU rm の fts は開いた fd のまま読む)。複製は元の fd と読む位置を分け合うが、元の fd は
/// `fstatat`・`unlinkat` にしか使わない。
pub fn tree_list(fd: BorrowedFd<'_>) -> io::Result<Vec<(Vec<u8>, TreeKind)>> {
    let copy = fd.try_clone_to_owned()?;
    let dir = rustix::fs::Dir::new(copy).map_err(tree_errno)?;
    let mut out = Vec::new();
    for entry in dir {
        let entry = entry.map_err(tree_errno)?;
        let name = entry.file_name().to_bytes();
        if name == b"." || name == b".." {
            continue;
        }
        let kind = match entry.file_type() {
            rustix::fs::FileType::Directory => TreeKind::Directory,
            rustix::fs::FileType::Unknown => TreeKind::Unknown,
            _ => TreeKind::Other,
        };
        out.push((name.to_vec(), kind));
    }
    Ok(out)
}

/// 親の fd(無ければ現在のフォルダ)から消す(`unlinkat`。`directory` なら `AT_REMOVEDIR`)。
pub fn tree_unlink(parent: Option<BorrowedFd<'_>>, name: &[u8], directory: bool) -> io::Result<()> {
    let flags = if directory {
        rustix::fs::AtFlags::REMOVEDIR
    } else {
        rustix::fs::AtFlags::empty()
    };
    match parent {
        Some(fd) => rustix::fs::unlinkat(fd, name, flags),
        None => rustix::fs::unlinkat(rustix::fs::CWD, name, flags),
    }
    .map_err(tree_errno)
}

#[cfg(test)]
mod tests {
    //! 信号・環境・fd 0 を変える検査は、このテスト実行物を子として起動し、その中だけで行う。
    use super::*;
    use std::os::unix::ffi::OsStrExt;
    use std::process::Command;

    const CHILD: &str = "S3IA_PLATFORM_CHILD";

    fn in_child(name: &str, env: Option<&[&str]>) -> bool {
        if std::env::var(CHILD).as_deref() == Ok(name) {
            return true;
        }
        let binary = std::env::current_exe().expect("test binary");
        let filter = format!("implement_guard::tests::{name}");
        let tail = [
            filter.as_str(),
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ];
        let mut command = match env {
            Some(entries) => {
                let mut command = Command::new("/usr/bin/env");
                command.arg("-i");
                command.args(entries);
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

    #[test]
    fn traps_record_without_restart_and_dispositions_return() {
        if !in_child("traps_record_without_restart_and_dispositions_return", None) {
            return;
        }
        clear_traps();
        for number in TRAP_SIGNALS {
            install_trap(number).unwrap();
            let action = sigaction_of(number);
            assert_ne!(action.sa_sigaction, libc::SIG_DFL);
            assert_ne!(action.sa_sigaction, libc::SIG_IGN);
            assert_eq!(action.sa_flags & libc::SA_RESTART, 0);
        }
        assert_eq!(pending_traps(), 0);
        // SAFETY: 自分へ信号を送る(処理は記録するだけ)。
        unsafe { libc::raise(libc::SIGHUP) };
        // SAFETY: 同上。
        unsafe { libc::raise(libc::SIGTERM) };
        assert_eq!(pending_traps(), (1 << SIGHUP) | (1 << SIGTERM));
        assert_eq!(first_trap(), Some(SIGHUP));
        clear_traps();
        assert_eq!(pending_traps(), 0);
        assert_eq!(first_trap(), None);
        simulate_trap(SIGINT);
        assert_eq!(pending_traps(), 1 << SIGINT);
        for number in TRAP_SIGNALS {
            set_disposition(number, Disposition::Ignore).unwrap();
            assert_eq!(disposition(number).unwrap(), Disposition::Ignore);
            set_disposition(number, Disposition::Default).unwrap();
            assert_eq!(disposition(number).unwrap(), Disposition::Default);
        }
        // SIGCHLD: 止めだけを外し、無視は残す。
        // SAFETY: この子のテスト実行物の中だけの設定。
        unsafe {
            libc::signal(libc::SIGCHLD, libc::SIG_IGN);
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigaddset(&mut set, libc::SIGCHLD);
            libc::sigaddset(&mut set, libc::SIGUSR1);
            libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
        }
        assert_ne!(blocked_signals().unwrap() & (1 << SIGCHLD), 0);
        unblock_child_signal().unwrap();
        assert_eq!(disposition(SIGCHLD).unwrap(), Disposition::Ignore);
        let blocked = blocked_signals().unwrap();
        assert_eq!(blocked & (1 << SIGCHLD), 0);
        assert_ne!(blocked & (1 << libc::SIGUSR1), 0);
        println!("CHILD-OK");
    }

    #[test]
    fn pipe_record_follows_inherited_disposition() {
        // 子は SIGPIPE を無視して起動する(env(1) は受け継いだ無視を保つ)。記録の関数を直接呼ぶ。
        if !in_child("pipe_record_follows_inherited_disposition", None) {
            return;
        }
        assert_eq!(inherited_pipe(), None);
        set_disposition(SIGPIPE, Disposition::Default).unwrap();
        remember_startup_state();
        assert_eq!(inherited_pipe(), Some(Disposition::Default));
        set_disposition(SIGPIPE, Disposition::Ignore).unwrap();
        remember_startup_state();
        assert_eq!(inherited_pipe(), Some(Disposition::Ignore));
        println!("CHILD-OK");
    }

    fn environment_now() -> Vec<Vec<u8>> {
        raw_environment()
            .into_iter()
            .filter(|entry| !entry.starts_with(CHILD.as_bytes()))
            .collect()
    }

    #[test]
    fn environment_is_replaced_as_a_whole() {
        if !in_child(
            "environment_is_replaced_as_a_whole",
            Some(&["A=1", "a-b=2", "A=3", "Z=4"]),
        ) {
            return;
        }
        // env(1) は同じ名前を setenv で置き換える(並びは最初の位置)。
        assert_eq!(
            environment_now(),
            [&b"A=3"[..], b"a-b=2", b"Z=4"]
                .iter()
                .map(|e| e.to_vec())
                .collect::<Vec<_>>()
        );
        replace_environment(&[
            b"B=x".to_vec(),
            b"no-equal".to_vec(),
            b"=empty-name".to_vec(),
            b"c-d=y=z".to_vec(),
            b"E=".to_vec(),
        ])
        .unwrap();
        let mut now = raw_environment();
        now.sort();
        assert_eq!(
            now,
            [&b"B=x"[..], b"E=", b"c-d=y=z"]
                .iter()
                .map(|e| e.to_vec())
                .collect::<Vec<_>>()
        );
        // 子へ渡る環境も同じ。
        let output = Command::new("/usr/bin/env").output().unwrap();
        let mut child: Vec<&[u8]> = output
            .stdout
            .split(|b| *b == b'\n')
            .filter(|l| !l.is_empty())
            .collect();
        child.sort();
        assert_eq!(child, [&b"B=x"[..], b"E=", b"c-d=y=z"]);
        println!("CHILD-OK");
    }

    #[test]
    fn stdin_moves_to_dev_null_and_umask() {
        if !in_child("stdin_moves_to_dev_null_and_umask", None) {
            return;
        }
        redirect_stdin_to_dev_null().unwrap();
        // fd 0 が /dev/null を指す(`/proc` と `/dev/fd` の見え方は OS で違うので、fd 0 そのものを fstat して、
        // /dev/null の stat と種類・rdev・ino・dev を比べる)。
        use std::os::fd::AsFd;
        let stdin = rustix::fs::fstat(std::io::stdin().as_fd()).unwrap();
        let null = rustix::fs::stat("/dev/null").unwrap();
        assert_eq!(
            rustix::fs::FileType::from_raw_mode(stdin.st_mode),
            rustix::fs::FileType::CharacterDevice
        );
        assert_eq!(
            (stdin.st_rdev, stdin.st_ino, stdin.st_dev),
            (null.st_rdev, null.st_ino, null.st_dev)
        );
        // 読み取り専用で開いた。
        // SAFETY: fcntl は整数だけを使う。
        let flags = unsafe { libc::fcntl(0, libc::F_GETFL) };
        assert_eq!(flags & libc::O_ACCMODE, libc::O_RDONLY);
        set_umask_077();
        // SAFETY: umask を読んで戻す。
        let mask = unsafe { libc::umask(0o022) };
        assert_eq!(mask, 0o077);
        assert_eq!(fd_kind(0), Some(FdKind::Other));
        println!("CHILD-OK");
    }

    /// 単体テスト用の新しいフォルダ。共有の /tmp を使わず HOME の下に、乱数の名前で作る(既にあれば失敗)。
    fn fresh_dir(tag: &str) -> std::path::PathBuf {
        use std::io::Read;
        let home = std::env::var_os("HOME").expect("HOME");
        let mut bytes = [0u8; 8];
        std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut bytes))
            .expect("/dev/urandom");
        let name: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        let dir = std::path::PathBuf::from(home).join(format!(".s3ia-{tag}-{name}"));
        std::fs::create_dir(&dir).expect("新しいフォルダを作れない(既にある)");
        dir
    }

    #[test]
    fn access_and_ids() {
        let dir = fresh_dir("platform");
        let file = dir.join("f");
        std::fs::write(&file, b"x").unwrap();
        std::fs::set_permissions(&file, std::os::unix::fs::PermissionsExt::from_mode(0o600))
            .unwrap();
        let path = file.as_os_str().as_bytes();
        let (uid, euid) = user_ids();
        assert_eq!(uid, euid);
        assert!(effective_readable(path));
        assert_eq!(effective_executable(path), euid == 0);
        assert!(!effective_readable(b"/nonexistent/s3ia"));
        assert_ne!(thread_id(), 0);
        assert!(fd_is_open(1));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn locale_rules_follow_glibc() {
        use super::locale::{l10nflist, normalize};
        assert_eq!(normalize(b"UTF-8"), b"utf8");
        assert_eq!(normalize(b"8859-1"), b"iso88591");
        let names = l10nflist(b"xx_YY.UTF-8@m");
        assert_eq!(
            names.first().map(Vec::as_slice),
            Some(&b"xx_YY.UTF-8@m"[..])
        );
        assert!(names.iter().any(|n| n == b"xx_YY.utf8@m"));
        assert!(names.iter().any(|n| n == b"xx"));
        assert_eq!(lc_all(b"C", None), Locale::Found);
        assert_eq!(lc_all(b"POSIX", None), Locale::Found);
        assert_eq!(lc_all(b"xx_YY", None), Locale::Missing);
        assert_eq!(lc_all(b"a/b", None), Locale::Failed(libc::EINVAL));
        // LOCPATH の下に種類のファイルを全部置いた名前は入っている。
        let dir = fresh_dir("locpath");
        let named = dir.join("qq_QQ.utf8");
        std::fs::create_dir(&named).unwrap();
        for category in [
            "LC_CTYPE",
            "LC_NUMERIC",
            "LC_TIME",
            "LC_COLLATE",
            "LC_MONETARY",
            "LC_MESSAGES",
            "LC_PAPER",
            "LC_NAME",
            "LC_ADDRESS",
            "LC_TELEPHONE",
            "LC_MEASUREMENT",
            "LC_IDENTIFICATION",
        ] {
            std::fs::write(named.join(category), b"x").unwrap();
        }
        let locpath = dir.as_os_str().as_bytes();
        assert_eq!(lc_all(b"qq_QQ.UTF-8", Some(locpath)), Locale::Found);
        assert_eq!(lc_all(b"qq_QQ.utf8", Some(locpath)), Locale::Found);
        assert_eq!(lc_all(b"qq_QQ.UTF-8", None), Locale::Missing);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
