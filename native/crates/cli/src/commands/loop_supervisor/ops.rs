//! 監督と親が使う OS の操作と時計の単位(設計の「比べ方の決まり」の操作の単位の表)と、Python の例外の値。
//! - 製品は本物の実装 [`Real`](platform の `r4q_process`)を使う。差し替えた実装は検査の中だけで使う。
//! - Python が C の中で自動で繰り返す `EINTR`(PEP 475)は、本物の実装の中で繰り返す。
//! - 標準ライブラリの中で呼ばれる OS の操作(`Popen.wait` の中の `waitpid` など)は、その単位の中に入れる。
//! - OS で分かれる所は組み立ての時点の判定ではなく、[`Ops::platform`]・[`Ops::has_pidfd`] で分ける。

use super::r4q_python::exception::{self, Filename, OsError};
use super::r4q_python::oserror;
use dev_workflow_platform::r4q_process as os;
use std::collections::HashMap;
use std::os::fd::{AsFd, AsRawFd, OwnedFd, RawFd};
use std::time::{Duration, Instant};

/// Python の例外の種類(`except` が捕まえるかを決める範囲)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Class {
    /// `OSError`(errno から下位の種類を選ぶ)。
    Os(i32),
    /// `ValueError` とその子(`JSONDecodeError`・`UnicodeDecodeError`)。値は型の名前。
    Value(&'static str),
    /// `RuntimeError` とその子(`RecursionError`)。値は型の名前。
    Runtime(&'static str),
    /// `subprocess.SubprocessError` とその子(`TimeoutExpired`)。値は型の名前。
    Subprocess(&'static str),
    /// check-process の `RecoveryError`。
    Recovery,
    /// check-process の `Interrupted`(`RecoveryError` の子)。
    Interrupted,
    /// check-process の `LaunchError`(監督を起動できない)。
    Launch,
    /// `OverflowError`。
    Overflow,
    /// `TypeError`。
    Type,
    /// `KeyError`。
    Key,
    /// `IndexError`。
    Index,
}

/// Python の例外(種類と `str(exc)`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyErr {
    pub class: Class,
    pub message: String,
}

impl PyErr {
    pub fn new(class: Class, message: impl Into<String>) -> Self {
        Self {
            class,
            message: message.into(),
        }
    }

    pub fn value(message: impl Into<String>) -> Self {
        Self::new(Class::Value("ValueError"), message)
    }

    pub fn runtime(message: impl Into<String>) -> Self {
        Self::new(Class::Runtime("RuntimeError"), message)
    }

    pub fn recovery(message: impl Into<String>) -> Self {
        Self::new(Class::Recovery, message)
    }

    /// `OSError(errno, strerror, filename)` の例外。
    pub fn os(failure: &OsFailure, filename: Option<Filename>) -> Self {
        let error = OsError {
            errno: failure.errno,
            strerror: failure.text.clone(),
            filename,
            filename2: None,
        };
        Self::new(Class::Os(failure.errno), error.message())
    }

    /// `type(exc).__name__`。
    pub fn type_name(&self) -> &'static str {
        match &self.class {
            Class::Os(errno) => oserror::exception_type(*errno),
            Class::Value(name) | Class::Runtime(name) | Class::Subprocess(name) => name,
            Class::Recovery => "RecoveryError",
            Class::Interrupted => "Interrupted",
            Class::Launch => "LaunchError",
            Class::Overflow => "OverflowError",
            Class::Type => "TypeError",
            Class::Key => "KeyError",
            Class::Index => "IndexError",
        }
    }

    /// errno(OSError のとき)。
    pub fn errno(&self) -> Option<i32> {
        match self.class {
            Class::Os(errno) => Some(errno),
            _ => None,
        }
    }
}

/// OS の誤り(errno と文。本物は基準の表の文、差し替えは応答の文)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OsFailure {
    pub errno: i32,
    pub text: String,
}

impl OsFailure {
    /// 本物の誤り(文はこの実行物の OS の表)。
    pub fn from_errno(errno: i32) -> Self {
        Self {
            errno,
            text: exception::strerror(errno),
        }
    }

    fn from_io(error: &std::io::Error) -> Self {
        Self::from_errno(error.raw_os_error().unwrap_or(0))
    }
}

pub type OsResult<T> = Result<T, OsFailure>;

/// 置く信号の処理(旧版の Python の関数の名前で区別する)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Handler {
    /// loop-supervisor の `interrupted`(後の信号で上書き)。
    Interrupted,
    /// check-process の `StopSignals.receive`(最初の 1 つ)。
    Receive,
    /// check-process の anchor の `lambda *_: None`。
    Nothing,
    /// `signal.signal(number, previous)` で戻す前の処理。
    Previous(Saved),
}

/// 置き換える前の処理の控え(番号)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Saved(pub usize);

/// OS の名前(`sys.platform`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Darwin,
}

impl Platform {
    pub fn name(self) -> &'static str {
        match self {
            Platform::Linux => "linux",
            Platform::Darwin => "darwin",
        }
    }
}

/// `os.open` の flags(名前の集合で記録する)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OpenFlags {
    /// 記録を作る(`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`、0o600)。
    Create,
    /// 記録を読む(`O_RDONLY|O_NONBLOCK|O_NOFOLLOW`)。
    Read,
}

impl OpenFlags {
    pub fn names(self) -> &'static [&'static str] {
        match self {
            OpenFlags::Create => &["O_CREAT", "O_EXCL", "O_NOFOLLOW", "O_WRONLY"],
            OpenFlags::Read => &["O_NOFOLLOW", "O_NONBLOCK", "O_RDONLY"],
        }
    }
}

/// `os.fstat` のうち旧版が見る値。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileInfo {
    pub regular: bool,
    pub size: i64,
}

/// check-process の `launch_monitor` に渡す値(argv と設定)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MonitorLaunch {
    /// 実際に起動する argv(監督のプログラムと前に付ける引数を含む)。
    pub argv: Vec<Vec<u8>>,
    pub cwd: Option<Vec<u8>>,
    /// 子の環境(辞書の並び。鍵は重ならない)。None は起動した側の環境。
    pub env: Option<Vec<(Vec<u8>, Vec<u8>)>>,
    pub stdout: i32,
    pub stderr: i32,
    pub pass_fds: Vec<i32>,
    pub new_session: bool,
}

/// `Popen.wait(timeout)` の失敗。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WaitFailure {
    /// `TimeoutExpired`(文は呼出元が作る)。
    Timeout,
    /// OSError。
    Os(OsFailure),
}

/// `select.select` の失敗。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SelectFailure {
    /// `ValueError('filedescriptor out of range in select()')`。
    OutOfRange,
    Os(OsFailure),
}

/// 監督と親が使う操作の単位。名前は設計の操作の単位の表の名前。
pub trait Ops {
    /// `time.monotonic()`。
    fn monotonic(&mut self) -> f64;
    /// `time.sleep(seconds)`(負の値は `ValueError`、大きすぎる値は `OverflowError`)。
    fn sleep(&mut self, seconds: f64) -> Result<(), PyErr>;
    fn getpid(&mut self) -> i32;
    fn getppid(&mut self) -> i32;
    /// `libc.prctl(option, arg, 0, 0, 0)` の戻り値。
    fn prctl(&mut self, option: i32, arg: i32) -> i32;
    /// `ctypes.get_errno()`。
    fn get_errno(&mut self) -> i32;
    /// `signal.signal(number, handler)`。戻り値は前の処理の控え。
    fn set_handler(&mut self, number: i32, handler: Handler) -> Saved;
    /// loop-supervisor の `STOP`(`interrupted` が書く番号)。操作ではない(記録しない)。
    fn latest_signal(&self) -> i32;
    fn set_latest_signal(&mut self, number: i32);
    /// Python の処理の区切りで、既定の SIGINT の処理(KeyboardInterrupt)の印を見て下ろす。操作ではない
    /// (記録しない)。SIGINT を別の処理に置き換えた後は立たない。
    fn keyboard_interrupt(&mut self) -> bool;
    /// `StopSignals.number`(`receive` が書く最初の番号)。操作ではない。
    fn first_signal(&self) -> i32;
    fn clear_first_signal(&mut self);
    /// loop-supervisor の `subprocess.Popen(argv, start_new_session=True, close_fds=True)`。pid を返す。
    fn spawn(&mut self, argv: &[Vec<u32>]) -> Result<i32, PyErr>;
    /// `os.waitpid(-1, os.WNOHANG)`(戻り値は pid と状態。`ECHILD` は誤り)。
    fn waitpid_any(&mut self) -> OsResult<(i32, i32)>;
    /// `Path(f'/proc/self/task/{pid}/children').read_text()`(開く段の OSError はファイル名付き)。
    fn read_children(&mut self, pid: i32) -> Result<Vec<u32>, PyErr>;
    /// `Path(f'/proc/{pid}/stat').read_bytes()`(`pid` は 10 進の字面。開く段の OSError はファイル名付き)。
    fn read_stat(&mut self, pid: &str) -> Result<Vec<u8>, PyErr>;
    fn pidfd_open(&mut self, pid: i32) -> OsResult<i32>;
    fn pidfd_send_signal(&mut self, fd: i32, number: i32) -> OsResult<()>;
    /// `os.close(fd)`。
    fn close(&mut self, fd: i32) -> OsResult<()>;
    /// check-process の `launch_monitor`(`OSError` は呼出元が `LaunchError` にする)。pid を返す。
    fn spawn_monitor(&mut self, launch: &MonitorLaunch) -> Result<i32, PyErr>;
    /// `process.poll()`。
    fn poll_monitor(&mut self, pid: i32) -> Option<i32>;
    /// `process.wait(timeout=…)`(None は期限なし)。
    fn wait_monitor(&mut self, pid: i32, timeout: Option<f64>) -> Result<i32, WaitFailure>;
    /// `secrets.token_hex(32)`(`os.urandom` の誤りは OSError・`NotImplementedError`・`RuntimeError`)。
    fn token_hex(&mut self) -> Result<String, PyErr>;
    /// `os.pipe()`(読み口と書き口)。
    fn pipe(&mut self) -> OsResult<(i32, i32)>;
    /// `select.select([fd], [], [], timeout)` で読めるか。
    fn select(&mut self, fd: i32, timeout: f64) -> Result<bool, SelectFailure>;
    /// `os.read(fd, size)`。
    fn read(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>>;
    /// `os.killpg(pgid, number)`。
    fn killpg(&mut self, pgid: i32, number: i32) -> OsResult<()>;
    /// `os.open(path, flags[, 0o600])`。
    fn open(&mut self, path: &[u8], flags: OpenFlags) -> OsResult<i32>;
    /// `os.fstat(fd)`(記録の fd)。
    fn fstat(&mut self, fd: i32) -> OsResult<FileInfo>;
    /// `os.write(fd, data)`。
    fn write(&mut self, fd: i32, data: &[u8]) -> OsResult<usize>;
    /// `os.fsync(fd)`。
    fn fsync(&mut self, fd: i32) -> OsResult<()>;
    /// `sys.platform`(Linux か、それ以外の POSIX)。
    fn platform(&mut self) -> Platform;
    /// `hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal')`。
    fn has_pidfd(&mut self) -> bool;
    /// `Path(path).open('w+b')`。
    fn open_output(&mut self, path: &[u8]) -> OsResult<i32>;
    /// 出力のファイルを閉じる(`with` の終わり。`open_output` の単位)。
    fn close_output(&mut self, fd: i32) -> OsResult<()>;
    /// `os.fstat(stream.fileno()).st_size`。
    fn fstat_output(&mut self, fd: i32) -> OsResult<i64>;
    /// `stream.flush()` と `stream.seek(0)`(`read_output` の単位)。
    fn rewind_output(&mut self, fd: i32) -> OsResult<()>;
    /// `stream.read(size)`(`read_output` の単位)。
    fn read_output(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>>;
}

/// `/proc` の読取りの失敗を、旧版の OSError にする(開く段だけファイル名が付く)。
fn proc_error(error: &os::ProcReadError, path: &str) -> PyErr {
    let failure = OsFailure::from_errno(error.errno());
    let filename = error
        .has_filename()
        .then(|| Filename::from_raw(path.as_bytes()));
    PyErr::os(&failure, filename)
}

/// getrandom(Mac は getentropy)が使えるか(CPython の `getrandom_works`。ENOSYS・EPERM で以後は偽)。
static GETRANDOM_WORKS: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);

/// `os.urandom(len(buffer))`(CPython 3.10.11 の `pyurandom(blocking=1, raise=1)`)。
/// - getrandom(Mac は getentropy。1 回 256 バイトまで)を、満ちるまで繰り返す。EINTR は繰り返す。
/// - ENOSYS・EPERM のときだけ、以後はプロセスの間ずっと `/dev/urandom` を読む(全部を読み直す)。
/// - ほかの errno は OSError(ファイル名なし)。`/dev/urandom` の段の誤りは [`os::UrandomError`] の文。
fn urandom(buffer: &mut [u8]) -> Result<(), PyErr> {
    use std::sync::atomic::Ordering;
    if GETRANDOM_WORKS.load(Ordering::SeqCst) {
        let mut filled = 0usize;
        while filled < buffer.len() {
            match dev_workflow_platform::restore_taskmd::random_bytes(&mut buffer[filled..]) {
                Ok(count) => filled += count,
                Err(error) => match error.raw_os_error() {
                    Some(os::errno::EINTR) => {}
                    Some(errno) if errno == os::errno::ENOSYS || errno == os::errno::EPERM => {
                        GETRANDOM_WORKS.store(false, Ordering::SeqCst);
                        break;
                    }
                    _ => return Err(PyErr::os(&OsFailure::from_io(&error), None)),
                },
            }
        }
        if GETRANDOM_WORKS.load(Ordering::SeqCst) {
            return Ok(());
        }
    }
    os::dev_urandom(buffer).map_err(|error| match error {
        os::UrandomError::NotFound => PyErr::new(
            Class::Runtime("NotImplementedError"),
            "/dev/urandom (or equivalent) not found",
        ),
        os::UrandomError::Open(error) => PyErr::os(
            &OsFailure::from_io(&error),
            Some(Filename::from_raw(b"/dev/urandom")),
        ),
        os::UrandomError::Fstat(error) | os::UrandomError::Read(error) => {
            PyErr::os(&OsFailure::from_io(&error), None)
        }
        os::UrandomError::Eof(remaining) => PyErr::runtime(format!(
            "Failed to read {remaining} bytes from /dev/urandom"
        )),
    })
}

/// `os.waitstatus_to_exitcode(status)`。
pub fn exit_code(status: i32) -> Result<i32, PyErr> {
    let low = status & 0x7f;
    if low == 0 {
        return Ok((status >> 8) & 0xff);
    }
    if low != 0x7f {
        return Ok(-low);
    }
    if status & 0xff == 0x7f {
        return Err(PyErr::value(format!(
            "process stopped by delivery of signal {}",
            (status >> 8) & 0xff
        )));
    }
    Err(PyErr::value(format!("invalid wait status: {status}")))
}

fn raw_status(termination: os::Termination) -> i32 {
    match termination {
        os::Termination::Exited(code) => i32::from(code) << 8,
        os::Termination::Signaled(number) => number,
        os::Termination::Unknown(raw) => raw,
    }
}

/// `Popen` の状態(returncode を控える)。
struct Monitor {
    returncode: Option<i32>,
}

/// 本物の操作。fd は番号で受け渡し、持ち主はこの値が持つ。
pub struct Real {
    origin: Instant,
    fds: HashMap<RawFd, OwnedFd>,
    saved: Vec<os::Previous>,
    errno: i32,
    monitors: HashMap<i32, Monitor>,
}

impl Default for Real {
    fn default() -> Self {
        Self::new()
    }
}

impl Real {
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
            fds: HashMap::new(),
            saved: Vec::new(),
            errno: 0,
            monitors: HashMap::new(),
        }
    }

    /// 持っている fd を受け取る(番号を返す)。
    fn hold(&mut self, fd: OwnedFd) -> i32 {
        let raw = fd.as_raw_fd();
        self.fds.insert(raw, fd);
        raw
    }

    fn borrow(&self, fd: i32) -> OsResult<std::os::fd::BorrowedFd<'_>> {
        self.fds
            .get(&fd)
            .map(AsFd::as_fd)
            .ok_or_else(|| OsFailure::from_errno(os::errno::EBADF))
    }

    /// `Popen._try_wait` と `_handle_exitstatus`(ECHILD は returncode 0)。
    fn try_wait(&mut self, pid: i32, block: bool) -> Option<i32> {
        if let Some(code) = self
            .monitors
            .get(&pid)
            .and_then(|monitor| monitor.returncode)
        {
            return Some(code);
        }
        let code = match os::retry_eintr(|| os::wait_pid(pid, block)) {
            Ok(os::PidWait::Running) => return None,
            Ok(os::PidWait::Exited(termination)) => exit_code(raw_status(termination)).unwrap_or(0),
            Ok(os::PidWait::NoChild) => 0,
            Err(_) => return None,
        };
        self.monitors.insert(
            pid,
            Monitor {
                returncode: Some(code),
            },
        );
        Some(code)
    }
}

/// `posixpath` の `os.get_exec_path(env)` の PATH(env が None なら、この実行物の環境)。
fn exec_path(env: Option<&[(Vec<u8>, Vec<u8>)]>) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    match env {
        Some(env) => env
            .iter()
            .rev()
            .find(|(key, _)| key == b"PATH")
            .map(|(_, value)| value.clone()),
        None => std::env::var_os("PATH").map(std::ffi::OsString::into_vec),
    }
}

/// platform の起動の失敗を、旧版の `Popen` の例外にする。`program` は exec の失敗のファイル名、
/// `cwd` は exec の前の失敗のファイル名。
pub fn spawn_error(error: os::SpawnError, program: &[u8], cwd: Option<&[u8]>) -> PyErr {
    match error {
        os::SpawnError::IllegalEnvironmentName => PyErr::value("illegal environment variable name"),
        os::SpawnError::EmbeddedNull => PyErr::value("embedded null byte"),
        os::SpawnError::Parent(error) => PyErr::os(&OsFailure::from_io(&error), None),
        os::SpawnError::Child { errno, before_exec } => {
            let filename = if before_exec { cwd } else { Some(program) };
            PyErr::os(
                &OsFailure::from_errno(errno),
                filename.map(Filename::from_raw),
            )
        }
        os::SpawnError::BadChildData(data) => PyErr::new(
            Class::Subprocess("SubprocessError"),
            format!(
                "Bad exception data from child: {}",
                super::r4q_python::value::text(&super::r4q_python::value::bytes_repr(&data))
            ),
        ),
    }
}

impl Ops for Real {
    fn monotonic(&mut self) -> f64 {
        self.origin.elapsed().as_secs_f64() + 1000.0
    }

    fn sleep(&mut self, seconds: f64) -> Result<(), PyErr> {
        if seconds.is_nan() {
            return Err(PyErr::value("Invalid value NaN (not a number)"));
        }
        let nanoseconds = (seconds * 1e9).ceil();
        if !(-9.223_372_036_854_776e18..9.223_372_036_854_776e18).contains(&nanoseconds) {
            return Err(PyErr::new(
                Class::Overflow,
                "timestamp too large to convert to C _PyTime_t",
            ));
        }
        if nanoseconds < 0.0 {
            return Err(PyErr::value("sleep length must be non-negative"));
        }
        std::thread::sleep(Duration::from_nanos(nanoseconds as u64));
        Ok(())
    }

    fn getpid(&mut self) -> i32 {
        os::getpid()
    }

    fn getppid(&mut self) -> i32 {
        os::getppid()
    }

    fn prctl(&mut self, option: i32, arg: i32) -> i32 {
        #[cfg(target_os = "linux")]
        let result = match option {
            36 if arg == 1 => os::set_child_subreaper(),
            1 => os::set_parent_death_signal(arg),
            _ => Err(std::io::Error::from_raw_os_error(os::errno::EINVAL)),
        };
        #[cfg(not(target_os = "linux"))]
        let result: std::io::Result<()> = {
            let _ = (option, arg);
            Err(std::io::Error::from_raw_os_error(os::errno::ENOSYS))
        };
        match result {
            Ok(()) => 0,
            Err(error) => {
                self.errno = error.raw_os_error().unwrap_or(0);
                -1
            }
        }
    }

    fn get_errno(&mut self) -> i32 {
        self.errno
    }

    fn set_handler(&mut self, number: i32, handler: Handler) -> Saved {
        let previous = match handler {
            Handler::Interrupted => os::set_handler(number, os::Handler::RecordLatest),
            Handler::Receive => os::set_handler(number, os::Handler::RecordFirst),
            Handler::Nothing => os::set_handler(number, os::Handler::Nothing),
            Handler::Previous(Saved(index)) => {
                // signal.signal(number, previous): 控えた処理へ戻す(戻り値は使わない)。
                if let Some(saved) = self.saved.get(index) {
                    let _ = os::restore_handler(saved);
                }
                return Saved(usize::MAX);
            }
        };
        match previous {
            Ok(previous) => {
                self.saved.push(previous);
                Saved(self.saved.len() - 1)
            }
            Err(_) => Saved(usize::MAX),
        }
    }

    fn keyboard_interrupt(&mut self) -> bool {
        dev_workflow_platform::check_links::take_interrupt()
    }

    fn latest_signal(&self) -> i32 {
        os::latest_signal()
    }

    fn set_latest_signal(&mut self, number: i32) {
        os::set_latest_signal(number);
    }

    fn first_signal(&self) -> i32 {
        os::first_signal()
    }

    fn clear_first_signal(&mut self) {
        os::clear_first_signal();
    }

    fn spawn(&mut self, argv: &[Vec<u32>]) -> Result<i32, PyErr> {
        let raw: Vec<Vec<u8>> = argv
            .iter()
            .map(|arg| fsencode(arg))
            .collect::<Result<_, _>>()?;
        let candidates = os::exec_candidates(&raw[0], exec_path(None).as_deref());
        let executables: Vec<&[u8]> = candidates.iter().map(Vec::as_slice).collect();
        let args: Vec<&[u8]> = raw.iter().map(Vec::as_slice).collect();
        os::spawn(&os::Spawn {
            argv: &args,
            executables: &executables,
            env: None,
            cwd: None,
            stdin: None,
            stdout: None,
            stderr: None,
            pass_fds: &[],
            new_session: true,
        })
        .map_err(|error| spawn_error(error, &raw[0], None))
    }

    fn waitpid_any(&mut self) -> OsResult<(i32, i32)> {
        match os::retry_eintr(os::wait_any_nohang) {
            Ok(os::Reaped::Child { pid, status }) => Ok((pid, raw_status(status))),
            Ok(os::Reaped::NotYet) => Ok((0, 0)),
            Ok(os::Reaped::NoChildren) => Err(OsFailure::from_errno(os::errno::ECHILD)),
            Err(error) => Err(OsFailure::from_io(&error)),
        }
    }

    fn read_children(&mut self, pid: i32) -> Result<Vec<u32>, PyErr> {
        let raw = os::read_own_children(pid)
            .map_err(|error| proc_error(&error, &os::own_children_path(pid)))?;
        super::r4q_python::codec::decode_utf8_strict(&raw)
            .map_err(|error| PyErr::new(Class::Value("UnicodeDecodeError"), error.message()))
    }

    fn read_stat(&mut self, pid: &str) -> Result<Vec<u8>, PyErr> {
        os::read_proc_stat(pid).map_err(|error| proc_error(&error, &os::proc_stat_path(pid)))
    }

    fn pidfd_open(&mut self, pid: i32) -> OsResult<i32> {
        #[cfg(target_os = "linux")]
        {
            let fd = os::pidfd_open(pid).map_err(|error| OsFailure::from_io(&error))?;
            Ok(self.hold(fd))
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = pid;
            Err(OsFailure::from_errno(os::errno::ENOSYS))
        }
    }

    fn pidfd_send_signal(&mut self, fd: i32, number: i32) -> OsResult<()> {
        #[cfg(target_os = "linux")]
        {
            let fd = self.borrow(fd)?;
            os::pidfd_send_signal(fd, number).map_err(|error| OsFailure::from_io(&error))
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (fd, number);
            Err(OsFailure::from_errno(os::errno::ENOSYS))
        }
    }

    fn close(&mut self, fd: i32) -> OsResult<()> {
        match self.fds.remove(&fd) {
            Some(owned) => os::close(owned).map_err(|error| OsFailure::from_io(&error)),
            None => Err(OsFailure::from_errno(os::errno::EBADF)),
        }
    }

    fn spawn_monitor(&mut self, launch: &MonitorLaunch) -> Result<i32, PyErr> {
        // Popen の `_get_handles`: stdin=DEVNULL を、誤りのパイプより前に開く。起動の後に閉じる。
        let devnull =
            os::open_dev_null().map_err(|error| PyErr::os(&OsFailure::from_io(&error), None))?;
        let candidates =
            os::exec_candidates(&launch.argv[0], exec_path(launch.env.as_deref()).as_deref());
        let executables: Vec<&[u8]> = candidates.iter().map(Vec::as_slice).collect();
        let args: Vec<&[u8]> = launch.argv.iter().map(Vec::as_slice).collect();
        let env: Option<Vec<(&[u8], &[u8])>> = launch.env.as_ref().map(|env| {
            env.iter()
                .map(|(key, value)| (key.as_slice(), value.as_slice()))
                .collect()
        });
        let stdout = self
            .borrow(launch.stdout)
            .map_err(|failure| PyErr::os(&failure, None))?;
        let stderr = self
            .borrow(launch.stderr)
            .map_err(|failure| PyErr::os(&failure, None))?;
        let mut pass = Vec::new();
        for fd in &launch.pass_fds {
            pass.push(
                self.borrow(*fd)
                    .map_err(|failure| PyErr::os(&failure, None))?,
            );
        }
        let result = os::spawn(&os::Spawn {
            argv: &args,
            executables: &executables,
            env: env.as_deref(),
            cwd: launch.cwd.as_deref(),
            stdin: Some(devnull.as_fd()),
            stdout: Some(stdout),
            stderr: Some(stderr),
            pass_fds: &pass,
            new_session: launch.new_session,
        });
        drop(devnull);
        let pid =
            result.map_err(|error| spawn_error(error, &launch.argv[0], launch.cwd.as_deref()))?;
        self.monitors.insert(pid, Monitor { returncode: None });
        Ok(pid)
    }

    fn poll_monitor(&mut self, pid: i32) -> Option<i32> {
        self.try_wait(pid, false)
    }

    fn wait_monitor(&mut self, pid: i32, timeout: Option<f64>) -> Result<i32, WaitFailure> {
        let Some(timeout) = timeout else {
            loop {
                if let Some(code) = self.try_wait(pid, true) {
                    return Ok(code);
                }
            }
        };
        // Popen.wait(timeout): WNOHANG の待ちを、1 ms から 50 ms まで倍にしながら繰り返す。
        let end = self.monotonic() + timeout;
        let mut delay = 0.0005_f64;
        loop {
            if let Some(code) = self.try_wait(pid, false) {
                return Ok(code);
            }
            let remaining = end - self.monotonic();
            if remaining <= 0.0 {
                return Err(WaitFailure::Timeout);
            }
            delay = (delay * 2.0).min(remaining).min(0.05);
            std::thread::sleep(Duration::from_secs_f64(delay));
        }
    }

    fn token_hex(&mut self) -> Result<String, PyErr> {
        let mut buffer = [0u8; 32];
        urandom(&mut buffer)?;
        Ok(buffer.iter().map(|byte| format!("{byte:02x}")).collect())
    }

    fn pipe(&mut self) -> OsResult<(i32, i32)> {
        let (reader, writer) = os::pipe().map_err(|error| OsFailure::from_io(&error))?;
        Ok((self.hold(reader), self.hold(writer)))
    }

    fn select(&mut self, fd: i32, timeout: f64) -> Result<bool, SelectFailure> {
        if !os::selectable(fd) {
            return Err(SelectFailure::OutOfRange);
        }
        let end = Instant::now() + Duration::from_secs_f64(timeout.max(0.0));
        loop {
            let left = end.saturating_duration_since(Instant::now());
            let fd_ref = self.borrow(fd).map_err(SelectFailure::Os)?;
            match os::select_readable(fd_ref, Some(left)) {
                Ok(ready) => return Ok(ready),
                Err(os::SelectError::OutOfRange) => return Err(SelectFailure::OutOfRange),
                Err(os::SelectError::Os(error))
                    if error.raw_os_error() == Some(os::errno::EINTR) =>
                {
                    if Instant::now() >= end {
                        return Ok(false);
                    }
                }
                Err(os::SelectError::Os(error)) => {
                    return Err(SelectFailure::Os(OsFailure::from_io(&error)));
                }
            }
        }
    }

    fn read(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>> {
        let mut buffer = vec![0u8; size];
        let fd_ref = self.borrow(fd)?;
        let count = os::retry_eintr(|| os::read(fd_ref, &mut buffer))
            .map_err(|error| OsFailure::from_io(&error))?;
        buffer.truncate(count);
        Ok(buffer)
    }

    fn killpg(&mut self, pgid: i32, number: i32) -> OsResult<()> {
        match os::killpg(pgid, number) {
            os::GroupSignal::Sent => Ok(()),
            os::GroupSignal::NoProcess => Err(OsFailure::from_errno(os::errno::ESRCH)),
            os::GroupSignal::NotPermitted => Err(OsFailure::from_errno(os::errno::EPERM)),
            os::GroupSignal::Failed(error) => Err(OsFailure::from_io(&error)),
        }
    }

    fn open(&mut self, path: &[u8], flags: OpenFlags) -> OsResult<i32> {
        let fd = os::retry_eintr(|| match flags {
            OpenFlags::Create => os::create_record(path),
            OpenFlags::Read => os::open_record(path),
        })
        .map_err(|error| OsFailure::from_io(&error))?;
        Ok(self.hold(fd))
    }

    fn fstat(&mut self, fd: i32) -> OsResult<FileInfo> {
        let info = os::fstat(self.borrow(fd)?).map_err(|error| OsFailure::from_io(&error))?;
        Ok(FileInfo {
            regular: info.regular,
            size: info.size,
        })
    }

    fn write(&mut self, fd: i32, data: &[u8]) -> OsResult<usize> {
        let fd_ref = self.borrow(fd)?;
        os::retry_eintr(|| os::write(fd_ref, data)).map_err(|error| OsFailure::from_io(&error))
    }

    fn fsync(&mut self, fd: i32) -> OsResult<()> {
        os::fsync(self.borrow(fd)?).map_err(|error| OsFailure::from_io(&error))
    }

    fn platform(&mut self) -> Platform {
        if cfg!(target_os = "linux") {
            Platform::Linux
        } else {
            Platform::Darwin
        }
    }

    fn has_pidfd(&mut self) -> bool {
        cfg!(target_os = "linux")
    }

    fn open_output(&mut self, path: &[u8]) -> OsResult<i32> {
        let fd = os::retry_eintr(|| os::open_output(path))
            .map_err(|error| OsFailure::from_io(&error))?;
        Ok(self.hold(fd))
    }

    fn close_output(&mut self, fd: i32) -> OsResult<()> {
        self.close(fd)
    }

    fn fstat_output(&mut self, fd: i32) -> OsResult<i64> {
        self.fstat(fd).map(|info| info.size)
    }

    fn rewind_output(&mut self, fd: i32) -> OsResult<()> {
        os::seek_start(self.borrow(fd)?)
            .map(|_| ())
            .map_err(|error| OsFailure::from_io(&error))
    }

    fn read_output(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>> {
        // BufferedRandom.read(size): 終わりに着くまで、size バイトになるまで読む。
        let mut result = Vec::with_capacity(size);
        let fd_ref = self.borrow(fd)?;
        let mut chunk = vec![0u8; size.min(1024 * 1024)];
        while result.len() < size {
            let want = (size - result.len()).min(chunk.len());
            let count = os::retry_eintr(|| os::read(fd_ref, &mut chunk[..want]))
                .map_err(|error| OsFailure::from_io(&error))?;
            if count == 0 {
                break;
            }
            result.extend_from_slice(&chunk[..count]);
        }
        Ok(result)
    }
}

/// `os.fsencode(text)`(surrogateescape。ほかの surrogate は `UnicodeEncodeError`)。
pub fn fsencode(codes: &[u32]) -> Result<Vec<u8>, PyErr> {
    super::r4q_python::value::encode_surrogateescape(codes).ok_or_else(|| {
        PyErr::new(
            Class::Value("UnicodeEncodeError"),
            "'utf-8' codec can't encode character: surrogates not allowed",
        )
    })
}
