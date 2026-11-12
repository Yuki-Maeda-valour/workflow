//! fd相対の読取り。旧Python 3.10の`os`の関数と同じ呼出しと同じエラー番号にする。
//!
//! パスは`&CStr`で受ける。NULを含む入力の扱い(旧Pythonの`ValueError`)は呼出側が決める。
//! 旧Pythonが`EINTR`で呼び直す関数(`os.open`・`os.read`)は、ここでも呼び直す。

use super::{Errno, last_errno};
#[cfg(target_os = "linux")]
use rustix::fd::AsFd;
use rustix::fd::{BorrowedFd, IntoRawFd, OwnedFd};
use rustix::fs::{self as rfs, AtFlags, Mode, OFlags};
use std::ffi::CStr;

/// 旧Python 3.10の`os.stat_result`と同じ整数(符号と幅も同じ変換)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stat {
    /// `(long long)st_dev`。Macの符号付き32ビットは負の数になりうる。
    pub dev: i128,
    /// `(unsigned long long)st_ino`。
    pub ino: i128,
    pub mode: u32,
    pub nlink: i128,
    /// `(uid_t)-1`だけ`-1`、ほかは符号なし。
    pub uid: i128,
    pub gid: i128,
    pub rdev: i128,
    pub size: i128,
    pub blksize: i128,
    pub atime_ns: i128,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

impl Stat {
    pub fn file_type(&self) -> rfs::FileType {
        rfs::FileType::from_raw_mode(self.mode as rfs::RawMode)
    }
    pub fn is_dir(&self) -> bool {
        self.file_type() == rfs::FileType::Directory
    }
    pub fn is_regular(&self) -> bool {
        self.file_type() == rfs::FileType::RegularFile
    }

    pub fn is_symlink(&self) -> bool {
        self.file_type() == rfs::FileType::Symlink
    }
}

fn id_value(raw: u32) -> i128 {
    if raw == u32::MAX { -1 } else { i128::from(raw) }
}

// statの各欄の整数型はOSとCPUで違う。旧Pythonと同じCの変換を、型ごとに明示して行う。
#[allow(clippy::unnecessary_cast, clippy::useless_conversion)]
fn convert(stat: &rfs::Stat) -> Stat {
    let time = |seconds: i64, nanoseconds: i64| {
        i128::from(seconds) * 1_000_000_000 + i128::from(nanoseconds as u64)
    };
    Stat {
        dev: i128::from(stat.st_dev as i64),
        ino: i128::from(stat.st_ino as u64),
        mode: stat.st_mode as u32,
        nlink: i128::from(stat.st_nlink as i64),
        uid: id_value(stat.st_uid as u32),
        gid: id_value(stat.st_gid as u32),
        rdev: i128::from(stat.st_rdev as i64),
        size: i128::from(stat.st_size as i64),
        blksize: i128::from(stat.st_blksize as i64),
        atime_ns: time(stat.st_atime as i64, stat.st_atime_nsec as i64),
        mtime_ns: time(stat.st_mtime as i64, stat.st_mtime_nsec as i64),
        ctime_ns: time(stat.st_ctime as i64, stat.st_ctime_nsec as i64),
    }
}

fn base(dir: Option<BorrowedFd<'_>>) -> BorrowedFd<'_> {
    dir.unwrap_or(rfs::CWD)
}

/// `os.stat(path, dir_fd=dir, follow_symlinks=follow)`。`dir`が無ければ現在のディレクトリを基準にする。
pub fn stat_at(dir: Option<BorrowedFd<'_>>, path: &CStr, follow: bool) -> Result<Stat, Errno> {
    let flags = if follow {
        AtFlags::empty()
    } else {
        AtFlags::SYMLINK_NOFOLLOW
    };
    rfs::statat(base(dir), path, flags).map(|stat| convert(&stat))
}

/// `os.fstat(fd)`。
pub fn fstat(fd: BorrowedFd<'_>) -> Result<Stat, Errno> {
    rfs::fstat(fd).map(|stat| convert(&stat))
}

/// `os.open(path, flags, dir_fd=dir)`。旧Pythonと同じく`O_CLOEXEC`を足し、権限は`0o777`を渡す。
pub fn open_at(dir: Option<BorrowedFd<'_>>, path: &CStr, flags: OFlags) -> Result<OwnedFd, Errno> {
    loop {
        match rfs::openat(
            base(dir),
            path,
            flags | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o777),
        ) {
            Err(Errno::INTR) => continue,
            other => return other,
        }
    }
}

/// `os.open(b"..", flags, dir_fd=dir)`。
pub fn open_parent(dir: BorrowedFd<'_>, flags: OFlags) -> Result<OwnedFd, Errno> {
    open_at(Some(dir), c"..", flags)
}

/// `os.isatty(fd)`(旧Pythonの`FileIO.isatty`。`tcgetattr`が通るか)。
pub fn isatty(fd: BorrowedFd<'_>) -> bool {
    rustix::termios::isatty(fd)
}

/// 旧Pythonの`readlink`の受け皿の大きさ(`MAXPATHLEN`)。長い内容は旧版と同じく切れる。
#[cfg(target_os = "linux")]
const MAXPATHLEN: usize = 4096;
#[cfg(not(target_os = "linux"))]
const MAXPATHLEN: usize = 1024;

/// `os.readlink(path, dir_fd=dir)`。
pub fn read_link(dir: Option<BorrowedFd<'_>>, path: &CStr) -> Result<Vec<u8>, Errno> {
    let mut buffer = vec![0u8; MAXPATHLEN];
    let length = rfs::readlinkat_raw(base(dir), path, &mut buffer[..])?;
    buffer.truncate(length);
    Ok(buffer)
}

/// `os.read(fd, length)`。`EINTR`は呼び直す。
pub fn read(fd: BorrowedFd<'_>, length: usize) -> Result<Vec<u8>, Errno> {
    let mut buffer = vec![0u8; length];
    loop {
        match rustix::io::read(fd, &mut buffer) {
            Ok(count) => {
                buffer.truncate(count);
                return Ok(buffer);
            }
            Err(Errno::INTR) => continue,
            Err(error) => return Err(error),
        }
    }
}

/// [`write`]の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WriteError {
    Os(Errno),
    /// `EINTR`の後の確かめが止めた(旧Pythonの`PyErr_CheckSignals`が例外を投げた)。
    Interrupted,
}

/// 旧Pythonの`FileIO.write`(`_Py_write`)。書けたバイト数を返す。
///
/// `EINTR`のたびに`interrupted`を呼ぶ。真なら`Interrupted`で止め、偽なら呼び直す(旧Pythonは
/// `EINTR`のたびに信号の処理を動かし、`KeyboardInterrupt`なら書込みをやめる)。
/// 標準ライブラリの`Stdout`と違い、`EBADF`を成功に変えない(閉じ直した標準出力を旧版と同じく失敗にする)。
pub fn write(
    fd: BorrowedFd<'_>,
    bytes: &[u8],
    interrupted: &mut dyn FnMut() -> bool,
) -> Result<usize, WriteError> {
    loop {
        match rustix::io::write(fd, bytes) {
            Err(Errno::INTR) => {
                if interrupted() {
                    return Err(WriteError::Interrupted);
                }
            }
            Err(error) => return Err(WriteError::Os(error)),
            Ok(count) => return Ok(count),
        }
    }
}

/// 標準のfd(0〜2)の番号を借りる。閉じていれば、閉じた番号のまま(使えば`EBADF`)。
///
/// 旧bashの組込みの書込みと、子の向き先に使う。番号は起動の時点の記録で閉じ直した後も変えない。
pub fn standard(fd: i32) -> BorrowedFd<'static> {
    assert!((0..=2).contains(&fd), "標準のfdは0〜2");
    // SAFETY: 0〜2はこのプロセスの間ずっと同じ番号の意味を持つ(閉じていれば使うと EBADF になる)。
    // 借りた番号を閉じる所有者は作らない。
    unsafe { BorrowedFd::borrow_raw(fd) }
}

/// `read(2)`を1回(`EINTR`のたびに`interrupted`を呼び、真なら`Interrupted`で止める)。
pub fn read_checked(
    fd: BorrowedFd<'_>,
    length: usize,
    interrupted: &mut dyn FnMut() -> bool,
) -> Result<Vec<u8>, WriteError> {
    let mut buffer = vec![0u8; length];
    loop {
        match rustix::io::read(fd, &mut buffer) {
            Ok(count) => {
                buffer.truncate(count);
                return Ok(buffer);
            }
            Err(Errno::INTR) => {
                if interrupted() {
                    return Err(WriteError::Interrupted);
                }
            }
            Err(error) => return Err(WriteError::Os(error)),
        }
    }
}

/// 旧bashの`>path`(`O_WRONLY|O_CREAT|O_TRUNC`、権限`0666`。`O_CLOEXEC`を付けて開き、子へは複製で渡す)。
pub fn create_truncate(path: &CStr) -> Result<OwnedFd, Errno> {
    loop {
        match rfs::openat(
            rfs::CWD,
            path,
            OFlags::WRONLY | OFlags::CREATE | OFlags::TRUNC | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o666),
        ) {
            Err(Errno::INTR) => continue,
            other => return other,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum BoundedError<E> {
    Os(Errno),
    Check(E),
}

/// 旧`pending-implementation.py`の`read_small`の読み方。
///
/// 読むたびの前に`check`を呼ぶ。1回の読取りは`chunk`と`limit + 1 - 読んだ量`の小さいほう。
/// 終わりか、読んだ量が`limit`を超えたら止める。返す量が`limit + 1`なら上限を超えている。
pub fn read_bounded<E>(
    fd: BorrowedFd<'_>,
    limit: usize,
    chunk: usize,
    mut check: impl FnMut() -> Result<(), E>,
) -> Result<Vec<u8>, BoundedError<E>> {
    let mut data = Vec::new();
    while data.len() <= limit {
        check().map_err(BoundedError::Check)?;
        let part = read(fd, chunk.min(limit + 1 - data.len())).map_err(BoundedError::Os)?;
        if part.is_empty() {
            break;
        }
        data.extend_from_slice(&part);
    }
    Ok(data)
}

/// `os.close(fd)`。旧Pythonと同じく`EINTR`でも呼び直さず、失敗を返す。
pub fn close(fd: OwnedFd) -> Result<(), Errno> {
    let raw = fd.into_raw_fd();
    // SAFETY: OwnedFdから取り出した、このプロセスだけが持つ番号を1回だけ閉じる。
    if unsafe { libc::close(raw) } == 0 {
        Ok(())
    } else {
        Err(last_errno())
    }
}

/// `os.access(path, mode)`(`effective`なら`AT_EACCESS`。旧bashの`eaccess`)。
pub fn access(path: &CStr, mode: rfs::Access, effective: bool) -> Result<(), Errno> {
    let flags = if effective {
        AtFlags::EACCESS
    } else {
        AtFlags::empty()
    };
    rfs::accessat(rfs::CWD, path, mode, flags)
}

/// `os.access(path, os.R_OK)`(実ID。失敗はすべて偽)。
pub fn readable(path: &CStr) -> bool {
    access(path, rfs::Access::READ_OK, false).is_ok()
}

/// 旧bashの`file_status`の`FS_EXECABLE`に当たる確かめ(`eaccess(path, X_OK)`)。
pub fn x_ok(path: &CStr) -> bool {
    access(path, rfs::Access::EXEC_OK, true).is_ok()
}

/// `dup2(fd, target)`(旧bashのコマンド置換の子が、pipeを標準出力へ付け替える)。
pub fn duplicate_to(fd: BorrowedFd<'_>, target: i32) -> Result<(), Errno> {
    use rustix::fd::AsRawFd;
    // SAFETY: 開いているfdと番号を渡すだけ。番号のfdは、このプロセスの標準の番号である。
    if unsafe { libc::dup2(fd.as_raw_fd(), target) } < 0 {
        Err(super::last_errno())
    } else {
        Ok(())
    }
}

/// 番号`fd`が開いているか(`fcntl(fd, F_GETFD)`。旧bashの`sh_validfd`)。
pub fn valid_fd(fd: i32) -> bool {
    // SAFETY: 番号を調べるだけ。
    fd >= 0 && unsafe { libc::fcntl(fd, libc::F_GETFD) } >= 0
}

/// 番号`fd`へ書けるか(`fcntl(fd, F_GETFL)`の読み書きの向き。glibcの`fdopen(fd, "w")`の確かめ)。
pub fn writable_fd(fd: i32) -> bool {
    // SAFETY: 番号を調べるだけ。
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    flags >= 0 && (flags & libc::O_ACCMODE) != libc::O_RDONLY
}

/// `os.get_terminal_size(fd).columns`(`TIOCGWINSZ`)。
pub fn terminal_columns(fd: BorrowedFd<'_>) -> Result<u16, Errno> {
    rustix::termios::tcgetwinsize(fd).map(|size| size.ws_col)
}

#[derive(Debug, PartialEq, Eq)]
pub enum ScanError<E> {
    /// `os.scandir(fd)`の開始か、名前の読取りの失敗。
    Os(Errno),
    /// 呼出側が止めた。
    Stopped(E),
}

/// `with os.scandir(fd) as entries: for entry in entries: ...`と同じ順と同じ読み方で名前を渡す。
///
/// `fd`を`F_DUPFD_CLOEXEC`で複製して読む(複製は読取りの位置を共有する)。`.`と`..`は渡さない。
/// 終わり・失敗・`each`の停止のどれでも、旧Pythonと同じく位置を先頭へ戻してから複製を閉じる。
/// Linuxはglibcと同じ大きさの受け皿で`getdents64`を呼び、Macはlibcの`readdir`を使う。
pub fn scandir<E>(
    fd: BorrowedFd<'_>,
    mut each: impl FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), ScanError<E>> {
    let copy = rustix::io::fcntl_dupfd_cloexec(fd, 0).map_err(ScanError::Os)?;
    platform_scandir(copy, &mut each)
}

#[cfg(target_os = "linux")]
fn platform_scandir<E>(
    copy: OwnedFd,
    each: &mut dyn FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), ScanError<E>> {
    // glibc 2.35 の fdopendir と同じ確かめと受け皿の大きさ(st_blksize を 32KiB〜1MiB に収める)。
    let info = rfs::fstat(&copy).map_err(ScanError::Os)?;
    if rfs::FileType::from_raw_mode(info.st_mode) != rfs::FileType::Directory {
        return Err(ScanError::Os(Errno::NOTDIR));
    }
    let flags = rfs::fcntl_getfl(&copy).map_err(ScanError::Os)?;
    if flags & OFlags::ACCMODE == OFlags::WRONLY {
        return Err(ScanError::Os(Errno::INVAL));
    }
    #[allow(clippy::unnecessary_cast)]
    let size = (info.st_blksize as usize).clamp(32768, 1048576);
    let mut storage: Vec<u8> = Vec::with_capacity(size + 8);
    let spare = storage.spare_capacity_mut();
    let offset = spare.as_ptr().align_offset(8);
    let buffer = &mut spare[offset..offset + size];
    let result = (|| {
        let mut entries = rfs::RawDir::new(copy.as_fd(), buffer);
        while let Some(entry) = entries.next() {
            let entry = match entry {
                Ok(entry) => entry,
                // glibc は削除済みのディレクトリの ENOENT を終わりとして扱う。
                Err(Errno::NOENT) => break,
                Err(error) => return Err(ScanError::Os(error)),
            };
            // glibc は d_ino が 0 の項目を飛ばす。CPython は . と .. を飛ばす。
            let name = entry.file_name().to_bytes();
            if entry.ino() == 0 || name == b"." || name == b".." {
                continue;
            }
            each(name).map_err(ScanError::Stopped)?;
        }
        Ok(())
    })();
    // rewinddir と closedir。旧版は失敗を見ない。
    let _ = rfs::seek(&copy, rfs::SeekFrom::Start(0));
    drop(copy);
    result
}

#[cfg(not(target_os = "linux"))]
fn platform_scandir<E>(
    copy: OwnedFd,
    each: &mut dyn FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), ScanError<E>> {
    // 旧Pythonと同じ libc の fdopendir・readdir・rewinddir・closedir を使う。
    let mut dir = rfs::Dir::new(copy).map_err(ScanError::Os)?;
    let result = (|| {
        while let Some(entry) = dir.read() {
            let entry = entry.map_err(ScanError::Os)?;
            let name = entry.file_name().to_bytes();
            if name == b"." || name == b".." {
                continue;
            }
            each(name).map_err(ScanError::Stopped)?;
        }
        Ok(())
    })();
    dir.rewind();
    drop(dir);
    result
}

/// `os.getcwdb()`。長いパスでも、旧版のlibcと同じ結果とエラー番号を返す。
///
/// Linuxは、glibcと同じく`getcwd`のシステムコールを先に呼び、`ENAMETOOLONG`か
/// 絶対パスでない結果なら、`..`をたどる方法(glibcの`__getcwd_generic`)で求める。
/// muslの`getcwd`はこの方法を持たないので使わない。Macは旧版と同じlibcの`getcwd`を使う。
pub fn getcwd() -> Result<Vec<u8>, Errno> {
    platform_getcwd()
}

#[cfg(target_os = "linux")]
fn platform_getcwd() -> Result<Vec<u8>, Errno> {
    let mut buffer = vec![0u8; 4096];
    // SAFETY: 受け皿の長さを渡し、カーネルはその範囲だけに書く。
    let length = unsafe {
        libc::syscall(
            libc::SYS_getcwd,
            buffer.as_mut_ptr(),
            buffer.len() as libc::c_ulong,
        )
    };
    if length > 0 && buffer[0] == b'/' {
        buffer.truncate(length as usize - 1);
        return Ok(buffer);
    }
    if length < 0 {
        let error = last_errno();
        if error != Errno::NAMETOOLONG {
            return Err(error);
        }
    }
    generic_getcwd()
}

/// glibc 2.35 の`__getcwd_generic`(`openat`を使う版)と同じ手順。
#[cfg(target_os = "linux")]
#[allow(clippy::unnecessary_cast)]
fn generic_getcwd() -> Result<Vec<u8>, Errno> {
    let here = rfs::statat(rfs::CWD, c".", AtFlags::SYMLINK_NOFOLLOW)?;
    let root = rfs::statat(rfs::CWD, c"/", AtFlags::SYMLINK_NOFOLLOW)?;
    let (root_dev, root_ino) = (root.st_dev as u64, root.st_ino as u64);
    let (mut this_dev, mut this_ino) = (here.st_dev as u64, here.st_ino as u64);
    let mut parts: Vec<Vec<u8>> = Vec::new();
    let mut current: Option<OwnedFd> = None;
    while !(this_dev == root_dev && this_ino == root_ino) {
        let parent = rfs::openat(
            current.as_ref().map(|fd| fd.as_fd()).unwrap_or(rfs::CWD),
            c"..",
            OFlags::RDONLY | OFlags::CLOEXEC,
            Mode::empty(),
        )?;
        let info = rfs::fstat(&parent)?;
        let (dot_dev, dot_ino) = (info.st_dev as u64, info.st_ino as u64);
        let mount_point = dot_dev != this_dev;
        let found = find_entry(&parent, this_dev, this_ino, mount_point)?;
        parts.push(found);
        this_dev = dot_dev;
        this_ino = dot_ino;
        current = Some(parent);
    }
    if parts.is_empty() {
        return Ok(b"/".to_vec());
    }
    let mut path = Vec::new();
    for part in parts.iter().rev() {
        path.push(b'/');
        path.extend_from_slice(part);
    }
    Ok(path)
}

/// 親のディレクトリから、子の`(dev, ino)`に当たる名前を探す。
/// まず`d_ino`の一致(マウント点では全項目)で候補を絞り、見つからなければ先頭へ戻して全項目を調べる。
#[cfg(target_os = "linux")]
fn find_entry(parent: &OwnedFd, dev: u64, ino: u64, mount_point: bool) -> Result<Vec<u8>, Errno> {
    for use_d_ino in [true, false] {
        rfs::seek(parent, rfs::SeekFrom::Start(0))?;
        let info = rfs::fstat(parent)?;
        #[allow(clippy::unnecessary_cast)]
        let size = (info.st_blksize as usize).clamp(32768, 1048576);
        let mut storage: Vec<u8> = Vec::with_capacity(size);
        let mut entries = rfs::RawDir::new(parent.as_fd(), storage.spare_capacity_mut());
        while let Some(entry) = entries.next() {
            let entry = match entry {
                Ok(entry) => entry,
                Err(Errno::NOENT) => break,
                Err(error) => return Err(error),
            };
            let name = entry.file_name();
            let bytes = name.to_bytes();
            if entry.ino() == 0 || bytes == b"." || bytes == b".." {
                continue;
            }
            if use_d_ino && !(entry.ino() == ino || mount_point) {
                continue;
            }
            #[allow(clippy::unnecessary_cast)]
            if let Ok(found) = rfs::statat(parent, name, AtFlags::SYMLINK_NOFOLLOW)
                && rfs::FileType::from_raw_mode(found.st_mode) == rfs::FileType::Directory
                && found.st_dev as u64 == dev
                && found.st_ino as u64 == ino
            {
                return Ok(bytes.to_vec());
            }
        }
    }
    Err(Errno::NOENT)
}

#[cfg(not(target_os = "linux"))]
fn platform_getcwd() -> Result<Vec<u8>, Errno> {
    // 旧Pythonの posix_getcwd と同じく、1024 バイトずつ広げて ERANGE の間だけ呼び直す。
    let mut length = 0usize;
    loop {
        length += 1024;
        let mut buffer = vec![0u8; length];
        // SAFETY: 受け皿の長さを渡し、libc はその範囲だけに書く。
        let result = unsafe { libc::getcwd(buffer.as_mut_ptr().cast(), buffer.len()) };
        if !result.is_null() {
            let end = buffer.iter().position(|byte| *byte == 0).unwrap_or(length);
            buffer.truncate(end);
            return Ok(buffer);
        }
        let error = last_errno();
        if error != Errno::RANGE {
            return Err(error);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustix::fd::AsFd;
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    struct Tree(PathBuf);
    impl Tree {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!("r4g-fs-{}-{name}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn c(&self, name: &str) -> CString {
            CString::new(self.0.join(name).as_os_str().as_bytes()).unwrap()
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    extern "C" fn interrupting_handler(_signal: std::os::raw::c_int) {}

    /// 詰まったpipeへの書込みを信号で`EINTR`にし、そのたびに確かめが呼ばれ、真なら止まることを見る。
    #[test]
    fn write_calls_the_check_on_each_eintr_and_stops_when_it_says_so() {
        // SAFETY: 何もしない関数を、SA_RESTARTを付けずにSIGUSR1へ置く(書込みをEINTRで返させる)。
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction =
                interrupting_handler as extern "C" fn(std::os::raw::c_int) as libc::sighandler_t;
            libc::sigemptyset(&mut action.sa_mask);
            assert_eq!(
                libc::sigaction(libc::SIGUSR1, &action, std::ptr::null_mut()),
                0
            );
        }
        for stop_after in [1usize, 3] {
            let (reader, writer) = super::super::process::pipe().unwrap();
            // 書けるだけ書いて、pipeを詰まらせる(非ブロッキングで埋めてから戻す)。
            let flags = rustix::fs::fcntl_getfl(&writer).unwrap();
            rustix::fs::fcntl_setfl(&writer, flags | OFlags::NONBLOCK).unwrap();
            while rustix::io::write(&writer, &[0u8; 4096]).is_ok() {}
            rustix::fs::fcntl_setfl(&writer, flags).unwrap();
            let (sender, receiver) = std::sync::mpsc::channel();
            let worker = std::thread::spawn(move || {
                // SAFETY: pthread_selfは引数を取らない。
                sender
                    .send(unsafe { libc::pthread_self() } as usize)
                    .unwrap();
                let mut calls = 0;
                let result = write(writer.as_fd(), &[1u8; 4096], &mut || {
                    calls += 1;
                    calls == stop_after
                });
                (result, calls)
            });
            let thread = receiver.recv().unwrap();
            for _ in 0..stop_after {
                std::thread::sleep(std::time::Duration::from_millis(100));
                // SAFETY: まだ動いている自分の子のスレッドへ、置いた関数の信号を送る。
                assert_eq!(
                    unsafe { libc::pthread_kill(thread as libc::pthread_t, libc::SIGUSR1) },
                    0
                );
            }
            let (result, calls) = worker.join().unwrap();
            assert_eq!(result, Err(WriteError::Interrupted));
            assert_eq!(calls, stop_after);
            drop(reader);
        }
    }

    fn python(script: &str, args: &[&[u8]]) -> Vec<u8> {
        let output = Command::new("python3")
            .arg("-I")
            .arg("-c")
            .arg(script)
            .args(args.iter().map(|arg| std::ffi::OsStr::from_bytes(arg)))
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        output.stdout
    }

    fn dir_fd(path: &Path) -> OwnedFd {
        open_at(
            None,
            &CString::new(path.as_os_str().as_bytes()).unwrap(),
            OFlags::RDONLY | OFlags::DIRECTORY,
        )
        .unwrap()
    }

    #[test]
    fn stat_values_match_python() {
        let tree = Tree::new("stat");
        std::fs::write(tree.0.join("file"), b"abc").unwrap();
        std::os::unix::fs::symlink("file", tree.0.join("link")).unwrap();
        let fd = dir_fd(&tree.0);
        let script = "import os, sys\nfor name in sys.argv[2:]:\n    for follow in (True, False):\n        s = os.stat(name, dir_fd=os.open(sys.argv[1], os.O_RDONLY), follow_symlinks=follow)\n        print(s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_uid, s.st_gid, s.st_rdev, s.st_size, s.st_blksize, s.st_atime_ns, s.st_mtime_ns, s.st_ctime_ns)";
        let expected = python(
            script,
            &[tree.0.as_os_str().as_bytes(), b"file", b"link", b"."],
        );
        let mut ours = String::new();
        for name in [c"file", c"link", c"."] {
            for follow in [true, false] {
                let s = stat_at(Some(fd.as_fd()), name, follow).unwrap();
                ours.push_str(&format!(
                    "{} {} {} {} {} {} {} {} {} {} {} {}\n",
                    s.dev,
                    s.ino,
                    s.mode,
                    s.nlink,
                    s.uid,
                    s.gid,
                    s.rdev,
                    s.size,
                    s.blksize,
                    s.atime_ns,
                    s.mtime_ns,
                    s.ctime_ns
                ));
            }
        }
        assert_eq!(String::from_utf8(expected).unwrap(), ours);
        assert_eq!(
            fstat(fd.as_fd()).unwrap(),
            stat_at(None, &tree.c("."), true).unwrap()
        );
        assert_eq!(
            stat_at(Some(fd.as_fd()), c"missing", false),
            Err(Errno::NOENT)
        );
    }

    #[test]
    fn scandir_matches_python_order_and_rewinds_the_shared_offset() {
        let tree = Tree::new("scandir");
        // 32KiB の受け皿を何回も満たす数と、長さの違う名前・UTF-8でない名前を入れる。
        for index in 0..3000 {
            let name = format!("{index:05}-{}", "x".repeat(index % 200));
            std::fs::write(tree.0.join(name), b"").unwrap();
        }
        std::fs::write(tree.0.join(std::ffi::OsStr::from_bytes(b"raw-\xff")), b"").unwrap();
        let fd = dir_fd(&tree.0);
        let expected = python(
            "import os, sys\nfd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\nwith os.scandir(fd) as it:\n    sys.stdout.buffer.write(b''.join(os.fsencode(e.name) + b'\\0' for e in it))",
            &[tree.0.as_os_str().as_bytes()],
        );
        let mut ours = Vec::new();
        scandir::<()>(fd.as_fd(), |name| {
            ours.extend_from_slice(name);
            ours.push(0);
            Ok(())
        })
        .unwrap();
        assert_eq!(ours, expected);
        assert_eq!(rfs::seek(&fd, rfs::SeekFrom::Current(0)).unwrap(), 0);
        // 途中で止めても、位置は先頭へ戻る。
        let mut count = 0;
        let stopped = scandir(fd.as_fd(), |_| {
            count += 1;
            if count == 2500 { Err("stop") } else { Ok(()) }
        });
        assert_eq!(stopped, Err(ScanError::Stopped("stop")));
        assert_eq!(rfs::seek(&fd, rfs::SeekFrom::Current(0)).unwrap(), 0);
        // 通常ファイルの fd は ENOTDIR。
        let file = open_at(Some(fd.as_fd()), c"00000-", OFlags::RDONLY).unwrap();
        assert_eq!(
            scandir::<()>(file.as_fd(), |_| Ok(())),
            Err(ScanError::Os(Errno::NOTDIR))
        );
    }

    #[test]
    fn parent_links_and_bounded_reads() {
        let tree = Tree::new("read");
        std::fs::create_dir(tree.0.join("child")).unwrap();
        std::os::unix::fs::symlink("child/../target-name", tree.0.join("link")).unwrap();
        std::fs::write(tree.0.join("data"), vec![b'a'; 20000]).unwrap();
        let child = dir_fd(&tree.0.join("child"));
        let parent = open_parent(child.as_fd(), OFlags::RDONLY | OFlags::DIRECTORY).unwrap();
        assert_eq!(
            fstat(parent.as_fd()).unwrap(),
            stat_at(None, &tree.c(""), true).unwrap()
        );
        assert_eq!(
            read_link(Some(parent.as_fd()), c"link").unwrap(),
            b"child/../target-name"
        );
        assert_eq!(read_link(None, &tree.c("data")), Err(Errno::INVAL));
        // 上限ちょうどは limit バイト、超えると limit + 1 バイトで止まる。チェックは読む前に呼ぶ。
        for (limit, expected_length, expected_checks) in
            [(20000, 20000, 4), (19999, 20000, 3), (100, 101, 1)]
        {
            let file = open_at(Some(parent.as_fd()), c"data", OFlags::RDONLY).unwrap();
            let mut checks = 0;
            let data = read_bounded::<()>(file.as_fd(), limit, 8192, || {
                checks += 1;
                Ok(())
            })
            .unwrap();
            assert_eq!((data.len(), checks), (expected_length, expected_checks));
            close(file).unwrap();
        }
        let file = open_at(Some(parent.as_fd()), c"data", OFlags::RDONLY).unwrap();
        assert_eq!(
            read_bounded(file.as_fd(), 10, 8192, || Err("budget")),
            Err(BoundedError::Check("budget"))
        );
        assert_eq!(
            open_at(
                Some(parent.as_fd()),
                c"link",
                OFlags::RDONLY | OFlags::NOFOLLOW
            )
            .err(),
            Some(Errno::LOOP)
        );
        assert!(access(&tree.c("data"), rfs::Access::READ_OK, true).is_ok());
        assert_eq!(
            access(&tree.c("data"), rfs::Access::EXEC_OK, true),
            Err(Errno::ACCESS)
        );
    }

    const CHILD: &str = "R4G_FS_CHILD";
    const DIRECTORY: &str = "R4G_FS_DIRECTORY";

    fn level() -> String {
        "d".repeat(200)
    }

    /// 現在のディレクトリを変えるので、自分自身を別のプロセスで動かす。
    fn run_child(action: &str, directory: &Path) -> (Vec<u8>, Vec<u8>) {
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "r4g::fs::tests::getcwd_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD, action)
            .env(DIRECTORY, directory)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let path = directory.with_extension("report");
        let report = std::fs::read(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        let mut parts = report.splitn(2, |byte| *byte == 0);
        (
            parts.next().unwrap().to_vec(),
            parts.next().unwrap_or_default().to_vec(),
        )
    }

    /// 報告のための子。環境変数が無い通常の実行では何もしない。
    #[test]
    fn getcwd_child() {
        let Some(action) = std::env::var_os(CHILD) else {
            return;
        };
        let directory = PathBuf::from(std::env::var_os(DIRECTORY).unwrap());
        let report = directory.with_extension("report");
        std::env::set_current_dir(&directory).unwrap();
        if action == "deep" {
            // 絶対パスでは開けない長さなので、1段ずつ相対パスで移る。
            let mut depth = 0;
            while Path::new(&level()).is_dir() {
                std::env::set_current_dir(level()).unwrap();
                depth += 1;
            }
            assert_eq!(depth, 25);
        } else if action == "deleted" {
            std::env::set_current_dir("gone").unwrap();
            std::fs::remove_dir(directory.join("gone")).unwrap();
        }
        let ours = match getcwd() {
            Ok(path) => path,
            Err(error) => format!("errno {}", error.raw_os_error()).into_bytes(),
        };
        let python = Command::new("python3")
            .args([
                "-I",
                "-c",
                "import os, sys\ntry:\n    sys.stdout.buffer.write(os.getcwdb())\nexcept OSError as e:\n    sys.stdout.buffer.write(b'errno %d' % e.errno)",
            ])
            .output()
            .unwrap()
            .stdout;
        let mut text = ours;
        text.push(0);
        text.extend_from_slice(&python);
        std::fs::write(report, text).unwrap();
    }

    #[test]
    fn getcwd_matches_python_for_long_and_deleted_directories() {
        let tree = Tree::new("cwd");
        // 1段 200 バイトの名前を 25 段重ね、4096 バイトを超える現在のディレクトリを作る。
        let name = CString::new(level()).unwrap();
        let mut fd = dir_fd(&tree.0);
        for _ in 0..25 {
            rfs::mkdirat(&fd, &name, Mode::from_raw_mode(0o755)).unwrap();
            fd = open_at(Some(fd.as_fd()), &name, OFlags::RDONLY | OFlags::DIRECTORY).unwrap();
        }
        let (ours, python) = run_child("deep", &tree.0);
        assert_eq!(ours, python);
        let mut expected = tree.0.as_os_str().as_bytes().to_vec();
        for _ in 0..25 {
            expected.push(b'/');
            expected.extend_from_slice(level().as_bytes());
        }
        assert!(expected.len() > 4096);
        assert_eq!(ours, expected);
        std::fs::create_dir(tree.0.join("gone")).unwrap();
        let (ours, python) = run_child("deleted", &tree.0);
        assert_eq!(ours, python);
        assert_eq!(ours, b"errno 2");
    }
}
