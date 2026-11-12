//! S2E専用。environment-guardの観測と控えの作成が使うOSの操作を、安全な関数として置く。
//!
//! - 多くの関数は1回だけシステムコールを呼び、`EINTR`の再試行は呼び出し側(cli)が決める。
//!   中で再試行する(`SIGINT`が来ていなければ繰り返し、来ていれば`EINTR`を返す。旧版の`PyErr_CheckSignals`)のは、
//!   `retry`と、それを使う`spawn`(errpipeの読取り)・`Child::wait`だけ。`Child::wait_timeout`は休みながら
//!   確かめ直し、`scan_dir`は`getdents`が`EINTR`を返せば誤りとして返す。
//! - `unsafe`を置く場所: `main`より前に受け継いだ`SIGPIPE`の扱いを控える初期化の関数・リンクを辿らない`chmod`(`fchmodat`)・信号の扱いの取得と設定・利用者データベースの読取り
//!   (`getpwnam_r`・`getpwuid_r`)・`fcntl`・`waitpid`・`kill`・子の起動(`fork`・`execve`)。各所に安全の理由を書く。
//! - `fork`の後の子(`child_main`)では、確保・ロック・`Rust`の入出力・パニックを使わない。`async-signal-safe`な
//!   システムコール(`close`・`dup`・`dup2`・`fcntl`・`chdir`・`sigaction`・`sigemptyset`・`close_range`・`execve`・`write`・`_exit`)だけを呼び、
//!   必要な材料(文字列・配列・fd)は`fork`の前にすべて用意する。
//! - 子の起動は旧Python 3.10の`subprocess.Popen`(`_posixsubprocess.fork_exec`)に合わせて`fork`・`execve`で行う。
//!   `Command`は、PATHの探し方・fdの受け継ぎ・信号の設定が旧版と違うので使わない。
#![deny(unsafe_op_in_unsafe_fn)]

use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fd::{BorrowedFd, OwnedFd};
use rustix::fs::{self, AtFlags, Dir, Mode, OFlags};
use std::ffi::{CString, c_char, c_int};
use std::io;
use std::os::fd::{AsFd, AsRawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub use crate::restore_taskmd::{S_IFDIR, S_IFLNK, S_IFMT, S_IFREG};

/// 旧版が比べるstatの値を損失なく持つ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stat {
    pub dev: u64,
    pub ino: u64,
    pub mode: u32,
    pub size: i64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
    pub uid: u32,
    pub gid: u32,
}

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
            uid: stat.st_uid as u32,
            gid: stat.st_gid as u32,
        }
    }

    pub fn kind(&self) -> u32 {
        self.mode & S_IFMT
    }
}

fn errno(error: rustix::io::Errno) -> io::Error {
    io::Error::from_raw_os_error(error.raw_os_error())
}

/// `os.open('/', O_RDONLY|O_DIRECTORY|O_CLOEXEC)`。
pub fn open_root() -> io::Result<OwnedFd> {
    fs::open(
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 親からリンクを辿らずにフォルダを開く。
pub fn open_dir_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 本文を読むために、リンクを辿らず・待たずに開く。
pub fn open_read_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(errno)
}

/// 排他の新規作成(`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`)。modeはumaskを受ける。
pub fn create_excl_at(dir: BorrowedFd<'_>, name: &[u8], mode: u32) -> io::Result<OwnedFd> {
    fs::openat(
        dir,
        name,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(mode as fs::RawMode),
    )
    .map_err(errno)
}

/// リンクを辿らない`fstatat`。
pub fn lstat_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<Stat> {
    fs::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW)
        .map(Stat::from_raw)
        .map_err(errno)
}

pub fn fstat(fd: BorrowedFd<'_>) -> io::Result<Stat> {
    fs::fstat(fd).map(Stat::from_raw).map_err(errno)
}

/// `readlinkat`。
pub fn readlink_at(dir: BorrowedFd<'_>, name: &[u8]) -> io::Result<Vec<u8>> {
    fs::readlinkat(dir, name, Vec::new())
        .map(|value| value.into_bytes())
        .map_err(errno)
}

pub fn mkdir_at(dir: BorrowedFd<'_>, name: &[u8], mode: u32) -> io::Result<()> {
    fs::mkdirat(dir, name, Mode::from_raw_mode(mode as fs::RawMode)).map_err(errno)
}

pub fn fchmod(fd: BorrowedFd<'_>, mode: u32) -> io::Result<()> {
    fs::fchmod(fd, Mode::from_raw_mode(mode as fs::RawMode)).map_err(errno)
}

pub fn fsync(fd: BorrowedFd<'_>) -> io::Result<()> {
    fs::fsync(fd).map_err(errno)
}

pub fn read(fd: BorrowedFd<'_>, buffer: &mut [u8]) -> io::Result<usize> {
    rustix::io::read(fd, buffer).map_err(errno)
}

pub fn write(fd: BorrowedFd<'_>, buffer: &[u8]) -> io::Result<usize> {
    rustix::io::write(fd, buffer).map_err(errno)
}

/// `os.chmod(name, mode, dir_fd=dir, follow_symlinks=False)`と同じ呼出し。
///
/// glibcの`fchmodat(AT_SYMLINK_NOFOLLOW)`を呼ぶ。リンクを辿れない環境は`EOPNOTSUPP`を返す。
pub fn chmod_nofollow_at(dir: BorrowedFd<'_>, name: &[u8], mode: u32) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    let mut c_name = Vec::with_capacity(name.len() + 1);
    c_name.extend_from_slice(name);
    if c_name.contains(&0) {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    c_name.push(0);
    // SAFETY: `c_name`はNUL終端で、呼出しの間だけ有効。dirは借りた有効なfd。
    let code = unsafe {
        libc::fchmodat(
            dir.as_raw_fd(),
            c_name.as_ptr().cast(),
            mode as libc::mode_t,
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if code == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// 開けるfdの数の上限(`RLIMIT_NOFILE`のsoft)を`want`まで上げる(hardは超えない。既に`want`以上なら何もしない)。
/// 戻り値は、その後のsoftの値。深い木の走査は、親のfdを開いたまま子を開くので、Macの既定(256)では足りない。
/// 検査の準備のための入口で、製品の入口は呼ばない(旧版も上限を変えないので、製品は旧版と同じ上限で動く)。
pub fn raise_open_file_limit(want: libc::rlim_t) -> io::Result<libc::rlim_t> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: `limit`は書込み可能な有効な領域。
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let target = if limit.rlim_max == libc::RLIM_INFINITY {
        want
    } else {
        want.min(limit.rlim_max)
    };
    if limit.rlim_cur >= target {
        return Ok(limit.rlim_cur);
    }
    limit.rlim_cur = target;
    // SAFETY: `limit`は有効な値。soft(`rlim_cur`)だけを上げ、hardは変えない。
    if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(target)
}

/// `scan_dir`の失敗: OSの誤りか、呼び出し側の関数の失敗。
#[derive(Debug)]
pub enum ScanError<E> {
    /// fdの複製に失敗した(旧版の`os.scandir(fd)`は、ファイル名の無い`OSError`にする)。
    Dup(io::Error),
    /// `fdopendir`か読取りに失敗した(旧版は、ファイル名がfdの整数の`OSError`にする)。
    Os(io::Error),
    Callback(E),
}

/// `os.scandir(fd)`と同じ読み方。fdを複製して`fdopendir`し(読み取り権限だけで読める)、
/// 1項目ごとに`each`を呼ぶ(旧版の`tick(1)`と名前の追加の間に、次の項目を読まない)。
/// `.`と`..`は渡さない。順序はOSが返した順。終わったら(失敗でも)fdの位置を先頭へ戻す
/// (旧版の`ScandirIterator_closedir`の`rewinddir`。複製したfdは位置を共有するため)。
pub fn scan_dir<E>(
    fd: BorrowedFd<'_>,
    mut each: impl FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), ScanError<E>> {
    let duplicate =
        rustix::io::fcntl_dupfd_cloexec(fd, 0).map_err(|code| ScanError::Dup(errno(code)))?;
    let mut dir = Dir::new(duplicate).map_err(|code| ScanError::Os(errno(code)))?;
    let result = (|| {
        while let Some(entry) = dir.read() {
            let entry = entry.map_err(|code| ScanError::Os(errno(code)))?;
            let name = entry.file_name().to_bytes();
            if name != b"." && name != b".." {
                each(name).map_err(ScanError::Callback)?;
            }
        }
        Ok(())
    })();
    drop(dir);
    // 複製したfdは位置を共有する。rustixの`Dir::rewind`は次の読取りの前に巻き戻す印を持つだけなので、
    // glibcの`rewinddir`と同じく、fdの位置を直接先頭へ戻す(失敗は旧版も無視する)。
    let _ = fs::seek(fd, rustix::fs::SeekFrom::Start(0));
    result
}

/// Macの`select`が受ける待ち時間の上限(秒)。旧版の`select`は、これを超えると`EINVAL`にする。
pub const MAC_SELECT_MAX_SECONDS: i64 = 100_000_000;

/// 1回の`poll`に渡す待ち時間(ミリ秒)の分け方。`c_int`に収まる大きさで区切る
/// (rustixは待ち時間を`c_int`のミリ秒に直す機種があり、約24.8日を超えると`EINVAL`になる)。
fn poll_slices(timeout_ms: i64) -> Vec<i64> {
    let limit = i64::from(i32::MAX);
    let mut slices = Vec::new();
    let mut left = timeout_ms.max(0);
    loop {
        let slice = left.min(limit);
        slices.push(slice);
        left -= slice;
        if left == 0 {
            return slices;
        }
    }
}

/// 待ち時間が旧版の`select`の上限を超えるか。上限は機種ごと: Macは1e8秒、Linuxは(この関数では)上限なし。
pub fn select_timeout_too_large(timeout_ms: i64, mac: bool) -> bool {
    mac && timeout_ms / 1000 > MAC_SELECT_MAX_SECONDS
}

/// 標準入力などのfdが読めるまで待つ。`timeout_ms`が負なら無期限。読めるか切断なら`true`。
/// 長い待ちは`c_int`に収まる長さに分けて待つ。Macで1e8秒を超える待ちは、旧版と同じく`EINVAL`。
pub fn wait_readable(fd: BorrowedFd<'_>, timeout_ms: i64) -> io::Result<bool> {
    if select_timeout_too_large(timeout_ms, cfg!(target_os = "macos")) {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    #[cfg(target_vendor = "apple")]
    if let Some(result) = select_readable(fd, timeout_ms) {
        return result;
    }
    let poll_once = |ms: Option<i64>| -> io::Result<bool> {
        let mut fds = [PollFd::new(&fd, PollFlags::IN)];
        let timeout = ms.map(|ms| Timespec {
            tv_sec: ms / 1000,
            tv_nsec: (ms % 1000) * 1_000_000,
        });
        Ok(poll(&mut fds, timeout.as_ref()).map_err(errno)? > 0)
    };
    if timeout_ms < 0 {
        return poll_once(None);
    }
    for slice in poll_slices(timeout_ms) {
        if poll_once(Some(slice))? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Macは、旧Pythonの`select.select`と同じ`select(2)`で待つ(`poll(2)`は、macOSでは端末などのデバイスを扱えない)。
/// `fd`が`FD_SETSIZE`以上のときは`select`に渡せないので`None`(`poll`で待つ)。待ち時間が負なら無期限。
#[cfg(target_vendor = "apple")]
fn select_readable(fd: BorrowedFd<'_>, timeout_ms: i64) -> Option<io::Result<bool>> {
    let raw = fd.as_raw_fd();
    if raw < 0 || raw >= libc::FD_SETSIZE as c_int {
        return None;
    }
    // SAFETY: `fd_set`はすべて0で有効な初期値。
    let mut set: libc::fd_set = unsafe { std::mem::zeroed() };
    // SAFETY: `raw`は`0..FD_SETSIZE`の範囲で、`set`は書込み可能。
    unsafe {
        libc::FD_ZERO(&mut set);
        libc::FD_SET(raw, &mut set);
    }
    let mut timeout = libc::timeval {
        tv_sec: (timeout_ms.max(0) / 1000) as libc::time_t,
        tv_usec: ((timeout_ms.max(0) % 1000) * 1000) as libc::suseconds_t,
    };
    let timeout_ptr = if timeout_ms < 0 {
        std::ptr::null_mut()
    } else {
        &mut timeout as *mut libc::timeval
    };
    // SAFETY: `set`と`timeout`は有効で書込み可能な領域(`timeout_ptr`は無期限のとき`null`)。書込み・例外の集合は渡さない。
    let found = unsafe {
        libc::select(
            raw + 1,
            &mut set,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            timeout_ptr,
        )
    };
    Some(if found < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(found > 0)
    })
}

/// `poll_fds`で待つ向き。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Interest {
    pub read: bool,
    pub write: bool,
}

/// 複数のfdを待つ(旧版の`selectors.DefaultSelector().select(timeout)`)。`timeout_ms`はミリ秒(負なら無期限)。
/// 戻り値は`fds`と同じ並びで、そのfdに何かの事象(読める・書ける・切断・誤り)があれば`true`。
/// 旧版は、読めるかの印以外(切断・誤り)も呼出し側の「対応する側の処理」へ回すので、区別しない。
/// `EINTR`は、そのまま返す(呼出し側が`retry`で包む)。
pub fn poll_fds(fds: &[(BorrowedFd<'_>, Interest)], timeout_ms: i64) -> io::Result<Vec<bool>> {
    let mut set: Vec<PollFd<'_>> = fds
        .iter()
        .map(|(fd, interest)| {
            let mut flags = PollFlags::empty();
            if interest.read {
                flags |= PollFlags::IN;
            }
            if interest.write {
                flags |= PollFlags::OUT;
            }
            PollFd::new(fd, flags)
        })
        .collect();
    let timeout = (timeout_ms >= 0).then(|| Timespec {
        tv_sec: timeout_ms / 1000,
        tv_nsec: (timeout_ms % 1000) * 1_000_000,
    });
    poll(&mut set, timeout.as_ref()).map_err(errno)?;
    Ok(set.iter().map(|item| !item.revents().is_empty()).collect())
}

/// 信号の扱い。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Default,
    Ignore,
    Handler,
}

fn disposition_of(action: &libc::sigaction) -> Disposition {
    if action.sa_sigaction == libc::SIG_DFL {
        Disposition::Default
    } else if action.sa_sigaction == libc::SIG_IGN {
        Disposition::Ignore
    } else {
        Disposition::Handler
    }
}

/// 今の信号の扱いを返す(変えない)。
pub fn signal_disposition(number: i32) -> io::Result<Disposition> {
    // SAFETY: sigactionはすべて0で有効な初期値。第2引数がnullなら設定は変えない。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: `action`は書込み可能な有効な領域。
    if unsafe { libc::sigaction(number, std::ptr::null(), &mut action) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(disposition_of(&action))
}

/// 起動時(`main`より前)に控えた`SIGPIPE`の扱い。0は未取得、1は既定、2は無視、3は関数。
static INHERITED_SIGPIPE: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

/// 初期化の関数が呼ばれた回数(検査用。1回だけ呼ばれることを確かめる)。
#[cfg(test)]
static CAPTURE_CALLS: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// `main`より前に、受け継いだ`SIGPIPE`の扱いを`sigaction`で読んで控える。
/// Rustの標準ライブラリは、`main`の前(`lang_start`)に`SIGPIPE`を無視へ変えるので、受け継いだ値はこの時点でしか読めない。
/// 確保・ロック・`Rust`の入出力を使わない(標準ライブラリの初期化より前に走るため)。
extern "C" fn capture_inherited_sigpipe() {
    #[cfg(test)]
    CAPTURE_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    // SAFETY: sigactionはすべて0で有効な初期値。第2引数がnullなら設定は変えない。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: `action`は書込み可能な有効な領域。
    if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut action) } != 0 {
        return;
    }
    let code = match disposition_of(&action) {
        Disposition::Default => 1,
        Disposition::Ignore => 2,
        Disposition::Handler => 3,
    };
    INHERITED_SIGPIPE.store(code, std::sync::atomic::Ordering::Relaxed);
}

/// 初期化の関数の表への登録(Linuxは`.init_array`、Macは`__DATA,__mod_init_func`)。ローダーが`main`より前に1回だけ呼ぶ。
#[used]
#[cfg_attr(
    any(target_os = "linux", target_os = "android"),
    unsafe(link_section = ".init_array")
)]
#[cfg_attr(
    target_vendor = "apple",
    unsafe(link_section = "__DATA,__mod_init_func")
)]
static CAPTURE_INHERITED_SIGPIPE: extern "C" fn() = capture_inherited_sigpipe;

/// 起動時に控えた、受け継いだ`SIGPIPE`の扱い。控えていなければ`None`(登録の無い機種など)。
/// 登録の静的な値を読んで、その目的ファイルが必ずリンクされるようにする。
pub fn inherited_sigpipe() -> Option<Disposition> {
    std::hint::black_box(&CAPTURE_INHERITED_SIGPIPE);
    match INHERITED_SIGPIPE.load(std::sync::atomic::Ordering::Relaxed) {
        1 => Some(Disposition::Default),
        2 => Some(Disposition::Ignore),
        3 => Some(Disposition::Handler),
        _ => None,
    }
}

/// 信号を無視にする(旧版の起動時の`SIGPIPE`・`SIGXFSZ`の扱い)。
pub fn ignore_signal(number: i32) -> io::Result<()> {
    // SAFETY: sigactionはすべて0で有効な初期値。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = libc::SIG_IGN;
    // SAFETY: `action.sa_mask`は書込み可能。
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 有効な設定を渡し、旧設定は受け取らない。
    if unsafe { libc::sigaction(number, &action, std::ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub const EINTR: i32 = libc::EINTR;
pub const EPIPE: i32 = libc::EPIPE;
pub const EOPNOTSUPP: i32 = libc::EOPNOTSUPP;
pub const ENOTSUP: i32 = libc::ENOTSUP;
pub const SIGINT: i32 = libc::SIGINT;
pub const SIGPIPE: i32 = libc::SIGPIPE;
pub const SIGXFSZ: i32 = libc::SIGXFSZ;

/// 利用者データベースの1件(`pwd.getpwnam`の`pw_dir`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PasswdLookup {
    Found(Vec<u8>),
    NotFound,
}

/// `getpwnam_r`・`getpwuid_r`の共通部分。バッファが足りなければ広げて引き直す。
fn passwd_dir(
    mut call: impl FnMut(&mut libc::passwd, &mut [u8], &mut *mut libc::passwd) -> c_int,
) -> io::Result<PasswdLookup> {
    let mut size = 1024usize;
    loop {
        let mut buffer = vec![0u8; size];
        // SAFETY: passwdは、すべて0で有効な初期値(ポインタは後で書き込まれる)。
        let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
        let mut result: *mut libc::passwd = std::ptr::null_mut();
        let code = call(&mut entry, &mut buffer, &mut result);
        if code == libc::ERANGE && size < (1 << 20) {
            size *= 4;
            continue;
        }
        if code != 0 {
            return Err(io::Error::from_raw_os_error(code));
        }
        if result.is_null() || entry.pw_dir.is_null() {
            return Ok(PasswdLookup::NotFound);
        }
        // SAFETY: 成功時、pw_dirはbuffer内のNUL終端の文字列を指す。bufferはまだ生きている。
        let home = unsafe { std::ffi::CStr::from_ptr(entry.pw_dir) };
        return Ok(PasswdLookup::Found(home.to_bytes().to_vec()));
    }
}

/// `getpwnam_r`で`pw_dir`を引く。NSSの有無は実行環境に従う(U6)。
pub fn passwd_home(name: &[u8]) -> io::Result<PasswdLookup> {
    if name.contains(&0) {
        return Ok(PasswdLookup::NotFound);
    }
    let mut c_name = name.to_vec();
    c_name.push(0);
    passwd_dir(|entry, buffer, result| {
        // SAFETY: c_nameはNUL終端。entry・buffer・resultは呼出しの間有効で書込み可能。
        unsafe {
            libc::getpwnam_r(
                c_name.as_ptr().cast(),
                entry,
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                result,
            )
        }
    })
}

/// `pwd.getpwuid(os.getuid()).pw_dir`。
pub fn passwd_home_of_current_user() -> io::Result<PasswdLookup> {
    // SAFETY: 引数なしで、失敗しない。
    let uid = unsafe { libc::getuid() };
    passwd_dir(|entry, buffer, result| {
        // SAFETY: entry・buffer・resultは呼出しの間有効で書込み可能。
        unsafe { libc::getpwuid_r(uid, entry, buffer.as_mut_ptr().cast(), buffer.len(), result) }
    })
}

// ---------------------------------------------------------------- SIGINT

static SIGINT_PENDING: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sigint(_number: c_int) {
    SIGINT_PENDING.store(true, Ordering::SeqCst);
}

/// 起動時に`SIGINT`が既定の扱いのときだけ、印を立てるだけの扱いを入れる(旧版の`default_int_handler`)。
/// 無視されていれば何も変えない(ジョブ制御の無い`bash`の`cmd &`など)。入れたときだけ`true`。
/// `SA_RESTART`を付けないので、待っているシステムコールは`EINTR`で戻る。
pub fn install_sigint_handler() -> io::Result<bool> {
    if signal_disposition(SIGINT)? != Disposition::Default {
        return Ok(false);
    }
    // SAFETY: sigactionはすべて0で有効な初期値。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = on_sigint as extern "C" fn(c_int) as usize;
    action.sa_flags = 0;
    // SAFETY: `action.sa_mask`は書込み可能。
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 有効な設定を渡し、旧設定は受け取らない。ハンドラは印を立てるだけで、信号に対して安全。
    if unsafe { libc::sigaction(SIGINT, &action, std::ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(true)
}

/// `SIGINT`が来たか(旧版の`KeyboardInterrupt`の元)。
pub fn sigint_pending() -> bool {
    SIGINT_PENDING.load(Ordering::SeqCst)
}

/// `SIGINT`の印を読んで消す。`KeyboardInterrupt`を投げる側(cli)が、後始末の前に呼ぶ。
pub fn take_sigint() -> bool {
    SIGINT_PENDING.swap(false, Ordering::SeqCst)
}

/// 旧版が`KeyboardInterrupt`で終わるときと同じ終わり方: 既定の扱いに戻して自分へ`SIGINT`を送る。
/// 信号で終わらなかったときだけ、旧版と同じ終了コード`130`を返す。
pub fn die_by_sigint() -> i32 {
    let code = 128 + SIGINT;
    if signal_set_default(SIGINT).is_err() {
        return code;
    }
    // SAFETY: 自分自身へ信号を送るだけ。
    unsafe { libc::kill(libc::getpid(), SIGINT) };
    code
}

/// 信号を既定の扱いに戻す。
pub fn signal_set_default(number: i32) -> io::Result<()> {
    // SAFETY: sigactionはすべて0で有効な初期値。SIG_DFLは0。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = libc::SIG_DFL;
    // SAFETY: `action.sa_mask`は書込み可能。
    if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 有効な設定を渡し、旧設定は受け取らない。
    if unsafe { libc::sigaction(number, &action, std::ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 旧版の起動時の設定。`SIGPIPE`と`SIGXFSZ`を無視する(子は起動で既定へ戻る)。
pub fn ignore_parent_signals() -> io::Result<()> {
    ignore_signal(SIGPIPE)?;
    ignore_signal(SIGXFSZ)
}

/// 今のスレッドの識別子。旧Pythonが起動時の`Fatal Python error`に書く`Current thread 0x…`の値(`pthread_self`)。
#[allow(clippy::unnecessary_cast)]
pub fn current_thread_id() -> u64 {
    // SAFETY: `pthread_self`は引数も前提も無く、呼んだスレッドの識別子を返すだけ。
    unsafe { libc::pthread_self() as u64 }
}

/// `EINTR`なら、`SIGINT`が来ていなければ繰り返す。来ていれば`EINTR`のまま返す。
pub fn retry<T>(mut call: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    loop {
        match call() {
            Err(error) if error.raw_os_error() == Some(libc::EINTR) => {
                if sigint_pending() {
                    return Err(error);
                }
            }
            other => return other,
        }
    }
}

// ---------------------------------------------------------------- 子の起動

/// 子の標準入出力の1本。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stdio {
    /// 親のfdをそのまま受け継ぐ。
    Inherit,
    /// パイプ。親の側を`Child`が持つ(標準エラーには使えない)。
    Pipe,
    /// `/dev/null`(`subprocess.DEVNULL`)。
    DevNull,
}

/// `environ`の生の項目から、旧Python 3.10の`os.environ`(`posixmodule.c`の`convertenviron`)と同じ
/// 名前と値の組を作る。各項目を最初の`=`で分け(名前が空でもよい)、`=`の無い項目は捨て、
/// 同じ名前が重なれば最初の値と位置を残す(`PyDict_SetDefault`)。
pub fn environ_pairs(entries: &[&[u8]]) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut pairs: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for entry in entries {
        let Some(split) = entry.iter().position(|byte| *byte == b'=') else {
            continue;
        };
        let (name, value) = (&entry[..split], &entry[split + 1..]);
        if !pairs.iter().any(|(seen, _)| seen == name) {
            pairs.push((name.to_vec(), value.to_vec()));
        }
    }
    pairs
}

#[cfg(target_vendor = "apple")]
fn raw_environ() -> *const *const c_char {
    // SAFETY: `_NSGetEnviron`は、`environ`の変数の場所を返す(実行ファイルでも共有ライブラリでも使える)。
    unsafe { *libc::_NSGetEnviron() as *const *const c_char }
}

#[cfg(not(target_vendor = "apple"))]
fn raw_environ() -> *const *const c_char {
    unsafe extern "C" {
        static environ: *const *const c_char;
    }
    // SAFETY: `environ`はlibcの変数で、読むだけ。
    unsafe { environ }
}

/// 今のプロセスの`environ`を、`environ_pairs`で組にする(旧版の`dict(os.environ)`)。
/// `std::env::vars_os`は2文字目以降の最初の`=`で分け、重なった名前は後の値が勝つので使わない。
///
/// ほかのスレッドが環境を書き換えない時に呼ぶ(コマンドの開始時の前提。`std::env::set_var`と同じ制約)。
pub fn process_environ() -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut entries: Vec<&[u8]> = Vec::new();
    let table = raw_environ();
    if !table.is_null() {
        let mut at = table;
        // SAFETY: `environ`はNULで終わる、NUL終端の文字列へのポインタの並び。読む間、書き換えない前提。
        unsafe {
            while !(*at).is_null() {
                entries.push(std::ffi::CStr::from_ptr(*at).to_bytes());
                at = at.add(1);
            }
        }
    }
    environ_pairs(&entries)
}

/// 旧Python 3.10が起動時に(PEP 538で)足す`LC_CTYPE`を、プロセスの環境へ同じく足す。足したら`true`。
/// 規則と注意は`restore_taskmd::coerce_c_locale`と同じ(R4Rと同じ形。環境変数だけで決まる場合の再現)。
/// `process_environ`の前に、部品の開始時に1回だけ呼ぶ。
pub fn coerce_c_locale() -> bool {
    crate::restore_taskmd::coerce_c_locale()
}

pub struct SpawnSpec<'a> {
    /// `args`。`argv[0]`が実行ファイル(`/`を含まなければPATHで探す)。
    pub argv: &'a [Vec<u8>],
    /// 子の環境変数の全集合。PATHの探索にも、この集合のPATHを使う。
    pub env: &'a [(Vec<u8>, Vec<u8>)],
    pub cwd: Option<&'a [u8]>,
    pub stdin: Stdio,
    pub stdout: Stdio,
    pub stderr: Stdio,
}

/// 起動の失敗の、旧版が例外に付けるファイル名の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpawnTarget {
    /// ファイル名を付けない(パイプ・`fork`)。
    None,
    /// `exec`の失敗: `argv[0]`の字面。
    Executable,
    /// `exec`の前の失敗(`dup2`・`chdir`・信号の設定など): `cwd`の字面。旧Python 3.10.12は、`exec`が呼ばれる前の
    /// 失敗をすべて`noexec`として報告し、`cwd`を指定していればそれをファイル名にする(測った値)。
    /// `cwd`が無ければファイル名を付けない。呼び出し側は`SpawnError::filename`で決める。
    Cwd,
    /// `/dev/null`を開けない。
    DevNull,
}

#[derive(Debug)]
pub enum SpawnError {
    Os {
        errno: i32,
        target: SpawnTarget,
    },
    /// `argv`・`env`・`cwd`にNULがある(旧版の`ValueError: embedded null byte`)。
    EmbeddedNul,
    /// `argv`が空(旧版の`IndexError`)。
    EmptyArgv,
    /// 環境変数の名前に`=`がある(旧版の`ValueError: illegal environment variable name`)。
    IllegalEnvName,
}

impl SpawnError {
    /// 旧版の`OSError.filename`: `Executable`は`argv[0]`、`Cwd`は指定した`cwd`(無ければ無し)。
    pub fn filename<'a>(&self, argv0: &'a [u8], cwd: Option<&'a [u8]>) -> Option<&'a [u8]> {
        match self {
            SpawnError::Os { target, .. } => match target {
                SpawnTarget::Executable => Some(argv0),
                SpawnTarget::Cwd => cwd,
                SpawnTarget::DevNull => Some(b"/dev/null"),
                SpawnTarget::None => None,
            },
            _ => None,
        }
    }
}

fn os_error(error: &io::Error, target: SpawnTarget) -> SpawnError {
    SpawnError::Os {
        errno: error.raw_os_error().unwrap_or(libc::EIO),
        target,
    }
}

/// 旧版の`os.defpath`。
pub const DEFPATH: &[u8] = b"/bin:/usr/bin";

/// 旧版の探し方: `/`を含めばそのまま、含まなければ`os.get_exec_path(env)`の各要素と`os.path.join`で作る。
/// 空の要素は`join('', 'git')`で`git`(作業場所からの相対)になる。
pub fn executable_candidates(executable: &[u8], env: &[(Vec<u8>, Vec<u8>)]) -> Vec<Vec<u8>> {
    if executable.contains(&b'/') {
        return vec![executable.to_vec()];
    }
    let path = env
        .iter()
        .rev()
        .find(|(key, _)| key == b"PATH")
        .map_or(DEFPATH, |(_, value)| value.as_slice());
    path.split(|&byte| byte == b':')
        .map(|dir| {
            let mut joined = dir.to_vec();
            if !joined.is_empty() && !joined.ends_with(b"/") {
                joined.push(b'/');
            }
            joined.extend_from_slice(executable);
            joined
        })
        .collect()
}

fn c_string(bytes: &[u8]) -> Result<CString, SpawnError> {
    CString::new(bytes).map_err(|_| SpawnError::EmbeddedNul)
}

fn pointers(strings: &[CString]) -> Vec<*const c_char> {
    let mut list: Vec<*const c_char> = strings.iter().map(|text| text.as_ptr()).collect();
    list.push(std::ptr::null());
    list
}

/// 子で`exec`の前に見る材料。`fork`の前にすべて用意する(子では確保しない)。
struct ChildPlan {
    executables: Vec<*const c_char>,
    argv: Vec<*const c_char>,
    envp: Vec<*const c_char>,
    cwd: *const c_char,
    stdin: c_int,
    stdout: c_int,
    stderr: c_int,
    close_in_child: [c_int; 4],
    /// 親で列挙した、3以上で開いているfd(`close_range`が使えないときだけ、子が閉じる)。列挙できなければ`None`。
    enumerated: Option<Vec<c_int>>,
    /// `sysconf(_SC_OPEN_MAX)`(取れなければ256)。列挙できないときの上限。
    open_max: c_int,
    /// 偽ならclose_rangeを使わない(単体テストが代わりの経路を通すため。製品では常に真)。
    use_close_range: bool,
    errpipe_write: c_int,
}

const STAGE_PREEXEC: u8 = 1;
const STAGE_EXEC: u8 = 2;

/// 子の中。`async-signal-safe`な呼出しだけを使う(確保・ロック・`Rust`の入出力を使わない)。
///
/// # Safety
/// `fork`の直後の子でだけ呼ぶ。`plan`の指す先はすべて`fork`の前に用意した有効な文字列と配列。
unsafe fn child_main(plan: &ChildPlan) -> ! {
    let mut stage = STAGE_PREEXEC;
    let mut error = 0;
    // SAFETY: すべて、有効なfdか定数での、`async-signal-safe`なシステムコール。
    unsafe {
        'run: {
            for fd in plan.close_in_child {
                if fd >= 0 {
                    libc::close(fd);
                }
            }
            let (mut write_out, mut write_err) = (plan.stdout, plan.stderr);
            // 0〜2が上書きされる場合は、先に別の番号へ退避する(CPythonの issue 12607・32270)。
            if write_out == 0 {
                write_out = libc::dup(write_out);
                if write_out < 0 || set_cloexec(write_out) < 0 {
                    error = last_errno();
                    break 'run;
                }
            }
            while write_err == 0 || write_err == 1 {
                write_err = libc::dup(write_err);
                if write_err < 0 || set_cloexec(write_err) < 0 {
                    error = last_errno();
                    break 'run;
                }
            }
            for (from, to) in [(plan.stdin, 0), (write_out, 1), (write_err, 2)] {
                if from == to {
                    if clear_cloexec(to) < 0 {
                        error = last_errno();
                        break 'run;
                    }
                } else if from != -1 && libc::dup2(from, to) < 0 {
                    error = last_errno();
                    break 'run;
                }
            }
            if !plan.cwd.is_null() && libc::chdir(plan.cwd) < 0 {
                error = last_errno();
                break 'run;
            }
            // 子で`SIGPIPE`・`SIGXFSZ`を既定へ戻す(`restore_signals=True`)。信号のmaskは受け継ぐ。
            for number in [libc::SIGPIPE, libc::SIGXFSZ] {
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = libc::SIG_DFL;
                libc::sigemptyset(&mut action.sa_mask);
                if libc::sigaction(number, &action, std::ptr::null_mut()) < 0 {
                    error = last_errno();
                    break 'run;
                }
            }
            close_other_fds(
                plan.errpipe_write,
                plan.enumerated.as_deref(),
                plan.open_max,
                plan.use_close_range,
            );
            stage = STAGE_EXEC;
            let mut first = 0;
            for &executable in &plan.executables {
                if executable.is_null() {
                    break;
                }
                libc::execve(executable, plan.argv.as_ptr(), plan.envp.as_ptr());
                let now = last_errno();
                if now != libc::ENOENT && now != libc::ENOTDIR && first == 0 {
                    first = now;
                }
                error = now;
            }
            // 最初のENOENT・ENOTDIR以外の誤りを返す。無ければ最後の誤り。
            if first != 0 {
                error = first;
            }
        }
        let mut report = [0u8; 5];
        report[0] = stage;
        report[1..].copy_from_slice(&error.to_le_bytes());
        let mut written = 0;
        while written < report.len() {
            let count = libc::write(
                plan.errpipe_write,
                report[written..].as_ptr().cast(),
                report.len() - written,
            );
            if count < 0 {
                if last_errno() == libc::EINTR {
                    continue;
                }
                break;
            }
            written += count as usize;
        }
        libc::_exit(255)
    }
}

/// # Safety
/// 有効でも無効でもよいfdを渡せる(失敗は戻り値)。
unsafe fn set_cloexec(fd: c_int) -> c_int {
    // SAFETY: fcntlは番号だけを見る。
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFD);
        if flags < 0 {
            return -1;
        }
        libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC)
    }
}

/// # Safety
/// 有効でも無効でもよいfdを渡せる(失敗は戻り値)。
unsafe fn clear_cloexec(fd: c_int) -> c_int {
    // SAFETY: fcntlは番号だけを見る。
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFD);
        if flags < 0 {
            return -1;
        }
        libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC)
    }
}

#[cfg(target_os = "linux")]
fn last_errno() -> c_int {
    // SAFETY: errnoの場所はスレッドごとで、読むだけ。
    unsafe { *libc::__errno_location() }
}

#[cfg(not(target_os = "linux"))]
fn last_errno() -> c_int {
    // SAFETY: errnoの場所はスレッドごとで、読むだけ。
    unsafe { *libc::__error() }
}

/// 親で開いているfdを列挙する(Linuxは`/proc/self/fd`、Macは`/dev/fd`。CPythonの`_close_open_fds`と同じ)。
/// 列挙に使ったフォルダのfdも入る(子では`EBADF`になり、無視する)。R4Rの`enumerate_open_fds`と同じ形。
fn enumerate_open_fds() -> io::Result<Vec<c_int>> {
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
            .and_then(|name| name.parse::<c_int>().ok())
            && fd >= 3
        {
            fds.push(fd);
        }
    }
    Ok(fds)
}

#[cfg(test)]
pub(crate) static FORCE_NO_CLOSE_RANGE: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
pub(crate) static FORCE_NO_ENUMERATION: AtomicBool = AtomicBool::new(false);

fn close_range_allowed() -> bool {
    #[cfg(test)]
    if FORCE_NO_CLOSE_RANGE.load(Ordering::SeqCst) {
        return false;
    }
    true
}

fn enumerated_fds() -> Option<Vec<c_int>> {
    #[cfg(test)]
    if FORCE_NO_ENUMERATION.load(Ordering::SeqCst) {
        return None;
    }
    enumerate_open_fds().ok()
}

/// CPythonの`safe_get_max_fd`(`sysconf(_SC_OPEN_MAX)`、取れなければ256)。親で呼ぶ。
fn open_max() -> c_int {
    // SAFETY: sysconfは整数だけを使う。
    let value = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if value < 0 {
        256
    } else {
        c_int::try_from(value).unwrap_or(c_int::MAX)
    }
}

/// 0〜2と`keep`以外のfdを閉じる(`close_fds=True`・`pass_fds=()`)。
/// 順は`close_range` → 親で列挙したfd → 上限までの番号。上限での打ち切りは設けない
/// (列挙できない環境で`sysconf`の上限まで順に閉じる。`RLIMIT_NOFILE`が極端に大きいと遅いが、CPythonも同じ)。
///
/// # Safety
/// `fork`の直後の子でだけ呼ぶ。`close`だけを呼び、確保しない(`enumerated`・`open_max`は親で用意した値)。
unsafe fn close_other_fds(
    keep: c_int,
    enumerated: Option<&[c_int]>,
    open_max: c_int,
    use_close_range: bool,
) {
    #[cfg(target_os = "linux")]
    if use_close_range {
        let close_range = |low: c_int, high: c_int| -> bool {
            // SAFETY: close_rangeはfd番号の範囲だけを見るシステムコール。
            unsafe { libc::syscall(libc::SYS_close_range, low as u32, high as u32, 0u32) == 0 }
        };
        let ok_low = keep <= 3 || close_range(3, (keep - 1) as c_int);
        let ok_high = close_range(if keep < 3 { 3 } else { keep + 1 }, c_int::MAX);
        if ok_low && ok_high {
            return;
        }
    }
    let _ = use_close_range;
    let close_one = |fd: c_int| {
        if fd >= 3 && fd != keep {
            // SAFETY: 閉じるだけ。失敗(列挙に使ったfdの`EBADF`など)は無視する。
            unsafe { libc::close(fd) };
        }
    };
    match enumerated {
        Some(fds) => fds.iter().copied().for_each(close_one),
        None => (3..=open_max).for_each(close_one),
    }
}

/// 起動した子。パイプの親の側と、旧版の`Popen`と同じ`returncode`を持つ。
#[derive(Debug)]
pub struct Child {
    pid: i32,
    pub stdin: Option<std::os::fd::OwnedFd>,
    pub stdout: Option<std::os::fd::OwnedFd>,
    returncode: Option<i32>,
}

fn status_to_code(status: c_int) -> i32 {
    if libc::WIFEXITED(status) {
        libc::WEXITSTATUS(status)
    } else if libc::WIFSIGNALED(status) {
        -libc::WTERMSIG(status)
    } else {
        // `waitstatus_to_exitcode`は止まった状態を受けない。`WUNTRACED`を使わないので来ない。
        -1
    }
}

/// 旧版の`Popen(...)`。標準エラーは`Inherit`か`DevNull`だけを受ける。
pub fn spawn(spec: &SpawnSpec<'_>) -> Result<Child, SpawnError> {
    let Some(first) = spec.argv.first() else {
        return Err(SpawnError::EmptyArgv);
    };
    if spec.stderr == Stdio::Pipe {
        return Err(SpawnError::Os {
            errno: libc::EINVAL,
            target: SpawnTarget::None,
        });
    }
    // 旧版は、環境変数の一覧を作る時点(`fork_exec`の前)で名前の`=`を確かめる。
    if spec.env.iter().any(|(key, _)| key.contains(&b'=')) {
        return Err(SpawnError::IllegalEnvName);
    }
    let (child_stdin, parent_stdin) = pipe_if(spec.stdin)?;
    let (parent_stdout, child_stdout) = pipe_if(spec.stdout)?;
    // 旧版の`_get_devnull`: 親で開き、子で2へ複製する。
    let devnull = if [spec.stdin, spec.stdout, spec.stderr].contains(&Stdio::DevNull) {
        Some(
            fs::open("/dev/null", OFlags::RDWR | OFlags::CLOEXEC, Mode::empty())
                .map_err(|code| os_error(&errno(code), SpawnTarget::DevNull))?,
        )
    } else {
        None
    };
    let pick = |mode: Stdio, pipe: &Option<std::os::fd::OwnedFd>| -> c_int {
        match mode {
            Stdio::Inherit => -1,
            Stdio::Pipe => pipe.as_ref().map_or(-1, |fd| fd.as_raw_fd()),
            Stdio::DevNull => devnull.as_ref().map_or(-1, |fd| fd.as_raw_fd()),
        }
    };
    let candidates = executable_candidates(first, spec.env);
    let executables: Vec<CString> = candidates
        .iter()
        .map(|text| c_string(text))
        .collect::<Result<_, _>>()?;
    let argv: Vec<CString> = spec
        .argv
        .iter()
        .map(|text| c_string(text))
        .collect::<Result<_, _>>()?;
    let mut env_strings = Vec::with_capacity(spec.env.len());
    for (key, value) in spec.env {
        let mut item = key.clone();
        item.push(b'=');
        item.extend_from_slice(value);
        env_strings.push(c_string(&item)?);
    }
    let cwd = spec.cwd.map(c_string).transpose()?;
    let (error_read, error_write) = std::io::pipe()
        .map(|(reader, writer)| {
            (
                std::os::fd::OwnedFd::from(reader),
                std::os::fd::OwnedFd::from(writer),
            )
        })
        .map_err(|error| os_error(&error, SpawnTarget::None))?;
    // 子では`sysconf`も列挙もしない。親で材料を用意する(列挙できなければ上限で閉じる)。
    let plan = ChildPlan {
        executables: pointers(&executables),
        argv: pointers(&argv),
        envp: pointers(&env_strings),
        cwd: cwd.as_ref().map_or(std::ptr::null(), |text| text.as_ptr()),
        stdin: pick(spec.stdin, &child_stdin),
        stdout: pick(spec.stdout, &child_stdout),
        stderr: pick(spec.stderr, &None),
        close_in_child: [
            parent_stdin.as_ref().map_or(-1, |fd| fd.as_raw_fd()),
            parent_stdout.as_ref().map_or(-1, |fd| fd.as_raw_fd()),
            error_read.as_raw_fd(),
            -1,
        ],
        enumerated: enumerated_fds(),
        open_max: open_max(),
        use_close_range: close_range_allowed(),
        errpipe_write: error_write.as_raw_fd(),
    };
    // SAFETY: `fork`の後の子は`child_main`だけを動かし、`exec`か`_exit`で終わる。
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(os_error(&io::Error::last_os_error(), SpawnTarget::None));
    }
    if pid == 0 {
        // SAFETY: forkの直後の子。planはfork前に用意した有効な領域を指す。
        unsafe { child_main(&plan) }
    }
    drop(error_write);
    drop(child_stdin);
    drop(child_stdout);
    drop(devnull);
    // 旧版は、errpipeが閉じる(execの成功)か50000バイトを超えるまで読む。読取りの誤りは、旧版の`Popen`と同じく
    // そのまま投げ、`Child`を作らない(`SIGINT`なら`EINTR`で、呼び出し側が`KeyboardInterrupt`にする)。
    // このとき子は`kill`も`wait`もしない(旧版の`_execute_child`は例外で抜けるだけ)。
    let mut data = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match retry(|| read(error_read.as_fd(), &mut chunk)) {
            Ok(0) => break,
            Ok(count) => {
                data.extend_from_slice(&chunk[..count]);
                if data.len() > 50000 {
                    break;
                }
            }
            Err(error) => return Err(os_error(&error, SpawnTarget::None)),
        }
    }
    if !data.is_empty() && data.len() < 5 {
        return Err(SpawnError::Os {
            errno: libc::EIO,
            target: SpawnTarget::None,
        });
    }
    if data.len() >= 5 {
        // 失敗した子を回収する(旧版は`ChildProcessError`を無視する)。
        let mut status = 0;
        // SAFETY: statusは書込み可能。
        while unsafe { libc::waitpid(pid, &mut status, 0) } < 0 && last_errno() == libc::EINTR {}
        let code = i32::from_le_bytes([data[1], data[2], data[3], data[4]]);
        let target = if data[0] == STAGE_EXEC {
            SpawnTarget::Executable
        } else {
            SpawnTarget::Cwd
        };
        return Err(SpawnError::Os {
            errno: code,
            target,
        });
    }
    Ok(Child {
        pid,
        stdin: parent_stdin,
        stdout: parent_stdout,
        returncode: None,
    })
}

type FdPair = (Option<std::os::fd::OwnedFd>, Option<std::os::fd::OwnedFd>);

/// `Pipe`なら`os.pipe()`(読む側・書く側の順)。両端とも`O_CLOEXEC`。
fn pipe_if(mode: Stdio) -> Result<FdPair, SpawnError> {
    if mode != Stdio::Pipe {
        return Ok((None, None));
    }
    let (reader, writer) = std::io::pipe().map_err(|error| os_error(&error, SpawnTarget::None))?;
    Ok((
        Some(std::os::fd::OwnedFd::from(reader)),
        Some(std::os::fd::OwnedFd::from(writer)),
    ))
}

impl Child {
    pub fn pid(&self) -> i32 {
        self.pid
    }

    pub fn returncode(&self) -> Option<i32> {
        self.returncode
    }

    fn try_wait(&mut self, flags: c_int) -> io::Result<Option<i32>> {
        let mut status = 0;
        // SAFETY: statusは書込み可能。
        let reaped = unsafe { libc::waitpid(self.pid, &mut status, flags) };
        if reaped < 0 {
            let error = io::Error::last_os_error();
            // SIGCHLDを無視していると、終わった子は自動で回収され、`ECHILD`になる。旧版は状態0とする。
            if error.raw_os_error() == Some(libc::ECHILD) {
                self.returncode = Some(0);
                return Ok(Some(0));
            }
            return Err(error);
        }
        if reaped == self.pid {
            let code = status_to_code(status);
            self.returncode = Some(code);
            return Ok(Some(code));
        }
        Ok(None)
    }

    /// `Popen.poll()`。`ECHILD`以外の誤りは捨てて`None`(旧版と同じ)。
    pub fn poll(&mut self) -> Option<i32> {
        if self.returncode.is_none() {
            let _ = self.try_wait(libc::WNOHANG);
        }
        self.returncode
    }

    /// `Popen.wait()`。`SIGINT`が来ていれば`EINTR`を返す。
    pub fn wait(&mut self) -> io::Result<i32> {
        loop {
            if let Some(code) = self.returncode {
                return Ok(code);
            }
            if sigint_pending() {
                return Err(io::Error::from_raw_os_error(libc::EINTR));
            }
            match self.try_wait(0) {
                Ok(Some(code)) => return Ok(code),
                Ok(None) => {}
                Err(error) if error.raw_os_error() == Some(libc::EINTR) => {
                    if sigint_pending() {
                        return Err(error);
                    }
                }
                Err(error) => return Err(error),
            }
        }
    }

    /// `Popen.wait(timeout)`(3.10)。時間内に終わらなければ`Ok(None)`(`TimeoutExpired`)。
    /// 待ち方も旧版と同じ(初回の確認→0.5msの倍々で最大50msの休み)。
    pub fn wait_timeout(&mut self, timeout: Duration) -> io::Result<Option<i32>> {
        let end = Instant::now() + timeout;
        let mut delay = Duration::from_micros(500);
        loop {
            if let Some(code) = self.returncode {
                return Ok(Some(code));
            }
            if let Some(code) = self.try_wait(libc::WNOHANG)? {
                return Ok(Some(code));
            }
            if sigint_pending() {
                return Err(io::Error::from_raw_os_error(libc::EINTR));
            }
            let remaining = end.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(None);
            }
            delay = (delay * 2).min(remaining).min(Duration::from_millis(50));
            std::thread::sleep(delay);
        }
    }

    /// `Popen.kill()`: 先に`poll`し、生きていれば`SIGKILL`。`ESRCH`は無視する。
    pub fn kill(&mut self) -> io::Result<()> {
        if self.poll().is_some() {
            return Ok(());
        }
        // SAFETY: 自分の子のpidへ信号を送るだけ。
        if unsafe { libc::kill(self.pid, libc::SIGKILL) } != 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                return Err(error);
            }
        }
        Ok(())
    }

    /// 旧版の`finally`の後始末: パイプを閉じる→生きていれば`kill`→`wait`。
    pub fn finish(&mut self) -> io::Result<i32> {
        drop(self.stdin.take());
        drop(self.stdout.take());
        if self.poll().is_none() {
            self.kill()?;
        }
        self.wait()
    }

    /// 標準入力のパイプの親の側を閉じる(旧版の`proc.stdin.close()`)。
    pub fn close_stdin(&mut self) {
        drop(self.stdin.take());
    }

    pub fn close_stdout(&mut self) {
        drop(self.stdout.take());
    }
}

/// `os.set_blocking(fd, False)`。
pub fn set_nonblocking(fd: BorrowedFd<'_>) -> io::Result<()> {
    // SAFETY: fcntlはfdの番号だけを見る。
    let flags = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: 同上。
    if unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::AsFd;

    /// 一意の名前で`create_dir`(既にあれば次の名前)して作ったフォルダ。作ったパスだけを呼び出し側が消す。
    pub(super) fn unique_dir(label: &str) -> std::path::PathBuf {
        let base = std::env::var_os("S2E_TEST_TMP")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        // `parent`と同じくリンクを辿らない使い方があるので、実パスで使う。
        let base = std::fs::canonicalize(base).unwrap();
        for count in 0u32.. {
            let path = base.join(format!(
                "s2e-platform-{}-{label}-{count}",
                std::process::id()
            ));
            match std::fs::create_dir(&path) {
                Ok(()) => return path,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("{}: {error}", path.display()),
            }
        }
        unreachable!()
    }

    fn open_dir_path(path: &std::path::Path) -> OwnedFd {
        fs::open(
            path,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .unwrap()
    }

    #[test]
    fn create_excl_fchmod_fsync_and_stat() {
        let dir = unique_dir("create");
        let root = open_dir_path(&dir);
        let fd = create_excl_at(root.as_fd(), b"a", 0o600).unwrap();
        write(fd.as_fd(), b"xyz").unwrap();
        fchmod(fd.as_fd(), 0o444).unwrap();
        fsync(fd.as_fd()).unwrap();
        let stat = lstat_at(root.as_fd(), b"a").unwrap();
        assert_eq!(stat.kind(), S_IFREG);
        assert_eq!(stat.mode & 0o777, 0o444);
        assert_eq!(stat.size, 3);
        assert_eq!(
            create_excl_at(root.as_fd(), b"a", 0o600)
                .unwrap_err()
                .raw_os_error(),
            Some(libc::EEXIST)
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn nofollow_open_and_readlink_and_chmod() {
        let dir = unique_dir("nofollow");
        std::os::unix::fs::symlink("target", dir.join("l")).unwrap();
        let root = open_dir_path(&dir);
        assert_eq!(readlink_at(root.as_fd(), b"l").unwrap(), b"target");
        assert_eq!(
            open_read_at(root.as_fd(), b"l").unwrap_err().raw_os_error(),
            Some(libc::ELOOP)
        );
        assert_eq!(lstat_at(root.as_fd(), b"l").unwrap().kind(), S_IFLNK);
        mkdir_at(root.as_fd(), b"d", 0o700).unwrap();
        chmod_nofollow_at(root.as_fd(), b"d", 0o750).unwrap();
        assert_eq!(lstat_at(root.as_fd(), b"d").unwrap().mode & 0o777, 0o750);
        // リンクは辿らない。glibcはEOPNOTSUPP、成功する環境でも対象はリンク自身だけ。
        let _ = chmod_nofollow_at(root.as_fd(), b"l", 0o600);
        assert_eq!(lstat_at(root.as_fd(), b"d").unwrap().kind(), S_IFDIR);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    fn scan_names(fd: BorrowedFd<'_>) -> io::Result<Vec<Vec<u8>>> {
        let mut names = Vec::new();
        match scan_dir::<()>(fd, |name| {
            names.push(name.to_vec());
            Ok(())
        }) {
            Ok(()) => Ok(names),
            Err(ScanError::Os(error) | ScanError::Dup(error)) => Err(error),
            Err(ScanError::Callback(())) => unreachable!(),
        }
    }

    #[test]
    fn scan_dir_skips_dots_visits_one_by_one_and_rewinds() {
        let dir = unique_dir("scan");
        for name in ["a", "b", "c"] {
            std::fs::write(dir.join(name), b"").unwrap();
        }
        let root = open_dir_path(&dir);
        let mut names = scan_names(root.as_fd()).unwrap();
        names.sort();
        assert_eq!(names, vec![b"a".to_vec(), b"b".to_vec(), b"c".to_vec()]);
        // 1項目ごとに呼び、呼び出し側が止めればそこで読むのをやめる。止めた後も先頭へ戻す。
        let mut seen = 0;
        let result = scan_dir(root.as_fd(), |_| {
            seen += 1;
            Err("上限")
        });
        assert!(
            matches!(result, Err(ScanError::Callback("上限"))),
            "{result:?}"
        );
        assert_eq!(seen, 1);
        assert_eq!(scan_names(root.as_fd()).unwrap().len(), 3);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// 旧版の`os.scandir(fd)`は、fdを複製して読むので、読み取り権限だけのフォルダも読める
    /// (Python 3.10.12は`[]`と中身を返した)。`openat(fd, ".")`の開き直しは検索権限が要るので使わない。
    #[test]
    fn scan_dir_reads_a_folder_with_only_read_permission() {
        use std::os::unix::fs::PermissionsExt;
        if unsafe { libc::geteuid() } == 0 {
            eprintln!("root は権限を無視するので省く");
            return;
        }
        let dir = unique_dir("scan-readonly");
        std::fs::create_dir(dir.join("empty")).unwrap();
        std::fs::create_dir(dir.join("full")).unwrap();
        std::fs::write(dir.join("full/x"), b"").unwrap();
        for name in ["empty", "full"] {
            std::fs::set_permissions(dir.join(name), std::fs::Permissions::from_mode(0o444))
                .unwrap();
        }
        let parent = open_dir_path(&dir);
        let empty = open_dir_at(parent.as_fd(), b"empty").unwrap();
        assert_eq!(scan_names(empty.as_fd()).unwrap(), Vec::<Vec<u8>>::new());
        let full = open_dir_at(parent.as_fd(), b"full").unwrap();
        assert_eq!(scan_names(full.as_fd()).unwrap(), vec![b"x".to_vec()]);
        for name in ["empty", "full"] {
            std::fs::set_permissions(dir.join(name), std::fs::Permissions::from_mode(0o755))
                .unwrap();
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn long_waits_are_split_to_fit_a_c_int_and_mac_limit_is_explicit() {
        let max = i64::from(i32::MAX);
        assert_eq!(poll_slices(0), vec![0]);
        assert_eq!(poll_slices(5), vec![5]);
        assert_eq!(poll_slices(max), vec![max]);
        assert_eq!(poll_slices(max + 1), vec![max, 1]);
        let month = 86_400_000i64 * 30;
        let slices = poll_slices(month);
        assert_eq!(slices.iter().sum::<i64>(), month);
        assert!(slices.iter().all(|&slice| slice <= max));
        // 機種ごとの上限: Macの`select`は1e8秒を超えると`EINVAL`。Linuxでは(ここでは)上限なし。
        let limit_ms = MAC_SELECT_MAX_SECONDS * 1000;
        assert!(!select_timeout_too_large(limit_ms + 999, true));
        assert!(select_timeout_too_large(limit_ms + 1000, true));
        assert!(!select_timeout_too_large(limit_ms + 1000, false));
        assert!(!select_timeout_too_large(-1, true));
        #[cfg(target_os = "macos")]
        {
            let (reader, _writer) = std::io::pipe().unwrap();
            assert_eq!(
                wait_readable(reader.as_fd(), limit_ms + 1000)
                    .unwrap_err()
                    .raw_os_error(),
                Some(libc::EINVAL)
            );
        }
    }

    #[test]
    fn wait_readable_times_out_and_sees_data() {
        let (reader, writer) = std::io::pipe().unwrap();
        assert!(!wait_readable(reader.as_fd(), 10).unwrap());
        write(writer.as_fd(), b"go\n").unwrap();
        assert!(wait_readable(reader.as_fd(), 10).unwrap());
    }

    #[test]
    fn poll_fds_reports_each_descriptor_in_order() {
        let (reader, writer) = std::io::pipe().unwrap();
        let (reader2, writer2) = std::io::pipe().unwrap();
        let read = Interest {
            read: true,
            write: false,
        };
        let write_side = Interest {
            read: false,
            write: true,
        };
        // 何も届いていない読み側は待ちきり、書き側は空いているので、すぐ「事象あり」。
        let ready = poll_fds(&[(reader.as_fd(), read), (writer2.as_fd(), write_side)], 10).unwrap();
        assert_eq!(ready, vec![false, true]);
        write(writer.as_fd(), b"x").unwrap();
        let ready = poll_fds(&[(reader.as_fd(), read), (reader2.as_fd(), read)], 10).unwrap();
        assert_eq!(ready, vec![true, false]);
        // 時間切れ: 何も起きなければ、すべて `false`。
        let started = Instant::now();
        let ready = poll_fds(&[(reader2.as_fd(), read)], 50).unwrap();
        assert_eq!(ready, vec![false]);
        assert!(started.elapsed() >= Duration::from_millis(40));
        // 書き側を閉じると、読み側は切断で「事象あり」。読み側を閉じると、書き側は誤りで「事象あり」。
        drop(writer2);
        assert_eq!(
            poll_fds(&[(reader2.as_fd(), read)], 1000).unwrap(),
            vec![true]
        );
        drop(reader);
        assert_eq!(
            poll_fds(&[(writer.as_fd(), write_side)], 1000).unwrap(),
            vec![true]
        );
        assert!(poll_fds(&[], 1).unwrap().is_empty());
    }

    #[test]
    fn the_open_file_limit_can_be_raised_but_not_above_the_hard_limit() {
        let mut limit = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: `limit`は書込み可能な有効な領域。
        assert_eq!(
            unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
            0
        );
        let want = 4096;
        let got = raise_open_file_limit(want).unwrap();
        let expect = if limit.rlim_max == libc::RLIM_INFINITY {
            want.max(limit.rlim_cur)
        } else {
            want.min(limit.rlim_max).max(limit.rlim_cur)
        };
        assert_eq!(got, expect);
        // 2回目は、もう上がっているので何も変えない。
        assert_eq!(raise_open_file_limit(want).unwrap(), got);
        assert_eq!(raise_open_file_limit(1).unwrap(), got);
    }

    #[test]
    fn passwd_root_and_missing() {
        match passwd_home(b"root").unwrap() {
            PasswdLookup::Found(home) => assert!(!home.is_empty()),
            PasswdLookup::NotFound => panic!("root が引けない"),
        }
        assert_eq!(
            passwd_home(b"no-such-user-s2e").unwrap(),
            PasswdLookup::NotFound
        );
        assert_eq!(passwd_home(b"a\0b").unwrap(), PasswdLookup::NotFound);
    }
}

/// 子の起動・信号・後始末の単体テスト。固定期待は旧Python 3.10から作った
/// `native/ci/s2e_fixtures/child_cases.json`。信号の扱いをプロセス全体で変える検査は、
/// 同じテスト実行ファイルを`--exact`で起動し直した別プロセスの中で行う。
#[cfg(test)]
mod child_tests {
    use super::*;
    use serde_json::Value;
    use std::os::fd::AsFd;
    use std::path::{Path, PathBuf};

    fn cases() -> Value {
        serde_json::from_str(include_str!("../../../ci/s2e_fixtures/child_cases.json"))
            .expect("child_cases.json が JSON でない")
    }

    fn scratch(label: &str) -> PathBuf {
        super::tests::unique_dir(label)
    }

    fn bytes(value: &Value) -> Vec<u8> {
        value.as_str().unwrap().as_bytes().to_vec()
    }

    fn pairs(value: &Value) -> Vec<(Vec<u8>, Vec<u8>)> {
        value
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| (bytes(&pair[0]), bytes(&pair[1])))
            .collect()
    }

    fn libc_errno(name: &str) -> i32 {
        match name {
            "ENOENT" => libc::ENOENT,
            "EACCES" => libc::EACCES,
            "ENOTDIR" => libc::ENOTDIR,
            "ENOEXEC" => libc::ENOEXEC,
            other => panic!("未対応の errno: {other}"),
        }
    }

    /// 子を起動し、標準出力を読み切り、終了を待つ。旧版の`_child_outcome`と同じ形にする。
    fn run(
        argv: &[Vec<u8>],
        env: &[(Vec<u8>, Vec<u8>)],
        cwd: Option<&[u8]>,
        stdin_data: &[u8],
    ) -> Result<(Vec<u8>, i32), SpawnError> {
        let mut child = spawn(&SpawnSpec {
            argv,
            env,
            cwd,
            stdin: Stdio::Pipe,
            stdout: Stdio::Pipe,
            stderr: Stdio::DevNull,
        })?;
        let stdin = child.stdin.take().unwrap();
        let mut offset = 0;
        while offset < stdin_data.len() {
            match write(stdin.as_fd(), &stdin_data[offset..]) {
                Ok(count) => offset += count,
                Err(error) if error.raw_os_error() == Some(libc::EPIPE) => break,
                Err(error) => panic!("{error}"),
            }
        }
        drop(stdin);
        let stdout = child.stdout.take().unwrap();
        let mut output = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            let count = read(stdout.as_fd(), &mut chunk).unwrap();
            if count == 0 {
                break;
            }
            output.extend_from_slice(&chunk[..count]);
        }
        Ok((output, child.wait().unwrap()))
    }

    fn assert_outcome(name: &str, expect: &Value, got: Result<(Vec<u8>, i32), SpawnError>) {
        match (expect.get("error"), got) {
            (None, Ok((out, code))) => {
                assert_eq!(out, bytes(&expect["stdout"]), "{name}");
                assert_eq!(
                    i64::from(code),
                    expect["returncode"].as_i64().unwrap(),
                    "{name}"
                );
            }
            (Some(error), Err(SpawnError::Os { errno, target })) => {
                assert_eq!(errno, libc_errno(error.as_str().unwrap()), "{name}");
                let want = match expect["target"].as_str().unwrap() {
                    "executable" => SpawnTarget::Executable,
                    "cwd" => SpawnTarget::Cwd,
                    "none" => SpawnTarget::None,
                    other => panic!("{other}"),
                };
                assert_eq!(target, want, "{name}");
            }
            (want, got) => panic!("{name}: 期待 {want:?} / 実際 {got:?}"),
        }
    }

    fn make_layout(root: &Path, entries: &Value) {
        use std::os::unix::fs::{PermissionsExt, symlink};
        for entry in entries.as_array().unwrap() {
            let path = root.join(entry["path"].as_str().unwrap());
            let kind = entry["kind"].as_str().unwrap();
            if kind == "dir" {
                std::fs::create_dir_all(&path).unwrap();
                continue;
            }
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            if kind == "dangling" {
                symlink("nowhere", &path).unwrap();
                continue;
            }
            let tag = entry["tag"].as_str().unwrap_or("x");
            let body = match kind {
                "exec" | "noexec" => format!("#!/bin/sh\necho {tag}\n"),
                "shebangless" => format!("echo {tag}\n"),
                "file" => "plain\n".to_owned(),
                other => panic!("{other}"),
            };
            std::fs::write(&path, body).unwrap();
            let mode = if matches!(kind, "noexec" | "file") {
                0o644
            } else {
                0o755
            };
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        }
    }

    #[test]
    fn executable_candidates_follow_get_exec_path() {
        let env = |path: Option<&str>| -> Vec<(Vec<u8>, Vec<u8>)> {
            path.map(|text| (b"PATH".to_vec(), text.as_bytes().to_vec()))
                .into_iter()
                .collect()
        };
        let list = |name: &str, path: Option<&str>| -> Vec<String> {
            executable_candidates(name.as_bytes(), &env(path))
                .into_iter()
                .map(|item| String::from_utf8(item).unwrap())
                .collect()
        };
        assert_eq!(list("git", Some("/a:/b/")), ["/a/git", "/b/git"]);
        assert_eq!(list("git", Some("")), ["git"]);
        assert_eq!(list("git", Some("/a::/b")), ["/a/git", "git", "/b/git"]);
        assert_eq!(list("git", None), ["/bin/git", "/usr/bin/git"]);
        assert_eq!(list("./git", Some("/a")), ["./git"]);
        assert_eq!(list("/x/git", Some("/a")), ["/x/git"]);
        assert_eq!(list("a/git", None), ["a/git"]);
        assert_eq!(list("", Some("/a")), ["/a/"]);
    }

    #[test]
    fn path_search_cases_match_python_3_10() {
        // 実行ファイルを書いてすぐ`exec`するので、ほかのスレッドの`fork`が書込みのfdを持つと`ETXTBSY`になる。
        // 多スレッドの共有プロセスを避け、別プロセス(1スレッド)で流す。
        if !isolated("path_search_cases_match_python_3_10", None) {
            return;
        }
        if unsafe { libc::geteuid() } == 0 {
            eprintln!("root では権限の入力が変わるので省く");
            return;
        }
        let all = cases();
        let list = all["path_cases"].as_array().unwrap();
        assert!(list.len() >= 25);
        for case in list {
            let name = case["name"].as_str().unwrap();
            let root = scratch(&format!("path-{name}"));
            make_layout(&root, &case["layout"]);
            let sub = |text: &str| text.replace("{r}", root.to_str().unwrap());
            let env: Vec<(Vec<u8>, Vec<u8>)> = case["path"]
                .as_str()
                .map(|text| (b"PATH".to_vec(), sub(text).into_bytes()))
                .into_iter()
                .collect();
            let mut argv = vec![sub(case["argv0"].as_str().unwrap()).into_bytes()];
            argv.extend(case["rest"].as_array().unwrap().iter().map(bytes));
            let cwd = case["cwd"].as_str().map(|text| sub(text).into_bytes());
            let got = run(&argv, &env, cwd.as_deref(), b"");
            assert_outcome(name, &case["expect"], got);
            std::fs::remove_dir_all(&root).unwrap();
        }
    }

    fn run_stdio_case(case: &Value) {
        let name = case["name"].as_str().unwrap();
        let argv: Vec<Vec<u8>> = case["argv"].as_array().unwrap().iter().map(bytes).collect();
        let env = pairs(&case["env"]);
        let cwd = case["cwd"].as_str().map(|text| text.as_bytes().to_vec());
        // 親の側にCLOEXECの無いfdを残しても、子には渡らない。
        let leaked = case["leak_fd"].as_bool().unwrap().then(|| {
            // SAFETY: 固定の文字列を開くだけ。得たfdはこの関数の中で閉じる。
            let fd = unsafe { libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY) };
            assert!(fd >= 0);
            fd
        });
        let got = run(&argv, &env, cwd.as_deref(), b"");
        if let Some(fd) = leaked {
            // SAFETY: このテストが開いたfdを閉じる。
            unsafe { libc::close(fd) };
        }
        assert_outcome(name, &case["expect"], got);
    }

    fn stdio_cases(leaking: bool) -> usize {
        let mut count = 0;
        for case in cases()["stdio_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            if case["leak_fd"].as_bool().unwrap() != leaking {
                continue;
            }
            if name.starts_with("fds-") && !cfg!(target_os = "linux") {
                continue;
            }
            run_stdio_case(case);
            count += 1;
        }
        count
    }

    #[test]
    fn stdio_env_cwd_and_fd_cases_match_python_3_10() {
        // 件数は機種ごとに決まっている: Linuxは固定期待の全件、Macは`fds-`(`/proc/$$/fd`を読む例)を除いた件数。
        // 固定期待の中身から数え、実際に流した件数と完全に一致することを確かめる。
        let all = cases();
        let list = all["stdio_cases"].as_array().unwrap();
        let plain = |case: &&Value| !case["leak_fd"].as_bool().unwrap();
        let total = list.iter().filter(plain).count();
        let fds = list
            .iter()
            .filter(plain)
            .filter(|case| case["name"].as_str().unwrap().starts_with("fds-"))
            .count();
        assert_eq!((total, fds), (6, 1), "固定期待の中身が変わった");
        let want = if cfg!(target_os = "linux") {
            total
        } else {
            total - fds
        };
        assert_eq!(stdio_cases(false), want);
    }

    /// 子の側から3番以降のfdが開いているかを、外部コマンド(`/usr/bin/true`)への`>&N`で確かめる(閉じたfdへの複製は失敗する)。
    /// 2桁以上の番号を使うので、dash(0〜9だけ)ではなくbashで流す(macOSにもbash 3.2がある)。
    /// 組み込みコマンドの`(: >&N)`は使わない: bash 3.2(macOSのsh)は、書き換えを戻すためにfd 1を10番以降へ控えるので、
    /// fd 10が閉じていても「開いている」と判定する。外部コマンドは子プロセスの中で書き換えるので、控えが入らない。
    /// 親の列挙・上限までの番号のどちらの経路でも、何も開いていない。
    fn assert_child_has_no_fd_above_2() {
        let env = vec![(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())];
        let script = b"n=3; while [ $n -lt 200 ]; do if /usr/bin/true 2>/dev/null >&$n; then echo open$n; fi; n=$((n+1)); done";
        let argv = [b"bash".to_vec(), b"-c".to_vec(), script.to_vec()];
        let (out, code) = run(&argv, &env, None, b"").unwrap();
        assert_eq!((out, code), (Vec::new(), 0));
    }

    /// CLOEXECの無いfdを持った子が、`fds`(番号)を「開いている」と確かめられること(この確かめが働いている証明)。
    fn assert_own_child_sees_fds(fds: &[c_int]) {
        let script = "n=3; while [ $n -lt 200 ]; do if /usr/bin/true 2>/dev/null >&$n; then echo open$n; fi; n=$((n+1)); done";
        let own = std::process::Command::new("bash")
            .args(["-c", script])
            .output()
            .unwrap();
        let want: String = fds.iter().map(|fd| format!("open{fd}\n")).collect();
        assert_eq!(String::from_utf8_lossy(&own.stdout), want);
    }

    /// 「CLOEXECの無いfdは子へ渡らない」(Linuxは`/proc/self/fd`、Macは`/dev/fd`の列挙と、上限までの番号の両経路)。
    /// CLOEXECの無いfdを持ったまま、ほかのテストが子を起動するとその子へ渡るので、別プロセスで行う。
    /// 低い番号(3以降の最初の空き)と、150番あたり(上限までの番号の経路だけが届く大きさ)の2つを漏らす。
    #[test]
    fn a_leaked_fd_is_not_inherited_by_the_child_via_shell_probe() {
        if !isolated(
            "a_leaked_fd_is_not_inherited_by_the_child_via_shell_probe",
            None,
        ) {
            return;
        }
        // Linuxでも`close_range`を使わせず、列挙と上限までの番号の経路を通す(Macは元から`close_range`が無い)。
        FORCE_NO_CLOSE_RANGE.store(true, Ordering::SeqCst);
        // SAFETY: 固定の文字列を開き、F_DUPFDで150以上の空きへ複製する。CLOEXECを付けず、このプロセスの終わりまで閉じない。
        let (low, high) = unsafe {
            let low = libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY);
            let high = libc::fcntl(low, libc::F_DUPFD, 150);
            (low, high)
        };
        assert!(low >= 3 && high >= 150, "{low} {high}");
        // 先に、この確かめが「開いているfd」を見つけられることを、普通に起動した子(fdを受け継ぐ)で示す。
        assert_own_child_sees_fds(&[low, high]);
        // 列挙の経路(`/dev/fd`か`/proc/self/fd`)が、漏らしたfdを見つけていること(黙って上限までの経路へ落ちていない)。
        let listed = enumerate_open_fds().unwrap();
        assert!(
            listed.contains(&low) && listed.contains(&high),
            "{listed:?}"
        );
        assert!(enumerated_fds().is_some());
        assert_child_has_no_fd_above_2();
        assert_exec_failure_reaches_parent();
        FORCE_NO_ENUMERATION.store(true, Ordering::SeqCst);
        assert!(enumerated_fds().is_none());
        assert_child_has_no_fd_above_2();
        assert_exec_failure_reaches_parent();
        assert!(open_max() >= 3);
    }

    /// 代わりの経路でも、errpipeの書く側(keep)を閉じないこと。閉じると`execve`の失敗が親へ届かず`Ok`になる。
    fn assert_exec_failure_reaches_parent() {
        let result = spawn(&SpawnSpec {
            argv: &[b"/nonexistent-s2e-fallback/x".to_vec()],
            env: &[],
            cwd: None,
            stdin: Stdio::DevNull,
            stdout: Stdio::DevNull,
            stderr: Stdio::DevNull,
        });
        assert!(matches!(
            result,
            Err(SpawnError::Os {
                errno: libc::ENOENT,
                target: SpawnTarget::Executable
            })
        ));
    }

    /// `close_range`が使えない環境の代わりの経路(親で列挙したfd、列挙できなければ上限まで)でも、
    /// CLOEXECの無いfdは子へ渡らない。
    #[cfg(target_os = "linux")]
    #[test]
    fn fallback_paths_close_a_leaked_fd() {
        if !isolated("fallback_paths_close_a_leaked_fd", None) {
            return;
        }
        FORCE_NO_CLOSE_RANGE.store(true, Ordering::SeqCst);
        assert_eq!(stdio_cases(true), 1);
        assert_exec_failure_reaches_parent();
        FORCE_NO_ENUMERATION.store(true, Ordering::SeqCst);
        assert_eq!(stdio_cases(true), 1);
        assert_exec_failure_reaches_parent();
        assert!(open_max() >= 3);
    }

    /// CLOEXECの無いfdを持ったまま、ほかのテストが子を起動するとその子へ渡るので、別プロセスで行う。
    #[cfg(target_os = "linux")]
    #[test]
    fn a_leaked_fd_is_not_inherited_by_the_child() {
        if !isolated("a_leaked_fd_is_not_inherited_by_the_child", None) {
            return;
        }
        assert_eq!(stdio_cases(true), 1);
    }

    #[test]
    fn stdin_and_stdout_pipes_carry_data() {
        let env = vec![(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())];
        let (out, code) = run(&[b"cat".to_vec()], &env, None, b"abc\n").unwrap();
        assert_eq!((out, code), (b"abc\n".to_vec(), 0));
    }

    #[test]
    fn spawn_rejects_nul_and_empty_argv() {
        let spec = |argv: &'static [Vec<u8>]| SpawnSpec {
            argv,
            env: &[],
            cwd: None,
            stdin: Stdio::DevNull,
            stdout: Stdio::DevNull,
            stderr: Stdio::DevNull,
        };
        assert!(matches!(spawn(&spec(&[])), Err(SpawnError::EmptyArgv)));
        let nul: &'static [Vec<u8>] = Box::leak(Box::new([b"a\0b".to_vec()]));
        assert!(matches!(spawn(&spec(nul)), Err(SpawnError::EmbeddedNul)));
    }

    fn sleeper(seconds: &str) -> Child {
        spawn(&SpawnSpec {
            argv: &[
                b"sh".to_vec(),
                b"-c".to_vec(),
                format!("sleep {seconds}").into_bytes(),
            ],
            env: &[(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())],
            cwd: None,
            stdin: Stdio::Pipe,
            stdout: Stdio::Pipe,
            stderr: Stdio::DevNull,
        })
        .unwrap()
    }

    #[test]
    fn wait_timeout_kill_and_finish() {
        let mut child = sleeper("5");
        assert_eq!(child.poll(), None);
        let start = Instant::now();
        assert_eq!(child.wait_timeout(Duration::from_millis(60)).unwrap(), None);
        assert!(start.elapsed() >= Duration::from_millis(60));
        child.kill().unwrap();
        assert_eq!(child.wait().unwrap(), -9);
        // 終わった子への`kill`・`wait`・`finish`は、そのまま同じ値を返す。
        child.kill().unwrap();
        assert_eq!(child.wait_timeout(Duration::ZERO).unwrap(), Some(-9));
        assert_eq!(child.finish().unwrap(), -9);
        // `finish`はパイプを閉じ、生きていれば`kill`して回収する。
        let mut live = sleeper("5");
        assert!(live.stdin.is_some() && live.stdout.is_some());
        assert_eq!(live.finish().unwrap(), -9);
        assert!(live.stdin.is_none() && live.stdout.is_none());
    }

    #[test]
    fn returncode_cases_match_python_3_10_without_sigchld_changes() {
        for case in cases()["returncode_cases"].as_array().unwrap() {
            if case["sigchld_ignored"].as_bool().unwrap() {
                continue;
            }
            assert_returncode_case(case);
        }
    }

    fn assert_returncode_case(case: &Value) {
        let name = case["name"].as_str().unwrap();
        let argv: Vec<Vec<u8>> = case["argv"].as_array().unwrap().iter().map(bytes).collect();
        let mut child = spawn(&SpawnSpec {
            argv: &argv,
            env: &[(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())],
            cwd: None,
            stdin: Stdio::DevNull,
            stdout: Stdio::Pipe,
            stderr: Stdio::DevNull,
        })
        .unwrap();
        let mut first = None;
        if case["kill"].as_bool().unwrap() {
            child.kill().unwrap();
        } else if case["poll_first"].as_bool().unwrap() {
            first = child.poll();
        }
        let code = child.wait().unwrap();
        child.close_stdout();
        let expect = &case["expect"];
        assert_eq!(
            first.map(i64::from),
            expect["poll_right_after_spawn"].as_i64(),
            "{name}"
        );
        assert_eq!(
            i64::from(code),
            expect["returncode"].as_i64().unwrap(),
            "{name}"
        );
    }

    // ---- environ の生の項目(旧Python 3.10の`dict(os.environ)`)

    #[test]
    fn environ_pairs_split_at_the_first_equals_and_keep_the_first_duplicate() {
        let entries: [&[u8]; 8] = [
            b"A=1",
            b"A=2",
            b"=foo=bar",
            b"PATH=/p",
            b"=",
            b"B=x=y",
            b"NOEQ",
            b"PATH=/q",
        ];
        let pairs = environ_pairs(&entries);
        let shown: Vec<(&[u8], &[u8])> = pairs
            .iter()
            .map(|(k, v)| (k.as_slice(), v.as_slice()))
            .collect();
        let want: [(&[u8], &[u8]); 4] = [
            (b"A", b"1"),
            (b"", b"foo=bar"),
            (b"PATH", b"/p"),
            (b"B", b"x=y"),
        ];
        assert_eq!(shown, want);
        assert!(environ_pairs(&[]).is_empty());
    }

    /// `envp`をそのまま持つ子として、このテストの実行ファイルを`execve`し、`name`の検査だけを流す。
    /// 標準出力を返す(重なった名前を持つ`envp`は`Command::env`では作れないので、`pre_exec`で`execve`する)。
    fn run_with_envp(name: &str, envp: &[String]) -> String {
        use std::ffi::CString;
        use std::os::unix::process::CommandExt;
        let exe = std::env::current_exe().unwrap();
        let path = CString::new(exe.as_os_str().as_encoded_bytes()).unwrap();
        let args: Vec<CString> = [
            exe.to_str().unwrap(),
            "--exact",
            &format!("environment_guard::child_tests::{name}"),
            "--nocapture",
            "--test-threads=1",
        ]
        .iter()
        .map(|arg| CString::new(*arg).unwrap())
        .collect();
        let env: Vec<CString> = envp
            .iter()
            .map(|e| CString::new(e.as_str()).unwrap())
            .collect();
        let mut argv: Vec<*const c_char> = args.iter().map(|a| a.as_ptr()).collect();
        argv.push(std::ptr::null());
        let mut table: Vec<*const c_char> = env.iter().map(|e| e.as_ptr()).collect();
        table.push(std::ptr::null());
        let mut command = std::process::Command::new(&exe);
        // 領域は`output`が返るまで、この関数が持ち続ける。closureには番地だけを渡す(`Sync`のため)。
        let (path_at, argv_at, table_at) = (
            path.as_ptr() as usize,
            argv.as_ptr() as usize,
            table.as_ptr() as usize,
        );
        // SAFETY: `fork`の後の子で、確保済みの領域だけを使って`execve`する(確保も解放もしない)。
        unsafe {
            command.pre_exec(move || {
                libc::execve(
                    path_at as *const c_char,
                    argv_at as *const *const c_char,
                    table_at as *const *const c_char,
                );
                Err(io::Error::last_os_error())
            });
        }
        let output = command.output().unwrap();
        let text = String::from_utf8_lossy(&output.stdout).into_owned();
        assert!(
            output.status.success() && text.contains("1 passed"),
            "{name}: {text}\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
        text
    }

    /// 別プロセスの中の本体。`S2E_PLATFORM_ISOLATED`が`environ`のときだけ働き、組を標準出力に出す。
    fn print_environ(coerce: bool) {
        if std::env::var_os("S2E_PLATFORM_ISOLATED").is_none_or(|value| value != "environ") {
            return;
        }
        if coerce {
            coerce_c_locale();
        }
        let pairs: Vec<Value> = process_environ()
            .iter()
            .map(|(k, v)| {
                serde_json::json!([
                    k.iter().map(|b| char::from(*b)).collect::<String>(),
                    v.iter().map(|b| char::from(*b)).collect::<String>()
                ])
            })
            .collect();
        println!("S2E_ENVIRON_BEGIN{}S2E_ENVIRON_END", Value::Array(pairs));
    }

    #[test]
    fn environ_child() {
        print_environ(false);
    }

    #[test]
    fn environ_child_coerced() {
        print_environ(true);
    }

    #[test]
    fn process_environ_matches_the_old_python_dict_of_os_environ() {
        let fixture: Value =
            serde_json::from_str(include_str!("../../../ci/s2e_fixtures/environ_cases.json"))
                .expect("environ_cases.json が JSON でない");
        // 件数は機種ごとに決まっている: Linuxは固定期待の全件、Macは`coerce`(Linuxで測った足し込み)を除いた件数。
        let list = fixture["cases"].as_array().unwrap();
        let total = list.len();
        let coerced = list
            .iter()
            .filter(|case| case["coerce"].as_bool().unwrap())
            .count();
        assert!(coerced >= 1 && total > coerced, "固定期待の中身が変わった");
        let want = if cfg!(target_os = "linux") {
            total
        } else {
            total - coerced
        };
        let mut ran = 0;
        for case in list {
            let name = case["name"].as_str().unwrap();
            let coerce = case["coerce"].as_bool().unwrap();
            // PEP 538 の足し込みの固定期待は Linux で測った。Mac は地域設定の判定が別なので比べない。
            if coerce && !cfg!(target_os = "linux") {
                continue;
            }
            let envp: Vec<String> = case["envp"]
                .as_array()
                .unwrap()
                .iter()
                .map(|e| e.as_str().unwrap().to_owned())
                .collect();
            let text = run_with_envp(
                if coerce {
                    "environ_child_coerced"
                } else {
                    "environ_child"
                },
                &envp,
            );
            let body = text
                .split_once("S2E_ENVIRON_BEGIN")
                .and_then(|(_, rest)| rest.split_once("S2E_ENVIRON_END"))
                .unwrap_or_else(|| panic!("{name}: 組が出ていない: {text}"))
                .0;
            let got: Value = serde_json::from_str(body).unwrap();
            assert_eq!(got, case["pairs"], "{name}");
            // 旧版は、この組を子の環境にして起動に成功する(名前に`=`を含まないので`ValueError`にならない)。
            assert_eq!(case["spawn"], "ok", "{name}");
            let pairs: Vec<(Vec<u8>, Vec<u8>)> = got
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| (bytes(&pair[0]), bytes(&pair[1])))
                .collect();
            let mut child = spawn(&SpawnSpec {
                argv: &[b"/bin/sh".to_vec(), b"-c".to_vec(), b"exit 0".to_vec()],
                env: &pairs,
                cwd: None,
                stdin: Stdio::DevNull,
                stdout: Stdio::DevNull,
                stderr: Stdio::DevNull,
            })
            .unwrap_or_else(|error| panic!("{name}: {error:?}"));
            assert_eq!(child.wait().unwrap(), 0, "{name}");
            ran += 1;
        }
        assert_eq!(ran, want, "比べた件数が固定期待と合わない");
    }

    // ---- プロセス全体の信号の扱いを変える検査(別プロセスで行う)

    /// 別プロセスの中なら`true`(検査の本体を流す)。外なら、同じ検査だけを別プロセスで流して確かめ、`false`。
    /// `signal`が`None`なら、成功と「1 passed」が必須(0件を流して通ることを防ぐ)。`Some`なら、その信号で終わることが必須。
    fn isolated(name: &str, signal: Option<i32>) -> bool {
        use std::os::unix::process::ExitStatusExt;
        if std::env::var_os("S2E_PLATFORM_ISOLATED").is_some_and(|value| value == name) {
            return true;
        }
        let exe = std::env::current_exe().unwrap();
        let output = std::process::Command::new(exe)
            .args([
                "--exact",
                &format!("environment_guard::child_tests::{name}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S2E_PLATFORM_ISOLATED", name)
            .output()
            .unwrap();
        let text = format!(
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        match signal {
            Some(number) => assert_eq!(output.status.signal(), Some(number), "{name}: {text}"),
            None => {
                assert!(
                    output.status.success(),
                    "別プロセスの検査が失敗({name}): {text}"
                );
                assert!(
                    text.contains("1 passed"),
                    "別プロセスが検査を流していない({name}): {text}"
                );
            }
        }
        false
    }

    fn signal_number(name: &str) -> i32 {
        match name {
            "INT" => libc::SIGINT,
            "USR1" => libc::SIGUSR1,
            "USR2" => libc::SIGUSR2,
            "PIPE" => libc::SIGPIPE,
            "XFSZ" => libc::SIGXFSZ,
            other => panic!("{other}"),
        }
    }

    extern "C" fn noop(_number: c_int) {}

    fn set_handler(number: i32) {
        // SAFETY: sigactionはすべて0で有効な初期値。
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = noop as extern "C" fn(c_int) as usize;
        // SAFETY: 有効な設定を渡す。
        assert_eq!(
            unsafe { libc::sigaction(number, &action, std::ptr::null_mut()) },
            0
        );
    }

    fn set_mask(blocked: &[i32]) {
        // SAFETY: 空の集合を作って、呼出しスレッドのmaskを置き換える。
        unsafe {
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            for &number in blocked {
                libc::sigaddset(&mut set, number);
            }
            assert_eq!(
                libc::pthread_sigmask(libc::SIG_SETMASK, &set, std::ptr::null_mut()),
                0
            );
        }
    }

    #[test]
    fn signal_cases_match_python_3_10() {
        if !isolated("signal_cases_match_python_3_10", None) {
            return;
        }
        let tracked = ["INT", "USR1", "USR2", "PIPE", "XFSZ"];
        let env = vec![(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())];
        // 子の信号の状態を読む手段(どちらも`SigIgn:`・`SigBlk:`の行に16進の集合を出す)。
        // Linuxは`/proc/self/status`。全機種で、Cの観測用プログラム(`cc`で組む。sigprocmaskとsigactionで読む)。
        // Macには`/proc`が無いので、Cの観測用プログラムが要る(`cc`が無ければ失敗にする。Xcodeのコマンドラインツールを入れる)。
        let observer = SignalObserver::build();
        assert!(
            observer.is_some() || cfg!(target_os = "linux"),
            "信号の観測用プログラムを組めない(`cc`が要る。macOSはxcode-select --installで入れる)"
        );
        let mut backends: Vec<Vec<Vec<u8>>> = Vec::new();
        if cfg!(target_os = "linux") {
            backends.push(vec![b"cat".to_vec(), b"/proc/self/status".to_vec()]);
        }
        if let Some(observer) = &observer {
            use std::os::unix::ffi::OsStrExt;
            backends.push(vec![observer.path().as_os_str().as_bytes().to_vec()]);
        }
        let list = &cases()["signal_cases"];
        assert!(list.as_array().unwrap().len() >= 7);
        for case in list.as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let spec = &case["spec"];
            let mut blocked = Vec::new();
            for key in tracked {
                // 指定が無いPIPE・XFSZは無視(旧版の起動時の状態)。ほかは既定。指定があればそのとおり。
                let how = spec[key]
                    .as_str()
                    .unwrap_or(if matches!(key, "PIPE" | "XFSZ") {
                        "ignore"
                    } else {
                        "default"
                    });
                let number = signal_number(key);
                match how {
                    "default" => signal_set_default(number).unwrap(),
                    "ignore" => ignore_signal(number).unwrap(),
                    "handler" => set_handler(number),
                    other => panic!("{other}"),
                }
                if spec[format!("{key}-block")].as_bool().unwrap_or(false) {
                    blocked.push(number);
                }
            }
            set_mask(&blocked);
            let outputs: Vec<(Vec<u8>, i32)> = backends
                .iter()
                .map(|argv| run(argv, &env, None, b"").unwrap())
                .collect();
            set_mask(&[]);
            for (out, code) in outputs {
                assert_eq!(code, 0, "{name}");
                let text = String::from_utf8(out).unwrap();
                let mask = |key: &str| -> u64 {
                    let line = text.lines().find(|line| line.starts_with(key)).unwrap();
                    u64::from_str_radix(line.split(':').nth(1).unwrap().trim(), 16).unwrap()
                };
                let pick = |mask: u64| -> Vec<String> {
                    tracked
                        .iter()
                        .filter(|key| mask >> (signal_number(key) - 1) & 1 == 1)
                        .map(|key| (*key).to_owned())
                        .collect()
                };
                let names = |value: &Value| -> Vec<String> {
                    value
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|item| item.as_str().unwrap().to_owned())
                        .collect()
                };
                assert_eq!(
                    pick(mask("SigIgn")),
                    names(&case["expect"]["ignored"]),
                    "{name}: ignored"
                );
                assert_eq!(
                    pick(mask("SigBlk")),
                    names(&case["expect"]["blocked"]),
                    "{name}: blocked"
                );
            }
        }
    }

    /// 子の信号の無視・maskを読むCのプログラム(`cc`で組む)。信号・環境・fdを変えないよう、シェルやPythonを通さずに直接起動する。
    struct SignalObserver {
        dir: std::path::PathBuf,
    }

    impl SignalObserver {
        const SOURCE: &'static str = "#include <signal.h>\n#include <stdio.h>\n\
int main(void) {\n\
    unsigned long long ignored = 0, blocked = 0;\n\
    sigset_t current;\n\
    sigemptyset(&current);\n\
    sigprocmask(SIG_BLOCK, NULL, &current);\n\
    for (int number = 1; number < NSIG && number <= 64; number++) {\n\
        struct sigaction action;\n\
        if (sigaction(number, NULL, &action) == 0 && action.sa_handler == SIG_IGN) {\n\
            ignored |= 1ULL << (number - 1);\n\
        }\n\
        if (sigismember(&current, number) == 1) {\n\
            blocked |= 1ULL << (number - 1);\n\
        }\n\
    }\n\
    printf(\"SigIgn:\\t%016llx\\nSigBlk:\\t%016llx\\n\", ignored, blocked);\n\
    return 0;\n\
}\n";

        /// 組めれば`Some`。`cc`が無ければ`None`。組めたのに動かなければ失敗にする。
        fn build() -> Option<SignalObserver> {
            // 排他に作った自分のフォルダ(`S2E_TEST_TMP`に従う)。消すのもこのパスだけ。
            let observer = SignalObserver {
                dir: scratch("signal-observer"),
            };
            let source = observer.dir.join("observer.c");
            std::fs::write(&source, Self::SOURCE).unwrap();
            let built = match std::process::Command::new("cc")
                .arg("-O1")
                .arg("-o")
                .arg(observer.path())
                .arg(&source)
                .output()
            {
                Ok(output) => output,
                Err(error) if error.kind() == io::ErrorKind::NotFound => return None,
                Err(error) => panic!("cc を起動できない: {error}"),
            };
            assert!(
                built.status.success(),
                "観測用プログラムを組めない: {}",
                String::from_utf8_lossy(&built.stderr)
            );
            Some(observer)
        }

        fn path(&self) -> std::path::PathBuf {
            self.dir.join("observer")
        }
    }

    impl Drop for SignalObserver {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn sigchld_ignored_returncode_cases_match_python_3_10() {
        if !isolated("sigchld_ignored_returncode_cases_match_python_3_10", None) {
            return;
        }
        ignore_signal(libc::SIGCHLD).unwrap();
        let all = cases();
        let ignored: Vec<&Value> = all["returncode_cases"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|case| case["sigchld_ignored"].as_bool().unwrap())
            .collect();
        assert_eq!(ignored.len(), 2);
        for case in ignored {
            assert_returncode_case(case);
        }
    }

    #[test]
    fn sigint_handler_installs_only_when_default() {
        if !isolated("sigint_handler_installs_only_when_default", None) {
            return;
        }
        // 無視されていれば何も変えない。
        ignore_signal(SIGINT).unwrap();
        assert!(!install_sigint_handler().unwrap());
        assert_eq!(signal_disposition(SIGINT).unwrap(), Disposition::Ignore);
        assert!(!sigint_pending());
        // 既定なら入れる。EINTRは、SIGINTが来るまで繰り返す。
        signal_set_default(SIGINT).unwrap();
        assert!(install_sigint_handler().unwrap());
        assert_eq!(signal_disposition(SIGINT).unwrap(), Disposition::Handler);
        let mut calls = 0;
        let value = retry(|| {
            calls += 1;
            if calls == 1 {
                Err(io::Error::from_raw_os_error(libc::EINTR))
            } else {
                Ok(7)
            }
        });
        assert_eq!((value.unwrap(), calls), (7, 2));
        // SAFETY: 自分自身へ信号を送る。ハンドラは印を立てるだけ。
        unsafe { libc::raise(libc::SIGINT) };
        assert!(sigint_pending());
        let interrupted = retry::<()>(|| Err(io::Error::from_raw_os_error(libc::EINTR)));
        assert_eq!(interrupted.unwrap_err().raw_os_error(), Some(libc::EINTR));
        // 待っている子も、SIGINT後はEINTRで戻る。印を消すまで、後始末を除いて待ちは戻り続ける。
        let mut child = sleeper("5");
        assert_eq!(child.wait().unwrap_err().raw_os_error(), Some(libc::EINTR));
        assert_eq!(
            child
                .wait_timeout(Duration::from_secs(5))
                .unwrap_err()
                .raw_os_error(),
            Some(libc::EINTR)
        );
        assert!(take_sigint());
        assert!(!sigint_pending());
        assert!(!take_sigint());
        // 印を消せば、後始末(kill→wait)はそのまま動く。
        assert_eq!(child.finish().unwrap(), -9);
    }

    #[test]
    fn die_by_sigint_ends_with_the_signal() {
        if !isolated("die_by_sigint_ends_with_the_signal", Some(libc::SIGINT)) {
            return;
        }
        // 信号で自分が終わる(戻れば旧版と同じ終了コード130)。
        assert_eq!(die_by_sigint(), 130);
    }

    #[test]
    fn parent_signals_are_ignored() {
        if !isolated("parent_signals_are_ignored", None) {
            return;
        }
        signal_set_default(SIGPIPE).unwrap();
        signal_set_default(SIGXFSZ).unwrap();
        ignore_parent_signals().unwrap();
        assert_eq!(signal_disposition(SIGPIPE).unwrap(), Disposition::Ignore);
        assert_eq!(signal_disposition(SIGXFSZ).unwrap(), Disposition::Ignore);
        signal_set_default(SIGXFSZ).unwrap();
        assert_eq!(signal_disposition(SIGXFSZ).unwrap(), Disposition::Default);
    }

    /// 親が決めた`SIGPIPE`の扱い(`ignore`か`default`)で、同じ検査だけを子として起動し、出力を返す。
    fn inherited_sigpipe_child(inherited: &str) -> String {
        use std::os::unix::process::CommandExt;
        let exe = std::env::current_exe().unwrap();
        let mut command = std::process::Command::new(exe);
        command
            .args([
                "--exact",
                "environment_guard::child_tests::inherited_sigpipe_is_recorded_before_main",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S2E_EXPECT_SIGPIPE", inherited);
        let ignore = inherited == "ignore";
        // SAFETY: `fork`の後の子で、`sigaction`だけを呼ぶ(async-signal-safe)。
        unsafe {
            command.pre_exec(move || {
                let mut action: libc::sigaction = std::mem::zeroed();
                action.sa_sigaction = if ignore { libc::SIG_IGN } else { libc::SIG_DFL };
                libc::sigemptyset(&mut action.sa_mask);
                libc::sigaction(libc::SIGPIPE, &action, std::ptr::null_mut());
                Ok(())
            });
        }
        let output = command.output().unwrap();
        format!(
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    }

    #[test]
    fn inherited_sigpipe_is_recorded_before_main() {
        if let Ok(want) = std::env::var("S2E_EXPECT_SIGPIPE") {
            // 子の中: Rustの実行時は`main`の前に無視へ変えているので、今の値は常に無視。控えた値だけが親の値を保つ。
            assert_eq!(signal_disposition(SIGPIPE).unwrap(), Disposition::Ignore);
            let want = if want == "ignore" {
                Disposition::Ignore
            } else {
                Disposition::Default
            };
            assert_eq!(inherited_sigpipe(), Some(want));
            // ローダーが`main`より前に1回だけ呼んでいる(この検査は呼んでいない)。
            assert_eq!(CAPTURE_CALLS.load(std::sync::atomic::Ordering::Relaxed), 1);
            return;
        }
        for inherited in ["ignore", "default"] {
            let text = inherited_sigpipe_child(inherited);
            assert!(text.contains("1 passed"), "{inherited}: {text}");
        }
    }

    #[test]
    fn passwd_current_user_has_a_home() {
        match passwd_home_of_current_user().unwrap() {
            PasswdLookup::Found(home) => assert!(!home.is_empty()),
            PasswdLookup::NotFound => eprintln!("現在の利用者が引けない環境(未確認の差)"),
        }
    }

    #[test]
    fn env_name_and_value_checks_match_python_3_10() {
        let all = cases();
        let list = all["env_error_cases"].as_array().unwrap();
        assert_eq!(list.len(), 6);
        for case in list {
            let name = case["name"].as_str().unwrap();
            let env = pairs(&case["env"]);
            let cwd: Option<&[u8]> =
                (name == "env-error-before-cwd-error").then_some(b"/nonexistent");
            let got = run(&[b"env".to_vec()], &env, cwd, b"");
            let expect = &case["expect"];
            match expect.get("error") {
                None => assert_outcome(name, expect, got),
                Some(_) => {
                    let message = expect["message"].as_str().unwrap();
                    match got {
                        Err(SpawnError::IllegalEnvName) => {
                            assert_eq!(message, "illegal environment variable name", "{name}")
                        }
                        Err(SpawnError::EmbeddedNul) => {
                            assert_eq!(message, "embedded null byte", "{name}")
                        }
                        other => panic!("{name}: {other:?}"),
                    }
                }
            }
        }
    }

    #[test]
    fn preexec_failures_carry_the_cwd_name_only_when_given() {
        let all = cases();
        let list = all["preexec_cases"].as_array().unwrap();
        assert_eq!(list.len(), 2);
        let error = SpawnError::Os {
            errno: libc::EBADF,
            target: SpawnTarget::Cwd,
        };
        for case in list {
            let cwd = case["cwd"].as_str().map(str::as_bytes);
            let filename = error.filename(b"true", cwd);
            match case["target"].as_str().unwrap() {
                "none" => assert_eq!(filename, None),
                "cwd" => assert_eq!(filename, cwd),
                other => panic!("{other}"),
            }
        }
        let exec = SpawnError::Os {
            errno: libc::ENOENT,
            target: SpawnTarget::Executable,
        };
        assert_eq!(exec.filename(b"git", Some(b"/")), Some(&b"git"[..]));
    }

    /// 親がstdinを閉じたら、子はEOFを受けて終わる(子が親の書込み側のfdを持っていれば終わらない)。
    #[test]
    fn a_child_reading_stdin_to_eof_ends_after_the_parent_closes_it() {
        let env = vec![(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())];
        let argv = [
            b"sh".to_vec(),
            b"-c".to_vec(),
            b"cat >/dev/null; echo eof".to_vec(),
        ];
        let mut child = spawn(&SpawnSpec {
            argv: &argv,
            env: &env,
            cwd: None,
            stdin: Stdio::Pipe,
            stdout: Stdio::Pipe,
            stderr: Stdio::DevNull,
        })
        .unwrap();
        let stdin = child.stdin.take().unwrap();
        write(stdin.as_fd(), b"data\n").unwrap();
        assert_eq!(
            child.wait_timeout(Duration::from_millis(100)).unwrap(),
            None
        );
        drop(stdin);
        assert_eq!(
            child.wait_timeout(Duration::from_secs(10)).unwrap(),
            Some(0)
        );
        let mut chunk = [0u8; 16];
        let count = read(child.stdout.as_ref().unwrap().as_fd(), &mut chunk).unwrap();
        assert_eq!(&chunk[..count], b"eof\n");
    }

    /// fd 0が閉じていると、パイプが0番になる。子のstdinが正しくそのパイプになる。
    #[test]
    fn a_pipe_that_gets_fd_zero_still_reaches_the_child() {
        if !isolated("a_pipe_that_gets_fd_zero_still_reaches_the_child", None) {
            return;
        }
        // SAFETY: このプロセスのfd 0を閉じる。この別プロセスは標準入力を使わない。
        assert_eq!(unsafe { libc::close(0) }, 0);
        let env = vec![(b"PATH".to_vec(), b"/usr/bin:/bin".to_vec())];
        let (out, code) = run(&[b"cat".to_vec()], &env, None, b"via-fd0\n").unwrap();
        assert_eq!((out, code), (b"via-fd0\n".to_vec(), 0));
        // 標準エラーだけを`/dev/null`にすると、`/dev/null`が0番になる(CLOEXECなので、子の0番は閉じる)。
        // 子の2番が`/dev/null`を指す(終了コード0)。標準出力は受け継ぐ。
        {
            let argv = [
                b"sh".to_vec(),
                b"-c".to_vec(),
                b"[ /dev/fd/2 -ef /dev/null ]".to_vec(),
            ];
            let mut child = spawn(&SpawnSpec {
                argv: &argv,
                env: &env,
                cwd: None,
                stdin: Stdio::Inherit,
                stdout: Stdio::Inherit,
                stderr: Stdio::DevNull,
            })
            .unwrap();
            assert_eq!(child.wait().unwrap(), 0);
        }
    }
}
