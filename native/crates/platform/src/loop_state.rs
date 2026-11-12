//! S2L専用。fdの番号・所有権・位置を呼出元へ公開しない。
use crate::fs::{FsError, HeldDir, HeldFile};
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fd::OwnedFd;
use rustix::fs::{self, AtFlags, Mode, OFlags};
use std::ffi::{OsStr, OsString};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStringExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::time::Duration;

/// 検査が期待を OS の値から作るためのエラー番号(Linux と Mac で番号が違う)。
pub mod errno {
    /// 空でないディレクトリの rmdir(Linux 39・Mac 66)。
    pub const ENOTEMPTY: i32 = libc::ENOTEMPTY;
    /// ファイル系が受け付けない名前(Mac の APFS の UTF-8 でない名前。Linux 84・Mac 92)。
    pub const EILSEQ: i32 = libc::EILSEQ;
}

// C0 の DirectoryPath 比較を保った S2L の祖先接続。fd は渡さない。
#[derive(Clone, Copy, Debug)]
pub enum DirectoryOperation {
    Input,
    Root,
    Stat,
    Open,
    Check,
}
#[derive(Debug)]
pub struct DirectoryError {
    pub error: FsError,
    pub name: OsString,
    pub operation: DirectoryOperation,
}
pub fn open_directory(
    path: &std::path::Path,
    before_open: impl FnMut(&std::path::Path, &crate::fs::FileSnapshot) -> Result<(), FsError>,
) -> Result<HeldDir, FsError> {
    open_directory_detailed(path, before_open).map_err(|failure| failure.error)
}
pub fn open_directory_detailed(
    path: &std::path::Path,
    mut before_open: impl FnMut(&std::path::Path, &crate::fs::FileSnapshot) -> Result<(), FsError>,
) -> Result<HeldDir, DirectoryError> {
    use std::os::unix::ffi::OsStrExt;
    let raw = path.as_os_str().as_bytes();
    if !raw.starts_with(b"/") || raw.contains(&0) {
        return Err(DirectoryError {
            error: FsError::InvalidInput,
            name: OsString::new(),
            operation: DirectoryOperation::Input,
        });
    }
    let mut parent =
        HeldDir::open_absolute(std::path::Path::new("/")).map_err(|error| DirectoryError {
            error,
            name: OsString::from("/"),
            operation: DirectoryOperation::Root,
        })?;
    let mut end = 0;
    for part in raw.split(|byte| *byte == b'/') {
        let start = end;
        end += part.len() + 1;
        if part.is_empty() {
            continue;
        }
        let name = OsStr::from_bytes(part);
        let before = parent.metadata(name).map_err(|error| DirectoryError {
            error,
            name: name.to_owned(),
            operation: DirectoryOperation::Stat,
        })?;
        let prefix = std::path::Path::new(OsStr::from_bytes(&raw[..start + part.len()]));
        before_open(prefix, &before).map_err(|error| DirectoryError {
            error,
            name: name.to_owned(),
            operation: DirectoryOperation::Check,
        })?;
        parent = parent
            .open_dir(name, &before, crate::fs::SnapshotMatch::DirectoryPath)
            .map_err(|error| DirectoryError {
                error,
                name: name.to_owned(),
                operation: DirectoryOperation::Open,
            })?;
    }
    Ok(parent)
}
// The callback may fail without releasing an unchecked descriptor to the caller.
// C0b's existing methods remain unchanged; only S2L needs this call boundary.
pub fn open_tree_directory<E: From<FsError>>(
    parent: &HeldDir,
    name: &OsStr,
    before: &crate::fs::FileSnapshot,
    check: impl FnOnce() -> Result<(), E>,
) -> Result<HeldDir, E> {
    parent.open_dir_before_match(name, before, check)
}
pub fn open_tree_file<E: From<FsError>>(
    parent: &HeldDir,
    name: &OsStr,
    before: &crate::fs::FileSnapshot,
    check: impl FnOnce() -> Result<(), E>,
) -> Result<HeldFile, E> {
    HeldFile::open_before_match(parent, name, before, check)
}

pub enum RawOutput {
    Stdout(std::io::Stdout),
    Stderr(std::io::Stderr),
}
impl Write for RawOutput {
    fn write(&mut self, raw: &[u8]) -> io::Result<usize> {
        Ok(match self {
            Self::Stdout(stream) => rustix::io::write(stream, raw)?,
            Self::Stderr(stream) => rustix::io::write(stream, raw)?,
        })
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub struct DirectoryNames(fs::Dir);
impl DirectoryNames {
    pub fn new(parent: &HeldDir) -> Result<Self, FsError> {
        // CPython3.10 os.scandir(fd): duplicate the same open-file
        // description. Reopening "." wrongly demands search permission.
        let duplicate = rustix::io::fcntl_dupfd_cloexec(parent.borrowed_fd(), 0)?;
        Ok(Self(fs::Dir::new(duplicate)?))
    }
}
impl Drop for DirectoryNames {
    fn drop(&mut self) {
        // CPython rewinds a descriptor-backed iterator on EOF and early close.
        // linux_raw Dir::rewind is lazy, so seek before its fd is dropped.
        #[cfg(target_os = "linux")]
        if let Ok(fd) = self.0.fd() {
            let _ = fs::seek(fd, fs::SeekFrom::Start(0));
        }
        #[cfg(not(target_os = "linux"))]
        self.0.rewind();
    }
}
impl Iterator for DirectoryNames {
    type Item = Result<OsString, FsError>;
    fn next(&mut self) -> Option<Self::Item> {
        loop {
            let entry = match self.0.next()? {
                Ok(v) => v,
                Err(e) => return Some(Err(e.into())),
            };
            let name = entry.file_name().to_bytes();
            if name != b"." && name != b".." {
                return Some(Ok(OsString::from_vec(name.to_vec())));
            }
        }
    }
}
pub fn read_link(parent: &HeldDir, name: &OsStr) -> Result<OsString, FsError> {
    Ok(OsString::from_vec(
        fs::readlinkat(parent.borrowed_fd(), name, Vec::new())?.into_bytes(),
    ))
}
pub fn device_number(parent: &HeldDir, name: &OsStr) -> Result<i128, FsError> {
    Ok(fs::statat(parent.borrowed_fd(), name, AtFlags::SYMLINK_NOFOLLOW)?.st_rdev as i128)
}
/// 状態の一時ファイル。旧 FileIO と同じく、1 回の write は 1 回の系呼出し(途中までの
/// 書込みも返す)にする。バッファは呼出元が旧 BufferedWriter と同じ規則で持つ。
pub struct StateWriter {
    file: OwnedFd,
}
impl StateWriter {
    pub fn create(parent: &HeldDir, name: &OsStr) -> io::Result<Self> {
        let fd = fs::openat(
            parent.borrowed_fd(),
            name,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::RUSR | Mode::WUSR,
        )?;
        Ok(Self { file: fd })
    }
    /// io.open が BufferedWriter の大きさを決める st_blksize。
    pub fn block_size(&self) -> io::Result<u64> {
        Ok(fs::fstat(&self.file)?.st_blksize as u64)
    }
    /// 旧 FileIO.write と同じ 1 回の write。EINTR だけは旧版と同じく送り直す。
    pub fn write_once(&mut self, raw: &[u8]) -> io::Result<usize> {
        loop {
            match rustix::io::write(&self.file, raw) {
                Err(rustix::io::Errno::INTR) => continue,
                result => return Ok(result?),
            }
        }
    }
    /// os.fsync と同じ fsync(Mac でも F_FULLFSYNC にしない)。
    pub fn sync(&self) -> io::Result<()> {
        Ok(fs::fsync(&self.file)?)
    }
}
/// 旧 os.open(path, flags, 0o600) の S2L 用の組(O_CLOEXEC は旧版と同じく常に付く)。
pub struct OpenFlags {
    pub read: bool,
    pub create_exclusive: bool,
    pub truncate: bool,
    pub nofollow: bool,
}
pub fn open_file(path: &std::path::Path, flags: OpenFlags) -> io::Result<std::fs::File> {
    let mut bits = if flags.read {
        OFlags::RDWR
    } else {
        OFlags::WRONLY
    } | OFlags::CLOEXEC;
    if flags.create_exclusive {
        bits |= OFlags::CREATE | OFlags::EXCL;
    }
    if flags.truncate {
        bits |= OFlags::TRUNC;
    }
    if flags.nofollow {
        bits |= OFlags::NOFOLLOW;
    }
    Ok(fs::open(path, bits, Mode::RUSR | Mode::WUSR)?.into())
}
/// 旧 os.write と同じ 1 回の write(途中までの書込みも成功として返す)。
pub fn write_once(file: &std::fs::File, raw: &[u8]) -> io::Result<usize> {
    loop {
        match rustix::io::write(file, raw) {
            Err(rustix::io::Errno::INTR) => continue,
            result => return Ok(result?),
        }
    }
}
pub fn unlink(parent: &HeldDir, name: &OsStr) -> io::Result<()> {
    Ok(fs::unlinkat(parent.borrowed_fd(), name, AtFlags::empty())?)
}
pub fn replace(parent: &HeldDir, source: &OsStr, target: &OsStr) -> io::Result<()> {
    Ok(fs::renameat(
        parent.borrowed_fd(),
        source,
        parent.borrowed_fd(),
        target,
    )?)
}

#[derive(Clone, Copy)]
pub enum InheritedInput<'a> {
    Held(&'a HeldFile),
    Memory(&'a MemoryFile<'a>),
}
impl InheritedInput<'_> {
    fn fd(&self) -> i32 {
        match self {
            Self::Held(file) => file.borrowed_fd().as_raw_fd(),
            Self::Memory(file) => file.fd.as_raw_fd(),
        }
    }
}
pub enum LoopArg<'a> {
    Raw(&'a OsStr),
    InputPath(InheritedInput<'a>),
}
pub enum MemoryPart<'a> {
    Raw(&'a [u8]),
    InputPath(InheritedInput<'a>),
}
pub struct MemoryFile<'a> {
    fd: OwnedFd,
    _inputs: Vec<InheritedInput<'a>>,
    #[cfg(test)]
    name: String,
}
#[derive(Debug)]
pub enum MemoryError {
    Create(io::Error),
    Write(io::Error),
}
impl<'a> MemoryFile<'a> {
    #[cfg(target_os = "linux")]
    pub fn create(name: &str, parts: &[MemoryPart<'a>]) -> Result<Self, MemoryError> {
        #[cfg(test)]
        tests::memory_event(tests::MemoryEvent::Create(name.to_owned()))
            .map_err(MemoryError::Create)?;
        let fd = fs::memfd_create(name, fs::MemfdFlags::CLOEXEC)
            .map_err(|e| MemoryError::Create(e.into()))?;
        #[cfg(test)]
        tests::memory_event(tests::MemoryEvent::Opened(name.to_owned(), fd.as_raw_fd()))
            .map_err(MemoryError::Write)?;
        let mut inputs = Vec::new();
        for part in parts {
            let path;
            let raw = match part {
                MemoryPart::Raw(raw) => *raw,
                MemoryPart::InputPath(input) => {
                    inputs.push(*input);
                    path = format!("/proc/self/fd/{}", input.fd());
                    path.as_bytes()
                }
            };
            let mut rest = raw;
            while !rest.is_empty() {
                #[cfg(test)]
                tests::memory_event(tests::MemoryEvent::Write(name.to_owned()))
                    .map_err(MemoryError::Write)?;
                let count =
                    rustix::io::write(&fd, rest).map_err(|e| MemoryError::Write(e.into()))?;
                rest = &rest[count..];
            }
        }
        #[cfg(test)]
        tests::memory_event(tests::MemoryEvent::Seek(name.to_owned()))
            .map_err(MemoryError::Write)?;
        fs::seek(&fd, fs::SeekFrom::Start(0)).map_err(|e| MemoryError::Write(e.into()))?;
        Ok(Self {
            fd,
            _inputs: inputs,
            #[cfg(test)]
            name: name.to_owned(),
        })
    }
    #[cfg(not(target_os = "linux"))]
    pub fn create(_name: &str, _parts: &[MemoryPart<'a>]) -> Result<Self, MemoryError> {
        Err(MemoryError::Create(io::Error::new(
            io::ErrorKind::Unsupported,
            "memfd_create",
        )))
    }
}

#[cfg(test)]
impl Drop for MemoryFile<'_> {
    fn drop(&mut self) {
        let _ = tests::memory_event(tests::MemoryEvent::Close(self.name.clone()));
    }
}

pub struct LoopChild {
    child: Child,
    stdout: Option<ChildStdout>,
}
impl LoopChild {
    pub fn id(&self) -> u32 {
        self.child.id()
    }
    pub fn spawn(
        mut command: Command,
        args: &[LoopArg<'_>],
        inputs: &[InheritedInput<'_>],
    ) -> io::Result<Self> {
        Self::start(&mut command, args, inputs, true)
    }
    pub fn spawn_quiet(mut command: Command, args: &[LoopArg<'_>]) -> io::Result<Self> {
        Self::start(&mut command, args, &[], false)
    }
    fn start(
        command: &mut Command,
        args: &[LoopArg<'_>],
        inputs: &[InheritedInput<'_>],
        output: bool,
    ) -> io::Result<Self> {
        let mut keep: Vec<_> = inputs.iter().map(InheritedInput::fd).collect();
        keep.sort_unstable();
        keep.dedup();
        for arg in args {
            match arg {
                LoopArg::Raw(raw) => {
                    command.arg(raw);
                }
                LoopArg::InputPath(input) => {
                    if !keep.contains(&input.fd()) {
                        return Err(io::ErrorKind::InvalidInput.into());
                    }
                    command.arg(format!("/proc/self/fd/{}", input.fd()));
                }
            }
        }
        #[cfg(test)]
        let setup = tests::setup();
        // SAFETY: 子では生の open/read-directory/fcntl/close/signal/getrlimit のみ。
        // Command の env/cwd/uid/gid/pre_exec 等は std の実装をそのまま保つ。
        unsafe {
            command.pre_exec(move || {
                // CPython restore_signals=True。親の signal disposition は変えない。
                c_signal(13, 0);
                c_signal(25, 0);

                #[cfg(target_os = "linux")]
                {
                    // Linux 5.11以降。fork後の全descriptorへ適用するため、並行openも漏らさない。
                    if {
                        #[cfg(test)]
                        let enabled = !setup.skip_range;
                        #[cfg(not(test))]
                        let enabled = true;
                        enabled
                    } && c_syscall(436, 3u32, u32::MAX, 4u32) == 0
                    {
                        for &fd in &keep {
                            let flags = c_fcntl(fd, 1, 0);
                            if flags < 0 || c_fcntl(fd, 2, flags & !1) < 0 {
                                return Err(io::Error::last_os_error());
                            }
                        }
                        return Ok(());
                    }
                    // CPython _Py_closerange と同じく、どの失敗でも下の代替へ進む。
                    // seccomp の EPERM でも子側 fd 列挙が使えれば閉鎖を完了できる。
                }
                // fork 後の一覧なので、親の別 thread が準備中に開いた fd も含む。
                #[cfg(test)]
                let unavailable = setup.skip_directory;
                #[cfg(not(test))]
                let unavailable = false;
                if unavailable || !child_descriptor_directory(&keep)? {
                    // CPython と同じ有限上限の代替。一覧が使えない場合だけ。
                    let mut limit = DescriptorLimit {
                        current: 0,
                        maximum: 0,
                    };
                    #[cfg(target_os = "linux")]
                    let resource = 7;
                    #[cfg(not(target_os = "linux"))]
                    let resource = 8;
                    if c_getrlimit(resource, &mut limit) != 0 {
                        limit.current = 256;
                    }
                    #[cfg(test)]
                    if let Some(maximum) = setup.limit {
                        limit.current = maximum;
                    }
                    let mut first = 3i64;
                    for &kept in &keep {
                        if kept < 3 {
                            continue;
                        }
                        for fd in first..i64::from(kept) {
                            child_descriptor_flags(fd as i32, &keep)?;
                        }
                        first = i64::from(kept) + 1;
                    }
                    for fd in first..=limit.current.min(i32::MAX as u64) as i64 {
                        child_descriptor_flags(fd as i32, &keep)?;
                    }
                }
                // 閉じた/無効な継承指定も成功化しない。
                for &fd in &keep {
                    let flags = c_fcntl(fd, 1, 0);
                    if flags < 0 || c_fcntl(fd, 2, flags & !1) < 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                Ok(())
            });
        }
        // Python subprocess.DEVNULL と同じ O_RDWR 実体を各標準 fd へ複製する。
        let null = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/null")?;
        command
            .stdin(Stdio::from(null.try_clone()?))
            .stderr(Stdio::from(null.try_clone()?));
        command.stdout(if output {
            Stdio::piped()
        } else {
            Stdio::from(null)
        });
        let mut child = command.spawn()?;
        let stdout = child.stdout.take();
        Ok(Self { child, stdout })
    }
    pub fn poll(&self, timeout: Duration) -> io::Result<bool> {
        let Some(output) = &self.stdout else {
            return Ok(false);
        };
        let mut fds = [PollFd::new(output, PollFlags::IN)];
        let time = Timespec::try_from(timeout).map_err(|_| io::ErrorKind::InvalidInput)?;
        poll(&mut fds, Some(&time))?;
        Ok(!fds[0].revents().is_empty())
    }
    pub fn read(&mut self, raw: &mut [u8]) -> io::Result<usize> {
        self.stdout.as_mut().unwrap().read(raw)
    }
    pub fn stdout_is_closed(&self) -> bool {
        self.stdout.is_none()
    }
    pub fn close_stdout(&mut self) {
        self.stdout.take();
    }
    pub fn try_wait(&mut self) -> io::Result<Option<i32>> {
        Ok(self
            .child
            .try_wait()?
            .map(|s| s.code().unwrap_or_else(|| -s.signal().unwrap_or(0))))
    }
    /// Popen.kill(): 終わった子は回収して送らない。動いていれば SIGKILL を送り、待たない。
    pub fn kill_if_running(&mut self) -> io::Result<()> {
        if self.child.try_wait()?.is_none() {
            self.child.kill()?;
        }
        Ok(())
    }
    pub fn kill_wait(&mut self) -> io::Result<()> {
        self.child.kill()?;
        self.child.wait()?;
        Ok(())
    }
}
unsafe extern "C" {
    #[link_name = "signal"]
    fn c_signal(signal: i32, handler: usize) -> usize;
    #[link_name = "fcntl"]
    fn c_fcntl(fd: i32, command: i32, ...) -> i32;
}
#[repr(C)]
struct DescriptorLimit {
    current: u64,
    maximum: u64,
}
unsafe extern "C" {
    #[link_name = "getrlimit"]
    fn c_getrlimit(resource: i32, limit: *mut DescriptorLimit) -> i32;
}
#[cfg(target_os = "linux")]
unsafe extern "C" {
    #[link_name = "syscall"]
    fn c_syscall(number: i64, ...) -> i64;
}
// メモリ確保・Rust のロックを使わない。std の exec-error pipe は CLOEXEC にするだけ。
fn child_descriptor_flags(fd: i32, keep: &[i32]) -> io::Result<()> {
    if fd < 3 {
        return Ok(());
    }
    // SAFETY: 整数 fd に対する fcntl のみ。
    unsafe {
        let flags = c_fcntl(fd, 1, 0);
        if flags < 0 {
            return Ok(());
        }
        let wanted = if keep.binary_search(&fd).is_ok() {
            flags & !1
        } else {
            flags | 1
        };
        if c_fcntl(fd, 2, wanted) < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}
fn child_descriptor_directory(keep: &[i32]) -> io::Result<bool> {
    #[cfg(target_os = "linux")]
    let (path, flags, name_offset) = (c"/proc/self/fd", 0x80000, 19usize);
    #[cfg(target_os = "macos")]
    let (path, flags, name_offset) = (c"/dev/fd", 0x1000000, 21usize);
    // SAFETY: 固定 NUL 終端パス。子だけで作成/閉鎖する fd。
    let directory = unsafe { c_open(path.as_ptr(), flags) };
    if directory < 0 {
        return Ok(false);
    }
    let result = (|| {
        let mut raw = [0u8; 8192];
        #[cfg(target_os = "macos")]
        let mut base = 0i64;
        loop {
            #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
            let number = 217;
            #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
            let number = 61;
            // SAFETY: 書込み可能な固定 stack buffer。返却長・各レコード長を検査する。
            #[cfg(target_os = "linux")]
            let count =
                unsafe { c_syscall(number, directory, raw.as_mut_ptr(), raw.len()) } as isize;
            // Apple Libc 11.0.1/15.0/15.6 の内部 export。公開 API ではない。
            // gen/FreeBSD/telldir.h の size_t ABI、XNU 64-bit dirent の固定配置を使用。
            // 実 SDK のリンクと実動は Mac CI で必須。EOF 予約末尾4byteは返却長に含まれない。
            #[cfg(target_os = "macos")]
            let count = unsafe {
                c_getdirentries64(directory, raw.as_mut_ptr().cast(), raw.len(), &mut base)
            } as isize;
            if count < 0 {
                if io::Error::last_os_error().raw_os_error() == Some(4) {
                    continue;
                }
                return Ok(false);
            }
            if count == 0 {
                return Ok(true);
            }
            let count = count as usize;
            if count > raw.len() {
                return Err(io::ErrorKind::InvalidData.into());
            }
            let mut at = 0;
            while at < count {
                if count - at < name_offset {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                let length = u16::from_ne_bytes([raw[at + 16], raw[at + 17]]) as usize;
                if length <= name_offset || length > count - at {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                let name = &raw[at + name_offset..at + length];
                let mut fd = 0i32;
                let mut digits = 0;
                let mut valid = true;
                for &byte in name {
                    if byte == 0 {
                        break;
                    }
                    if !byte.is_ascii_digit() {
                        valid = false;
                        break;
                    }
                    if let Some(value) = fd
                        .checked_mul(10)
                        .and_then(|v| v.checked_add((byte - b'0') as i32))
                    {
                        fd = value;
                        digits += 1
                    } else {
                        valid = false;
                        break;
                    }
                }
                if valid && digits > 0 {
                    child_descriptor_flags(fd, keep)?
                }
                at += length;
            }
        }
    })();
    // SAFETY: 所有する列挙用 fd の単回 close。
    unsafe { c_close(directory) };
    result
}
unsafe extern "C" {
    #[link_name = "open"]
    fn c_open(path: *const std::ffi::c_char, flags: i32, ...) -> i32;
    #[link_name = "close"]
    fn c_close(fd: i32) -> i32;
}
#[cfg(target_os = "macos")]
unsafe extern "C" {
    #[link_name = "__getdirentries64"]
    fn c_getdirentries64(
        fd: i32,
        buffer: *mut std::ffi::c_void,
        size: usize,
        base: *mut i64,
    ) -> usize;
}

#[path = "loop_state_signal.rs"]
pub mod signal;

#[cfg(test)]
#[path = "loop_state_tests.rs"]
mod tests;
