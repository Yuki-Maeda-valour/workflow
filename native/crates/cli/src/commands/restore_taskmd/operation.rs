//! 復元の本体(旧 helper の `Restore` :82-572)。関数の順序と失敗の扱いを旧 helper に合わせる。
use super::args::Request;
use super::child::{ChildProcess, RealChild};
use super::realpath::{self, Links, RealpathError};
use super::response::path_field;
use dev_workflow_core::budget::{Budget, BudgetLimits};
use dev_workflow_platform::restore_taskmd::{self as os, ChildKind, Stat};
use sha2::{Digest, Sha256};
use std::io;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub(super) const WORK_SECONDS: Duration = Duration::from_secs(300);
pub(super) const STOP_SECONDS: Duration = Duration::from_secs(5);
pub(super) const CLEANUP_SECONDS: Duration = Duration::from_secs(5);
pub(super) const POLL_SECONDS: Duration = Duration::from_millis(50);
pub(super) const CHUNK_SIZE: u64 = 65536;
pub(super) const MAX_RESPONSE: usize = 4 * 1024 * 1024;
pub(super) const VERSION_BYTES: usize = 8192;
pub(super) const FD_ROOTS: [&[u8]; 2] = [b"/proc/self/fd", b"/dev/fd"];
/// `select` の上限(FD_SETSIZE)。これ以上の番号は Python の `ValueError`。
const SELECT_LIMIT: RawFd = 1024;
const PAYLOAD: &[u8] = b"fd-probe\n";

/// 旧 helper の例外の区別。
#[derive(Debug)]
pub(super) enum Error {
    /// `Failure(reason)`。
    Failure(&'static str),
    /// OSError(番号は応答に出さないので持たない)。
    Os,
    /// ValueError(`select` の fd の範囲外)。
    Value,
    /// ほかの例外(RecursionError と panic)。
    Unexpected,
}

impl From<io::Error> for Error {
    fn from(_: io::Error) -> Self {
        Self::Os
    }
}

type Outcome<T = ()> = Result<T, Error>;

fn fail<T>(reason: &'static str) -> Outcome<T> {
    Err(Error::Failure(reason))
}

/// OSError だけを `reason` に換える(`except OSError: raise Failure(reason)`)。
fn os_as(reason: &'static str) -> impl Fn(Error) -> Error {
    move |error| match error {
        Error::Os => Error::Failure(reason),
        other => other,
    }
}

/// OSError と ValueError を `reason` に換える。
fn os_value_as(reason: &'static str) -> impl Fn(Error) -> Error {
    move |error| match error {
        Error::Os | Error::Value => Error::Failure(reason),
        other => other,
    }
}

/// `getrandom`(Mac は `getentropy`)が使えるか。ENOSYS・EPERM の後はプロセスの間ずっと false。
static GETRANDOM_WORKS: AtomicBool = AtomicBool::new(true);

/// 記録した最初の信号。検査では、旧テストの `op.on_signal()` の直接の呼び出しに当たる
/// 「記録したことにする」値も見る(`cfg(test)` のときだけ)。
pub(super) fn recorded_signal() -> Option<i32> {
    #[cfg(test)]
    if let Some(number) = super::tests::pretended_signal() {
        return Some(number);
    }
    os::recorded_signal()
}

#[cfg(all(test, target_os = "linux"))]
pub(super) fn reset_getrandom() {
    GETRANDOM_WORKS.store(true, Ordering::SeqCst);
}

type Identity = (u64, u64, u32);
type Fingerprint = (u64, u64, i64, u32, i128, i128);

fn identity(info: &Stat) -> Identity {
    (info.dev, info.ino, info.mode & os::S_IFMT)
}

fn fingerprint(info: &Stat) -> Fingerprint {
    (
        info.dev,
        info.ino,
        info.size,
        info.mode,
        info.mtime_ns,
        info.ctime_ns,
    )
}

fn is_kind(info: &Stat, kind: u32) -> bool {
    info.mode & os::S_IFMT == kind
}

fn permissions(info: &Stat) -> u32 {
    info.mode & 0o7777
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Touched {
    None,
    Copied,
    Replaced,
    Unknown,
}

impl Touched {
    fn text(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Copied => "copied",
            Self::Replaced => "replaced",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Temp {
    None,
    Unknown,
    Retained,
    Removed,
}

impl Temp {
    fn text(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Unknown => "unknown",
            Self::Retained => "retained",
            Self::Removed => "removed",
        }
    }
}

/// 検査で短くする定数(旧テストが `mock.patch` で差し替える値)。製品では既定のまま。
pub(super) struct Limits {
    pub(super) stop: Duration,
    pub(super) cleanup: Duration,
    pub(super) max_response: usize,
    pub(super) fd_roots: Vec<Vec<u8>>,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            stop: STOP_SECONDS,
            cleanup: CLEANUP_SECONDS,
            max_response: MAX_RESPONSE,
            fd_roots: FD_ROOTS.iter().map(|root| root.to_vec()).collect(),
        }
    }
}

/// 開いた fd の置き場の番号(旧 helper の `self.fds` の位置)。
pub(super) type FdId = usize;

/// 単一の所有者。
pub struct Restore {
    pub(super) request: Request,
    pub(super) budget: Budget,
    pub(super) limits: Limits,
    pub(super) child: Option<Box<dyn ChildProcess>>,
    pub(super) child_unreaped: bool,
    pub(super) temp_name: Vec<u8>,
    pub(super) temp_path: Vec<u8>,
    pub(super) temp_identity: Option<Identity>,
    pub(super) temp_fd: Option<FdId>,
    pub(super) parent_fd: Option<FdId>,
    pub(super) destination_name: Vec<u8>,
    pub(super) destination_info: Option<Stat>,
    pub(super) entries: Vec<(Vec<u8>, Identity)>,
    pub(super) touched: Touched,
    pub(super) temp: Temp,
    pub(super) reason: Option<&'static str>,
    pub(super) source_fd: Option<FdId>,
    pub(super) prepared_fd: Option<FdId>,
    pub(super) source_info: Option<Stat>,
    pub(super) size: u64,
    pub(super) fd_root: Vec<u8>,
    pub(super) cp: Vec<u8>,
    pub(super) fds: Vec<Option<OwnedFd>>,
    pub(super) chains: Vec<(FdId, Vec<u8>, Identity)>,
    pub(super) handlers_installed: bool,
    pub(super) response_finalized: bool,
    pub(super) response_stopped: Option<i32>,
    #[cfg(test)]
    pub(super) hook: Option<super::tests::Hook>,
}

/// 実際の `lstat` と `readlink`(`--cp` の解決)。
struct OsLinks;

impl Links for OsLinks {
    fn is_link(&mut self, path: &[u8]) -> bool {
        os::lstat_path(path).is_ok_and(|info| is_kind(&info, os::S_IFLNK))
    }
    fn read_link(&mut self, path: &[u8]) -> io::Result<Vec<u8>> {
        os::read_link(path)
    }
}

/// `'%s/%d' % (fd_root, fd)`。
fn alias(root: &[u8], fd: RawFd) -> Vec<u8> {
    let mut path = root.to_vec();
    path.push(b'/');
    path.extend_from_slice(fd.to_string().as_bytes());
    path
}

/// os.path.dirname と os.path.join(dirname, name)。
fn sibling(path: &[u8], name: &[u8]) -> Vec<u8> {
    let i = path.iter().rposition(|c| *c == b'/').map_or(0, |i| i + 1);
    let mut head = path.get(..i).unwrap_or_default().to_vec();
    if !head.is_empty() && !head.iter().all(|c| *c == b'/') {
        while head.last() == Some(&b'/') {
            head.pop();
        }
    }
    if !head.is_empty() && head.last() != Some(&b'/') {
        head.push(b'/');
    }
    head.extend_from_slice(name);
    head
}

fn hex(bytes: &[u8]) -> Vec<u8> {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = Vec::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[usize::from(byte >> 4)]);
        out.push(DIGITS[usize::from(byte & 15)]);
    }
    out
}

macro_rules! inject {
    ($self:ident, $call:expr) => {{
        #[cfg(test)]
        let injected: io::Result<()> = match $self.inject($call) {
            Some(error) => Err(error),
            None => Ok(()),
        };
        #[cfg(not(test))]
        let injected: io::Result<()> = {
            let _ = || $call;
            Ok(())
        };
        injected
    }};
}

/// `EINTR` を再試行する呼び出し(PEP 475 の関数)。
macro_rules! retry {
    ($self:ident, $call:expr, $body:expr) => {
        loop {
            let result = inject!($self, $call).and_then(|()| $body);
            match result {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                other => break other,
            }
        }
    };
}

/// `EINTR` を再試行しない呼び出し。
macro_rules! once {
    ($self:ident, $call:expr, $body:expr) => {
        inject!($self, $call).and_then(|()| $body)
    };
}

impl Restore {
    /// 検査済みの要求から復元の処理を作る。300 秒の期限の起点。
    pub fn new(request: Request) -> Self {
        let started = Instant::now();
        Self {
            request,
            budget: Budget::new(
                BudgetLimits {
                    items: u64::MAX,
                    total_bytes: u64::MAX,
                    file_bytes: u64::MAX,
                },
                started + WORK_SECONDS,
            ),
            limits: Limits::default(),
            child: None,
            child_unreaped: false,
            temp_name: Vec::new(),
            temp_path: Vec::new(),
            temp_identity: None,
            temp_fd: None,
            parent_fd: None,
            destination_name: Vec::new(),
            destination_info: None,
            entries: Vec::new(),
            touched: Touched::None,
            temp: Temp::None,
            reason: None,
            source_fd: None,
            prepared_fd: None,
            source_info: None,
            size: 0,
            fd_root: Vec::new(),
            cp: Vec::new(),
            fds: Vec::new(),
            chains: Vec::new(),
            handlers_installed: false,
            response_finalized: false,
            response_stopped: None,
            #[cfg(test)]
            hook: None,
        }
    }

    /// TERM・INT・HUP の処理を設定したか(書き出しの 2 段目で既定へ戻すため)。
    pub fn handlers_installed(&self) -> bool {
        self.handlers_installed
    }

    // ---- fd の置き場 ----

    fn keep(&mut self, fd: OwnedFd) -> FdId {
        self.fds.push(Some(fd));
        self.fds.len() - 1
    }

    pub(super) fn bfd(&self, id: FdId) -> io::Result<BorrowedFd<'_>> {
        self.fds
            .get(id)
            .and_then(Option::as_ref)
            .map(AsFd::as_fd)
            .ok_or_else(|| io::Error::from_raw_os_error(os::errno::EBADF))
    }

    pub(super) fn raw(&self, id: FdId) -> RawFd {
        self.bfd(id).map_or(-1, |fd| fd.as_raw_fd())
    }

    fn required(value: Option<FdId>) -> io::Result<FdId> {
        value.ok_or_else(|| io::Error::from_raw_os_error(os::errno::EBADF))
    }

    // ---- OS の操作(関数ごとの EINTR の扱い) ----

    fn os_open_root(&mut self) -> io::Result<OwnedFd> {
        retry!(self, Call::OpenRoot, os::open_root())
    }

    fn os_open_directory(&mut self, dir: FdId, name: &[u8]) -> io::Result<OwnedFd> {
        retry!(
            self,
            Call::OpenDirectory(name.to_vec()),
            os::open_directory_at(self.bfd(dir)?, name)
        )
    }

    fn os_open_read(&mut self, dir: FdId, name: &[u8]) -> io::Result<OwnedFd> {
        retry!(
            self,
            Call::OpenRead(name.to_vec()),
            os::open_read_at(self.bfd(dir)?, name)
        )
    }

    fn os_create(&mut self, dir: FdId, name: &[u8]) -> io::Result<OwnedFd> {
        retry!(
            self,
            Call::Create(name.to_vec()),
            os::create_at(self.bfd(dir)?, name)
        )
    }

    fn os_fstat(&mut self, fd: FdId) -> io::Result<Stat> {
        let raw = self.raw(fd);
        retry!(self, Call::Fstat(raw), os::fstat(self.bfd(fd)?))
    }

    /// `os.stat(name, dir_fd=…, follow_symlinks=False)`。再試行しない。
    fn os_stat_at(&mut self, dir: FdId, name: &[u8]) -> io::Result<Stat> {
        once!(
            self,
            Call::StatAt(name.to_vec()),
            os::stat_at(self.bfd(dir)?, name)
        )
    }

    /// `os.stat(path)`(リンクを辿る)。再試行しない。
    fn os_stat_path(&mut self, path: &[u8]) -> io::Result<Stat> {
        #[allow(unused_mut)]
        let mut result = once!(self, Call::StatPath(path.to_vec()), os::stat_path(path));
        #[cfg(test)]
        if let Ok(info) = result.as_mut() {
            self.adjust_stat(path, info);
        }
        result
    }

    fn os_mkdir(&mut self, dir: FdId, name: &[u8], mode: u32) -> io::Result<()> {
        once!(
            self,
            Call::Mkdir(name.to_vec()),
            os::mkdir_at(self.bfd(dir)?, name, mode)
        )
    }

    fn os_unlink(&mut self, dir: FdId, name: &[u8]) -> io::Result<()> {
        once!(
            self,
            Call::Unlink(name.to_vec()),
            os::unlink_at(self.bfd(dir)?, name)
        )
    }

    fn os_rmdir(&mut self, dir: FdId, name: &[u8]) -> io::Result<()> {
        once!(
            self,
            Call::Rmdir(name.to_vec()),
            os::rmdir_at(self.bfd(dir)?, name)
        )
    }

    fn os_rename(&mut self, from: FdId, old: &[u8], to: FdId, new: &[u8]) -> io::Result<()> {
        once!(
            self,
            Call::Rename(old.to_vec()),
            os::rename_at(self.bfd(from)?, old, self.bfd(to)?, new)
        )
    }

    fn os_fchmod(&mut self, fd: FdId, mode: u32) -> io::Result<()> {
        let raw = self.raw(fd);
        retry!(self, Call::Fchmod(raw), os::fchmod(self.bfd(fd)?, mode))
    }

    fn os_ftruncate(&mut self, fd: FdId, length: u64) -> io::Result<()> {
        let raw = self.raw(fd);
        retry!(
            self,
            Call::Ftruncate(raw),
            os::ftruncate(self.bfd(fd)?, length)
        )
    }

    /// `os.lseek`。再試行しない。
    fn os_seek_start(&mut self, fd: FdId) -> io::Result<u64> {
        let raw = self.raw(fd);
        once!(self, Call::Seek(raw), os::seek_start(self.bfd(fd)?))
    }

    /// `os.read(fd, n)`。
    fn os_read(&mut self, fd: FdId, size: usize) -> io::Result<Vec<u8>> {
        let raw = self.raw(fd);
        let mut buffer = vec![0u8; size];
        let count = retry!(
            self,
            Call::Read(raw, size),
            os::read(self.bfd(fd)?, &mut buffer)
        )?;
        buffer.truncate(count);
        Ok(buffer)
    }

    /// `os.read` を保持していない fd(`--version` のパイプ)に使う。
    fn os_read_borrowed(&mut self, fd: BorrowedFd<'_>, size: usize) -> io::Result<Vec<u8>> {
        let mut buffer = vec![0u8; size];
        let count = retry!(
            self,
            Call::Read(fd.as_raw_fd(), size),
            os::read(fd, &mut buffer)
        )?;
        buffer.truncate(count);
        Ok(buffer)
    }

    fn os_write(&mut self, fd: FdId, data: &[u8]) -> io::Result<usize> {
        let raw = self.raw(fd);
        retry!(self, Call::Write(raw), os::write(self.bfd(fd)?, data))
    }

    fn os_dev_null(&mut self) -> io::Result<OwnedFd> {
        retry!(self, Call::DevNull, os::open_dev_null())
    }

    /// `os.urandom(16)`(CPython 3.10.12 の `pyurandom`)。`getrandom`(Mac は `getentropy`)で
    /// 16 バイトを読むまで繰り返す(EINTR は再試行)。ENOSYS・EPERM なら、以後はプロセスの間ずっと
    /// `/dev/urandom` を読む(`getrandom_works = 0` と `dev_urandom`)。
    fn token_hex(&mut self) -> io::Result<Vec<u8>> {
        let mut bytes = [0u8; 16];
        if GETRANDOM_WORKS.load(Ordering::SeqCst) {
            let mut filled = 0usize;
            while filled < bytes.len() {
                let read = retry!(
                    self,
                    Call::Random,
                    os::random_bytes(bytes.get_mut(filled..).unwrap_or_default())
                );
                match read {
                    Ok(count) => filled += count,
                    Err(error)
                        if matches!(
                            error.raw_os_error(),
                            Some(n) if n == os::errno::ENOSYS || n == os::errno::EPERM
                        ) =>
                    {
                        GETRANDOM_WORKS.store(false, Ordering::SeqCst);
                        break;
                    }
                    Err(error) => return Err(error),
                }
            }
            if GETRANDOM_WORKS.load(Ordering::SeqCst) {
                return Ok(hex(&bytes));
            }
        }
        once!(self, Call::Urandom, os::dev_urandom(&mut bytes))?;
        Ok(hex(&bytes))
    }

    /// `select.select([pipe], [], [], timeout)`。EINTR は残り時間を計り直して再試行する。
    fn os_select(&mut self, fd: BorrowedFd<'_>, timeout: Duration) -> io::Result<bool> {
        let end = Instant::now() + timeout;
        loop {
            let left = end.saturating_duration_since(Instant::now());
            let result = inject!(self, Call::Select).and_then(|()| os::wait_readable(fd, left));
            match result {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                other => return other,
            }
        }
    }

    fn os_spawn(
        &mut self,
        args: &[&[u8]],
        devnull: BorrowedFd<'_>,
        kind: ChildKind<'_>,
    ) -> io::Result<i32> {
        let version = matches!(kind, ChildKind::Version { .. });
        inject!(self, Call::Spawn(version))?;
        os::spawn(&self.cp, args, devnull, kind)
    }

    // ---- 旧 helper の関数 ----

    /// `checkpoint`。信号を受けていれば interrupted、期限ちょうど以降なら budget-exceeded。
    pub(super) fn checkpoint(&mut self, stage: &'static str) -> Outcome {
        #[cfg(test)]
        self.at(Point::Checkpoint(stage))?;
        #[cfg(not(test))]
        let _ = stage;
        if recorded_signal().is_some() {
            return fail("interrupted");
        }
        if self.budget.remaining().is_err() {
            return fail("budget-exceeded");
        }
        Ok(())
    }

    fn check_features(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("check_features", 0))?;
        self.cp = match realpath::realpath(&mut OsLinks, &self.request.cp) {
            Ok(path) => path,
            Err(RealpathError::Os) => return Err(Error::Os),
            Err(RealpathError::Recursion) => return Err(Error::Unexpected),
        };
        let cp = self.cp.clone();
        let info = self.os_stat_path(&cp)?;
        let executable = once!(self, Call::Access, os::access_executable(&cp)).is_ok();
        if !is_kind(&info, os::S_IFREG) || !executable {
            return fail("runtime-unavailable");
        }
        self.check_version()
    }

    fn reap_if_exited(&mut self) -> bool {
        let Some(child) = self.child.as_mut() else {
            return true;
        };
        if child.poll().is_none() {
            return false;
        }
        let _ = child.wait();
        self.child = None;
        true
    }

    pub(super) fn stop_child(&mut self) -> bool {
        if self.child_unreaped {
            return false;
        }
        if self.child.is_none() {
            return true;
        }
        for kill in [false, true] {
            if self.reap_if_exited() {
                return true;
            }
            if let Some(child) = self.child.as_mut() {
                let _ = if kill {
                    child.kill()
                } else {
                    child.terminate()
                };
            }
            let until = Instant::now() + self.limits.stop;
            while Instant::now() < until {
                if self.reap_if_exited() {
                    return true;
                }
                std::thread::sleep(
                    POLL_SECONDS.min(until.saturating_duration_since(Instant::now())),
                );
            }
        }
        if self.reap_if_exited() {
            return true;
        }
        self.child_unreaped = true;
        false
    }

    fn child_poll(&mut self) -> Option<i32> {
        self.child.as_mut().and_then(|child| child.poll())
    }

    fn child_wait(&mut self) -> Outcome<i32> {
        match self.child.as_mut() {
            Some(child) => Ok(child.wait()?),
            None => Ok(0),
        }
    }

    fn check_version(&mut self) -> Outcome {
        let mut pipe: Option<OwnedFd> = None;
        let result = self
            .version_body(&mut pipe)
            .map_err(os_value_as("runtime-unavailable"));
        if self.child.is_some() && !self.stop_child() {
            self.reason = Some("child-unreaped");
        }
        let closed = pipe.map(os::close);
        result?;
        if let Some(Err(_)) = closed {
            return Err(Error::Os);
        }
        if self.child_unreaped {
            return fail("child-unreaped");
        }
        Ok(())
    }

    fn version_body(&mut self, pipe: &mut Option<OwnedFd>) -> Outcome {
        let devnull = self.os_dev_null()?;
        let (reader, writer) = inject!(self, Call::Pipe).and_then(|()| os::pipe())?;
        let spawned = self.os_spawn(
            &[b"--version"],
            devnull.as_fd(),
            ChildKind::Version {
                stdout: writer.as_fd(),
            },
        );
        drop(devnull);
        drop(writer);
        let pid = spawned?;
        self.child = Some(Box::new(RealChild::new(pid)));
        let reader: &OwnedFd = pipe.insert(reader);
        let end = self
            .budget
            .deadline()
            .min(Instant::now() + Duration::from_secs(5));
        let mut output: Vec<u8> = Vec::new();
        let mut eof = false;
        while !eof || self.child_poll().is_none() {
            self.checkpoint("version")?;
            if Instant::now() >= end {
                return fail("runtime-unavailable");
            }
            if !eof {
                if reader.as_raw_fd() >= SELECT_LIMIT {
                    return Err(Error::Value);
                }
                if self.os_select(reader.as_fd(), POLL_SECONDS)? {
                    let block =
                        self.os_read_borrowed(reader.as_fd(), VERSION_BYTES + 1 - output.len())?;
                    if block.is_empty() {
                        eof = true;
                    } else {
                        output.extend_from_slice(&block);
                        if output.len() > VERSION_BYTES {
                            return fail("runtime-unavailable");
                        }
                    }
                }
            } else {
                std::thread::sleep(POLL_SECONDS);
            }
        }
        let rc = self.child_wait()?;
        self.child = None;
        if rc != 0 || !output.starts_with(b"cp (GNU coreutils) ") {
            return fail("runtime-unavailable");
        }
        Ok(())
    }

    fn open_parent(&mut self, path: &[u8], reason: &'static str) -> Outcome<(FdId, Vec<u8>)> {
        self.open_parent_body(path).map_err(os_as(reason))
    }

    fn open_parent_body(&mut self, path: &[u8]) -> Outcome<(FdId, Vec<u8>)> {
        let root = self.os_open_root()?;
        let mut fd = self.keep(root);
        let mut chain = Vec::new();
        let parts: Vec<&[u8]> = path
            .get(1..)
            .unwrap_or_default()
            .split(|c| *c == b'/')
            .collect();
        let (name, parents) = parts.split_last().ok_or(Error::Unexpected)?;
        for component in parents {
            self.checkpoint("open-parent")?;
            let opened = self.os_open_directory(fd, component)?;
            let child = self.keep(opened);
            let info = self.os_fstat(child)?;
            chain.push((fd, component.to_vec(), identity(&info)));
            fd = child;
        }
        self.chains.extend(chain);
        Ok((fd, name.to_vec()))
    }

    /// `stat_at`。不在だけ None。
    pub(super) fn stat_at(&mut self, dir: FdId, name: &[u8]) -> Outcome<Option<Stat>> {
        #[cfg(test)]
        if self.absent(name) {
            return Ok(None);
        }
        match self.os_stat_at(dir, name) {
            Ok(info) => Ok(Some(info)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(Error::Os),
        }
    }

    fn check_chains(&mut self) -> Outcome {
        for (fd, name, expected) in self.chains.clone() {
            let info = self.stat_at(fd, &name)?;
            if info.is_none_or(|info| identity(&info) != expected) {
                return fail("verify-failed");
            }
        }
        Ok(())
    }

    fn hash_fd(&mut self, fd: FdId, reason: &'static str) -> Outcome {
        let before = self.os_fstat(fd)?;
        if !is_kind(&before, os::S_IFREG) || u64::try_from(before.size).ok() != Some(self.size) {
            return fail(reason);
        }
        self.os_seek_start(fd)?;
        let mut digest = Sha256::new();
        let mut remaining = self.size;
        while remaining > 0 {
            self.checkpoint("hash-before")?;
            let block = self.os_read(
                fd,
                usize::try_from(CHUNK_SIZE.min(remaining)).unwrap_or(65536),
            )?;
            self.checkpoint("hash-after")?;
            if block.is_empty() {
                return fail(reason);
            }
            digest.update(&block);
            remaining = remaining.saturating_sub(block.len() as u64);
        }
        self.checkpoint("hash-tail-before")?;
        let extra = self.os_read(fd, 1)?;
        self.checkpoint("hash-tail-after")?;
        let after = self.os_fstat(fd)?;
        if !extra.is_empty()
            || fingerprint(&before) != fingerprint(&after)
            || hex(&digest.finalize()) != self.request.expected_sha256
        {
            return fail(reason);
        }
        if permissions(&after) != self.request.expected_mode {
            return fail(reason);
        }
        Ok(())
    }

    fn read_source(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("read_source", 0))?;
        let source = self.request.source.clone();
        let (fd, name) = self.open_parent(&source, "taskmd-body")?;
        self.read_source_body(fd, &name)
            .map_err(os_as("taskmd-body"))
    }

    fn read_source_body(&mut self, dir: FdId, name: &[u8]) -> Outcome {
        let opened = self.os_open_read(dir, name)?;
        let fd = self.keep(opened);
        self.source_fd = Some(fd);
        let info = self.os_fstat(fd)?;
        self.source_info = Some(info);
        if !is_kind(&info, os::S_IFREG)
            || info.size < 0
            || permissions(&info) != self.request.expected_mode
        {
            return fail("taskmd-body");
        }
        self.size = u64::try_from(info.size).unwrap_or(0);
        self.hash_fd(fd, "taskmd-body")
    }

    fn read_destination(&mut self) -> Outcome {
        let destination = self.request.destination.clone();
        let (fd, name) = self.open_parent(&destination, "dest-symlink")?;
        self.parent_fd = Some(fd);
        self.destination_name = name.clone();
        self.destination_info = self.stat_at(fd, &name)?;
        let Some(info) = self.destination_info else {
            return Ok(());
        };
        if is_kind(&info, os::S_IFLNK) {
            return fail("dest-symlink");
        }
        if is_kind(&info, os::S_IFDIR) {
            return fail("dest-dir");
        }
        if !is_kind(&info, os::S_IFREG) {
            return fail("dest-special");
        }
        Ok(())
    }

    fn set_entry(&mut self, name: &[u8], value: Identity) {
        match self.entries.iter_mut().find(|(key, _)| key == name) {
            Some(entry) => entry.1 = value,
            None => self.entries.push((name.to_vec(), value)),
        }
    }

    fn entry(&self, name: &[u8]) -> Outcome<Identity> {
        self.entries
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| *value)
            .ok_or(Error::Unexpected)
    }

    fn pop_entry(&mut self, name: &[u8]) -> Outcome<Identity> {
        let index = self
            .entries
            .iter()
            .position(|(key, _)| key == name)
            .ok_or(Error::Unexpected)?;
        Ok(self.entries.remove(index).1)
    }

    fn create_file(&mut self, name: &[u8]) -> Outcome<FdId> {
        let temp = Self::required(self.temp_fd)?;
        let opened = self.os_create(temp, name)?;
        let fd = self.keep(opened);
        let info = self.os_fstat(fd)?;
        self.set_entry(name, identity(&info));
        Ok(fd)
    }

    fn prepare_directory(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("prepare_directory", 0))?;
        self.checkpoint("before-prepare")?;
        let mut name = b".restore-taskmd-".to_vec();
        name.extend_from_slice(&self.token_hex()?);
        self.temp_name = name;
        self.temp_path = sibling(&self.request.destination, &self.temp_name);
        if path_field(&self.temp_path).len() + 256 > self.limits.max_response {
            return fail("internal-argument");
        }
        self.prepare_body().map_err(os_as("prepare-failed"))
    }

    fn prepare_body(&mut self) -> Outcome {
        let parent = Self::required(self.parent_fd)?;
        let name = self.temp_name.clone();
        self.os_mkdir(parent, &name, 0o700)?;
        self.temp = Temp::Unknown;
        let info = self.os_stat_at(parent, &name)?;
        self.temp_identity = Some(identity(&info));
        let opened = self.os_open_directory(parent, &name)?;
        let fd = self.keep(opened);
        self.temp_fd = Some(fd);
        let info = self.os_fstat(fd)?;
        if Some(identity(&info)) != self.temp_identity || permissions(&info) != 0o700 {
            return fail("prepare-failed");
        }
        self.temp = Temp::Retained;
        self.prepared_fd = Some(self.create_file(b"prepared")?);
        Ok(())
    }

    pub(super) fn copy_file(
        &mut self,
        source: FdId,
        destination: FdId,
        size: u64,
        root: &[u8],
    ) -> Outcome {
        #[cfg(test)]
        {
            let raw = self.raw(source) as i64;
            self.at(Point::Enter("copy_file", raw))?;
        }
        self.checkpoint("before-copy")?;
        let pid = self
            .spawn_copy(source, destination, size, root)
            .map_err(|_| Error::Failure("runtime-unavailable"))?;
        self.child = Some(Box::new(RealChild::new(pid)));
        let result = self.copy_wait(destination, size);
        if self.child.is_some() && !self.stop_child() {
            self.reason = Some("child-unreaped");
        }
        result?;
        if self.child_unreaped {
            return fail("child-unreaped");
        }
        #[cfg(test)]
        {
            let raw = self.raw(source) as i64;
            self.at(Point::After("copy_file", raw))?;
        }
        Ok(())
    }

    fn spawn_copy(
        &mut self,
        source: FdId,
        destination: FdId,
        size: u64,
        root: &[u8],
    ) -> io::Result<i32> {
        let devnull = self.os_dev_null()?;
        inject!(self, Call::Spawn(false))?;
        let input = alias(root, self.raw(source));
        let output = alias(root, self.raw(destination));
        let args: [&[u8]; 5] = [b"-aL", b"--reflink=never", b"--", &input, &output];
        let keep = [self.bfd(source)?, self.bfd(destination)?];
        os::spawn(
            &self.cp,
            &args,
            devnull.as_fd(),
            ChildKind::Copy {
                keep,
                file_size: size,
            },
        )
    }

    fn copy_wait(&mut self, destination: FdId, size: u64) -> Outcome {
        while self.child_poll().is_none() {
            self.checkpoint("copy")?;
            if u64::try_from(self.os_fstat(destination)?.size).unwrap_or(u64::MAX) > size {
                return fail("copy-failed");
            }
            std::thread::sleep(POLL_SECONDS);
        }
        let rc = self.child_wait()?;
        self.child = None;
        self.checkpoint("after-copy")?;
        if rc != 0 || u64::try_from(self.os_fstat(destination)?.size).ok() != Some(size) {
            return fail("copy-failed");
        }
        Ok(())
    }

    fn aliases_match(&mut self, root: &[u8], fds: &[FdId]) -> Outcome<bool> {
        for &fd in fds {
            let path = alias(root, self.raw(fd));
            let info = self.os_stat_path(&path)?;
            if !is_kind(&info, os::S_IFREG) {
                return Ok(false);
            }
            let held = self.os_fstat(fd)?;
            if identity(&info) != identity(&held) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn probe(&mut self) -> Outcome {
        let (source, destination) = self.probe_setup().map_err(|error| match error {
            Error::Os | Error::Value => Error::Failure("runtime-unavailable"),
            other => other,
        })?;
        let mut chosen = None;
        let mut accepted = None;
        for root in self.limits.fd_roots.clone() {
            self.checkpoint("probe")?;
            match self.probe_alias(&root, source, destination) {
                Ok(Some(info)) => {
                    chosen = Some(root);
                    accepted = Some(info);
                    break;
                }
                Ok(None) | Err(Error::Os) | Err(Error::Value) => continue,
                Err(Error::Failure(reason)) => {
                    if reason != "copy-failed" || self.child_unreaped {
                        return fail(reason);
                    }
                }
                Err(Error::Unexpected) => return Err(Error::Unexpected),
            }
        }
        let (Some(root), Some(info)) = (chosen, accepted) else {
            return fail("fd-copy-unavailable");
        };
        self.probe_finish(&info)
            .map_err(os_value_as("runtime-unavailable"))?;
        self.fd_root = root;
        Ok(())
    }

    fn probe_setup(&mut self) -> Outcome<(FdId, FdId)> {
        let temp = Self::required(self.temp_fd)?;
        self.os_mkdir(temp, b"probe-directory", 0o700)?;
        let opened = self.os_open_directory(temp, b"probe-directory")?;
        let directory = self.keep(opened);
        let info = self.os_fstat(directory)?;
        self.set_entry(b"probe-directory", identity(&info));
        self.remove_entry(b"probe-directory")?;
        let source = self.create_file(b"probe-source")?;
        let destination = self.create_file(b"probe-destination")?;
        if self.os_write(source, PAYLOAD)? != PAYLOAD.len() {
            return fail("runtime-unavailable");
        }
        self.os_fchmod(source, 0o640)?;
        Ok((source, destination))
    }

    fn probe_alias(
        &mut self,
        root: &[u8],
        source: FdId,
        destination: FdId,
    ) -> Outcome<Option<Stat>> {
        let held = [
            Self::required(self.source_fd)?,
            Self::required(self.prepared_fd)?,
            source,
            destination,
        ];
        if !self.aliases_match(root, &held)? {
            return Ok(None);
        }
        self.os_ftruncate(destination, 0)?;
        self.copy_file(source, destination, PAYLOAD.len() as u64, root)?;
        if self.child_unreaped {
            return fail("child-unreaped");
        }
        self.os_seek_start(destination)?;
        let info = self.os_fstat(destination)?;
        if self.os_read(destination, PAYLOAD.len() + 1)? != PAYLOAD || permissions(&info) != 0o640 {
            return Ok(None);
        }
        if identity(&info) != self.entry(b"probe-destination")? {
            return Ok(None);
        }
        Ok(Some(info))
    }

    fn probe_finish(&mut self, info: &Stat) -> Outcome {
        let temp = Self::required(self.temp_fd)?;
        self.os_rename(temp, b"probe-destination", temp, b"probe-renamed")?;
        let moved = self.pop_entry(b"probe-destination")?;
        self.set_entry(b"probe-renamed", moved);
        if identity(&self.os_stat_at(temp, b"probe-renamed")?) != identity(info) {
            return fail("runtime-unavailable");
        }
        self.remove_entry(b"probe-renamed")?;
        self.remove_entry(b"probe-source")
    }

    fn check_prepared_name(&mut self) -> Outcome {
        let temp = Self::required(self.temp_fd)?;
        let prepared = Self::required(self.prepared_fd)?;
        let info = self.stat_at(temp, b"prepared")?;
        let same = match info {
            None => false,
            Some(info) => identity(&info) == identity(&self.os_fstat(prepared)?),
        };
        if !same {
            return fail("verify-failed");
        }
        let parent = Self::required(self.parent_fd)?;
        let name = self.temp_name.clone();
        let info = self.stat_at(parent, &name)?;
        if info.is_none_or(|info| Some(identity(&info)) != self.temp_identity) {
            return fail("verify-failed");
        }
        Ok(())
    }

    fn before_publish(&mut self) -> Outcome {
        self.checkpoint("before-publish")?;
        self.check_chains()?;
        self.check_prepared_name()?;
        let source = Self::required(self.source_fd)?;
        let now = self.os_fstat(source)?;
        if self.source_info.map(|info| fingerprint(&info)) != Some(fingerprint(&now)) {
            return fail("taskmd-body");
        }
        let parent = Self::required(self.parent_fd)?;
        let name = self.destination_name.clone();
        let now = self.stat_at(parent, &name)?;
        if now.is_none() != self.destination_info.is_none() {
            return fail("verify-failed");
        }
        if let (Some(now), Some(before)) = (now, self.destination_info)
            && fingerprint(&now) != fingerprint(&before)
        {
            return fail("verify-failed");
        }
        self.checkpoint("publish-ready")
    }

    fn publish(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("publish", 0))?;
        self.before_publish()?;
        let mask = inject!(self, Call::Block).and_then(|()| os::block_recorded_signals())?;
        let result = self.publish_masked();
        let restored = inject!(self, Call::Unblock).and_then(|()| os::restore_signal_mask(&mask));
        restored?;
        result?;
        self.checkpoint("after-publish")
    }

    fn publish_masked(&mut self) -> Outcome {
        self.checkpoint("replace")?;
        // syscall が返らない・予期しない例外のときは無変更とは断言しない。
        self.touched = Touched::Unknown;
        let temp = Self::required(self.temp_fd)?;
        let parent = Self::required(self.parent_fd)?;
        let name = self.destination_name.clone();
        if self.os_rename(temp, b"prepared", parent, &name).is_err() {
            self.touched = Touched::None;
            return fail("replace-failed");
        }
        #[cfg(test)]
        self.at(Point::After("replace", 0))?;
        self.touched = if self.destination_info.is_none() {
            Touched::Copied
        } else {
            Touched::Replaced
        };
        self.pop_entry(b"prepared")?;
        Ok(())
    }

    fn verify_published(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("verify_published", 0))?;
        self.check_chains()?;
        self.check_published_name()?;
        let prepared = Self::required(self.prepared_fd)?;
        self.hash_fd(prepared, "verify-failed")?;
        self.check_published_name()?;
        self.checkpoint("after-verify")
    }

    fn check_published_name(&mut self) -> Outcome {
        let parent = Self::required(self.parent_fd)?;
        let prepared = Self::required(self.prepared_fd)?;
        let name = self.destination_name.clone();
        let info = self.stat_at(parent, &name)?;
        let same = match info {
            None => false,
            Some(info) => identity(&info) == identity(&self.os_fstat(prepared)?),
        };
        if !same {
            return fail("verify-failed");
        }
        Ok(())
    }

    pub(super) fn remove_entry(&mut self, name: &[u8]) -> Outcome {
        #[cfg(test)]
        self.at(Point::Remove(name.to_vec()))?;
        let temp = Self::required(self.temp_fd)?;
        let Some(info) = self.stat_at(temp, name)? else {
            return fail("cleanup-failed");
        };
        if identity(&info) != self.entry(name)? {
            return fail("cleanup-failed");
        }
        if is_kind(&info, os::S_IFDIR) {
            self.os_rmdir(temp, name)?;
        } else {
            self.os_unlink(temp, name)?;
        }
        self.pop_entry(name)?;
        Ok(())
    }

    fn cleanup(&mut self) -> Outcome {
        #[cfg(test)]
        self.at(Point::Enter("cleanup", 0))?;
        if self.temp == Temp::None {
            return Ok(());
        }
        let end = Instant::now() + self.limits.cleanup;
        self.cleanup_body(end)
            .map_err(os_value_as("cleanup-failed"))
    }

    fn cleanup_body(&mut self, end: Instant) -> Outcome {
        self.temp = Temp::Unknown;
        self.check_chains()?;
        let parent = Self::required(self.parent_fd)?;
        let name = self.temp_name.clone();
        let info = self.stat_at(parent, &name)?;
        let same = matches!((info, self.temp_identity), (Some(info), Some(expected)) if identity(&info) == expected);
        if !same {
            self.temp = Temp::Unknown;
            return fail("cleanup-failed");
        }
        self.temp = Temp::Retained;
        if self.child_unreaped {
            return Ok(());
        }
        if self.temp_fd.is_none() {
            return fail("cleanup-failed");
        }
        let names: Vec<Vec<u8>> = self.entries.iter().map(|(key, _)| key.clone()).collect();
        for entry in names {
            if Instant::now() >= end {
                return fail("cleanup-failed");
            }
            self.remove_entry(&entry)?;
        }
        if Instant::now() >= end {
            return fail("cleanup-failed");
        }
        // 親の名前と保持 fd の両方がまだ自分の専用領域であることを確認する。
        let info = self.stat_at(parent, &name)?;
        if info.is_none_or(|info| Some(identity(&info)) != self.temp_identity) {
            self.temp = Temp::Unknown;
            return fail("cleanup-failed");
        }
        self.os_rmdir(parent, &name)?;
        self.temp = Temp::Removed;
        if Instant::now() >= end {
            return fail("cleanup-failed");
        }
        Ok(())
    }

    /// 応答を作る。確定後は確定した時の信号の記録を使う。
    pub(super) fn response(&mut self) -> (u8, Vec<u8>) {
        let stopped = if self.response_finalized {
            self.response_stopped
        } else {
            recorded_signal()
        };
        if stopped.is_some() {
            self.reason = Some("interrupted");
        } else if self.child_unreaped {
            self.reason = Some("child-unreaped");
        }
        let rc = if stopped.is_some() {
            20
        } else if self.reason.is_some() {
            33
        } else {
            0
        };
        let mut result = format!(
            "RESTORED={}\nTOUCHED={}\nTEMP={}\n",
            if rc != 0 { "no" } else { "yes" },
            self.touched.text(),
            self.temp.text()
        )
        .into_bytes();
        if let Some(reason) = self.reason {
            result.extend_from_slice(format!("REASON={reason}\n").as_bytes());
        }
        if self.temp == Temp::Retained {
            result.extend_from_slice(b"TEMP_PATH=");
            result.extend_from_slice(&path_field(&self.temp_path));
            result.push(b'\n');
        }
        (rc, result)
    }

    fn run_main(&mut self) -> Outcome {
        self.handlers_installed = true;
        inject!(self, Call::Handlers)?;
        os::install_recording_handlers()?;
        #[cfg(test)]
        super::tests::reset_pretended();
        self.checkpoint("start")?;
        self.check_features()?;
        self.read_source()?;
        self.read_destination()?;
        self.prepare_directory()?;
        self.probe()?;
        let source = Self::required(self.source_fd)?;
        let prepared = Self::required(self.prepared_fd)?;
        let root = self.fd_root.clone();
        self.copy_file(source, prepared, self.size, &root)?;
        let now = self.os_fstat(source)?;
        if self.source_info.map(|info| fingerprint(&info)) != Some(fingerprint(&now)) {
            return fail("taskmd-body");
        }
        self.hash_fd(prepared, "verify-failed")?;
        self.publish()?;
        self.verify_published()
    }

    fn set_reason_if_none(&mut self, reason: &'static str) {
        if self.reason.is_none() {
            self.reason = Some(reason);
        }
    }

    /// 復元を実行し、終了値と応答を返す。想定外の失敗(panic を含む)は、一時フォルダの有無で
    /// runtime-unavailable か verify-failed にし、中身・パスを応答に混ぜない。
    pub fn run(&mut self) -> (u8, Vec<u8>) {
        let previous = std::panic::take_hook();
        std::panic::set_hook(Box::new(|_| {}));
        let main = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.run_main()));
        match main {
            Ok(Ok(())) => {}
            Ok(Err(Error::Failure(reason))) => self.reason = Some(reason),
            Ok(Err(_)) | Err(_) => {
                self.reason = Some(if self.temp == Temp::None {
                    "runtime-unavailable"
                } else {
                    "verify-failed"
                });
            }
        }
        if self.child.is_some() {
            self.stop_child();
        }
        if let Err(Error::Failure(reason)) = self.checkpoint("before-cleanup") {
            self.set_reason_if_none(reason);
        }
        let cleaned = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.cleanup()));
        if !matches!(cleaned, Ok(Ok(()))) {
            self.set_reason_if_none("cleanup-failed");
        }
        for index in (0..self.fds.len()).rev() {
            let Some(fd) = self.fds.get_mut(index).and_then(Option::take) else {
                continue;
            };
            #[cfg(test)]
            let injected = self.inject(Call::Close(fd.as_raw_fd()));
            #[cfg(not(test))]
            let injected: Option<io::Error> = None;
            let closed = os::close(fd);
            if injected.is_some() || closed.is_err() {
                self.set_reason_if_none("cleanup-failed");
            }
        }
        if let Err(Error::Failure(reason)) = self.checkpoint("before-response") {
            self.set_reason_if_none(reason);
        }
        std::panic::set_hook(previous);
        self.response()
    }

    /// `main` の `operation.checkpoint('before-output')`。
    pub fn checkpoint_before_output(&mut self) {
        if let Err(Error::Failure(reason)) = self.checkpoint("before-output") {
            self.set_reason_if_none(reason);
        }
    }
}

/// 検査の差し込み点の呼び出しの種類。製品では作らない。
#[derive(Clone, Debug, PartialEq, Eq)]
#[cfg_attr(not(test), allow(dead_code))]
pub(super) enum Call {
    Handlers,
    OpenRoot,
    OpenDirectory(Vec<u8>),
    OpenRead(Vec<u8>),
    Create(Vec<u8>),
    Fstat(RawFd),
    StatAt(Vec<u8>),
    StatPath(Vec<u8>),
    Access,
    Mkdir(Vec<u8>),
    Unlink(Vec<u8>),
    Rmdir(Vec<u8>),
    Rename(Vec<u8>),
    Fchmod(RawFd),
    Ftruncate(RawFd),
    Seek(RawFd),
    Read(RawFd, usize),
    Write(RawFd),
    DevNull,
    Pipe,
    Spawn(bool),
    Select,
    Random,
    Urandom,
    Block,
    Unblock,
    Close(RawFd),
}

/// 検査の差し込み点。
#[cfg(test)]
#[derive(Clone, Debug)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub(super) enum Point {
    Checkpoint(&'static str),
    Enter(&'static str, i64),
    After(&'static str, i64),
    Remove(Vec<u8>),
    /// `stat_at` の helper(旧テストが `op.stat_at` を差し替える所)。
    StatAt(Vec<u8>),
    Os(Call),
}
