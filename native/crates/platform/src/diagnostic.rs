//! R4D-2 専用。診断の上限付きの読み取り(旧版 `diagnostic-runtime.py` の `Reader`)が使う OS の操作。
//!
//! 旧版の Python 3.10.11 の `os` の関数と同じ呼出しと同じ errno にする。どの関数も OS を 1 回だけ呼び、
//! EINTR で呼び直さない(旧版と同じ呼び直しは、呼出側の Reader の継ぎ目で行う)。
//! 名前やパスに NUL があれば、OS を呼ばずに [`DiagError::Nul`](旧版の `ValueError`)を返す。
//! 新しい `unsafe` は足さない。閉じる失敗は `fs::close_owned` で返す。

#[cfg(target_os = "linux")]
use rustix::fd::AsFd;
use rustix::fd::{BorrowedFd, OwnedFd};
use rustix::fs::{self as rfs, AtFlags, Mode, OFlags};
use rustix::io::Errno;

/// 旧版の `os.stat_result` と同じ値(旧版が使う 7 欄)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stat {
    /// `(long long)st_dev`(本家 3.10.11 の `_PyLong_FromDev` は `PyLong_FromLongLong`)。
    pub dev: i64,
    /// `(unsigned long long)st_ino`。
    pub ino: u64,
    pub mode: u32,
    pub size: i64,
    pub uid: u32,
    /// 秒 × 10 億 + ナノ秒。2262 年より後の時刻も桁あふれさせない。
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

/// `stat.S_IFMT` の印。
pub const S_IFMT: u32 = 0o170000;
const S_IFREG: u32 = 0o100000;
const S_IFDIR: u32 = 0o040000;

impl Stat {
    /// `stat.S_IFMT(mode)`。
    pub fn format(&self) -> u32 {
        self.mode & S_IFMT
    }
    pub fn is_regular(&self) -> bool {
        self.format() == S_IFREG
    }
    pub fn is_dir(&self) -> bool {
        self.format() == S_IFDIR
    }
}

// stat の各欄の整数型は OS と CPU で違う。旧版と同じ C の変換を、型ごとに明示して行う。
#[allow(clippy::unnecessary_cast, clippy::useless_conversion)]
fn convert(stat: &rfs::Stat) -> Stat {
    let time = |seconds: i64, nanoseconds: i64| {
        i128::from(seconds) * 1_000_000_000 + i128::from(nanoseconds as u64)
    };
    Stat {
        dev: stat.st_dev as i64,
        ino: stat.st_ino as u64,
        mode: stat.st_mode as u32,
        size: stat.st_size as i64,
        uid: stat.st_uid as u32,
        mtime_ns: time(stat.st_mtime as i64, stat.st_mtime_nsec as i64),
        ctime_ns: time(stat.st_ctime as i64, stat.st_ctime_nsec as i64),
    }
}

/// OS の失敗(errno の番号)か、NUL を含む名前(旧版の `ValueError`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DiagError {
    Os(i32),
    Nul,
}

impl From<Errno> for DiagError {
    fn from(error: Errno) -> Self {
        Self::Os(error.raw_os_error())
    }
}

/// よく使う errno の番号(この OS の値)。
pub const ENOENT: i32 = Errno::NOENT.raw_os_error();
pub const ENOTDIR: i32 = Errno::NOTDIR.raw_os_error();
pub const EBADF: i32 = Errno::BADF.raw_os_error();
pub const EINTR: i32 = Errno::INTR.raw_os_error();
pub const EIO: i32 = Errno::IO.raw_os_error();
pub const ELOOP: i32 = Errno::LOOP.raw_os_error();
pub const EACCES: i32 = Errno::ACCESS.raw_os_error();
pub const ENAMETOOLONG: i32 = Errno::NAMETOOLONG.raw_os_error();

/// errno の名前(Python の `errno.errorcode` と同じ字面)。表に無い番号は `E<番号>`。
pub fn errno_name(number: i32) -> String {
    let table = [
        (Errno::PERM, "EPERM"),
        (Errno::NOENT, "ENOENT"),
        (Errno::SRCH, "ESRCH"),
        (Errno::INTR, "EINTR"),
        (Errno::IO, "EIO"),
        (Errno::NXIO, "ENXIO"),
        (Errno::TOOBIG, "E2BIG"),
        (Errno::NOEXEC, "ENOEXEC"),
        (Errno::BADF, "EBADF"),
        (Errno::CHILD, "ECHILD"),
        (Errno::AGAIN, "EAGAIN"),
        (Errno::NOMEM, "ENOMEM"),
        (Errno::ACCESS, "EACCES"),
        (Errno::FAULT, "EFAULT"),
        (Errno::BUSY, "EBUSY"),
        (Errno::EXIST, "EEXIST"),
        (Errno::XDEV, "EXDEV"),
        (Errno::NODEV, "ENODEV"),
        (Errno::NOTDIR, "ENOTDIR"),
        (Errno::ISDIR, "EISDIR"),
        (Errno::INVAL, "EINVAL"),
        (Errno::NFILE, "ENFILE"),
        (Errno::MFILE, "EMFILE"),
        (Errno::NOTTY, "ENOTTY"),
        (Errno::FBIG, "EFBIG"),
        (Errno::NOSPC, "ENOSPC"),
        (Errno::SPIPE, "ESPIPE"),
        (Errno::ROFS, "EROFS"),
        (Errno::MLINK, "EMLINK"),
        (Errno::PIPE, "EPIPE"),
        (Errno::NAMETOOLONG, "ENAMETOOLONG"),
        (Errno::LOOP, "ELOOP"),
        (Errno::NOTEMPTY, "ENOTEMPTY"),
        (Errno::OVERFLOW, "EOVERFLOW"),
        (Errno::STALE, "ESTALE"),
    ];
    table
        .iter()
        .find(|(errno, _)| errno.raw_os_error() == number)
        .map_or_else(|| format!("E{number}"), |(_, name)| (*name).to_owned())
}

fn reject_nul(name: &[u8]) -> Result<(), DiagError> {
    if name.contains(&0) {
        Err(DiagError::Nul)
    } else {
        Ok(())
    }
}

fn dir_flags() -> OFlags {
    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC
}

/// `os.open(b'/', O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC)`。
pub fn open_root() -> Result<OwnedFd, DiagError> {
    Ok(rfs::open("/", dir_flags(), Mode::empty())?)
}

/// `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC, dir_fd=dir)`。
pub fn open_dir_at(dir: BorrowedFd<'_>, name: &[u8]) -> Result<OwnedFd, DiagError> {
    reject_nul(name)?;
    Ok(rfs::openat(dir, name, dir_flags(), Mode::empty())?)
}

/// `os.open(name, O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC, dir_fd=dir)`。
pub fn open_file_at(dir: BorrowedFd<'_>, name: &[u8]) -> Result<OwnedFd, DiagError> {
    reject_nul(name)?;
    let flags = OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    Ok(rfs::openat(dir, name, flags, Mode::empty())?)
}

/// `os.stat(name, dir_fd=dir, follow_symlinks=False)`。空の名前は検査せずに OS へ渡す(ENOENT)。
pub fn stat_at(dir: BorrowedFd<'_>, name: &[u8]) -> Result<Stat, DiagError> {
    reject_nul(name)?;
    Ok(convert(&rfs::statat(dir, name, AtFlags::SYMLINK_NOFOLLOW)?))
}

/// `os.fstat(fd)`。
pub fn fstat(fd: BorrowedFd<'_>) -> Result<Stat, DiagError> {
    Ok(convert(&rfs::fstat(fd)?))
}

/// `os.read(fd, max)`。読めた分だけの長さにして返す(0 バイトは終わり)。
pub fn read(fd: BorrowedFd<'_>, max: usize) -> Result<Vec<u8>, DiagError> {
    let mut buffer = vec![0u8; max];
    let count = rustix::io::read(fd, &mut buffer[..])?;
    buffer.truncate(count);
    Ok(buffer)
}

/// `os.close(fd)`。閉じる失敗を返す(失敗しても fd は再び使わない)。
pub fn close(fd: OwnedFd) -> Result<(), DiagError> {
    crate::fs::close_owned(fd).map_err(|error| match error {
        crate::fs::FsError::Io(error) => DiagError::Os(error.raw_os_error().unwrap_or(EIO)),
        _ => DiagError::Os(EIO),
    })
}

/// `os.geteuid()`。
pub fn geteuid() -> u32 {
    rustix::process::geteuid().as_raw()
}

/// 一覧の読み取りの失敗。
#[derive(Debug, PartialEq, Eq)]
pub enum NamesError<E> {
    /// 複製・一覧の開始・名前の読み取りの失敗。
    Os(i32),
    /// 呼出元の関数が止めた。
    Stopped(E),
}

/// `with os.scandir(fd) as entries: for entry in entries: ...` と同じ順と同じ読み方で名前を渡す。
///
/// `fd` を `F_DUPFD_CLOEXEC` で複製して読む(複製は読み取りの位置を共有する)。`.` と `..` は渡さない。
/// 終わり・失敗・`each` の停止のどれでも、旧版と同じく位置を先頭へ戻してから複製を閉じる(閉じる失敗は見ない)。
/// Linux は glibc と同じ大きさの受け皿で `getdents64` を呼び、Mac は libc の `readdir` を使う
/// (R4G の `r4g/fs.rs` の `scandir` と同じ手順。R4G 専用のモジュールなので呼ばずに写した)。
pub fn names<E>(
    fd: BorrowedFd<'_>,
    mut each: impl FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), NamesError<E>> {
    let copy =
        rustix::io::fcntl_dupfd_cloexec(fd, 0).map_err(|e| NamesError::Os(e.raw_os_error()))?;
    platform_names(copy, &mut each)
}

#[cfg(target_os = "linux")]
fn platform_names<E>(
    copy: OwnedFd,
    each: &mut dyn FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), NamesError<E>> {
    let os = |error: Errno| NamesError::Os(error.raw_os_error());
    // glibc 2.35 の fdopendir と同じ確かめと受け皿の大きさ(st_blksize を 32KiB〜1MiB に収める)。
    let info = rfs::fstat(&copy).map_err(os)?;
    if rfs::FileType::from_raw_mode(info.st_mode) != rfs::FileType::Directory {
        return Err(os(Errno::NOTDIR));
    }
    let flags = rfs::fcntl_getfl(&copy).map_err(os)?;
    if flags & OFlags::ACCMODE == OFlags::WRONLY {
        return Err(os(Errno::INVAL));
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
                // glibc は削除済みのフォルダの ENOENT を終わりとして扱う。
                Err(Errno::NOENT) => break,
                Err(error) => return Err(os(error)),
            };
            // glibc は d_ino が 0 の項目を飛ばす。CPython は . と .. を飛ばす。
            let name = entry.file_name().to_bytes();
            if entry.ino() == 0 || name == b"." || name == b".." {
                continue;
            }
            each(name).map_err(NamesError::Stopped)?;
        }
        Ok(())
    })();
    // rewinddir と closedir。旧版は失敗を見ない。
    let _ = rfs::seek(&copy, rfs::SeekFrom::Start(0));
    drop(copy);
    result
}

#[cfg(not(target_os = "linux"))]
fn platform_names<E>(
    copy: OwnedFd,
    each: &mut dyn FnMut(&[u8]) -> Result<(), E>,
) -> Result<(), NamesError<E>> {
    // 旧版と同じ libc の fdopendir・readdir・rewinddir・closedir を使う。
    let mut dir = rfs::Dir::new(copy).map_err(|e| NamesError::Os(e.raw_os_error()))?;
    let result = (|| {
        while let Some(entry) = dir.read() {
            let entry = entry.map_err(|e| NamesError::Os(e.raw_os_error()))?;
            let name = entry.file_name().to_bytes();
            if name == b"." || name == b".." {
                continue;
            }
            each(name).map_err(NamesError::Stopped)?;
        }
        Ok(())
    })();
    dir.rewind();
    drop(dir);
    result
}

/// `os.getcwdb()`。長いパスでも、旧版の libc と同じ結果と errno を返す。
///
/// Linux は rustix の安全な `getcwd`(システムコール)を先に呼び、ENAMETOOLONG か
/// `/` で始まらない結果なら、`..` をたどる方法(glibc の `__getcwd_generic`)で求める
/// (R4G の `r4g/fs.rs` の `getcwd` と同じ手順を写した)。Mac は rustix の結果をそのまま使う(rustix が libc を呼ぶ)。
pub fn getcwd() -> Result<Vec<u8>, DiagError> {
    platform_getcwd()
}

#[cfg(target_os = "linux")]
fn platform_getcwd() -> Result<Vec<u8>, DiagError> {
    match rustix::process::getcwd(Vec::new()) {
        Ok(path) if path.as_bytes().first() == Some(&b'/') => Ok(path.into_bytes()),
        Ok(_) | Err(Errno::NAMETOOLONG) => generic_getcwd().map_err(DiagError::from),
        Err(error) => Err(error.into()),
    }
}

/// glibc 2.35 の `__getcwd_generic`(`openat` を使う版)と同じ手順。
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

/// 親のフォルダから、子の `(dev, ino)` に当たる名前を探す。
/// まず `d_ino` の一致(マウント点では全項目)で候補を絞り、見つからなければ先頭へ戻して全項目を調べる。
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
fn platform_getcwd() -> Result<Vec<u8>, DiagError> {
    Ok(rustix::process::getcwd(Vec::new())?.into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rustix::fd::AsFd;
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;

    struct Tree(PathBuf);
    impl Tree {
        fn new(name: &str) -> Self {
            let path =
                std::env::temp_dir().join(format!("r4d2-platform-{}-{name}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// 木の根のフォルダを、`/` から 1 段ずつ開く(製品と同じ旗)。
    fn open_path(path: &Path) -> OwnedFd {
        let mut fd = open_root().unwrap();
        for part in path.as_os_str().as_bytes().split(|b| *b == b'/') {
            if part.is_empty() {
                continue;
            }
            let child = open_dir_at(fd.as_fd(), part).unwrap();
            close(fd).unwrap();
            fd = child;
        }
        fd
    }

    fn python_open_errno(path: &Path, directory: bool) -> String {
        let flags = if directory {
            "os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC"
        } else {
            "os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW|os.O_CLOEXEC"
        };
        let script = format!(
            "import errno, os, sys\ntry:\n    os.close(os.open(sys.argv[1], {flags}))\n    print('ok')\nexcept OSError as e:\n    print(errno.errorcode[e.errno])"
        );
        let output = Command::new("python3")
            .args(["-I", "-c", &script])
            .arg(path)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }

    #[test]
    fn links_are_refused_with_the_same_errno_as_python() {
        let tree = Tree::new("links");
        std::fs::create_dir(tree.0.join("dir")).unwrap();
        std::fs::write(tree.0.join("file"), b"abc").unwrap();
        std::os::unix::fs::symlink("dir", tree.0.join("dir-link")).unwrap();
        std::os::unix::fs::symlink("file", tree.0.join("file-link")).unwrap();
        let fd = open_path(&tree.0);
        for (name, directory) in [
            ("dir-link", true),
            ("file-link", false),
            ("file", true),
            ("dir-link", false),
        ] {
            let ours = if directory {
                open_dir_at(fd.as_fd(), name.as_bytes())
            } else {
                open_file_at(fd.as_fd(), name.as_bytes())
            };
            let ours = match ours {
                Ok(opened) => {
                    close(opened).unwrap();
                    "ok".to_owned()
                }
                Err(DiagError::Os(number)) => errno_name(number),
                Err(DiagError::Nul) => "nul".to_owned(),
            };
            assert_eq!(
                ours,
                python_open_errno(&tree.0.join(name), directory),
                "{name} {directory}"
            );
            assert_ne!(ours, "ok", "{name} {directory}");
        }
        // 最後の名前のリンクは、辿らずに調べる。
        let link = stat_at(fd.as_fd(), b"file-link").unwrap();
        assert_eq!(link.format(), 0o120000);
        assert!(stat_at(fd.as_fd(), b"file").unwrap().is_regular());
        close(fd).unwrap();
    }

    #[test]
    fn fifo_opens_without_blocking() {
        let tree = Tree::new("fifo");
        let fd = open_path(&tree.0);
        // rustix の mkfifoat は Mac に無いので、FIFO は mkfifo のコマンドで作る(検査の準備だけに使う)。
        let status = Command::new("mkfifo")
            .arg(tree.0.join("fifo"))
            .status()
            .unwrap();
        assert!(status.success());
        // 書く側が無くても、O_NONBLOCK なので止まらずに開ける。
        let opened = open_file_at(fd.as_fd(), b"fifo").unwrap();
        assert_eq!(fstat(opened.as_fd()).unwrap().format(), 0o010000);
        assert_eq!(read(opened.as_fd(), 10).unwrap(), b"");
        close(opened).unwrap();
        close(fd).unwrap();
    }

    #[test]
    fn empty_names_reach_the_os_and_nul_does_not() {
        let tree = Tree::new("names");
        let fd = open_path(&tree.0);
        assert_eq!(stat_at(fd.as_fd(), b""), Err(DiagError::Os(ENOENT)));
        assert_eq!(stat_at(fd.as_fd(), b"a\0b"), Err(DiagError::Nul));
        assert_eq!(open_dir_at(fd.as_fd(), b"\0").err(), Some(DiagError::Nul));
        assert_eq!(open_file_at(fd.as_fd(), b"x\0").err(), Some(DiagError::Nul));
        assert_eq!(stat_at(fd.as_fd(), b"missing"), Err(DiagError::Os(ENOENT)));
        assert_eq!(errno_name(ENOENT), "ENOENT");
        assert_eq!(errno_name(-7), "E-7");
        close(fd).unwrap();
    }

    #[test]
    fn stat_values_match_python() {
        let tree = Tree::new("stat");
        std::fs::write(tree.0.join("file"), b"abcd").unwrap();
        let fd = open_path(&tree.0);
        let ours = stat_at(fd.as_fd(), b"file").unwrap();
        let output = Command::new("python3")
            .args([
                "-I",
                "-c",
                "import os, sys\ns = os.lstat(sys.argv[1])\nprint(s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_uid, s.st_mtime_ns, s.st_ctime_ns)",
            ])
            .arg(tree.0.join("file"))
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert_eq!(
            String::from_utf8(output.stdout).unwrap().trim(),
            format!(
                "{} {} {} {} {} {} {}",
                ours.dev, ours.ino, ours.mode, ours.size, ours.uid, ours.mtime_ns, ours.ctime_ns
            )
        );
        assert_eq!(ours.uid, geteuid());
        let file = open_file_at(fd.as_fd(), b"file").unwrap();
        assert_eq!(fstat(file.as_fd()).unwrap(), ours);
        assert_eq!(read(file.as_fd(), 3).unwrap(), b"abc");
        assert_eq!(read(file.as_fd(), 3).unwrap(), b"d");
        assert_eq!(read(file.as_fd(), 3).unwrap(), b"");
        close(file).unwrap();
        close(fd).unwrap();
    }

    #[test]
    fn names_skip_dots_stop_on_request_and_rewind() {
        let tree = Tree::new("list");
        for index in 0..300 {
            std::fs::write(tree.0.join(format!("{index:04}")), b"").unwrap();
        }
        let fd = open_path(&tree.0);
        let mut seen = Vec::new();
        names::<()>(fd.as_fd(), |name| {
            seen.push(name.to_vec());
            Ok(())
        })
        .unwrap();
        assert_eq!(seen.len(), 300);
        assert!(!seen.iter().any(|name| name == b"." || name == b".."));
        seen.sort();
        assert_eq!(seen[0], b"0000");
        assert_eq!(rfs::seek(&fd, rfs::SeekFrom::Current(0)).unwrap(), 0);
        // 途中で止めても、位置は先頭へ戻り、呼出元の失敗をそのまま返す。
        let mut count = 0;
        let stopped = names(fd.as_fd(), |_| {
            count += 1;
            if count == 7 { Err("stop") } else { Ok(()) }
        });
        assert_eq!(stopped, Err(NamesError::Stopped("stop")));
        assert_eq!(count, 7);
        assert_eq!(rfs::seek(&fd, rfs::SeekFrom::Current(0)).unwrap(), 0);
        // 通常ファイルの fd は ENOTDIR。
        let file = open_file_at(fd.as_fd(), b"0000").unwrap();
        assert_eq!(
            names::<()>(file.as_fd(), |_| Ok(())),
            Err(NamesError::Os(ENOTDIR))
        );
        close(file).unwrap();
        close(fd).unwrap();
    }

    const CHILD: &str = "R4D2_PLATFORM_CWD_CHILD";
    const DIRECTORY: &str = "R4D2_PLATFORM_CWD_DIRECTORY";

    fn level() -> String {
        "d".repeat(200)
    }

    /// 作業場所を変えるので、自分自身を別のプロセスで動かす(R4G の `r4g/fs.rs` の検査と同じ手順)。
    fn run_child(action: &str, directory: &Path) -> (Vec<u8>, Vec<u8>) {
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "diagnostic::tests::getcwd_child",
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
            // 絶対パスでは開けない長さなので、1 段ずつ相対パスで移る。
            while Path::new(&level()).is_dir() {
                std::env::set_current_dir(level()).unwrap();
            }
        } else if action == "deleted" {
            std::env::set_current_dir("gone").unwrap();
            std::fs::remove_dir(directory.join("gone")).unwrap();
        }
        let ours = match getcwd() {
            Ok(path) => path,
            Err(DiagError::Os(number)) => format!("errno {number}").into_bytes(),
            Err(DiagError::Nul) => b"nul".to_vec(),
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
        // 1 段 200 バイトの名前を 25 段重ね、4096 バイトを超える作業場所を作る(開いたフォルダからの相対の mkdir)。
        let name = level();
        let mut fd = open_path(&tree.0);
        for _ in 0..25 {
            rfs::mkdirat(&fd, OsStr::new(&name), Mode::from_raw_mode(0o755)).unwrap();
            let child = open_dir_at(fd.as_fd(), name.as_bytes()).unwrap();
            close(fd).unwrap();
            fd = child;
        }
        close(fd).unwrap();
        let (ours, python) = run_child("deep", &tree.0);
        assert_eq!(ours, python);
        let mut expected = tree.0.as_os_str().as_bytes().to_vec();
        for _ in 0..25 {
            expected.push(b'/');
            expected.extend_from_slice(level().as_bytes());
        }
        assert!(expected.len() > 4096);
        assert_eq!(ours, expected);
        // 深い木は、絶対パスでは消せないので、フォルダの fd からの相対で消す。
        remove_deep(&tree.0, 25);
        std::fs::create_dir(tree.0.join("gone")).unwrap();
        let (ours, python) = run_child("deleted", &tree.0);
        assert_eq!(ours, python);
        assert_eq!(ours, format!("errno {ENOENT}").into_bytes());
    }

    fn remove_deep(root: &Path, depth: usize) {
        let name = level();
        let mut chain = vec![open_path(root)];
        for _ in 0..depth - 1 {
            let next = open_dir_at(chain.last().unwrap().as_fd(), name.as_bytes()).unwrap();
            chain.push(next);
        }
        while let Some(fd) = chain.pop() {
            rfs::unlinkat(&fd, OsStr::new(&name), AtFlags::REMOVEDIR).unwrap();
            close(fd).unwrap();
        }
    }
}
