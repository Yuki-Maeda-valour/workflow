//! R4U(#480)専用の OS 部品。旧版 check_links.py(本家 CPython 3.10.11)と同じ OS 呼出しを、1 回ずつ行う。
//!
//! - パスは整えた文字列を連結した完全なパスで渡す。ディレクトリの fd から辿る呼出し・リンクの拒否・量の上限は足さない。
//! - 開く・読む・書くは EINTR を自動で繰り返さない。呼出元が Python と同じ所で SIGINT の印を見て、繰り返すかを決める。
//! - stdout と stderr への書込みは、すべて libc の `write` を通す(共有ライブラリの差し込みで記録と故障の注入ができるように)。
//!   Linux の rustix は libc を通らない(linux_raw)ので、書込みには使わない。
//! - rustix に無い `sigaction`・`kill`・`getpid`・`pthread_self` は、ここで `unsafe extern "C"` として宣言し、安全な関数で包む。
//! - 大きさと回数も旧版に合わせる: getcwd は 1024 バイトずつ広げる、readlink は MAXPATHLEN で 1 回、
//!   一覧は Linux が glibc の opendir と同じ大きさ(32768 バイト以上)の getdents64、Mac が libc の opendir・readdir。
//! - main の前に控える関数 `capture_before_main` を持つ(SIGPIPE の扱いと、fd 0〜2 が使えるか)。
//!   main の前に呼ぶ static は、検査用の実行物(cli の tests/check_links.rs)だけが置く。製品の実行物には入れない。
use rustix::fd::{AsRawFd, BorrowedFd, OwnedFd};
use rustix::fs::{self, FileType, Mode, OFlags};
use std::ffi::{CString, OsStr};
use std::os::unix::ffi::OsStrExt;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};

/// OS エラーの番号。
pub type Errno = i32;

/// Linux と Mac で同じ番号。
#[cfg(target_os = "linux")]
const ENOENT: Errno = 2;
const EINVAL: Errno = 22;
const ERANGE: Errno = 34;
#[cfg(target_os = "linux")]
const ENOTDIR: Errno = 20;
/// CPython の os.readlink が渡す大きさ(MAXPATHLEN)。
#[cfg(target_os = "macos")]
const MAXPATHLEN: usize = 1024;
#[cfg(not(target_os = "macos"))]
const MAXPATHLEN: usize = 4096;

fn last_errno() -> Errno {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// パスだけで開く(Linux は glibc の open と同じ `openat(AT_FDCWD)`、Mac は libc の open)。
fn open_path(path: &[u8], flags: OFlags) -> rustix::io::Result<OwnedFd> {
    #[cfg(target_os = "linux")]
    let fd = fs::openat(fs::CWD, arg(path), flags, Mode::empty());
    #[cfg(not(target_os = "linux"))]
    let fd = fs::open(arg(path), flags, Mode::empty());
    fd
}

fn errno(error: rustix::io::Errno) -> Errno {
    error.raw_os_error()
}

fn arg(path: &[u8]) -> &OsStr {
    OsStr::from_bytes(path)
}

/// stat の結果のうち、旧版が使う所。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Meta {
    pub kind: Kind,
    /// `st_blksize`(Python の FileIO の `_blksize` の元)。
    pub blksize: u64,
    /// `st_size`(FileIO.readall の最初の読取りの大きさの元)。
    pub size: i64,
}

/// ファイルの種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Directory,
    Regular,
    Symlink,
    Other,
}

fn meta(stat: &fs::Stat) -> Meta {
    let kind = match FileType::from_raw_mode(stat.st_mode) {
        FileType::Directory => Kind::Directory,
        FileType::RegularFile => Kind::Regular,
        FileType::Symlink => Kind::Symlink,
        _ => Kind::Other,
    };
    Meta {
        kind,
        blksize: u64::try_from(stat.st_blksize).unwrap_or(0),
        size: stat.st_size,
    }
}

/// `os.stat(path)`(リンクを辿る)。`path` に NUL を含めない(呼出元が ValueError にする)。
pub fn stat(path: &[u8]) -> Result<Meta, Errno> {
    fs::stat(arg(path)).map(|stat| meta(&stat)).map_err(errno)
}

/// `os.lstat(path)`。
pub fn lstat(path: &[u8]) -> Result<Meta, Errno> {
    fs::lstat(arg(path)).map(|stat| meta(&stat)).map_err(errno)
}

/// `os.readlink(path)`(`readlink(path, buf, MAXPATHLEN)` を 1 回だけ呼ぶ)。
pub fn readlink(path: &[u8]) -> Result<Vec<u8>, Errno> {
    let path = CString::new(path).map_err(|_| EINVAL)?;
    let mut buffer = vec![0_u8; MAXPATHLEN + 1];
    // SAFETY: path は NUL で終わる文字列、buffer は MAXPATHLEN バイト以上の書込み先で、どちらも呼出しの間有効。
    let length = unsafe { sys::readlink(path.as_ptr(), buffer.as_mut_ptr().cast(), MAXPATHLEN) };
    if length < 0 {
        return Err(last_errno());
    }
    buffer.truncate(length.unsigned_abs());
    Ok(buffer)
}

/// `os.getcwd()`(libc の getcwd を、ERANGE の間は 1024 バイトずつ広げて呼ぶ)。
pub fn getcwd() -> Result<Vec<u8>, Errno> {
    let mut size = 0;
    loop {
        size += 1024;
        let mut buffer = vec![0_u8; size];
        // SAFETY: buffer は渡す大きさの書込み先で、呼出しの間有効。
        let result = unsafe { sys::getcwd(buffer.as_mut_ptr().cast(), buffer.len()) };
        if !result.is_null() {
            let end = buffer
                .iter()
                .position(|&byte| byte == 0)
                .unwrap_or(buffer.len());
            buffer.truncate(end);
            return Ok(buffer);
        }
        let error = last_errno();
        if error != ERANGE {
            return Err(error);
        }
    }
}

/// readdir の種類(d_type)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryKind {
    Directory,
    Symlink,
    /// DT_UNKNOWN(種類を返さないファイルシステム)。
    Unknown,
    Other,
}

/// `os.scandir` の 1 件(`.` と `..` は含めない)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub name: Vec<u8>,
    pub kind: EntryKind,
}

/// 一覧の取得の失敗。どちらも `path_error(scandir の引数)` になる。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScandirError {
    Open(Errno),
    Read(Errno),
}

#[cfg(target_os = "linux")]
fn entry_kind(file_type: FileType) -> EntryKind {
    match file_type {
        FileType::Directory => EntryKind::Directory,
        FileType::Symlink => EntryKind::Symlink,
        FileType::Unknown => EntryKind::Unknown,
        _ => EntryKind::Other,
    }
}

/// glibc の opendir の溜めの大きさ(`__alloc_dir`: st_blksize を 32768〜1048576 に収める)。
#[cfg(target_os = "linux")]
fn allocation(blksize: u64) -> usize {
    usize::try_from(blksize.clamp(32768, 1_048_576)).unwrap_or(32768)
}

/// `list(os.scandir(path))`。glibc の opendir・readdir と同じ呼出し(開く → fstat → 同じ大きさの getdents64 を
/// 0 が返るまで → 閉じる)で、readdir の順のまま全件を集める。
#[cfg(target_os = "linux")]
pub fn scandir(path: &[u8]) -> Result<Vec<Entry>, ScandirError> {
    use std::mem::MaybeUninit;
    let fd = open_path(
        path,
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::DIRECTORY | OFlags::CLOEXEC,
    )
    .map_err(|error| ScandirError::Open(errno(error)))?;
    // glibc の opendir は fstatat(fd, "", AT_EMPTY_PATH) で種類と st_blksize を見る。
    let opened = fs::statat(&fd, "", fs::AtFlags::EMPTY_PATH)
        .map_err(|error| ScandirError::Open(errno(error)))?;
    if FileType::from_raw_mode(opened.st_mode) != FileType::Directory {
        return Err(ScandirError::Open(ENOTDIR));
    }
    let size = allocation(u64::try_from(opened.st_blksize).unwrap_or(0));
    // RawDir は溜めの先頭を 8 バイトの境目に合わせる。合わせた後に、ちょうど size バイトを渡す。
    let mut storage = vec![MaybeUninit::<u8>::uninit(); size + 8];
    let offset = storage.as_ptr().align_offset(8);
    let mut dir = fs::RawDir::new(&fd, &mut storage[offset..offset + size]);
    let mut entries = Vec::new();
    while let Some(entry) = dir.next() {
        let entry = match entry {
            Ok(entry) => entry,
            // glibc の readdir は getdents64 の ENOENT を終わりと扱う。
            Err(error) if errno(error) == ENOENT => break,
            Err(error) => return Err(ScandirError::Read(errno(error))),
        };
        let name = entry.file_name().to_bytes();
        if name == b"." || name == b".." {
            continue;
        }
        entries.push(Entry {
            name: name.to_vec(),
            kind: entry_kind(entry.file_type()),
        });
    }
    Ok(entries)
}

/// `list(os.scandir(path))`。Python と同じく libc の opendir・readdir・closedir を呼ぶ。
#[cfg(target_os = "macos")]
pub fn scandir(path: &[u8]) -> Result<Vec<Entry>, ScandirError> {
    let path = CString::new(path).map_err(|_| ScandirError::Open(EINVAL))?;
    // SAFETY: path は NUL で終わる文字列で、呼出しの間有効。
    let dir = unsafe { sys::opendir(path.as_ptr()) };
    if dir.is_null() {
        return Err(ScandirError::Open(last_errno()));
    }
    let mut entries = Vec::new();
    let result = loop {
        // SAFETY: errno の場所は呼んだスレッドで常に有効。readdir の前に 0 にして、誤りと終わりを見分ける。
        unsafe {
            *errno_location() = 0;
        }
        // SAFETY: dir は opendir が返した、まだ閉じていない一覧。
        let entry = unsafe { sys::readdir(dir) };
        if entry.is_null() {
            let error = last_errno();
            break if error == 0 { Ok(()) } else { Err(error) };
        }
        // SAFETY: readdir が返した項目は、次の readdir か closedir までは有効で、名前の長さは d_namlen。
        let (name, kind) = unsafe {
            let entry = &*entry;
            let length = usize::from(entry.d_namlen).min(entry.d_name.len());
            (entry.d_name[..length].to_vec(), entry.d_type)
        };
        if name == b"." || name == b".." {
            continue;
        }
        let kind = match kind {
            sys::DT_DIR => EntryKind::Directory,
            sys::DT_LNK => EntryKind::Symlink,
            sys::DT_UNKNOWN => EntryKind::Unknown,
            _ => EntryKind::Other,
        };
        entries.push(Entry { name, kind });
    };
    // SAFETY: dir は opendir が返した、まだ閉じていない一覧。ここで一度だけ閉じる。
    unsafe {
        sys::closedir(dir);
    }
    result.map(|()| entries).map_err(ScandirError::Read)
}

/// 読取り用に開いたファイル。閉じるのは drop のとき。
#[derive(Debug)]
pub struct ReadFile(OwnedFd);

/// `open(path, O_RDONLY | O_CLOEXEC)` を 1 回だけ呼ぶ(FIFO は書き手が来るまで待つ。EINTR はそのまま返す)。
pub fn open_read(path: &[u8]) -> Result<ReadFile, Errno> {
    open_path(path, OFlags::RDONLY | OFlags::CLOEXEC)
        .map(ReadFile)
        .map_err(errno)
}

impl ReadFile {
    /// 開いた fd の種類と大きさ(FileIO の初期化と readall の fstat)。
    pub fn meta(&self) -> Result<Meta, Errno> {
        fs::fstat(&self.0).map(|stat| meta(&stat)).map_err(errno)
    }

    /// `FileIO.isatty()`(`_io.open` が溜めの方式を決めるときに呼ぶ。結果は読取りに効かない)。
    pub fn isatty(&self) -> bool {
        libc_isatty(self.0.as_raw_fd())
    }

    /// `lseek(fd, 0, SEEK_CUR)`(FileIO.tell と readall の位置)。
    pub fn tell(&self) -> Result<u64, Errno> {
        fs::seek(&self.0, fs::SeekFrom::Current(0)).map_err(errno)
    }

    /// `read(2)` を 1 回だけ呼ぶ(EINTR はそのまま返す)。
    pub fn read(&self, buffer: &mut [u8]) -> Result<usize, Errno> {
        rustix::io::read(&self.0, buffer).map_err(errno)
    }
}

fn borrowed(fd: i32) -> BorrowedFd<'static> {
    assert!((0..=2).contains(&fd), "標準入出力の fd だけを借りる");
    // SAFETY: 0〜2 は実行中ずっと開いている(Rust の起動処理が、閉じていた番号へ /dev/null を開く)。
    // この部品はどれも閉じない。
    unsafe { BorrowedFd::borrow_raw(fd) }
}

/// 標準入出力の fd の種類(`_Py_fstat_noraise`)。
pub fn fstat_std(fd: i32) -> Result<Meta, Errno> {
    fs::fstat(borrowed(fd))
        .map(|stat| meta(&stat))
        .map_err(errno)
}

/// libc の isatty(Python の FileIO.isatty と同じ ioctl を呼ぶ。errno を保つ)。
fn libc_isatty(fd: i32) -> bool {
    let location = errno_location();
    // SAFETY: errno の場所は呼んだスレッドで常に有効。
    let saved = unsafe { *location };
    // SAFETY: 番号を渡すだけ。開いていない番号でも 0 を返して errno を設定するだけ。
    let result = unsafe { sys::isatty(fd) } == 1;
    // SAFETY: 同じ場所へ、呼ぶ前の値を戻すだけ。
    unsafe {
        *location = saved;
    }
    result
}

/// 標準入出力の fd が端末か(FileIO.isatty)。
pub fn isatty_std(fd: i32) -> bool {
    libc_isatty(borrowed(fd).as_raw_fd())
}

/// Mac の OS の文(Python の strerror と同じ)。Linux では使わない(glibc の固定表を使う)。
pub fn os_strerror(errno: Errno) -> String {
    let text = std::io::Error::from_raw_os_error(errno).to_string();
    let suffix = format!(" (os error {errno})");
    text.strip_suffix(&suffix).unwrap_or(&text).to_owned()
}

mod sys {
    use std::ffi::{c_char, c_int, c_void};

    /// Mac の 64 ビットの inode の struct dirent(Python と同じ readdir の結果)。
    #[cfg(target_os = "macos")]
    #[repr(C)]
    pub struct Dirent {
        pub d_ino: u64,
        pub d_seekoff: u64,
        pub d_reclen: u16,
        pub d_namlen: u16,
        pub d_type: u8,
        pub d_name: [u8; 1024],
    }

    #[cfg(target_os = "macos")]
    pub const DT_UNKNOWN: u8 = 0;
    #[cfg(target_os = "macos")]
    pub const DT_DIR: u8 = 4;
    #[cfg(target_os = "macos")]
    pub const DT_LNK: u8 = 10;

    #[cfg(target_os = "linux")]
    #[repr(C)]
    pub struct SigAction {
        pub handler: usize,
        pub mask: [u64; 16],
        pub flags: c_int,
        pub restorer: usize,
    }

    #[cfg(target_os = "macos")]
    #[repr(C)]
    pub struct SigAction {
        pub handler: usize,
        pub mask: u32,
        pub flags: c_int,
    }

    #[cfg(target_os = "linux")]
    pub const SA_ONSTACK: c_int = 0x0800_0000;
    #[cfg(target_os = "macos")]
    pub const SA_ONSTACK: c_int = 0x0001;

    unsafe extern "C" {
        pub fn write(fd: c_int, buf: *const c_void, count: usize) -> isize;
        pub fn sigaction(signum: c_int, act: *const SigAction, old: *mut SigAction) -> c_int;
        pub fn kill(pid: i32, signum: c_int) -> c_int;
        pub fn getpid() -> i32;
        pub fn pthread_self() -> usize;
        #[cfg(target_os = "linux")]
        pub fn __errno_location() -> *mut c_int;
        #[cfg(target_os = "macos")]
        pub fn __error() -> *mut c_int;
        pub fn getcwd(buf: *mut c_char, size: usize) -> *mut c_char;
        pub fn isatty(fd: c_int) -> c_int;
        pub fn readlink(path: *const c_char, buf: *mut c_char, size: usize) -> isize;
        #[cfg(target_os = "linux")]
        pub fn dup(fd: c_int) -> c_int;
        #[cfg(target_os = "linux")]
        pub fn close(fd: c_int) -> c_int;
        #[cfg(target_os = "macos")]
        #[cfg_attr(target_arch = "x86_64", link_name = "fstat$INODE64")]
        pub fn fstat(fd: c_int, buf: *mut c_void) -> c_int;
        #[cfg(target_os = "macos")]
        #[cfg_attr(target_arch = "x86_64", link_name = "opendir$INODE64")]
        pub fn opendir(name: *const c_char) -> *mut c_void;
        #[cfg(target_os = "macos")]
        #[cfg_attr(target_arch = "x86_64", link_name = "readdir$INODE64")]
        pub fn readdir(dir: *mut c_void) -> *const Dirent;
        #[cfg(target_os = "macos")]
        pub fn closedir(dir: *mut c_void) -> c_int;
    }
}

/// `write(2)` を libc から 1 回だけ呼ぶ。EINTR もそのまま返す。
pub fn write(fd: i32, data: &[u8]) -> Result<usize, Errno> {
    // SAFETY: data は呼出しの間だけ有効な読取り専用の領域で、長さも data から取る。
    let written = unsafe { sys::write(fd, data.as_ptr().cast(), data.len()) };
    if written < 0 {
        Err(std::io::Error::last_os_error().raw_os_error().unwrap_or(0))
    } else {
        Ok(written.unsigned_abs())
    }
}

const SIGINT: i32 = 2;
const SIGPIPE: i32 = 13;
const SIGXFSZ: i32 = 25;
const SIG_DFL: usize = 0;
const SIG_IGN: usize = 1;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);
static HANDLING: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sigint(_signum: std::ffi::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}

fn set_handler(signum: i32, handler: usize) -> Option<usize> {
    #[cfg(target_os = "linux")]
    let action = sys::SigAction {
        handler,
        mask: [0; 16],
        flags: sys::SA_ONSTACK,
        restorer: 0,
    };
    #[cfg(target_os = "macos")]
    let action = sys::SigAction {
        handler,
        mask: 0,
        flags: sys::SA_ONSTACK,
    };
    let mut old = empty_action();
    // SAFETY: どちらの構造体も、この OS の libc の struct sigaction と同じ配置で、呼出しの間だけ有効。
    let result = unsafe { sys::sigaction(signum, &action, &mut old) };
    (result == 0).then_some(old.handler)
}

fn empty_action() -> sys::SigAction {
    #[cfg(target_os = "linux")]
    let action = sys::SigAction {
        handler: SIG_DFL,
        mask: [0; 16],
        flags: 0,
        restorer: 0,
    };
    #[cfg(target_os = "macos")]
    let action = sys::SigAction {
        handler: SIG_DFL,
        mask: 0,
        flags: 0,
    };
    action
}

fn current_handler(signum: i32) -> Option<usize> {
    let mut old = empty_action();
    // SAFETY: 新しい動作は渡さず(NULL)、古い動作を書く先だけを渡す。
    let result = unsafe { sys::sigaction(signum, std::ptr::null(), &mut old) };
    (result == 0).then_some(old.handler)
}

/// main の前(Rust の起動処理が SIGPIPE を無視にし、閉じた fd 0〜2 へ /dev/null を開く前)に控えたか。
static CAPTURED: AtomicBool = AtomicBool::new(false);
/// 控えた SIGPIPE の扱い。
static SIGPIPE_AT_START: AtomicUsize = AtomicUsize::new(SIG_DFL);
/// 控えたときに使えなかった fd 0〜2(bit 0 が fd 0)。
static INVALID_STD_AT_START: AtomicU8 = AtomicU8::new(0);

/// errno の場所(呼んだスレッドのもの)。
fn errno_location() -> *mut std::ffi::c_int {
    #[cfg(target_os = "linux")]
    // SAFETY: 引数を取らず、呼んだスレッドの errno の場所を返すだけ。
    let location = unsafe { sys::__errno_location() };
    #[cfg(target_os = "macos")]
    // SAFETY: 引数を取らず、呼んだスレッドの errno の場所を返すだけ。
    let location = unsafe { sys::__error() };
    location
}

/// CPython 3.10.11 の `is_valid_fd`(Linux は dup して閉じる。ほかは fstat)。
fn valid_fd(fd: std::ffi::c_int) -> bool {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: 番号を複製するだけ。複製できたら、その新しい番号だけを閉じる。
        let copy = unsafe { sys::dup(fd) };
        if copy >= 0 {
            // SAFETY: 直前の dup が返した、自分だけが持つ番号。
            unsafe {
                sys::close(copy);
            }
        }
        copy >= 0
    }
    #[cfg(target_os = "macos")]
    {
        // struct stat(Mac の 64 ビットの inode は 144 バイト)より大きく、8 バイトの境目に合う書込み先。
        let mut buffer = [0_u64; 32];
        // SAFETY: buffer は呼出しの間有効な、struct stat より大きい書込み先。
        unsafe { sys::fstat(fd, buffer.as_mut_ptr().cast()) == 0 }
    }
}

/// main の前に呼ぶ控え(Linux は `.init_array`、Mac は `__mod_init_func` に置く)。
///
/// 親から受け継いだ SIGPIPE の扱いと、fd 0〜2 が使えるかを読むだけ(扱いも fd も変えない。errno を保つ)。
/// 旧 Python は、起動の判定の不正な環境変数の診断を受け継いだ扱いのまま書き、閉じていた fd 0〜2 は使わない。
/// Rust の起動処理は main の前に SIGPIPE を無視にし、閉じた fd 0〜2 へ /dev/null を開くので、その前に控える。
/// 置くのは検査用の実行物だけ(製品へつなぐときは、配布の担当が入口に同じ static を置く)。
///
/// # Safety
/// main の前に一度だけ、ほかのスレッドが無い状態で呼ぶ。
pub unsafe extern "C" fn capture_before_main() {
    let location = errno_location();
    // SAFETY: errno の場所は呼んだスレッドで常に有効。
    let saved = unsafe { *location };
    let mut old = empty_action();
    // SAFETY: 新しい動作は渡さず(NULL)、古い動作を書く先だけを渡す。
    if unsafe { sys::sigaction(SIGPIPE, std::ptr::null(), &mut old) } == 0 {
        SIGPIPE_AT_START.store(old.handler, Ordering::SeqCst);
    }
    let mut invalid = 0;
    for fd in 0..=2 {
        if !valid_fd(fd) {
            invalid |= 1 << fd;
        }
    }
    INVALID_STD_AT_START.store(invalid, Ordering::SeqCst);
    CAPTURED.store(true, Ordering::SeqCst);
    // SAFETY: 同じ場所へ、読んだときの値を戻すだけ。
    unsafe {
        *location = saved;
    }
}

/// main の前の控えがあるか。
pub fn captured_before_main() -> bool {
    CAPTURED.load(Ordering::SeqCst)
}

/// 控えた SIGPIPE の扱い。Some(真) は無視、Some(偽) は既定、None は控えが無い。
pub fn sigpipe_ignored_at_start() -> Option<bool> {
    captured_before_main().then(|| SIGPIPE_AT_START.load(Ordering::SeqCst) == SIG_IGN)
}

/// 控えたときに fd(0〜2)が使えたか(Python の is_valid_fd)。控えが無ければ None。
pub fn std_valid_at_start(fd: i32) -> Option<bool> {
    let bit = u8::try_from(fd).ok().filter(|&fd| fd <= 2)?;
    captured_before_main().then(|| INVALID_STD_AT_START.load(Ordering::SeqCst) & (1 << bit) == 0)
}

/// 控えた SIGPIPE の扱いに戻す(起動の判定の 1・2 の診断を、旧 Python と同じ扱いで書くため)。
pub fn restore_start_sigpipe() {
    if captured_before_main() {
        set_handler(SIGPIPE, SIGPIPE_AT_START.load(Ordering::SeqCst));
    }
}

/// `signal_install_handlers` の `PyOS_setsig(SIGPIPE, SIG_IGN)`・`PyOS_setsig(SIGXFSZ, SIG_IGN)`。
pub fn ignore_sigpipe_and_sigxfsz() {
    set_handler(SIGPIPE, SIG_IGN);
    set_handler(SIGXFSZ, SIG_IGN);
}

/// 起動時に SIGINT が既定の動作のときだけ、印を立てる処理を置く(SA_RESTART を付けない)。
/// 置いたかを返す。無視の状態で始めたときは置かない。
pub fn install_sigint() -> bool {
    if current_handler(SIGINT) != Some(SIG_DFL) {
        return false;
    }
    INTERRUPTED.store(false, Ordering::SeqCst);
    HANDLING.store(true, Ordering::SeqCst);
    let handler = on_sigint as extern "C" fn(std::ffi::c_int) as usize;
    set_handler(SIGINT, handler).is_some()
}

/// 立った印を 1 回だけ取る(`PyErr_CheckSignals`)。処理を戻した後は常に偽。
pub fn take_interrupt() -> bool {
    HANDLING.load(Ordering::SeqCst) && INTERRUPTED.swap(false, Ordering::SeqCst)
}

/// 印が立っているか(取らずに見る)。
pub fn interrupt_pending() -> bool {
    HANDLING.load(Ordering::SeqCst) && INTERRUPTED.load(Ordering::SeqCst)
}

/// `_PySignal_Fini`。自分で置いた SIGINT の処理だけを既定の動作に戻し、立っている印を捨てる。
pub fn finish_sigint() {
    if HANDLING.swap(false, Ordering::SeqCst) {
        set_handler(SIGINT, SIG_DFL);
    }
    INTERRUPTED.store(false, Ordering::SeqCst);
}

/// `exit_sigint`。SIGINT を既定の動作に戻して自分へ送る。終わらなかったときの終了値(130)を返す。
pub fn exit_by_sigint() -> u8 {
    HANDLING.store(false, Ordering::SeqCst);
    if set_handler(SIGINT, SIG_DFL).is_some() {
        // SAFETY: 引数は自分の pid と信号の番号だけ。
        unsafe {
            sys::kill(sys::getpid(), SIGINT);
        }
    }
    130
}

/// `PyThread_get_thread_ident()`(実行中のスレッドの pthread_self の値)。
pub fn thread_ident() -> usize {
    // SAFETY: 引数を取らず、呼んだスレッドの識別子を返すだけ。
    unsafe { sys::pthread_self() }
}
