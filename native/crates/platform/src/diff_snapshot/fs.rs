use super::{Audit, AuditEvent, FaultStage, Operation, Operations, SyncPoint};
use crate::fs::{FsError, HeldDir, HeldFile};
use rustix::fs::{Access, Mode, OFlags, access, open, openat};
use std::ffi::OsStr;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
fn event(
    phase: &'static str,
    operation: Operation,
    path: &Path,
    file: &File,
    bytes: u64,
) -> AuditEvent {
    let m = file.metadata().ok();
    AuditEvent {
        phase,
        operation,
        path: path.to_path_buf(),
        dev: m.as_ref().map(MetadataExt::dev),
        ino: m.as_ref().map(MetadataExt::ino),
        fd: Some(file.as_raw_fd()),
        bytes,
        child_pid: None,
        arguments: Vec::new(),
    }
}
/// A failed read of a file the old script redirected into a tool: the shell's
/// open of `<FILE` failed, or the tool's read failed after some bytes.
#[derive(Debug)]
pub enum ReadFailure {
    Open(io::Error),
    Read(io::Error, Vec<u8>),
}
fn read_opened(
    path: &Path,
    limit: Option<u64>,
    phase: &'static str,
    audit: &mut impl Audit,
) -> Result<(File, Vec<u8>), ReadFailure> {
    if let Some(error) = super::injected_at(phase, FaultStage::Open) {
        return Err(ReadFailure::Open(error));
    }
    let mut file = File::open(path).map_err(ReadFailure::Open)?;
    let opened = event(phase, Operation::Open, path, &file, 0);
    super::observe(opened.clone());
    audit.record(opened);
    let mut bytes = Vec::new();
    let mut buffer = [0; 65536];
    loop {
        let size = limit.map_or(buffer.len(), |limit| {
            limit
                .saturating_sub(bytes.len() as u64)
                .min(buffer.len() as u64) as usize
        });
        if size == 0 {
            break;
        }
        if let Some(error) = super::injected_at(phase, FaultStage::Read) {
            return Err(ReadFailure::Read(error, bytes));
        }
        let n = match file.read(&mut buffer[..size]) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(ReadFailure::Read(e, bytes)),
            Ok(n) => n,
        };
        let read = event(phase, Operation::Read, path, &file, n as u64);
        super::observe(read.clone());
        audit.record(read);
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..n]);
    }
    Ok((file, bytes))
}
pub fn read(
    path: &Path,
    limit: Option<u64>,
    phase: &'static str,
    audit: &mut impl Audit,
) -> Result<Vec<u8>, ReadFailure> {
    read_opened(path, limit, phase, audit).map(|(_, bytes)| bytes)
}
pub fn read_and_hash<T>(
    path: &Path,
    phase: &'static str,
    audit: &mut impl Audit,
    hash: impl FnOnce(&[u8]) -> T,
) -> Result<T, ReadFailure> {
    let (file, bytes) = read_opened(path, None, phase, audit)?;
    let result = hash(&bytes);
    let hashed = event(phase, Operation::Hash, path, &file, bytes.len() as u64);
    super::observe(hashed.clone());
    audit.record(hashed);
    Ok(result)
}
/// `readlink -- FILE` of a path that was a symlink a moment before.
pub fn read_link(path: &Path, point: &'static str) -> io::Result<std::path::PathBuf> {
    if let Some(error) = super::injected(point) {
        return Err(error);
    }
    fs::read_link(path)
}
/// The old script's write of a Git input file `$TMPD/NAME` (Bash's `printf …
/// >>FILE` or `cat … >FILE`); a typed fault named NAME may replace it.
pub fn write_input(path: &Path, data: &[u8], name: &str) -> io::Result<()> {
    if let Some(error) = super::injected(name) {
        return Err(error);
    }
    fs::write(path, data)
}
/// `wc -c <FILE`: the shell opens FILE, then the size comes from the descriptor.
pub fn open_size(path: &Path, point: &'static str) -> io::Result<u64> {
    if let Some(error) = super::injected(point) {
        return Err(error);
    }
    File::open(path)?.metadata().map(|m| m.len())
}
pub fn readable(path: &Path) -> bool {
    access(path, Access::READ_OK).is_ok()
}
pub fn searchable(path: &Path) -> bool {
    access(path, Access::EXEC_OK).is_ok()
}
pub fn exists_or_link(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
pub fn symlink(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
}
pub fn current_profile(
    root: &Path,
    ops: &mut impl Operations,
) -> Result<Option<Vec<u8>>, ProfileError> {
    let profile_path = root.join(".claude/project-profile.yml");
    let flags =
        OFlags::RDONLY | OFlags::NONBLOCK | OFlags::NOFOLLOW | OFlags::DIRECTORY | OFlags::CLOEXEC;
    let root = match open(root, flags, Mode::empty()) {
        Ok(v) => v,
        Err(e) if e == rustix::io::Errno::NOENT => return Ok(None),
        Err(e) => {
            return Err(ProfileError::Os("profile の親を安全に開けない", e.into()));
        }
    };
    let directory = match openat(&root, ".claude", flags, Mode::empty()) {
        Ok(v) => v,
        Err(e) if e == rustix::io::Errno::NOENT => return Ok(None),
        Err(e) => {
            return Err(ProfileError::Os("profile の親を安全に開けない", e.into()));
        }
    };
    let directory = HeldDir::from_owned_fd(directory)
        .map_err(|e| ProfileError::Os("profile の親を安全に開けない", fs_error(e)))?;
    let name = OsStr::new("project-profile.yml");
    let before = match directory.metadata(name) {
        Ok(v) => v,
        Err(e) if e.is_not_found() => return Ok(None),
        Err(e) => {
            return Err(ProfileError::Os(
                "現在の profile を検査できない",
                fs_error(e),
            ));
        }
    };
    if !before.is_regular() {
        return Err(ProfileError::Text("現在の profile が通常ファイルでない"));
    }
    if before.size > 1048576 {
        return Err(ProfileError::Text("現在の profile が上限 1 MiB を超える"));
    }
    ops.sync(SyncPoint::ProfileInspected)
        .map_err(|e| ProfileError::Os("現在の profile を安全に開けない", e))?;
    let mut file = HeldFile::open_at(&directory, name, &before).map_err(|e| match e {
        FsError::Changed | FsError::NotRegular => {
            ProfileError::Text("現在の profile が読取中に差し替わった")
        }
        e => ProfileError::Os("現在の profile を安全に開けない", fs_error(e)),
    })?;
    let descriptor = file.raw_fd();
    super::observe(AuditEvent {
        phase: "profile",
        operation: Operation::Open,
        path: profile_path.clone(),
        dev: Some(before.dev as u64),
        ino: Some(before.ino as u64),
        fd: Some(descriptor),
        bytes: 0,
        child_pid: None,
        arguments: vec![],
    });
    ops.sync(SyncPoint::ProfileOpened)
        .map_err(|e| ProfileError::Os("現在の profile を読めない", e))?;
    let mut session = file
        .begin_read()
        .map_err(|e| ProfileError::Bare(fs_error(e)))?;
    let mut bytes = Vec::new();
    let mut block = [0; 65536];
    loop {
        ops.sync(SyncPoint::ProfileReading)
            .map_err(|e| ProfileError::Os("現在の profile を読めない", e))?;
        let n = match session.read_chunk(&mut block) {
            Ok(v) => v,
            Err(FsError::Io(e)) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(ProfileError::Os("現在の profile を読めない", fs_error(e))),
        };
        super::observe(AuditEvent {
            phase: "profile",
            operation: Operation::Read,
            path: profile_path.clone(),
            dev: Some(before.dev as u64),
            ino: Some(before.ino as u64),
            fd: Some(descriptor),
            bytes: n as u64,
            child_pid: None,
            arguments: vec![],
        });
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&block[..n]);
        if bytes.len() > 1048576 {
            return Err(ProfileError::Text(
                "現在の profile が読取中に上限 1 MiB を超えた",
            ));
        }
    }
    ops.sync(SyncPoint::ProfileRead)
        .map_err(|e| ProfileError::Os("現在の profile を読めない", e))?;
    if bytes.len() as i128 != before.size || session.verify_unchanged().is_err() {
        return Err(ProfileError::Text("現在の profile が読取中に変更された"));
    }
    Ok(Some(bytes))
}
fn fs_error(error: FsError) -> io::Error {
    match error {
        FsError::Io(e) => e,
        _ => io::Error::from_raw_os_error(rustix::io::Errno::INVAL.raw_os_error()),
    }
}
/// The reason the current profile could not be read. The caller renders OS
/// errors with its fixed `strerror` table (never Rust's `io::Error` text).
#[derive(Debug)]
pub enum ProfileError {
    Text(&'static str),
    Os(&'static str, io::Error),
    Bare(io::Error),
}
impl ProfileError {
    pub fn text(&self, strerror: impl Fn(&io::Error) -> String) -> String {
        match self {
            Self::Text(text) => (*text).to_owned(),
            Self::Os(prefix, error) => format!("{prefix}: {}", strerror(error)),
            Self::Bare(error) => strerror(error),
        }
    }
}

/// The process working directory, kept open so that an in-process caller (the
/// typed tests) gets it back after the old script's `cd -P -- "$TOP"`.
pub struct SavedDirectory(File);
impl SavedDirectory {
    pub fn save() -> Option<Self> {
        File::open(".").ok().map(Self)
    }
    pub fn restore(&self) {
        let _ = rustix::process::fchdir(&self.0);
    }
}
/// A standard descriptor (0, 1 or 2) without taking ownership of it.
pub(crate) fn standard(fd: i32) -> rustix::fd::BorrowedFd<'static> {
    match fd {
        0 => rustix::stdio::stdin(),
        1 => rustix::stdio::stdout(),
        _ => rustix::stdio::stderr(),
    }
}
/// Write shell-owned bytes without Rust's stdout buffering or startup repair.
pub fn write_standard(fd: i32, mut bytes: &[u8]) -> io::Result<()> {
    if bytes.is_empty() {
        return Ok(());
    }
    if super::signals::initially_closed(fd) {
        return Err(io::Error::from_raw_os_error(
            rustix::io::Errno::BADF.raw_os_error(),
        ));
    }
    while !bytes.is_empty() {
        let count = match rustix::io::write(standard(fd), bytes) {
            Ok(count) => count,
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => return Err(error.into()),
        };
        if count == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        bytes = &bytes[count..];
    }
    Ok(())
}

/// How Bash 5.2.21 judges an inherited `BASH_XTRACEFD` descriptor number at
/// start-up: `sh_validfd` (`fcntl(F_GETFD)`), then `fdopen(fd, "w")`, which
/// refuses a descriptor whose access mode is read-only (glibc and BSD alike).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TraceDescriptor {
    Closed,
    ReadOnly,
    Writable,
}
/// Judge descriptor `fd` as the old shell did at its start. A standard
/// descriptor that was closed when the process started counts as closed even
/// if the Rust runtime has since put /dev/null there. libc remains here:
/// rustix only accepts a descriptor that is known to be open.
pub fn trace_descriptor(fd: i32) -> TraceDescriptor {
    if fd < 0 || ((0..=2).contains(&fd) && super::signals::initially_closed(fd)) {
        return TraceDescriptor::Closed;
    }
    // SAFETY: F_GETFD and F_GETFL only read descriptor flags; a closed number fails with EBADF.
    if unsafe { libc::fcntl(fd, libc::F_GETFD) } == -1 {
        return TraceDescriptor::Closed;
    }
    // SAFETY: as above.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags == -1 || flags & libc::O_ACCMODE == libc::O_RDONLY {
        TraceDescriptor::ReadOnly
    } else {
        TraceDescriptor::Writable
    }
}

/// The legacy helper rejects an inherited directory before opening text stdin.
pub fn standard_is_directory(fd: i32) -> bool {
    rustix::fs::fstat(standard(fd)).is_ok_and(|stat| {
        rustix::fs::FileType::from_raw_mode(stat.st_mode) == rustix::fs::FileType::Directory
    })
}

/// The operating system's own `strerror` text, used on macOS only (Linux uses
/// the fixed glibc table of the caller because the static musl texts differ).
/// libc remains here: rustix has no `strerror`.
#[cfg(not(target_os = "linux"))]
pub fn os_strerror(code: i32) -> String {
    let mut buffer = [0 as libc::c_char; 512];
    // SAFETY: the buffer is writable for its length and NUL terminated on success.
    let rc = unsafe { libc::strerror_r(code, buffer.as_mut_ptr(), buffer.len()) };
    if rc != 0 {
        return format!("Unknown error: {code}");
    }
    // SAFETY: strerror_r wrote a NUL-terminated string into the buffer.
    unsafe { std::ffi::CStr::from_ptr(buffer.as_ptr()) }
        .to_string_lossy()
        .into_owned()
}

/// `date '+%Y-%m-%d %H:%M:%S'`: local time of the process time zone.
pub fn local_time() -> String {
    let now = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(after) => after.as_secs() as libc::time_t,
        Err(before) => -(before.duration().as_secs() as libc::time_t),
    };
    // rustix has no time zone conversion; libc's localtime_r remains.
    // SAFETY: zeroed is a valid libc::tm and localtime_r only writes into it.
    let mut tm = unsafe { std::mem::zeroed::<libc::tm>() };
    if unsafe { libc::localtime_r(&now, &mut tm) }.is_null() {
        return String::new();
    }
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        i64::from(tm.tm_year) + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}

/// Random bytes for temporary names, as mktemp's gen_tempname uses.
pub fn random_bytes(buffer: &mut [u8]) {
    for chunk in buffer.chunks_mut(256) {
        #[cfg(target_os = "linux")]
        let _ = rustix::rand::getrandom(chunk, rustix::rand::GetRandomFlags::empty());
        // rustix has no getentropy for macOS; libc remains for it.
        #[cfg(not(target_os = "linux"))]
        // SAFETY: getentropy writes at most 256 bytes into the given chunk.
        let _ = unsafe { libc::getentropy(chunk.as_mut_ptr().cast(), chunk.len()) };
    }
}
