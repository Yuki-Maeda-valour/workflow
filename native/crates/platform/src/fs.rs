//! 保持した親から開いた同じ実体を検査する。パスの閉じ込めは行わない。
use rustix::fd::OwnedFd;
use rustix::fs::{self, AtFlags, FileType, Mode, OFlags};
use std::ffi::OsStr;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileSnapshot {
    pub dev: i128,
    pub ino: i128,
    pub mode: u32,
    pub size: i128,
    pub mtime_seconds: i128,
    pub mtime_nanoseconds: i128,
    pub ctime_seconds: i128,
    pub ctime_nanoseconds: i128,
}

// stat と RawMode の整数型は Linux と Mac で異なる。取得値を損失なく保存する。
#[allow(clippy::unnecessary_cast)]
impl FileSnapshot {
    fn from_stat(stat: fs::Stat) -> Self {
        Self {
            dev: stat.st_dev as i128,
            ino: stat.st_ino as i128,
            mode: stat.st_mode as u32,
            size: stat.st_size as i128,
            mtime_seconds: stat.st_mtime as i128,
            mtime_nanoseconds: stat.st_mtime_nsec as i128,
            ctime_seconds: stat.st_ctime as i128,
            ctime_nanoseconds: stat.st_ctime_nsec as i128,
        }
    }

    pub fn is_regular(&self) -> bool {
        FileType::from_raw_mode(self.mode as fs::RawMode) == FileType::RegularFile
    }
    pub fn is_directory(&self) -> bool {
        FileType::from_raw_mode(self.mode as fs::RawMode) == FileType::Directory
    }
    pub fn matches(&self, other: &Self, comparison: SnapshotMatch) -> bool {
        match comparison {
            SnapshotMatch::Full => self == other,
            SnapshotMatch::DirectoryPath => {
                self.dev == other.dev
                    && self.ino == other.ino
                    && FileType::from_raw_mode(self.mode as fs::RawMode)
                        == FileType::from_raw_mode(other.mode as fs::RawMode)
                    && Mode::from_raw_mode(self.mode as fs::RawMode)
                        == Mode::from_raw_mode(other.mode as fs::RawMode)
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SnapshotMatch {
    Full,
    DirectoryPath,
}

#[derive(Debug)]
pub enum FsError {
    Io(io::Error),
    InvalidInput,
    NotRegular,
    NotDirectory,
    Changed,
    AlreadyRead,
}

impl FsError {
    pub fn is_not_found(&self) -> bool {
        matches!(self, Self::Io(error) if error.raw_os_error() == Some(rustix::io::Errno::NOENT.raw_os_error()))
    }
}

impl From<rustix::io::Errno> for FsError {
    fn from(error: rustix::io::Errno) -> Self {
        Self::Io(error.into())
    }
}

fn flags() -> OFlags {
    OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::CLOEXEC
}
fn component(name: &OsStr) -> Result<(), FsError> {
    let raw = name.as_bytes();
    if raw.is_empty() || raw.contains(&b'/') || raw.contains(&0) {
        Err(FsError::InvalidInput)
    } else {
        Ok(())
    }
}

#[derive(Debug)]
pub struct HeldDir {
    fd: OwnedFd,
}

impl HeldDir {
    pub(crate) fn from_owned_fd(fd: OwnedFd) -> Result<Self, FsError> {
        let stat = FileSnapshot::from_stat(fs::fstat(&fd)?);
        if !stat.is_directory() {
            return Err(FsError::NotDirectory);
        }
        Ok(Self { fd })
    }

    pub fn open_absolute(path: &Path) -> Result<Self, FsError> {
        let raw = path.as_os_str().as_bytes();
        if !raw.starts_with(b"/") || raw.contains(&0) {
            return Err(FsError::InvalidInput);
        }
        let mut parent = Self {
            fd: fs::open("/", flags() | OFlags::DIRECTORY, Mode::empty())?,
        };
        // Path::components は a/./b を消すため使用しない。旧版と同じ実体から順に辿る。
        for part in raw
            .split(|byte| *byte == b'/')
            .filter(|part| !part.is_empty())
        {
            let name = OsStr::from_bytes(part);
            let expected = parent.metadata(name)?;
            parent = parent.open_dir(name, &expected, SnapshotMatch::DirectoryPath)?;
        }
        Ok(parent)
    }

    pub fn metadata(&self, name: &OsStr) -> Result<FileSnapshot, FsError> {
        component(name)?;
        Ok(FileSnapshot::from_stat(fs::statat(
            &self.fd,
            name,
            AtFlags::SYMLINK_NOFOLLOW,
        )?))
    }

    pub fn open_dir(
        &self,
        name: &OsStr,
        expected: &FileSnapshot,
        comparison: SnapshotMatch,
    ) -> Result<Self, FsError> {
        component(name)?;
        let fd = fs::openat(&self.fd, name, flags() | OFlags::DIRECTORY, Mode::empty())?;
        let opened = FileSnapshot::from_stat(fs::fstat(&fd)?);
        if !expected.is_directory() || !opened.is_directory() {
            return Err(FsError::NotDirectory);
        }
        if !expected.matches(&opened, comparison) {
            return Err(FsError::Changed);
        }
        Ok(Self { fd })
    }

    pub fn verify_entry(
        &self,
        name: &OsStr,
        expected: &FileSnapshot,
        comparison: SnapshotMatch,
    ) -> Result<(), FsError> {
        let now = self.metadata(name)?;
        if expected.matches(&now, comparison) {
            Ok(())
        } else {
            Err(FsError::Changed)
        }
    }
}

#[derive(Debug)]
pub struct HeldFile {
    fd: OwnedFd,
    opened: FileSnapshot,
    started: bool,
}

impl HeldFile {
    pub(crate) fn raw_fd(&self) -> std::os::fd::RawFd {
        std::os::fd::AsRawFd::as_raw_fd(&self.fd)
    }

    pub fn open_at(
        parent: &HeldDir,
        name: &OsStr,
        expected: &FileSnapshot,
    ) -> Result<Self, FsError> {
        component(name)?;
        let fd = fs::openat(&parent.fd, name, flags(), Mode::empty())?;
        let opened = FileSnapshot::from_stat(fs::fstat(&fd)?);
        if !opened.is_regular() {
            return Err(FsError::NotRegular);
        }
        if !expected.matches(&opened, SnapshotMatch::Full) {
            return Err(FsError::Changed);
        }
        Ok(Self {
            fd,
            opened,
            started: false,
        })
    }

    pub fn snapshot(&self) -> Result<FileSnapshot, FsError> {
        Ok(FileSnapshot::from_stat(fs::fstat(&self.fd)?))
    }

    pub fn verify_unchanged(&self) -> Result<(), FsError> {
        if self.opened == self.snapshot()? {
            Ok(())
        } else {
            Err(FsError::Changed)
        }
    }

    pub fn begin_read(&mut self) -> Result<ReadSession<'_>, FsError> {
        if self.started {
            return Err(FsError::AlreadyRead);
        }
        self.started = true;
        Ok(ReadSession { file: self })
    }
}

#[derive(Debug)]
pub struct ReadSession<'a> {
    file: &'a mut HeldFile,
}

impl ReadSession<'_> {
    pub fn opened_snapshot(&self) -> &FileSnapshot {
        &self.file.opened
    }
    pub fn verify_unchanged(&self) -> Result<(), FsError> {
        self.file.verify_unchanged()
    }
    pub fn read_chunk(&mut self, buffer: &mut [u8]) -> Result<usize, FsError> {
        Ok(rustix::io::read(&self.file.fd, buffer)?)
    }
}

// ---- R4D-1: 旧版 diagnostic-reftable.py と同じ開き方と閉じ方に要る部品 ----
// 既存の open_absolute(全段でリンクを辿らない)・HeldFile・ReadSession は変えず、混ぜて使わない。
// 旧版の Python 3.10.11 は os.open・os.fstat・os.read を EINTR で呼び直し、os.stat・os.lseek・os.close は
// 呼び直さない。足す部品も、開く・fstat・読むだけを呼び直す。

fn retry_eintr<T>(mut operation: impl FnMut() -> rustix::io::Result<T>) -> rustix::io::Result<T> {
    loop {
        match operation() {
            Err(rustix::io::Errno::INTR) => continue,
            other => return other,
        }
    }
}

fn reject_nul(path: &Path) -> Result<(), FsError> {
    if path.as_os_str().as_bytes().contains(&0) {
        Err(FsError::InvalidInput)
    } else {
        Ok(())
    }
}

/// 閉じる失敗を返す。所有する fd を取り出して 1 回だけ閉じ、失敗しても再び使わない。
fn close_owned(fd: OwnedFd) -> Result<(), FsError> {
    let raw = std::os::fd::IntoRawFd::into_raw_fd(fd);
    // SAFETY: raw は直前に OwnedFd から取り出した、自分だけが持つ有効な fd である。
    // 取り出した後に OwnedFd の Drop で閉じる経路は無く、ここで 1 回だけ閉じる。
    // close は失敗しても fd を無効にするので、失敗した後も再び使わない。
    unsafe { rustix::io::try_close(raw) }.map_err(FsError::from)
}

/// 作業場所から見たパスを、最後の段だけリンクを辿らずに調べる(途中の段は OS が辿る)。
/// 相対パスも受ける。空のパスは検査せずに OS へ渡す(OS の ENOENT を返す)。呼び直さない。
pub fn lstat_path(path: &Path) -> Result<FileSnapshot, FsError> {
    reject_nul(path)?;
    Ok(FileSnapshot::from_stat(fs::lstat(path)?))
}

/// `HeldDir::open_path` や `HeldDir::open_plain` で開いた通常ファイルの fd。種類や状態は比べない。
#[derive(Debug)]
pub struct PlainFile {
    fd: OwnedFd,
}

impl PlainFile {
    pub fn snapshot(&self) -> Result<FileSnapshot, FsError> {
        Ok(FileSnapshot::from_stat(retry_eintr(|| {
            fs::fstat(&self.fd)
        })?))
    }

    /// 位置を指定して読む(ファイルの位置は変えない)。
    pub fn read_at(&self, offset: u64, buffer: &mut [u8]) -> Result<usize, FsError> {
        Ok(retry_eintr(|| {
            rustix::io::pread(&self.fd, &mut *buffer, offset)
        })?)
    }

    /// 閉じて、閉じる失敗を返す。
    pub fn close(self) -> Result<(), FsError> {
        close_owned(self.fd)
    }
}

impl HeldDir {
    /// パスを `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` で開く。比べる処理は呼出側が行う。
    /// 空のパスも検査せずに OS へ渡す。
    pub fn open_path(path: &Path) -> Result<Self, FsError> {
        reject_nul(path)?;
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let fd = retry_eintr(|| fs::open(path, flags, Mode::empty()))?;
        Ok(Self { fd })
    }

    /// 保持したフォルダの fd の状態。
    pub fn snapshot(&self) -> Result<FileSnapshot, FsError> {
        Ok(FileSnapshot::from_stat(retry_eintr(|| {
            fs::fstat(&self.fd)
        })?))
    }

    /// 保持したフォルダから `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC` で開く。種類や状態は比べない。
    pub fn open_plain(&self, name: &OsStr) -> Result<PlainFile, FsError> {
        component(name)?;
        let fd = retry_eintr(|| fs::openat(&self.fd, name, flags(), Mode::empty()))?;
        Ok(PlainFile { fd })
    }

    /// 閉じて、閉じる失敗を返す。
    pub fn close(self) -> Result<(), FsError> {
        close_owned(self.fd)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs as stdfs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Tree(PathBuf);
    impl Tree {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "c0b-platform-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            stdfs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn parent(&self) -> HeldDir {
            HeldDir::open_absolute(&self.0).unwrap()
        }
        fn file(&self) -> HeldFile {
            stdfs::write(self.0.join("file"), b"abc").unwrap();
            let parent = self.parent();
            let before = parent.metadata(OsStr::new("file")).unwrap();
            HeldFile::open_at(&parent, OsStr::new("file"), &before).unwrap()
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            stdfs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn snapshot_comparisons_have_distinct_attributes() {
        let tree = Tree::new();
        let parent = tree.parent();
        stdfs::create_dir(tree.0.join("child")).unwrap();
        let before = parent.metadata(OsStr::new("child")).unwrap();
        for field in 0..8 {
            let mut after = before;
            match field {
                0 => after.dev += 1,
                1 => after.ino += 1,
                2 => after.mode ^= 0o100,
                3 => after.size += 1,
                4 => after.mtime_seconds += 1,
                5 => after.mtime_nanoseconds += 1,
                6 => after.ctime_seconds += 1,
                7 => after.ctime_nanoseconds += 1,
                _ => unreachable!(),
            }
            assert!(!before.matches(&after, SnapshotMatch::Full));
            assert_eq!(
                before.matches(&after, SnapshotMatch::DirectoryPath),
                field >= 3
            );
        }
        let mut after = before;
        after.mode = (after.mode & !0o170000) | 0o100000;
        assert!(!before.matches(&after, SnapshotMatch::DirectoryPath));
    }

    #[test]
    fn dot_dotdot_empty_components_and_non_utf8_are_not_normalized() {
        let tree = Tree::new();
        stdfs::create_dir(tree.0.join("child")).unwrap();
        let path = PathBuf::from(format!("{}//child/.././", tree.0.display()));
        let parent = HeldDir::open_absolute(&path).unwrap();
        let name = OsStr::from_bytes(b"raw-\xff");
        // Mac の対応可否は比較器で記録する。Linux は必ず実在名を検査する。
        if let Err(error) = stdfs::write(tree.0.join(name), b"value") {
            if !cfg!(target_os = "macos")
                || !matches!(error.raw_os_error(), Some(code) if code == rustix::io::Errno::ILSEQ.raw_os_error() || code == rustix::io::Errno::INVAL.raw_os_error())
            {
                panic!("{error}");
            }
            eprintln!("T2 未実施: Mac の実在名を作れません: {error}");
        } else {
            assert!(parent.metadata(name).unwrap().is_regular());
        }
        symlink("child", tree.0.join("link")).unwrap();
        assert!(HeldDir::open_absolute(&tree.0.join("link/..")).is_err());
    }

    #[test]
    fn invalid_names_and_relative_paths_do_not_reach_os() {
        let tree = Tree::new();
        let parent = tree.parent();
        assert!(matches!(
            HeldDir::open_absolute(Path::new(".")),
            Err(FsError::InvalidInput)
        ));
        for name in [b"".as_slice(), b"a/b", b"a\0b"] {
            assert!(matches!(
                parent.metadata(OsStr::from_bytes(name)),
                Err(FsError::InvalidInput)
            ));
        }
    }

    #[test]
    fn missing_is_only_enoent_and_explicit_entry_checks_are_separate() {
        let tree = Tree::new();
        let parent = tree.parent();
        let mut held = tree.file();
        let before = held.snapshot().unwrap();
        stdfs::rename(tree.0.join("file"), tree.0.join("old")).unwrap();
        stdfs::write(tree.0.join("file"), b"new").unwrap();
        assert!(matches!(
            parent.verify_entry(OsStr::new("file"), &before, SnapshotMatch::Full),
            Err(FsError::Changed)
        ));
        let mut session = held.begin_read().unwrap();
        let mut raw = [0; 3];
        assert_eq!(session.read_chunk(&mut raw).unwrap(), 3);
        assert_eq!(&raw, b"abc");
        stdfs::remove_file(tree.0.join("file")).unwrap();
        assert!(
            parent
                .verify_entry(OsStr::new("file"), &before, SnapshotMatch::Full)
                .unwrap_err()
                .is_not_found()
        );
        let error = FsError::from(rustix::io::Errno::NOTDIR);
        assert!(!error.is_not_found());
        for code in [
            rustix::io::Errno::ACCESS,
            rustix::io::Errno::LOOP,
            rustix::io::Errno::BADF,
        ] {
            assert!(!FsError::from(code).is_not_found());
        }
    }

    #[test]
    fn every_raw_session_consumes_the_single_start() {
        for action in ["unread", "zero", "partial", "eof", "error"] {
            let tree = Tree::new();
            let mut held = tree.file();
            if action == "error" {
                held.fd = tree.parent().fd;
            }
            {
                let mut session = held.begin_read().unwrap();
                let mut buffer = [0; 8];
                match action {
                    "unread" => (),
                    "zero" => assert_eq!(session.read_chunk(&mut []).unwrap(), 0),
                    "partial" => assert_eq!(session.read_chunk(&mut buffer[..1]).unwrap(), 1),
                    "eof" => {
                        assert_eq!(session.read_chunk(&mut buffer).unwrap(), 3);
                        assert_eq!(session.read_chunk(&mut buffer).unwrap(), 0);
                    }
                    "error" => assert!(matches!(
                        session.read_chunk(&mut buffer),
                        Err(FsError::Io(_))
                    )),
                    _ => unreachable!(),
                }
            }
            assert!(matches!(held.begin_read(), Err(FsError::AlreadyRead)));
            // 拒否で位置が進まないことを非公開 fd から確かめる。
            if action == "partial" {
                let mut next = [0; 2];
                assert_eq!(rustix::io::read(&held.fd, &mut next).unwrap(), 2);
                assert_eq!(&next, b"bc");
            }
        }
    }

    #[test]
    fn changed_file_and_full_directory_are_rejected() {
        let tree = Tree::new();
        let held = tree.file();
        stdfs::set_permissions(
            tree.0.join("file"),
            stdfs::Permissions::from_mode(held.snapshot().unwrap().mode ^ 0o100),
        )
        .unwrap();
        assert!(matches!(held.verify_unchanged(), Err(FsError::Changed)));
        let parent = tree.parent();
        stdfs::create_dir(tree.0.join("child")).unwrap();
        stdfs::File::open(tree.0.join("child"))
            .unwrap()
            .set_times(stdfs::FileTimes::new().set_modified(std::time::SystemTime::UNIX_EPOCH))
            .unwrap();
        let before = parent.metadata(OsStr::new("child")).unwrap();
        stdfs::write(tree.0.join("child/new"), b"x").unwrap();
        assert!(
            parent
                .open_dir(OsStr::new("child"), &before, SnapshotMatch::DirectoryPath)
                .is_ok()
        );
        assert!(matches!(
            parent.open_dir(OsStr::new("child"), &before, SnapshotMatch::Full),
            Err(FsError::Changed)
        ));
    }
}

#[cfg(test)]
mod diagnostic_part_tests {
    use super::*;
    use std::fs as stdfs;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Tree(PathBuf);
    impl Tree {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4d-platform-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            stdfs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            stdfs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn lstat_path_does_not_follow_only_the_last_component() {
        let tree = Tree::new();
        stdfs::create_dir(tree.0.join("real")).unwrap();
        stdfs::write(tree.0.join("real/file"), b"x").unwrap();
        symlink("real", tree.0.join("link")).unwrap();
        // 途中の段のリンクは辿る。
        assert!(lstat_path(&tree.0.join("link/file")).unwrap().is_regular());
        // 最後の段のリンクは辿らない。
        let link = lstat_path(&tree.0.join("link")).unwrap();
        assert!(!link.is_directory() && !link.is_regular());
        assert!(lstat_path(&tree.0.join("real")).unwrap().is_directory());
        // 末尾のスラッシュは OS が辿る。
        assert!(lstat_path(&tree.0.join("link/")).unwrap().is_directory());
    }

    #[test]
    fn relative_paths_and_empty_paths_go_to_the_os() {
        // 作業場所は変えない。相対パスは現在の作業場所から見る(Cargo は manifest のフォルダ)。
        assert!(lstat_path(Path::new(".")).unwrap().is_directory());
        let error = lstat_path(Path::new("")).unwrap_err();
        assert!(error.is_not_found());
        let error = HeldDir::open_path(Path::new("")).unwrap_err();
        assert!(error.is_not_found());
        assert!(matches!(
            lstat_path(Path::new("a\0b")),
            Err(FsError::InvalidInput)
        ));
        assert!(matches!(
            HeldDir::open_path(Path::new("a\0b")),
            Err(FsError::InvalidInput)
        ));
        let dir = HeldDir::open_path(Path::new(".")).unwrap();
        assert!(dir.snapshot().unwrap().is_directory());
        dir.close().unwrap();
    }

    #[test]
    fn open_path_refuses_a_final_symlink_and_non_directories() {
        let tree = Tree::new();
        stdfs::create_dir(tree.0.join("real")).unwrap();
        stdfs::write(tree.0.join("file"), b"x").unwrap();
        symlink("real", tree.0.join("link")).unwrap();
        assert!(matches!(
            HeldDir::open_path(&tree.0.join("link")),
            Err(FsError::Io(_))
        ));
        assert!(matches!(
            HeldDir::open_path(&tree.0.join("file")),
            Err(FsError::Io(_))
        ));
        // 途中の段のリンクは辿る。
        symlink(&tree.0, tree.0.join("up")).unwrap();
        let dir = HeldDir::open_path(&tree.0.join("up/real")).unwrap();
        assert!(dir.snapshot().unwrap().is_directory());
        dir.close().unwrap();
    }

    #[test]
    fn open_plain_reads_at_positions_without_moving_the_file_position() {
        let tree = Tree::new();
        stdfs::write(tree.0.join("file"), b"abcdef").unwrap();
        symlink("file", tree.0.join("link")).unwrap();
        let dir = HeldDir::open_path(&tree.0).unwrap();
        let before = dir.metadata(OsStr::new("file")).unwrap();
        let file = dir.open_plain(OsStr::new("file")).unwrap();
        assert_eq!(file.snapshot().unwrap(), before);
        let mut buffer = [0; 3];
        assert_eq!(file.read_at(2, &mut buffer).unwrap(), 3);
        assert_eq!(&buffer, b"cde");
        assert_eq!(file.read_at(0, &mut buffer).unwrap(), 3);
        assert_eq!(&buffer, b"abc");
        assert_eq!(file.read_at(6, &mut buffer).unwrap(), 0);
        assert_eq!(file.read_at(4, &mut buffer).unwrap(), 2);
        assert_eq!(&buffer[..2], b"ef");
        file.close().unwrap();
        // 名前が最後の段のリンクなら開かない。
        assert!(matches!(
            dir.open_plain(OsStr::new("link")),
            Err(FsError::Io(_))
        ));
        assert!(matches!(
            dir.open_plain(OsStr::new("a/b")),
            Err(FsError::InvalidInput)
        ));
        assert!(
            dir.open_plain(OsStr::new("missing"))
                .unwrap_err()
                .is_not_found()
        );
        dir.close().unwrap();
    }

    #[test]
    fn close_consumes_the_descriptor_and_reports_success() {
        // close(self) は所有する fd を取り出して 1 回だけ閉じる。self を消費するので、閉じた後に file や dir を
        // 使う式や、Drop でもう一度閉じる道は、型の上で書けない。
        // 閉じる失敗の割り当てと優先は、reftable.rs の差し込み口(Hooks)の検査で確かめる。
        let tree = Tree::new();
        stdfs::write(tree.0.join("file"), b"x").unwrap();
        let dir = HeldDir::open_path(&tree.0).unwrap();
        let file = dir.open_plain(OsStr::new("file")).unwrap();
        assert!(file.close().is_ok());
        assert!(dir.close().is_ok());
    }
}
