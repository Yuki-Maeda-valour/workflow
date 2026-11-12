//! R4Aだけが必要とするOS境界。
//!
//! 起動時に閉じた標準fd(0〜2)は、Rust標準の実行時の初期化(`/dev/null` で開き直す)をそのまま使う
//! (2026-11-12 08:55の利用者決定)。起動時に閉じたfdを記録する部品は持たない。

use rustix::fs::{self, FileType};
use std::sync::atomic::{AtomicBool, Ordering};

unsafe extern "C" {
    fn signal(signal: i32, handler: usize) -> usize;
    fn siginterrupt(signal: i32, flag: i32) -> i32;
    fn sigaction(signal: i32, new: *const u8, old: *mut u8) -> i32;
    fn raise(signal: i32) -> i32;
    fn close(fd: i32) -> i32;
    fn pthread_self() -> usize;
    #[cfg(not(target_os = "macos"))]
    fn __errno_location() -> *mut i32;
    #[cfg(target_os = "macos")]
    fn __error() -> *mut i32;
}

/// 標準fdの現在の存在と種別。fdの所有権は取得しない。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FdKind {
    Regular,
    Directory,
    Character,
    Other,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StandardFd {
    Stdin,
    Stdout,
    Stderr,
}

/// 実fdの種別を得る。EBADF などの失敗は呼出元へ返す(起動時に閉じた fd は Rust 標準の実行時が `/dev/null` で開き直す)。
fn classify(fd: impl rustix::fd::AsFd) -> Result<FdKind, rustix::io::Errno> {
    // 所有権の取得、close、dup を行わず、Python の fstat と同じ実fdを使う。
    let stat = fs::fstat(fd)?;
    Ok(match FileType::from_raw_mode(stat.st_mode) {
        FileType::RegularFile => FdKind::Regular,
        FileType::Directory => FdKind::Directory,
        FileType::CharacterDevice => FdKind::Character,
        _ => FdKind::Other,
    })
}

pub fn standard_fd_kind(fd: StandardFd) -> Result<FdKind, rustix::io::Errno> {
    match fd {
        StandardFd::Stdin => classify(std::io::stdin()),
        StandardFd::Stdout => classify(std::io::stdout()),
        StandardFd::Stderr => classify(std::io::stderr()),
    }
}

/// 標準fdの種別・ブロック長・端末か。fdの所有権は取得しない。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StreamInfo {
    pub kind: FdKind,
    /// `st_blksize`(Python の BufferedWriter の容量の元)。
    pub blksize: u64,
    pub is_tty: bool,
}

fn info_of(fd: impl rustix::fd::AsFd) -> Result<StreamInfo, rustix::io::Errno> {
    let stat = fs::fstat(&fd)?;
    let kind = match FileType::from_raw_mode(stat.st_mode) {
        FileType::RegularFile => FdKind::Regular,
        FileType::Directory => FdKind::Directory,
        FileType::CharacterDevice => FdKind::Character,
        _ => FdKind::Other,
    };
    Ok(StreamInfo {
        kind,
        blksize: stat.st_blksize as u64,
        is_tty: rustix::termios::isatty(&fd),
    })
}

/// 標準fdの現在の状態。
pub fn standard_fd_info(fd: StandardFd) -> Result<StreamInfo, rustix::io::Errno> {
    match fd {
        StandardFd::Stdin => info_of(std::io::stdin()),
        StandardFd::Stdout => info_of(std::io::stdout()),
        StandardFd::Stderr => info_of(std::io::stderr()),
    }
}

/// 標準fdへの生の write 1 回。`EINTR` は再試行し(SIGINT を受け取っていれば旗を下ろして `Err(INTR)`)、`EAGAIN` は `Ok(None)` にする。
/// 短い write はそのまま返す(再送しない)。
pub fn write_once(fd: StandardFd, bytes: &[u8]) -> Result<Option<usize>, rustix::io::Errno> {
    fn raw(fd: impl rustix::fd::AsFd, bytes: &[u8]) -> Result<Option<usize>, rustix::io::Errno> {
        loop {
            match rustix::io::write(&fd, bytes) {
                Ok(count) => return Ok(Some(count)),
                // 旧 Python の `_Py_write` は EINTR のたびに信号を調べ、SIGINT なら KeyboardInterrupt にする。
                Err(rustix::io::Errno::INTR) if take_sigint() => {
                    return Err(rustix::io::Errno::INTR);
                }
                Err(rustix::io::Errno::INTR) => continue,
                Err(rustix::io::Errno::AGAIN) => return Ok(None),
                Err(error) => return Err(error),
            }
        }
    }
    match fd {
        StandardFd::Stdin => raw(std::io::stdin(), bytes),
        StandardFd::Stdout => raw(std::io::stdout(), bytes),
        StandardFd::Stderr => raw(std::io::stderr(), bytes),
    }
}

/// 標準入力からの生の read 1 回(`EINTR` は再試行・`EAGAIN` は `Ok(None)`)。
/// SIGINT が届いて中断されたときは `Err(INTR)`(旧 Python の `KeyboardInterrupt` に当たる)。
pub fn read_once(buffer: &mut [u8]) -> Result<Option<usize>, rustix::io::Errno> {
    read_fd_once(std::io::stdin(), buffer)
}

fn read_fd_once(
    fd: impl rustix::fd::AsFd,
    buffer: &mut [u8],
) -> Result<Option<usize>, rustix::io::Errno> {
    loop {
        match rustix::io::read(&fd, &mut *buffer) {
            // 旧 Python は、read が返った直後の次の確認(バイトコードの切れ目)で届いた SIGINT を処理する。
            Ok(_) if sigint_pending() => return Err(rustix::io::Errno::INTR),
            Ok(count) => return Ok(Some(count)),
            Err(rustix::io::Errno::INTR) if sigint_pending() => {
                return Err(rustix::io::Errno::INTR);
            }
            Err(rustix::io::Errno::INTR) => continue,
            Err(rustix::io::Errno::AGAIN) => return Ok(None),
            Err(error) => return Err(error),
        }
    }
}

const SIGINT: i32 = 2;
static SIGINT_SEEN: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sigint(_: i32) {
    SIGINT_SEEN.store(true, Ordering::SeqCst);
}

/// SIGINT の受け取りを始める(旧 Python と同じく、待っている read・open・write を中断させる)。
/// CPython は、受け継いだ扱いが既定(`SIG_DFL`)のときだけ受け手を入れる。`SIG_IGN` などは、そのまま残す。
/// `SA_RESTART` を外すため、`signal` の後に `siginterrupt` を呼ぶ。
pub fn install_sigint_handler() {
    SIGINT_SEEN.store(false, Ordering::SeqCst);
    // 受け手を入れる前に、今の扱いを調べる(入れてから戻すと、その間に届いた SIGINT が旗を立てて残る)。
    if current_sigint_handler() != SIG_DFL {
        return;
    }
    unsafe {
        signal(SIGINT, on_sigint as *const () as usize);
        siginterrupt(SIGINT, 1);
    }
}

/// `sigaction(SIGINT, NULL, &old)` で今の扱いを読む(Linux・Mac とも、構造体の先頭が受け手のアドレス)。
/// 構造体の大きさは OS ごとに違うので、十分に大きい領域を渡す。
fn current_sigint_handler() -> usize {
    let mut old = [0_usize; 64];
    let result = unsafe { sigaction(SIGINT, std::ptr::null(), old.as_mut_ptr().cast()) };
    if result == 0 { old[0] } else { SIG_DFL }
}

const SIG_DFL: usize = 0;

/// 受け取った SIGINT があるか(`install_sigint_handler` の後)。
pub fn sigint_pending() -> bool {
    SIGINT_SEEN.load(Ordering::SeqCst)
}

/// 受け取った SIGINT があれば旗を下ろして `true`(旧 Python が `KeyboardInterrupt` を起こす切れ目に当たる)。
pub fn take_sigint() -> bool {
    SIGINT_SEEN.swap(false, Ordering::SeqCst)
}

/// `open(path, "rb").read()` に当たる。`EINTR` は再試行し、SIGINT を受け取っていれば `Err(INTR)`
/// (旗は下ろさない。呼出元の次の切れ目の `take_sigint` が `KeyboardInterrupt` にする)。
pub fn read_file(path: &[u8]) -> Result<Vec<u8>, rustix::io::Errno> {
    use rustix::fs::{Mode, OFlags};
    let fd = loop {
        match fs::open(path, OFlags::RDONLY | OFlags::CLOEXEC, Mode::empty()) {
            Ok(fd) => break fd,
            Err(rustix::io::Errno::INTR) if sigint_pending() => {
                return Err(rustix::io::Errno::INTR);
            }
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => return Err(error),
        }
    };
    let mut data = Vec::new();
    let mut buffer = vec![0_u8; 65536];
    loop {
        match rustix::io::read(&fd, &mut buffer) {
            Ok(0) => return Ok(data),
            Ok(count) => data.extend_from_slice(&buffer[..count]),
            Err(rustix::io::Errno::INTR) if sigint_pending() => {
                return Err(rustix::io::Errno::INTR);
            }
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => return Err(error),
        }
    }
}

/// 既定の動作に戻して自分に SIGINT を送り、信号で終わる(旧 Python の `KeyboardInterrupt` の後の終わり方)。
pub fn exit_by_sigint() -> ! {
    unsafe {
        signal(SIGINT, 0);
        raise(SIGINT);
    }
    std::process::exit(130)
}

/// 標準出力(`sys.__stdout__`)の端末の (列, 行)。端末でなければ `None`。
pub fn terminal_size() -> Option<(i64, i64)> {
    let size = rustix::termios::tcgetwinsize(std::io::stdout()).ok()?;
    Some((i64::from(size.ws_col), i64::from(size.ws_row)))
}

/// permlog の読取りの失敗(旧版の `except (OSError, ValueError, UnicodeError)` の対象)。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PermlogError {
    /// OS の失敗。`name` は `os.open` に渡した名前(`os.read` などは `None`)。
    Os { errno: i32, name: Option<Vec<u8>> },
    /// 通常ファイルでない、または宣言サイズが 1 MiB を超える。
    NotRegularOrTooLarge,
    /// 宣言サイズまで読む前に EOF。
    Truncated,
    /// 読取り中に属性が変わった、または宣言サイズの後にまだ読める。
    Changed,
}

#[derive(Debug, Eq, PartialEq)]
pub struct PermlogRead {
    pub result: Result<Vec<u8>, PermlogError>,
    /// 逆順に fd を閉じる途中で最初に失敗した `close` の errno(旧版では未処理の `OSError`)。
    pub close_error: Option<i32>,
}

#[allow(clippy::unnecessary_cast)] // OS ごとに型が違う(Linux では同じ型)
fn attributes(stat: &rustix::fs::Stat) -> (u64, u64, u32, i64, i128, i128) {
    (
        stat.st_dev as u64,
        stat.st_ino as u64,
        stat.st_mode as u32,
        stat.st_size as i64,
        i128::from(stat.st_mtime as i64) * 1_000_000_000 + i128::from(stat.st_mtime_nsec as i64),
        i128::from(stat.st_ctime as i64) * 1_000_000_000 + i128::from(stat.st_ctime_nsec as i64),
    )
}

/// permlog の読取りの途中の出来事を、検査が観測・差込みするための点(検査専用。製品は `NoHooks` を渡す)。
/// 競合(別の処理による変更)や `close` の失敗を、決まった出来事で起こす。何も起こさなければ読取りの動作は変わらない。
pub trait PermlogHooks {
    /// 親ディレクトリ `name` を開く直前。
    fn before_parent_open(&mut self, _name: &[u8]) {}
    /// 末端を開く直前。
    fn before_leaf_open(&mut self) {}
    /// `n` 回目(1 から。最後の 1 バイトの read も数える)の read の直前。
    fn before_read(&mut self, _n: u32) {}
    /// `n` 回目の read の直後。
    fn after_read(&mut self, _n: u32) {}
    /// 末端を開いた後の `close` の失敗として報告する errno(検査用)。
    fn close_error(&mut self) -> Option<i32> {
        None
    }
}

/// 何もしない `PermlogHooks`。
pub struct NoHooks;

impl PermlogHooks for NoHooks {}

/// permlog の読取り。`components` は絶対パスを `/` で分けた 2 番目以降(空の名前を含む)。
/// `/` を開き、親は `O_DIRECTORY|O_NOFOLLOW`、末端は `O_NOFOLLOW|O_NONBLOCK` で 1 段ずつ開く。
/// 開く前の属性検査はしない。読取り後の fstat・1 バイト追加の read・属性の比較の順は旧版のまま。
/// `accepts_size` は宣言サイズ(fstat の `st_size`)を受け取り、受け付けないとき `false`(呼出元が `Budget::check_file_bytes` で決める)。
pub fn read_permlog(
    components: &[Vec<u8>],
    accepts_size: &mut dyn FnMut(u64) -> bool,
    hooks: &mut dyn PermlogHooks,
) -> PermlogRead {
    use rustix::fs::{Mode, OFlags};
    use std::os::fd::{AsFd, IntoRawFd, OwnedFd};
    let mut fds: Vec<OwnedFd> = Vec::new();
    let mut leaf_opened = false;
    let mut reads = 0_u32;
    let result = (|| -> Result<Vec<u8>, PermlogError> {
        let os = |errno: rustix::io::Errno, name: &[u8]| PermlogError::Os {
            errno: errno.raw_os_error(),
            name: Some(name.to_vec()),
        };
        let root = fs::open(
            "/",
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|e| os(e, b"/"))?;
        fds.push(root);
        let Some((last, parents)) = components.split_last() else {
            return Err(PermlogError::Os {
                errno: rustix::io::Errno::NOENT.raw_os_error(),
                name: Some(Vec::new()),
            });
        };
        for name in parents {
            hooks.before_parent_open(name);
            let fd = fs::openat(
                fds.last().expect("root").as_fd(),
                name.as_slice(),
                OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .map_err(|e| os(e, name))?;
            fds.push(fd);
        }
        hooks.before_leaf_open();
        let fd = fs::openat(
            fds.last().expect("root").as_fd(),
            last.as_slice(),
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|e| os(e, last))?;
        fds.push(fd);
        leaf_opened = true;
        let file = fds.last().expect("file").as_fd();
        let read_error = |errno: rustix::io::Errno| PermlogError::Os {
            errno: errno.raw_os_error(),
            name: None,
        };
        let before = fs::fstat(file).map_err(read_error)?;
        if FileType::from_raw_mode(before.st_mode) != FileType::RegularFile
            || !accepts_size(before.st_size as u64)
        {
            return Err(PermlogError::NotRegularOrTooLarge);
        }
        let size = before.st_size as usize;
        let mut raw: Vec<u8> = Vec::with_capacity(size);
        while raw.len() < size {
            let want = 65536.min(size - raw.len());
            let mut chunk = vec![0_u8; want];
            reads += 1;
            hooks.before_read(reads);
            let count = loop {
                match rustix::io::read(file, &mut chunk) {
                    Ok(count) => break count,
                    Err(rustix::io::Errno::INTR) => continue,
                    Err(error) => return Err(read_error(error)),
                }
            };
            hooks.after_read(reads);
            if count == 0 {
                return Err(PermlogError::Truncated);
            }
            raw.extend_from_slice(&chunk[..count]);
        }
        let after = fs::fstat(file).map_err(read_error)?;
        let mut extra = [0_u8; 1];
        reads += 1;
        hooks.before_read(reads);
        let more = loop {
            match rustix::io::read(file, &mut extra) {
                Ok(count) => break count,
                Err(rustix::io::Errno::INTR) => continue,
                Err(error) => return Err(read_error(error)),
            }
        };
        hooks.after_read(reads);
        if more != 0 || attributes(&after) != attributes(&before) {
            return Err(PermlogError::Changed);
        }
        Ok(raw)
    })();
    let mut close_error = None;
    while let Some(fd) = fds.pop() {
        let raw = fd.into_raw_fd();
        if unsafe { close(raw) } == -1 {
            let errno = unsafe { *errno_location() };
            close_error = Some(errno);
            break;
        }
    }
    if close_error.is_none() && leaf_opened {
        close_error = hooks.close_error();
    }
    PermlogRead {
        result,
        close_error,
    }
}

pub fn again_errno() -> i32 {
    rustix::io::Errno::AGAIN.raw_os_error()
}

/// 現在の native thread を pthread の不透明値として返す。
pub fn pthread_identity() -> u64 {
    unsafe { pthread_self() as u64 }
}

unsafe fn errno_location() -> *mut i32 {
    #[cfg(not(target_os = "macos"))]
    {
        unsafe { __errno_location() }
    }
    #[cfg(target_os = "macos")]
    {
        unsafe { __error() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pthread_identity_is_stable_here_and_differs_in_other_thread() {
        let here = pthread_identity();
        assert_eq!(here, unsafe { pthread_self() as u64 });
        let other = std::thread::spawn(pthread_identity).join().unwrap();
        assert_ne!(here, other);
    }

    #[test]
    fn info_and_raw_io_on_owned_fds() {
        let directory = std::fs::File::open(".").unwrap();
        let info = info_of(&directory).unwrap();
        assert_eq!((info.kind, info.is_tty), (FdKind::Directory, false));
        let null = std::fs::File::open("/dev/null").unwrap();
        assert!(info_of(&null).unwrap().blksize > 0);
        // 満杯の装置への write は ENOSPC(`/dev/full` は Linux だけにある)。
        #[cfg(target_os = "linux")]
        {
            let full = std::fs::OpenOptions::new()
                .write(true)
                .open("/dev/full")
                .unwrap();
            assert_eq!(
                rustix::io::write(&full, b"x"),
                Err(rustix::io::Errno::NOSPC)
            );
        }
        let (read_end, write_end) = std::os::unix::net::UnixStream::pair().unwrap();
        write_end.set_nonblocking(true).unwrap();
        let mut total = 0;
        loop {
            match rustix::io::write(&write_end, &[0_u8; 4096]) {
                Ok(count) => total += count,
                Err(rustix::io::Errno::AGAIN) => break,
                Err(other) => panic!("{other:?}"),
            }
        }
        assert!(total >= 4096);
        drop(read_end);
    }

    fn within_limit(size: u64) -> bool {
        size <= 1_048_576
    }

    fn parts(path: &std::path::Path) -> Vec<Vec<u8>> {
        use std::os::unix::ffi::OsStrExt;
        path.as_os_str()
            .as_bytes()
            .split(|b| *b == b'/')
            .skip(1)
            .map(<[u8]>::to_vec)
            .collect()
    }

    #[test]
    fn permlog_reads_regular_files_and_reports_each_refusal() {
        // Mac の一時フォルダは `/var/folders/...`(`/var` は `/private/var` へのリンク)。親を辿らない確かめのため、実体のパスにする。
        let temp = std::env::temp_dir().canonicalize().unwrap();
        let dir = temp.join(format!("r4a-permlog-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("log");
        std::fs::write(&file, b"abc\n").unwrap();
        assert_eq!(
            read_permlog(&parts(&file), &mut within_limit, &mut NoHooks).result,
            Ok(b"abc\n".to_vec())
        );
        std::fs::write(&file, b"").unwrap();
        assert_eq!(
            read_permlog(&parts(&file), &mut within_limit, &mut NoHooks).result,
            Ok(Vec::new())
        );
        // 末端のリンクは ELOOP(Linux は 40・Mac は 62)。
        let link = dir.join("link");
        std::os::unix::fs::symlink(&file, &link).unwrap();
        assert!(matches!(
            read_permlog(&parts(&link), &mut within_limit, &mut NoHooks).result,
            Err(PermlogError::Os { errno, .. }) if errno == rustix::io::Errno::LOOP.raw_os_error()
        ));
        // 親のリンクも辿らない(`O_DIRECTORY|O_NOFOLLOW` の組は ENOTDIR=20)。
        let linked_dir = temp.join(format!("r4a-permlog-dir-{}", std::process::id()));
        let _ = std::fs::remove_file(&linked_dir);
        std::os::unix::fs::symlink(&dir, &linked_dir).unwrap();
        assert!(matches!(
            read_permlog(
                &parts(&linked_dir.join("log")),
                &mut within_limit,
                &mut NoHooks
            )
            .result,
            Err(PermlogError::Os { errno: 20, .. })
        ));
        // ディレクトリは通常ファイルでない。不在は ENOENT で名前を持つ。
        assert_eq!(
            read_permlog(&parts(&dir), &mut within_limit, &mut NoHooks).result,
            Err(PermlogError::NotRegularOrTooLarge)
        );
        assert!(matches!(
            read_permlog(&parts(&dir.join("absent")), &mut within_limit, &mut NoHooks).result,
            Err(PermlogError::Os {
                errno: 2,
                name: Some(_)
            })
        ));
        // 1 MiB の境界。
        std::fs::write(&file, vec![b'x'; 1_048_576]).unwrap();
        assert_eq!(
            read_permlog(&parts(&file), &mut within_limit, &mut NoHooks)
                .result
                .map(|v| v.len()),
            Ok(1_048_576)
        );
        std::fs::write(&file, vec![b'x'; 1_048_577]).unwrap();
        assert_eq!(
            read_permlog(&parts(&file), &mut within_limit, &mut NoHooks).result,
            Err(PermlogError::NotRegularOrTooLarge)
        );
        // 空の名前(`//x` の形)は ENOENT。
        assert!(matches!(
            read_permlog(
                &[Vec::new(), b"x".to_vec()],
                &mut within_limit,
                &mut NoHooks
            )
            .result,
            Err(PermlogError::Os { errno: 2, .. })
        ));
        let _ = std::fs::remove_file(&linked_dir);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn sigint_is_ignored_when_inherited_and_interrupts_a_blocked_read_otherwise() {
        unsafe extern "C" {
            fn pthread_kill(thread: usize, signal: i32) -> i32;
        }
        // 受け継いだ扱いが SIG_IGN なら、受け手を入れず、そのまま残す(CPython と同じ)。
        unsafe { signal(SIGINT, 1) };
        install_sigint_handler();
        assert_eq!(current_sigint_handler(), 1);
        // 自分に SIGINT を送っても無視され、旗は立たない。
        unsafe { raise(SIGINT) };
        assert!(!sigint_pending());
        // 既定なら受け手を入れ、待っている read を中断する。届くまで短い間隔で送り続ける(sleep の偶然に頼らない)。
        unsafe { signal(SIGINT, SIG_DFL) };
        install_sigint_handler();
        let (reader, _writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let main_thread = pthread_identity() as usize;
        let done = std::sync::Arc::new(AtomicBool::new(false));
        let stop = std::sync::Arc::clone(&done);
        let sender = std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                std::thread::sleep(std::time::Duration::from_millis(5));
                unsafe { pthread_kill(main_thread, SIGINT) };
            }
        });
        let mut buffer = [0_u8; 8];
        let result = read_fd_once(&reader, &mut buffer);
        done.store(true, Ordering::SeqCst);
        sender.join().unwrap();
        assert_eq!(result, Err(rustix::io::Errno::INTR));
        assert!(sigint_pending());
        assert!(take_sigint());
        assert!(!sigint_pending());
        // 後始末: 既定の動作へ戻す。
        unsafe { signal(SIGINT, SIG_DFL) };
    }

    #[test]
    fn read_file_reads_a_file_and_reports_errors() {
        let path = std::env::temp_dir().join(format!("r4a-read-file-{}", std::process::id()));
        std::fs::write(&path, b"abc").unwrap();
        use std::os::unix::ffi::OsStrExt;
        assert_eq!(read_file(path.as_os_str().as_bytes()), Ok(b"abc".to_vec()));
        std::fs::remove_file(&path).unwrap();
        assert_eq!(
            read_file(path.as_os_str().as_bytes()),
            Err(rustix::io::Errno::NOENT)
        );
        assert_eq!(read_file(b"."), Err(rustix::io::Errno::ISDIR));
    }

    #[test]
    fn terminal_size_is_none_without_a_terminal() {
        // テストの標準出力は端末でない(捕捉される)ので取得できない。
        if !rustix::termios::isatty(std::io::stdout()) {
            assert_eq!(terminal_size(), None);
        }
    }

    #[test]
    fn fd_kind_uses_live_owners() {
        let directory = std::fs::File::open(".").unwrap();
        assert_eq!(classify(&directory), Ok(FdKind::Directory));
        let path = std::env::temp_dir().join(format!("r4a-rest-fd-kind-{}", std::process::id()));
        let regular = std::fs::File::create(&path).unwrap();
        assert_eq!(classify(&regular), Ok(FdKind::Regular));
        let character = std::fs::File::open("/dev/null").unwrap();
        assert_eq!(classify(&character), Ok(FdKind::Character));
        let mut child = std::process::Command::new("true")
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        assert_eq!(classify(child.stdout.as_ref().unwrap()), Ok(FdKind::Other));
        let _ = child.wait();
        let _ = std::fs::remove_file(path);
    }
}
