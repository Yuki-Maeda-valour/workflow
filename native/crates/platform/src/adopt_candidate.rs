//! R4C専用。候補の採用が使う OS の操作を、安全な関数として置く。
//!
//! - 親は `/` から各段を `O_NOFOLLOW|O_DIRECTORY` で開く(リンクを辿らない)。
//! - 候補の末端は `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`。開く前の属性検査は加えない。
//! - 失敗は `Failure`(errno とファイル名)で返す。Python の例外の表示は呼び出し側が組み立てる。
//! - `unsafe` は次の所だけに置く。各所に安全の理由を書く。
//!   - 子の中(`pre_exec`)の `signal`・`sigprocmask`・`fchdir`・`fcntl`。
//!   - 親の `fcntl`(継承 fd の印)・`signal`(SIGXFSZ の無視)・`pthread_self`・検査専用の `BorrowedFd::borrow_raw`・`umask`。
//!   - SIGINT の処理: 入れるときの `sigaction`・`sigemptyset`、受け取ったときの処理の中の
//!     `pthread_self`・`pthread_kill`(別のスレッドで受けたとき、持ち主のスレッドへ送り直す)・`waitpid`・`clock_gettime`・`nanosleep`・`kill`・`write`・`signal`・`raise`、
//!     子の回収の前後で SIGINT を止める・今の mask を読む `pthread_sigmask`・`sigemptyset`・`sigaddset`。
#![deny(unsafe_op_in_unsafe_fn)]

use rustix::fd::{AsFd, AsRawFd, OwnedFd};
use rustix::fs::{self, AtFlags, Mode, OFlags};
use rustix::io::Errno;
use std::io::{self, Read};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// 失敗した OS 操作。`filename`・`filename2` は Python の `OSError` が持つ値に当たる。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub errno: i32,
    pub filename: Option<Vec<u8>>,
    pub filename2: Option<Vec<u8>>,
}

impl Failure {
    fn new(errno: Errno, filename: Option<&[u8]>) -> Self {
        Self {
            errno: errno.raw_os_error(),
            filename: filename.map(<[u8]>::to_vec),
            filename2: None,
        }
    }
}

/// 保持した ディレクトリ。閉じるのは Drop。
#[derive(Debug)]
pub struct Dir(OwnedFd);

impl Dir {
    pub fn as_fd(&self) -> rustix::fd::BorrowedFd<'_> {
        self.0.as_fd()
    }
}

/// `directory()` の失敗。`..` の拒否は、それまでの各段を開いた後に起きる。
#[derive(Debug, PartialEq, Eq)]
pub enum DirError {
    Os(Failure),
    ParentReference,
}

/// `/` から各成分を NOFOLLOW で開く。成分が `.` か `..` なら、そこまで開いた後で拒否する。
pub fn open_chain(components: &[Vec<u8>]) -> Result<Dir, DirError> {
    let mut fd = fs::open(
        "/",
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| DirError::Os(Failure::new(error, Some(b"/"))))?;
    for part in components {
        if part == b"." || part == b".." {
            return Err(DirError::ParentReference);
        }
        fd = fs::openat(
            &fd,
            part.as_slice(),
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| DirError::Os(Failure::new(error, Some(part))))?;
    }
    Ok(Dir(fd))
}

/// 候補の同一性に使う属性。mode は含めない(旧版も比べない)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Identity {
    pub dev: u64,
    pub ino: u64,
    pub size: i64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

#[allow(clippy::unnecessary_cast)]
impl Identity {
    fn from_raw(stat: &fs::Stat) -> Self {
        Self {
            dev: stat.st_dev as u64,
            ino: stat.st_ino as u64,
            size: stat.st_size as i64,
            mtime_ns: stat.st_mtime as i128 * 1_000_000_000 + stat.st_mtime_nsec as i128,
            ctime_ns: stat.st_ctime as i128 * 1_000_000_000 + stat.st_ctime_nsec as i128,
        }
    }
}

/// `read_candidate` の失敗。
#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    /// 開く(filename は名前のみ)・fstat・read の OS の失敗。
    Os(Failure),
    /// 通常ファイルでない、または宣言サイズが上限を超える。
    NotRegularOrLarge,
    /// 読取り前後の属性、または実読長が違う。
    Changed,
}

fn is_regular(stat: &fs::Stat) -> bool {
    fs::FileType::from_raw_mode(stat.st_mode) == fs::FileType::RegularFile
}

/// 旧 `read_candidate`。宣言サイズ+1 まで、各 read は `min(65536, 宣言サイズ+1-既読量)`。
pub fn read_candidate(
    dir: &Dir,
    name: &[u8],
    max_size: i64,
) -> Result<(Vec<u8>, Identity), ReadError> {
    read_candidate_with(dir, name, max_size, || {})
}

/// `read_candidate` と同じ。空でない塊を 1 回読めるたびに `after_chunk` を呼ぶ(検査専用の注入の差し込み口。
/// 製品は何もしない関数を渡す)。
pub fn read_candidate_with(
    dir: &Dir,
    name: &[u8],
    max_size: i64,
    mut after_chunk: impl FnMut(),
) -> Result<(Vec<u8>, Identity), ReadError> {
    let handle = fs::openat(
        dir.as_fd(),
        name,
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|error| ReadError::Os(Failure::new(error, Some(name))))?;
    let before_raw =
        fs::fstat(&handle).map_err(|error| ReadError::Os(Failure::new(error, None)))?;
    let before = Identity::from_raw(&before_raw);
    if !is_regular(&before_raw) || before.size > max_size {
        return Err(ReadError::NotRegularOrLarge);
    }
    let declared = before.size as usize;
    let mut data = Vec::new();
    while data.len() <= declared {
        let want = 65536.min(declared + 1 - data.len());
        let start = data.len();
        data.resize(start + want, 0);
        let count = loop {
            match rustix::io::read(&handle, &mut data[start..]) {
                Err(Errno::INTR) => continue,
                other => break other,
            }
        };
        match count {
            Ok(0) => {
                data.truncate(start);
                break;
            }
            Ok(count) => {
                data.truncate(start + count);
                after_chunk();
            }
            Err(error) => return Err(ReadError::Os(Failure::new(error, None))),
        }
    }
    let after_raw = fs::fstat(&handle).map_err(|error| ReadError::Os(Failure::new(error, None)))?;
    if before != Identity::from_raw(&after_raw) || data.len() != declared {
        return Err(ReadError::Changed);
    }
    Ok((data, before))
}

/// リンクを辿らない stat の要点。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Entry {
    pub regular: bool,
    pub dev: u64,
    pub ino: u64,
}

/// `os.stat(name, dir_fd=…, follow_symlinks=False)`。不在(ENOENT)だけを `None` にする。
pub fn stat_nofollow(dir: &Dir, name: &[u8]) -> Result<Option<Entry>, Failure> {
    match fs::statat(dir.as_fd(), name, AtFlags::SYMLINK_NOFOLLOW) {
        Ok(stat) => Ok(Some(Entry {
            regular: is_regular(&stat),
            #[allow(clippy::unnecessary_cast)]
            dev: stat.st_dev as u64,
            #[allow(clippy::unnecessary_cast)]
            ino: stat.st_ino as u64,
        })),
        Err(Errno::NOENT) => Ok(None),
        Err(error) => Err(Failure::new(error, Some(name))),
    }
}

/// `os.path.lexists`。壊れたリンクも存在とする。調べられない場合は偽。
pub fn lexists(path: &[u8]) -> bool {
    fs::lstat(path).is_ok()
}

/// `renameat2` の結果の読み方。ENOSYS・EINVAL だけ link/unlink へ進む(`Ok(false)`)。他の失敗は `Err`。
pub fn classify_rename(result: Result<(), Errno>) -> Result<bool, Failure> {
    match result {
        Ok(()) => Ok(true),
        Err(Errno::NOSYS | Errno::INVAL) => Ok(false),
        Err(error) => Err(Failure::new(error, None)),
    }
}

/// errno の数から `classify_rename` へ渡す結果を作る(検査専用の注入が使う)。
pub fn classify_rename_errno(errno: i32) -> Result<bool, Failure> {
    classify_rename(Err(Errno::from_raw_os_error(errno)))
}

/// Linux の `renameat2(RENAME_NOREPLACE)`。使えた(改名した)なら `Ok(true)`。
/// 関数が無い(Linux 以外)・ENOSYS・EINVAL のときだけ `Ok(false)`(link/unlink へ進む)。他の失敗は `Err`。
pub fn rename_noreplace(dir: &Dir, source: &[u8], destination: &[u8]) -> Result<bool, Failure> {
    #[cfg(any(target_os = "linux", target_os = "android"))]
    {
        classify_rename(fs::renameat_with(
            dir.as_fd(),
            source,
            dir.as_fd(),
            destination,
            fs::RenameFlags::NOREPLACE,
        ))
    }
    #[cfg(not(any(target_os = "linux", target_os = "android")))]
    {
        let _ = (dir, source, destination);
        Ok(false)
    }
}

/// `link` の失敗。既存の名前は区別する。
#[derive(Debug, PartialEq, Eq)]
pub enum LinkError {
    Exists,
    Os(Failure),
}

/// 元のリンクを辿らず、既存の名前を上書きしない `link`。
pub fn link_noreplace(dir: &Dir, source: &[u8], destination: &[u8]) -> Result<(), LinkError> {
    match fs::linkat(
        dir.as_fd(),
        source,
        dir.as_fd(),
        destination,
        AtFlags::empty(),
    ) {
        Ok(()) => Ok(()),
        Err(Errno::EXIST) => Err(LinkError::Exists),
        Err(error) => Err(LinkError::Os(Failure {
            errno: error.raw_os_error(),
            filename: Some(source.to_vec()),
            filename2: Some(destination.to_vec()),
        })),
    }
}

/// `os.unlink(name, dir_fd=…)`。
pub fn unlink(dir: &Dir, name: &[u8]) -> Result<(), Failure> {
    fs::unlinkat(dir.as_fd(), name, AtFlags::empty())
        .map_err(|error| Failure::new(error, Some(name)))
}

/// 捕捉した子の結果。終了値は Python の `returncode`(信号は負)。
#[derive(Debug, PartialEq, Eq)]
pub struct Captured {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// 子の起動と回収の失敗。
#[derive(Debug)]
pub enum ChildError {
    /// 起動できない。filename は argv[0](Python の `OSError` と同じ)。
    Spawn(Failure),
    /// 期限を過ぎた。直接の子を kill して wait した後。
    Timeout,
    /// 読取り・回収の OS の失敗。
    Io(i32),
}

/// 旧 `subprocess.run` の実行ファイルの探し方: `/` を含む名前はそのまま、含まなければ子の環境の PATH
/// (無ければ `os.defpath` の `/bin:/usr/bin`)の各フォルダ。空の項目は作業場所からの相対(`os.path.join('', 名前)` は
/// 名前だけで、旧版の子は作業場所を保持した root へ移した後に起動を試す)。
/// 相対の候補は、標準ライブラリが PATH を探さないよう `./` を付けて渡す。
fn exec_candidates(program: &[u8], env: &[(Vec<u8>, Vec<u8>)]) -> Vec<Vec<u8>> {
    if program.contains(&b'/') {
        return vec![program.to_vec()];
    }
    let path = env
        .iter()
        .find(|(key, _)| key == b"PATH")
        .map_or(b"/bin:/usr/bin".as_slice(), |(_, value)| value.as_slice());
    path.split(|byte| *byte == b':')
        .map(|dir| {
            if dir.is_empty() {
                [b"./".as_slice(), program].concat()
            } else {
                [dir, b"/", program].concat()
            }
        })
        .collect()
}

/// 起動を試す前の絞り込みの結果(旧版は子の中で候補ごとに `execve` を試し、ENOENT・ENOTDIR は次へ、
/// それ以外の最初の失敗を報告する)。
enum Probe {
    /// 存在しない(ENOENT・ENOTDIR の errno)。fork しない。
    Skip(i32),
    /// 起動できない(errno)。fork しない。
    Fail(i32),
    /// 起動を試す。
    Try,
}

/// 実行ファイルの候補を、fork の前に stat と実行権限で絞る。相対の候補は、保持した作業場所からの相対で調べる。
fn probe_executable(dir: &Dir, candidate: &[u8]) -> Probe {
    let at = if candidate.starts_with(b"/") {
        fs::CWD
    } else {
        dir.as_fd()
    };
    match fs::statat(at, candidate, AtFlags::empty()) {
        Err(error @ (Errno::NOENT | Errno::NOTDIR)) => return Probe::Skip(error.raw_os_error()),
        Err(error) => return Probe::Fail(error.raw_os_error()),
        // ディレクトリ・FIFO など通常ファイルでないものを起動しようとすると、`execve` は EACCES で失敗する。
        Ok(stat) if !is_regular(&stat) => return Probe::Fail(libc::EACCES),
        Ok(_) => {}
    }
    match fs::accessat(at, candidate, fs::Access::EXEC_OK, AtFlags::empty()) {
        Ok(()) => Probe::Try,
        Err(error) => Probe::Fail(error.raw_os_error()),
    }
}

/// 子の中(`exec` の直前)で、旧 `subprocess.run`(`restore_signals=True`・`pass_fds=(root,)`)と同じ状態にする:
/// SIGPIPE・SIGXFSZ を既定へ戻し、信号の mask を起動した側の元の値(`original`)に戻し、root の fd へ作業場所を移し、その fd を継承させる。
/// async-signal-safe な呼び出しだけを使う。fd を子の中で閉じない(標準ライブラリの起動失敗の通知用 pipe を壊すため)。
unsafe fn prepare_child(raw: i32, original: libc::sigset_t) -> io::Result<()> {
    // SAFETY: 呼び出し側(`pre_exec`)は fork 後・exec 前の子の中。以下は引数だけを取るシステムコール。
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
        libc::signal(libc::SIGXFSZ, libc::SIG_DFL);
        // 親は子を起動する間 SIGINT を止める。mask は fork と exec で受け継がれるので、止める前の元の mask に戻す
        // (旧版の子は、起動した側の元の mask を受け継ぐ。起動した側が別の信号を止めていれば、子も止めたまま)。
        libc::sigprocmask(libc::SIG_SETMASK, &original, std::ptr::null_mut());
        if libc::fchdir(raw) != 0 {
            return Err(io::Error::last_os_error());
        }
        libc::fcntl(raw, libc::F_SETFD, 0);
    }
    Ok(())
}

/// 旧 `close_fds=True` に当たる処理を親の側で行う: 3 以上の fd(`keep` 以外)を exec で閉じる印(CLOEXEC)にする。
/// 標準ライブラリが作る fd は元から CLOEXEC なので、継承の対象になるのは、起動した側から受け継いだ fd だけ。
fn close_inherited_fds_on_exec(keep: i32) {
    #[cfg(any(target_os = "linux", target_os = "android"))]
    let fds: Vec<i32> = std::fs::read_dir("/proc/self/fd")
        .map(|entries| {
            entries
                .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse::<i32>().ok())
                .collect()
        })
        .unwrap_or_default();
    #[cfg(not(any(target_os = "linux", target_os = "android")))]
    let fds: Vec<i32> = (3..4096).collect();
    for fd in fds.into_iter().filter(|fd| *fd >= 3 && *fd != keep) {
        // SAFETY: 開いていない fd への `fcntl` は EBADF で失敗するだけ。`F_SETFD` は印を付けるだけで fd を閉じない。
        unsafe {
            libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
        }
    }
}

// ---- SIGINT(旧 Python の KeyboardInterrupt) ----
// 2026-11-11 22:31 の決定: 信号で終わる・最後の行が `KeyboardInterrupt`・直接の子を残さない。
// link から unlink の間(保護区間)だけは、旧版が例外を `BaseException` で受けて巻き戻すので、死なずに印を残す。
// 旧 `Popen.communicate` は KeyboardInterrupt を受けると、子の終了を最大 0.25 秒(`_sigint_wait_secs`)待ってから、
// 終わっていなければ kill する(端末の Ctrl-C は子にも届くので、子は自分で片付けて終われる)。

static CHILD_PID: AtomicI32 = AtomicI32::new(0);
static PROTECTED: AtomicBool = AtomicBool::new(false);
static INTERRUPTED: AtomicBool = AtomicBool::new(false);
/// 保護区間の外で SIGINT を受け、終わる途中(子の回収・kill・呼出履歴の書込み・信号での終了)であることを示す。
/// 処理は、SIGINT を止めていない別のスレッドで走り得る(検査の実行ファイルは複数のスレッドを持つ)。終わる前に、
/// 本流が子の回収の失敗(処理が先に回収した)などで戻って続きを進めないよう、`run_in_dir` はこの印を見て待つ。
static DYING: AtomicBool = AtomicBool::new(false);
/// SIGINT の処理を入れたスレッド(子を起動・回収するスレッド)。別のスレッドが信号を受けたら、このスレッドへ送り直す。
/// このスレッドだけが、子の起動と回収の間 SIGINT を止める(`with_sigint_blocked`)ので、処理は必ずここで走る。
/// 条件: `install_interrupt_handler` を呼ぶスレッドは、`run_in_dir` を呼ぶスレッドと同じで、プロセスの最後まで生きること
/// (製品では、`main` を実行する本流のスレッド。終わったスレッドへ送り直すと、信号が誰にも処理されない)。
/// 単体テスト `interrupt_owner_is_the_installing_thread_and_forwarding_works` が、持ち主が呼んだスレッドであることと、
/// 別のスレッドで受けた SIGINT が送り直されることを固定する。
static OWNER: AtomicU64 = AtomicU64::new(0);

/// 旧 `Popen._sigint_wait_secs`。
const SIGINT_WAIT_NANOS: i64 = 250_000_000;

/// 単調時計の今(ナノ秒)。async-signal-safe。
#[allow(clippy::useless_conversion)] // `timespec` の欄の型は OS・ビット幅で違う。
fn monotonic_nanos() -> i64 {
    let mut now = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `clock_gettime` は async-signal-safe で、書込み先は自分の領域。
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut now);
    }
    i64::from(now.tv_sec) * 1_000_000_000 + i64::from(now.tv_nsec)
}

/// 直接の子が終わるのを最大 0.25 秒待つ。終わっていれば回収して真。async-signal-safe な呼び出しだけを使う。
fn wait_for_child_briefly(pid: i32) -> bool {
    let deadline = monotonic_nanos() + SIGINT_WAIT_NANOS;
    loop {
        let mut status = 0;
        // SAFETY: `waitpid` は async-signal-safe。`status` は自分の領域。
        let reaped = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
        if reaped != 0 {
            return true;
        }
        if monotonic_nanos() >= deadline {
            return false;
        }
        let pause = libc::timespec {
            tv_sec: 0,
            tv_nsec: 2_000_000,
        };
        // SAFETY: `nanosleep` は async-signal-safe。残り時間は要らないので null を渡す。
        unsafe {
            libc::nanosleep(&pause, std::ptr::null_mut());
        }
    }
}

/// KeyboardInterrupt の最後: 呼出履歴の最後の 2 行を標準エラーへ書き、既定の動作に戻して自分へ SIGINT を送る
/// (信号で終わる)。async-signal-safe な呼び出しだけを使う。
fn die_by_keyboard_interrupt() {
    let message = b"Traceback (most recent call last):\nKeyboardInterrupt\n";
    // SAFETY: `write`・`signal`・`raise` は async-signal-safe。
    unsafe {
        libc::write(2, message.as_ptr().cast(), message.len());
        libc::signal(libc::SIGINT, libc::SIG_DFL);
        libc::raise(libc::SIGINT);
    }
}

extern "C" fn on_interrupt(_signal: libc::c_int) {
    let owner = OWNER.load(Ordering::SeqCst);
    // SAFETY: `pthread_self`・`pthread_kill` は async-signal-safe。送り直した信号は、持ち主のスレッドが止めていれば、
    // 解除されるまで保留される。
    unsafe {
        if owner != 0 && libc::pthread_self() as u64 != owner {
            libc::pthread_kill(owner as libc::pthread_t, libc::SIGINT);
            return;
        }
    }
    if PROTECTED.load(Ordering::SeqCst) {
        INTERRUPTED.store(true, Ordering::SeqCst);
        return;
    }
    DYING.store(true, Ordering::SeqCst);
    let pid = CHILD_PID.load(Ordering::SeqCst);
    if pid > 0 && !wait_for_child_briefly(pid) {
        // SAFETY: `kill`・`waitpid` は async-signal-safe。子の回収と `CHILD_PID` の消去は SIGINT を止めて行うので、ここへ来た pid は回収されていない。
        unsafe {
            libc::kill(pid, libc::SIGKILL);
            let mut status = 0;
            libc::waitpid(pid, &mut status, 0);
        }
    }
    die_by_keyboard_interrupt();
}

/// SIGINT の処理を入れる(最初に 1 回)。起動したときに SIGINT が既定の動作でなければ(無視して起動された)、
/// 旧 Python と同じく何も入れない(無視のまま。子の Git も無視を受け継ぐ)。入れたときだけ真。
pub fn install_interrupt_handler() -> bool {
    // SAFETY: `sigaction` に、現在の動作の読出し(新しい動作は null)と、async-signal-safe な処理だけを行う関数・
    // 空の mask の設定を渡す。`sigemptyset` は自分の領域を初期化するだけ。
    unsafe {
        let mut current: libc::sigaction = std::mem::zeroed();
        if libc::sigaction(libc::SIGINT, std::ptr::null(), &mut current) != 0
            || current.sa_sigaction != libc::SIG_DFL
        {
            return false;
        }
        OWNER.store(libc::pthread_self() as u64, Ordering::SeqCst);
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = on_interrupt as extern "C" fn(libc::c_int) as usize;
        libc::sigemptyset(&mut action.sa_mask);
        libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut()) == 0
    }
}

/// 保護区間を抜けた後に、区間の中で SIGINT が届いていたときの終わり方(旧版の、`try` の外へ出た
/// KeyboardInterrupt に当たる): 子はいない。呼出履歴の最後の行を書き、信号で終わる。
pub fn interrupt_exit() {
    die_by_keyboard_interrupt();
}

/// 呼んだスレッドの今の信号の mask。
fn current_mask() -> libc::sigset_t {
    // SAFETY: 新しい集合を null にした `pthread_sigmask` は、今の mask を書き出すだけ(変えない)。
    unsafe {
        let mut current: libc::sigset_t = std::mem::zeroed();
        libc::pthread_sigmask(libc::SIG_BLOCK, std::ptr::null(), &mut current);
        current
    }
}

/// SIGINT を止めたまま `body` を行い、戻ってから元に戻す(止めている間に届いたものは、戻した後に処理される)。
/// 子を回収するとき、回収と `CHILD_PID` の消去の間に処理が走り、回収済みの pid へ信号を送るのを防ぐ。
fn with_sigint_blocked<T>(body: impl FnOnce() -> T) -> T {
    // SAFETY: `sigemptyset`・`sigaddset` は自分の領域を設定するだけ。`pthread_sigmask` は呼んだスレッドの mask を変える。
    let previous = unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGINT);
        let mut previous: libc::sigset_t = std::mem::zeroed();
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut previous);
        previous
    };
    let result = body();
    // SAFETY: 上で保存した mask へ戻す。
    unsafe {
        libc::pthread_sigmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut());
    }
    result
}

/// 保護区間の出入り。入るときは印を消す。出るときは、区間の中で SIGINT が届いたかを返す。
pub fn interrupt_protection(enter: bool) -> bool {
    if enter {
        INTERRUPTED.store(false, Ordering::SeqCst);
        PROTECTED.store(true, Ordering::SeqCst);
        false
    } else {
        PROTECTED.store(false, Ordering::SeqCst);
        INTERRUPTED.swap(false, Ordering::SeqCst)
    }
}

/// 保護区間の中で SIGINT が届いたか(印は消さない)。
pub fn interrupted() -> bool {
    INTERRUPTED.load(Ordering::SeqCst)
}

/// 保持した ディレクトリの fd から子の作業場所を決め、stdin を継承し、stdout・stderr だけ捕捉する。
/// 環境は `env` だけ(親の環境は渡さない)。期限を過ぎたら直接の子だけを kill して wait する。
pub fn run_in_dir(
    dir: &Dir,
    argv: &[Vec<u8>],
    env: &[(Vec<u8>, Vec<u8>)],
    timeout: Duration,
) -> Result<Captured, ChildError> {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    let raw = dir.0.as_raw_fd();
    close_inherited_fds_on_exec(raw);
    let original = current_mask();
    let mut first_failure: Option<io::Error> = None;
    let mut last_failure: Option<io::Error> = None;
    let mut spawned = None;
    for candidate in exec_candidates(&argv[0], env) {
        match probe_executable(dir, &candidate) {
            Probe::Skip(code) => {
                last_failure = Some(io::Error::from_raw_os_error(code));
                continue;
            }
            Probe::Fail(code) => {
                if first_failure.is_none() {
                    first_failure = Some(io::Error::from_raw_os_error(code));
                }
                continue;
            }
            Probe::Try => {}
        }
        let mut command = Command::new(OsStr::from_bytes(&candidate));
        command
            .arg0(OsStr::from_bytes(&argv[0]))
            .args(argv[1..].iter().map(|arg| OsStr::from_bytes(arg)))
            .env_clear()
            .envs(
                env.iter()
                    .map(|(key, value)| (OsStr::from_bytes(key), OsStr::from_bytes(value))),
            )
            .stdin(Stdio::inherit())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // SAFETY: 子の中で `prepare_child` の async-signal-safe な呼び出しだけを行う。fd は親が保持している。
        unsafe {
            command.pre_exec(move || prepare_child(raw, original));
        }
        // 起動と `CHILD_PID` の記録の間に SIGINT が届くと、処理が子を知らないまま終わり、子が残る。
        // SIGINT を止めて起動し、記録してから戻す(届いていた分は、戻した後に処理され、子を片付ける)。
        // 子の mask は、子の中(`prepare_child`)で止める前の元の値へ戻す。
        let started = with_sigint_blocked(|| {
            let started = command.spawn();
            if let Ok(child) = &started {
                CHILD_PID.store(child.id() as i32, Ordering::SeqCst);
            }
            started
        });
        match started {
            Ok(child) => {
                spawned = Some(child);
                break;
            }
            Err(error) => {
                // 旧版と同じく、ENOENT・ENOTDIR は次の候補へ。最初のそれ以外の失敗を報告する。
                let code = error.raw_os_error();
                if code != Some(libc::ENOENT)
                    && code != Some(libc::ENOTDIR)
                    && first_failure.is_none()
                {
                    first_failure = Some(error);
                } else {
                    last_failure = Some(error);
                }
            }
        }
    }
    let Some(mut child) = spawned else {
        let error = first_failure
            .or(last_failure)
            .unwrap_or_else(|| io::Error::from_raw_os_error(libc::ENOENT));
        return Err(ChildError::Spawn(Failure {
            errno: error.raw_os_error().unwrap_or(0),
            filename: Some(argv[0].clone()),
            filename2: None,
        }));
    };
    let result = collect(&mut child, timeout);
    park_while_dying();
    if result.is_err() {
        let _ = child.kill();
        with_sigint_blocked(|| {
            let _ = child.wait();
            CHILD_PID.store(0, Ordering::SeqCst);
        });
    }
    CHILD_PID.store(0, Ordering::SeqCst);
    result
}

/// SIGINT の処理が終わる途中なら、プロセスが信号で終わるまで待つ(戻って続きを進めない)。
fn park_while_dying() {
    while DYING.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// 子を回収できたら、同じ SIGINT を止めた区間の中で `CHILD_PID` を 0 にする(回収済みの pid へ信号を送らないため)。
fn reap_if_exited(child: &mut std::process::Child) -> io::Result<Option<std::process::ExitStatus>> {
    with_sigint_blocked(|| {
        let status = child.try_wait();
        if matches!(status, Ok(Some(_))) {
            CHILD_PID.store(0, Ordering::SeqCst);
        }
        status
    })
}

fn collect(child: &mut std::process::Child, timeout: Duration) -> Result<Captured, ChildError> {
    use rustix::event::{PollFd, PollFlags, Timespec, poll};
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    for stream in [
        stdout.as_ref().map(AsFd::as_fd),
        stderr.as_ref().map(AsFd::as_fd),
    ]
    .into_iter()
    .flatten()
    {
        let flags =
            rustix::fs::fcntl_getfl(stream).map_err(|e| ChildError::Io(e.raw_os_error()))?;
        rustix::fs::fcntl_setfl(stream, flags | OFlags::NONBLOCK)
            .map_err(|e| ChildError::Io(e.raw_os_error()))?;
    }
    let started = Instant::now();
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let mut status = None;
    loop {
        if started.elapsed() >= timeout {
            return Err(ChildError::Timeout);
        }
        if status.is_none() {
            status =
                reap_if_exited(child).map_err(|e| ChildError::Io(e.raw_os_error().unwrap_or(0)))?;
        }
        if stdout.is_none() && stderr.is_none() && status.is_some() {
            break;
        }
        let wait = timeout
            .saturating_sub(started.elapsed())
            .min(Duration::from_millis(50));
        let mut fds = Vec::new();
        if let Some(stream) = stdout.as_ref() {
            fds.push(PollFd::new(stream, PollFlags::IN));
        }
        if let Some(stream) = stderr.as_ref() {
            fds.push(PollFd::new(stream, PollFlags::IN));
        }
        if fds.is_empty() {
            std::thread::sleep(wait);
        } else {
            match poll(&mut fds, Some(&Timespec::try_from(wait).unwrap())) {
                Ok(_) | Err(Errno::INTR) => {}
                Err(error) => return Err(ChildError::Io(error.raw_os_error())),
            }
        }
        drop(fds);
        drain(&mut stdout, &mut out)?;
        drain(&mut stderr, &mut err)?;
    }
    let status = status.unwrap();
    Ok(Captured {
        code: status
            .code()
            .unwrap_or_else(|| -status.signal().unwrap_or(1)),
        stdout: out,
        stderr: err,
    })
}

fn drain<R: Read>(pipe: &mut Option<R>, output: &mut Vec<u8>) -> Result<(), ChildError> {
    let mut buffer = [0; 65536];
    if let Some(stream) = pipe {
        match stream.read(&mut buffer) {
            Ok(0) => {
                pipe.take();
            }
            Ok(count) => output.extend_from_slice(&buffer[..count]),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) => {}
            Err(error) => return Err(ChildError::Io(error.raw_os_error().unwrap_or(0))),
        }
    }
    Ok(())
}

/// 標準の fd(1 か 2)へ 1 回書く。書けたバイト数か errno。EINTR は再試行する(Python の `os.write` と同じ)。
pub fn write_standard(fd: i32, bytes: &[u8]) -> Result<usize, i32> {
    loop {
        let result = match fd {
            1 => rustix::io::write(std::io::stdout(), bytes),
            _ => rustix::io::write(std::io::stderr(), bytes),
        };
        match result {
            Err(Errno::INTR) => continue,
            Ok(count) => return Ok(count),
            Err(error) => return Err(error.raw_os_error()),
        }
    }
}

/// 標準の fd(0〜2)がディレクトリか。調べられなければ偽。
pub fn standard_is_directory(fd: i32) -> bool {
    let stat = match fd {
        0 => fs::fstat(std::io::stdin()),
        1 => fs::fstat(std::io::stdout()),
        _ => fs::fstat(std::io::stderr()),
    };
    stat.is_ok_and(|stat| fs::FileType::from_raw_mode(stat.st_mode) == fs::FileType::Directory)
}

/// `SIGXFSZ` を無視する(Python は起動時に無視する)。ファイルの大きさの上限を超える書込みは、
/// 信号で終わらず `EFBIG` で失敗する。
pub fn ignore_file_size_signal() {
    // SAFETY: シグナルの処理を「無視」にするだけで、ハンドラの関数は渡さない。
    unsafe {
        libc::signal(libc::SIGXFSZ, libc::SIG_IGN);
    }
}

/// 標準の fd(0〜2)が端末か(旧 Python の `isatty`。標準出力が端末なら `line_buffering=True` になる)。
pub fn standard_is_terminal(fd: i32) -> bool {
    match fd {
        0 => rustix::termios::isatty(std::io::stdin()),
        1 => rustix::termios::isatty(std::io::stdout()),
        _ => rustix::termios::isatty(std::io::stderr()),
    }
}

/// 標準の fd の `st_blksize`(旧 Python の `BufferedWriter` の大きさになる)。調べられなければ 0。
pub fn standard_blksize(fd: i32) -> i64 {
    let stat = match fd {
        0 => fs::fstat(std::io::stdin()),
        1 => fs::fstat(std::io::stdout()),
        _ => fs::fstat(std::io::stderr()),
    };
    #[allow(clippy::unnecessary_cast)]
    stat.map_or(0, |stat| stat.st_blksize as i64)
}

/// 検査専用: 通知用の fd へ 1 回書き、続行用の fd が閉じられるか失敗するまで読み続ける
/// (呼び出し側が SIGKILL で止めるまでの窓を、sleep に頼らず作る)。
pub fn fault_injection_notify_and_wait(notify_fd: i32, wait_fd: i32) {
    use rustix::fd::BorrowedFd;
    // SAFETY: 検査の制御用に親から渡された fd 番号。この関数の間だけ借りる。
    let (notify, wait) = unsafe {
        (
            BorrowedFd::borrow_raw(notify_fd),
            BorrowedFd::borrow_raw(wait_fd),
        )
    };
    let _ = rustix::io::write(notify, b"linked");
    let mut buffer = [0u8; 16];
    while matches!(rustix::io::read(wait, &mut buffer), Ok(count) if count > 0) {}
}

/// 検査専用: プロセスの umask を設定し、前の値を返す(単体テストが、作る実体の権限を機械に依らず決めるため)。
pub fn set_umask(mask: u32) -> u32 {
    // SAFETY: `umask` は引数 1 つを取り、前の値を返すだけで、メモリを触らない。
    unsafe { libc::umask(mask as libc::mode_t) as u32 }
}

/// 呼び出したスレッドの識別値(Python の `Current thread 0x…` と同じ種類の値)。
pub fn current_thread_id() -> u64 {
    // SAFETY: `pthread_self` は引数を取らず、呼び出したスレッドの識別値を返すだけ。
    unsafe { libc::pthread_self() as u64 }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::path::{Path, PathBuf};

    struct Scratch(PathBuf);
    impl Scratch {
        /// 先に置かれた symlink を辿らないよう、`create_dir`(既存なら失敗)と乱数の名前で作り、実体のパスにする。
        fn new(label: &str) -> Self {
            let base = std::env::temp_dir().canonicalize().unwrap();
            for attempt in 0..100u32 {
                let nanos = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .subsec_nanos();
                let path = base.join(format!(
                    "r4c-platform-{label}-{}-{nanos:x}-{attempt}",
                    std::process::id()
                ));
                if std::fs::create_dir(&path).is_ok() {
                    return Self(path.canonicalize().unwrap());
                }
            }
            panic!("一時フォルダを作れない");
        }
        fn dir(&self) -> Dir {
            open_chain(&components(&self.0)).unwrap()
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn components(path: &Path) -> Vec<Vec<u8>> {
        use std::os::unix::ffi::OsStrExt;
        path.components()
            .filter_map(|part| match part {
                std::path::Component::Normal(name) => Some(name.as_bytes().to_vec()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn chain_refuses_links_and_parent_references_after_opening_earlier_parts() {
        let scratch = Scratch::new("chain");
        std::fs::create_dir(scratch.0.join("real")).unwrap();
        symlink("real", scratch.0.join("link")).unwrap();
        let mut path = components(&scratch.0);
        path.push(b"link".to_vec());
        assert!(
            matches!(open_chain(&path), Err(DirError::Os(Failure { errno, .. })) if errno == libc::ELOOP || errno == libc::ENOTDIR)
        );
        let mut path = components(&scratch.0);
        path.extend([b"real".to_vec(), b"..".to_vec()]);
        assert_eq!(open_chain(&path).unwrap_err(), DirError::ParentReference);
        let mut path = components(&scratch.0);
        path.extend([b"absent".to_vec(), b"..".to_vec()]);
        assert!(
            matches!(open_chain(&path), Err(DirError::Os(Failure { errno, .. })) if errno == libc::ENOENT)
        );
    }

    #[test]
    fn read_checks_type_size_and_returns_the_identity() {
        let scratch = Scratch::new("read");
        let dir = scratch.dir();
        std::fs::write(scratch.0.join("a"), b"body").unwrap();
        let (data, identity) = read_candidate(&dir, b"a", 8 << 20).unwrap();
        assert_eq!((data.as_slice(), identity.size), (&b"body"[..], 4));
        assert_eq!(
            read_candidate(&dir, b"a", 3),
            Err(ReadError::NotRegularOrLarge)
        );
        std::fs::create_dir(scratch.0.join("d")).unwrap();
        assert_eq!(
            read_candidate(&dir, b"d", 8 << 20),
            Err(ReadError::NotRegularOrLarge)
        );
        symlink("a", scratch.0.join("l")).unwrap();
        assert!(
            matches!(read_candidate(&dir, b"l", 8 << 20), Err(ReadError::Os(Failure { errno, .. })) if errno == libc::ELOOP)
        );
        assert!(
            matches!(read_candidate(&dir, b"absent", 8 << 20), Err(ReadError::Os(Failure { errno, .. })) if errno == libc::ENOENT)
        );
    }

    #[test]
    fn read_accepts_the_8mib_boundary_and_rejects_one_more_byte() {
        let scratch = Scratch::new("limit");
        let dir = scratch.dir();
        std::fs::write(scratch.0.join("edge"), vec![b'x'; 8 << 20]).unwrap();
        assert_eq!(
            read_candidate(&dir, b"edge", 8 << 20).unwrap().0.len(),
            8 << 20
        );
        std::fs::write(scratch.0.join("over"), vec![b'x'; (8 << 20) + 1]).unwrap();
        assert_eq!(
            read_candidate(&dir, b"over", 8 << 20),
            Err(ReadError::NotRegularOrLarge)
        );
        std::fs::write(scratch.0.join("empty"), b"").unwrap();
        assert_eq!(read_candidate(&dir, b"empty", 8 << 20).unwrap().0.len(), 0);
    }

    #[test]
    fn stat_distinguishes_absence_from_other_failures_without_following_links() {
        let scratch = Scratch::new("stat");
        let dir = scratch.dir();
        assert_eq!(stat_nofollow(&dir, b"absent"), Ok(None));
        symlink("nowhere", scratch.0.join("dangling")).unwrap();
        let entry = stat_nofollow(&dir, b"dangling").unwrap().unwrap();
        assert!(!entry.regular);
        std::fs::write(scratch.0.join("f"), b"x").unwrap();
        assert!(stat_nofollow(&dir, b"f").unwrap().unwrap().regular);
        let failure = stat_nofollow(&dir, b"f/child").unwrap_err();
        assert_eq!(failure.errno, libc::ENOTDIR);
        assert_eq!(failure.filename.as_deref(), Some(&b"f/child"[..]));
    }

    #[test]
    fn rename_never_replaces_and_link_reports_existing_names() {
        let scratch = Scratch::new("rename");
        let dir = scratch.dir();
        std::fs::write(scratch.0.join("src"), b"a").unwrap();
        std::fs::write(scratch.0.join("taken"), b"b").unwrap();
        let blocked = rename_noreplace(&dir, b"src", b"taken");
        #[cfg(target_os = "linux")]
        assert_eq!(blocked.unwrap_err().errno, libc::EEXIST);
        #[cfg(not(target_os = "linux"))]
        assert_eq!(blocked, Ok(false));
        assert_eq!(std::fs::read(scratch.0.join("taken")).unwrap(), b"b");
        assert_eq!(
            link_noreplace(&dir, b"src", b"taken"),
            Err(LinkError::Exists)
        );
        assert_eq!(link_noreplace(&dir, b"src", b"both"), Ok(()));
        assert_eq!(unlink(&dir, b"src"), Ok(()));
        assert_eq!(std::fs::read(scratch.0.join("both")).unwrap(), b"a");
        let missing = unlink(&dir, b"src").unwrap_err();
        assert_eq!(
            (missing.errno, missing.filename.as_deref()),
            (libc::ENOENT, Some(&b"src"[..]))
        );
        #[cfg(target_os = "linux")]
        {
            assert_eq!(rename_noreplace(&dir, b"both", b"moved"), Ok(true));
            assert!(scratch.0.join("moved").exists() && !scratch.0.join("both").exists());
        }
    }

    #[test]
    fn rename_results_fall_back_only_for_enosys_and_einval() {
        assert_eq!(classify_rename(Ok(())), Ok(true));
        for errno in [libc::ENOSYS, libc::EINVAL] {
            assert_eq!(classify_rename_errno(errno), Ok(false));
        }
        for errno in [
            libc::EEXIST,
            libc::EPERM,
            libc::EACCES,
            libc::ENOENT,
            libc::EXDEV,
            libc::EIO,
        ] {
            assert_eq!(classify_rename_errno(errno).unwrap_err().errno, errno);
        }
    }

    #[test]
    fn read_hook_runs_after_each_non_empty_chunk() {
        let scratch = Scratch::new("hook");
        let dir = scratch.dir();
        std::fs::write(scratch.0.join("big"), vec![b'x'; 70000]).unwrap();
        let mut calls = 0;
        read_candidate_with(&dir, b"big", 8 << 20, || calls += 1).unwrap();
        assert_eq!(calls, 2, "65536 と残りの 2 回(宣言サイズ+1 まで読む)");
        std::fs::write(scratch.0.join("empty"), b"").unwrap();
        let mut calls = 0;
        read_candidate_with(&dir, b"empty", 8 << 20, || calls += 1).unwrap();
        assert_eq!(calls, 0);
    }

    #[test]
    fn child_runs_in_the_held_directory_with_only_the_given_environment() {
        let scratch = Scratch::new("child");
        let dir = scratch.dir();
        std::fs::write(scratch.0.join("marker"), b"").unwrap();
        let argv = [
            b"/bin/sh".to_vec(),
            b"-c".to_vec(),
            b"ls marker; echo \"$ONLY\"; echo \"${HOME-unset}\" >&2".to_vec(),
        ];
        let env = [(b"ONLY".to_vec(), b"yes".to_vec())];
        let captured = run_in_dir(&dir, &argv, &env, Duration::from_secs(10)).unwrap();
        assert_eq!(captured.code, 0);
        assert_eq!(captured.stdout, b"marker\nyes\n");
        assert_eq!(captured.stderr, b"unset\n");
        let missing = run_in_dir(
            &dir,
            &[b"definitely-not-a-command".to_vec()],
            &[],
            Duration::from_secs(10),
        );
        assert!(
            matches!(missing, Err(ChildError::Spawn(Failure { errno, ref filename, .. })) if errno == libc::ENOENT && filename.as_deref() == Some(&b"definitely-not-a-command"[..]))
        );
        let signaled = run_in_dir(
            &dir,
            &[
                b"/bin/sh".to_vec(),
                b"-c".to_vec(),
                b"kill -TERM $$".to_vec(),
            ],
            &[],
            Duration::from_secs(10),
        )
        .unwrap();
        assert_eq!(signaled.code, -libc::SIGTERM);
    }

    /// 子の信号の状態(旧 `subprocess.run` の `restore_signals=True`): SIGPIPE・SIGXFSZ は既定へ戻る。
    /// 起動した側(Rust の標準の入口は SIGPIPE を無視し、この検査は SIGXFSZ も無視する)の設定を受け継がない。
    /// OS ごとの `/proc` に頼らず、子が自分へ信号を送って、既定の動作(信号で終わる)になっているかで確かめる。
    #[test]
    fn child_state_restores_default_signals_like_the_old_subprocess_run() {
        let scratch = Scratch::new("state");
        let dir = scratch.dir();
        ignore_file_size_signal();
        // INT: 親が起動の間 SIGINT を止めても、子は止めた状態を受け継がない(止まっていれば、信号で終わらず `survived` を出す)。
        for (name, number) in [
            ("PIPE", libc::SIGPIPE),
            ("XFSZ", libc::SIGXFSZ),
            ("INT", libc::SIGINT),
        ] {
            let script = format!("ulimit -c 0; kill -{name} $$; echo survived");
            let argv = [b"/bin/sh".to_vec(), b"-c".to_vec(), script.into_bytes()];
            let captured = run_in_dir(
                &dir,
                &argv,
                &[(b"PATH".to_vec(), b"/bin:/usr/bin".to_vec())],
                Duration::from_secs(10),
            )
            .unwrap();
            assert_eq!(captured.code, -number, "{name}");
            assert_eq!(captured.stdout, b"", "{name}");
        }
    }

    /// 起動した側が止めていた信号は、子も止めたまま受け継ぐ(旧版の子は、起動した側の元の mask を受け継ぐ)。
    /// 内部で SIGINT を止めて起動しても、子の SIGINT の mask は、元の値(止めていない)に戻る。
    #[test]
    fn child_inherits_the_callers_original_signal_mask() {
        let scratch = Scratch::new("mask");
        let dir = scratch.dir();
        let run = |signal: &str| {
            let script = format!("ulimit -c 0; kill -{signal} $$; echo survived");
            run_in_dir(
                &dir,
                &[b"/bin/sh".to_vec(), b"-c".to_vec(), script.into_bytes()],
                &[(b"PATH".to_vec(), b"/bin:/usr/bin".to_vec())],
                Duration::from_secs(10),
            )
            .unwrap()
        };
        // SAFETY: このスレッドの mask に USR2 を足して、最後に戻す。
        let previous = unsafe {
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigaddset(&mut set, libc::SIGUSR2);
            let mut previous: libc::sigset_t = std::mem::zeroed();
            libc::pthread_sigmask(libc::SIG_BLOCK, &set, &mut previous);
            previous
        };
        let blocked = run("USR2");
        let unblocked = run("INT");
        // SAFETY: 保存した mask へ戻す。
        unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, &previous, std::ptr::null_mut());
        }
        assert_eq!(
            (blocked.code, blocked.stdout.as_slice()),
            (0, &b"survived\n"[..])
        );
        assert_eq!(unblocked.code, -libc::SIGINT);
    }

    /// Linux だけ: `/proc` で、子の fd(0〜2 と root のみ)と無視された信号の集合を直接読む。
    #[cfg(target_os = "linux")]
    #[test]
    fn child_fds_and_ignored_signals_match_the_old_subprocess_run_on_linux() {
        let scratch = Scratch::new("procstate");
        let dir = scratch.dir();
        ignore_file_size_signal();
        let script = b"ls /proc/self/fd | wc -l; grep SigIgn /proc/self/status";
        let argv = [b"/bin/sh".to_vec(), b"-c".to_vec(), script.to_vec()];
        let captured = run_in_dir(
            &dir,
            &argv,
            &[(b"PATH".to_vec(), b"/bin:/usr/bin".to_vec())],
            Duration::from_secs(10),
        )
        .unwrap();
        let text = String::from_utf8(captured.stdout).unwrap();
        let mut lines = text.lines();
        // /proc/self/fd の一覧を作る `ls` 自身の fd(開いたディレクトリ 1 本)と 0〜2、root の fd。
        assert_eq!(lines.next().unwrap().trim(), "5");
        let mask =
            u64::from_str_radix(lines.next().unwrap().split_whitespace().last().unwrap(), 16)
                .unwrap();
        for signal in [libc::SIGPIPE, libc::SIGXFSZ] {
            assert_eq!(mask & (1 << (signal - 1)), 0, "signal {signal}");
        }
    }

    #[test]
    fn executable_search_follows_the_old_rules() {
        let env = |value: Option<&str>| {
            value
                .map(|v| vec![(b"PATH".to_vec(), v.as_bytes().to_vec())])
                .unwrap_or_default()
        };
        assert_eq!(
            exec_candidates(b"git", &env(None)),
            [b"/bin/git".to_vec(), b"/usr/bin/git".to_vec()]
        );
        assert_eq!(
            exec_candidates(b"git", &env(Some("/a::/b"))),
            [b"/a/git".to_vec(), b"./git".to_vec(), b"/b/git".to_vec()]
        );
        assert_eq!(
            exec_candidates(b"./git", &env(Some("/a"))),
            [b"./git".to_vec()]
        );
        let scratch = Scratch::new("search");
        let dir = scratch.dir();
        // PATH が無いときは /bin:/usr/bin を探す(musl の既定の探索に頼らない)。
        let captured = run_in_dir(
            &dir,
            &[b"sh".to_vec(), b"-c".to_vec(), b"exit 3".to_vec()],
            &[],
            Duration::from_secs(10),
        )
        .unwrap();
        assert_eq!(captured.code, 3);
        // 空の PATH の項目は、保持した作業場所からの相対で探す(標準ライブラリの PATH 探索には任せない)。
        {
            use std::os::unix::fs::PermissionsExt;
            let tool = scratch.0.join("relative-tool");
            std::fs::write(&tool, b"#!/bin/sh\necho relative\n").unwrap();
            std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o755)).unwrap();
            let env = [(
                b"PATH".to_vec(),
                b"/nonexistent-r4c-a:/nonexistent-r4c-b/x:".to_vec(),
            )];
            // 書いた直後の実行は、同時に fork する別の検査が書込み用の fd を持っていると ETXTBSY(26)になる。少し待って繰り返す。
            let mut attempts = 0;
            let captured = loop {
                match run_in_dir(
                    &dir,
                    &[b"relative-tool".to_vec()],
                    &env,
                    Duration::from_secs(10),
                ) {
                    Err(ChildError::Spawn(Failure { errno, .. }))
                        if errno == libc::ETXTBSY && attempts < 100 =>
                    {
                        attempts += 1;
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    other => break other.unwrap(),
                }
            };
            assert_eq!(captured.stdout, b"relative\n");
            // どの候補にも無ければ ENOENT。
            let missing = run_in_dir(
                &dir,
                &[b"no-such-tool".to_vec()],
                &env,
                Duration::from_secs(10),
            );
            assert!(
                matches!(missing, Err(ChildError::Spawn(Failure { errno, .. })) if errno == libc::ENOENT)
            );
            // 通常ファイルでない候補(ディレクトリ)は EACCES で、後ろの候補を試し続ける。
            std::fs::create_dir(scratch.0.join("dir-tool")).unwrap();
            std::fs::create_dir(scratch.0.join("dirs")).unwrap();
            std::fs::create_dir(scratch.0.join("dirs/dir-tool")).unwrap();
            let env = [(b"PATH".to_vec(), b"dirs:.".to_vec())];
            assert!(matches!(
                run_in_dir(&dir, &[b"dir-tool".to_vec()], &env, Duration::from_secs(10)),
                Err(ChildError::Spawn(Failure { errno, .. })) if errno == libc::EACCES
            ));
        }
        // 先に見つかった実行ファイルでない(権限なし)失敗を報告する。
        let bin = scratch.0.join("bin");
        std::fs::create_dir(&bin).unwrap();
        std::fs::write(bin.join("tool"), b"#!/bin/sh\n").unwrap();
        let env = [(
            b"PATH".to_vec(),
            format!("{}:/bin", bin.display()).into_bytes(),
        )];
        let failure =
            run_in_dir(&dir, &[b"tool".to_vec()], &env, Duration::from_secs(10)).unwrap_err();
        assert!(
            matches!(failure, ChildError::Spawn(Failure { errno, .. }) if errno == libc::EACCES)
        );
    }

    /// SIGINT の処理は、プロセスごと終わる形で試すので、検査の実行ファイル自身を子として起動する。
    /// 環境変数 `R4C_INTERRUPT_CHILD` の値で動きを選ぶ(`run`・`trap-fast`・`trap-slow`・`ignored`)。
    #[test]
    fn interrupt_child_mode() {
        let Ok(mode) = std::env::var("R4C_INTERRUPT_CHILD") else {
            return;
        };
        if mode == "ignored" {
            // 起動したときに SIGINT が無視されていた(`nohup`・非対話のバックグラウンドなど)。
            // SAFETY: シグナルの処理を「無視」にするだけ。
            unsafe {
                libc::signal(libc::SIGINT, libc::SIG_IGN);
            }
            assert!(!install_interrupt_handler(), "無視して起動したら入れない");
        } else {
            assert!(install_interrupt_handler());
            // 持ち主は、これから子を起動・回収する(この)スレッド。
            // SAFETY: `pthread_self` は引数を取らない。
            assert_eq!(
                OWNER.load(Ordering::SeqCst),
                unsafe { libc::pthread_self() } as u64
            );
        }
        // 作業場所は親の検査が作って片付ける(この子は SIGINT で終わり、後始末を走らせない)。
        let notify = std::env::var("R4C_NOTIFY").unwrap();
        let dir = open_chain(&components(Path::new(&notify).parent().unwrap())).unwrap();
        if mode == "forward" {
            // 別のスレッドが SIGINT を受ける(そのスレッドで処理が走る)形を、決めて作る: 子の pid が書かれたら、
            // 補助のスレッドが自分へ SIGINT を送る(`raise` は呼んだスレッドへ届く)。持ち主へ送り直されて、同じ終わり方になる。
            let pid_file = format!("{notify}.pid");
            std::thread::spawn(move || {
                while std::fs::read_to_string(&pid_file)
                    .unwrap_or_default()
                    .trim()
                    .is_empty()
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                // SAFETY: 自分のスレッドへ SIGINT を送る(処理は送り直す)。
                unsafe {
                    libc::raise(libc::SIGINT);
                }
            });
        }
        // 直接の子が動いている間に SIGINT を受ける。
        let script = match mode.as_str() {
            "run" | "forward" => format!("echo $$ > {notify}.pid; exec sleep 60"),
            // INT を受けたら、短い片付けをして終わる(端末の Ctrl-C は子にも届く)。
            "trap-fast" => format!(
                "trap 'kill $!; echo cleaned > {notify}.cleaned; exit 0' INT; sleep 60 & echo $$ > {notify}.pid; wait"
            ),
            // 片付けが 0.25 秒を超える。
            "trap-slow" => format!(
                "trap 'sleep 2; echo cleaned > {notify}.cleaned; exit 0' INT; sleep 60 & echo $$ > {notify}.pid; wait"
            ),
            "ignored" => {
                format!("echo $$ > {notify}.pid; sleep 0.6; echo finished > {notify}.finished")
            }
            other => panic!("未知の mode: {other}"),
        };
        let result = run_in_dir(
            &dir,
            &[b"/bin/sh".to_vec(), b"-c".to_vec(), script.into_bytes()],
            &[(b"PATH".to_vec(), b"/bin:/usr/bin".to_vec())],
            Duration::from_secs(60),
        );
        if mode == "ignored" {
            assert_eq!(result.unwrap().code, 0);
            std::fs::write(format!("{notify}.parent-done"), b"").unwrap();
            return;
        }
        unreachable!("SIGINT で終わるはず");
    }

    struct InterruptRun {
        status: std::process::ExitStatus,
        stderr: String,
        child_pid: i32,
        scratch: Scratch,
    }

    /// 検査の子を `mode` で起動し、直接の子の pid が書かれるのを待ってから、SIGINT を送る
    /// (`to_child` なら、端末の Ctrl-C のように、直接の子へも送る)。終了まで待つ。
    fn interrupt_run(mode: &str, to_child: bool) -> InterruptRun {
        let scratch = Scratch::new("interrupt");
        let notify = scratch.0.join("notify");
        // 検査の後で、孫(trap の中の sleep・後ろで動かす sleep)まで片付けるため、専用のプロセスグループで起動する。
        let mut parent = Command::new(std::env::current_exe().unwrap())
            .process_group(0)
            .args([
                "--exact",
                "adopt_candidate::tests::interrupt_child_mode",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("R4C_INTERRUPT_CHILD", mode)
            .env("R4C_NOTIFY", &notify)
            .stderr(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let pid_file = scratch.0.join("notify.pid");
        let started = Instant::now();
        while !pid_file.exists()
            || std::fs::read_to_string(&pid_file)
                .unwrap_or_default()
                .trim()
                .is_empty()
        {
            assert!(
                started.elapsed() < Duration::from_secs(20),
                "子が始まらない"
            );
            std::thread::sleep(Duration::from_millis(20));
        }
        let child_pid: i32 = std::fs::read_to_string(&pid_file)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // SAFETY: 起動したばかりの検査の子(と、その子)へ SIGINT を送る。
        unsafe {
            libc::kill(parent.id() as i32, libc::SIGINT);
            if to_child {
                libc::kill(child_pid, libc::SIGINT);
            }
        }
        let mut stderr = String::new();
        std::io::Read::read_to_string(parent.stderr.as_mut().unwrap(), &mut stderr).unwrap();
        let status = parent.wait().unwrap();
        // SAFETY: 自分が作ったプロセスグループの残り(孫)を止める。もう無ければ ESRCH で失敗するだけ。
        unsafe {
            libc::killpg(parent.id() as i32, libc::SIGKILL);
        }
        InterruptRun {
            status,
            stderr,
            child_pid,
            scratch,
        }
    }

    fn assert_child_gone(pid: i32) {
        // 殺された直後の子は、数ミリ秒の間、終わる途中(`D`)・回収待ち(`Z`)に見えることがある。短い間だけ待って確かめる。
        let state = || {
            std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .map(|text| text.rsplit(')').next().unwrap_or("").trim().chars().next())
        };
        let has_proc = Path::new("/proc/self").exists();
        for _ in 0..100 {
            let gone = if has_proc {
                !matches!(state(), Some(Some(code)) if code != 'Z' && code != 'X')
            } else {
                // SAFETY: シグナル 0 は存在確認だけ。
                unsafe { libc::kill(pid, 0) != 0 }
            };
            if gone {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("子が残っている: {:?}", state());
    }

    /// 持ち主のスレッドは、`install_interrupt_handler` を呼んだスレッドで、別のスレッドが受けた SIGINT は持ち主へ送り直される。
    #[test]
    fn interrupt_owner_is_the_installing_thread_and_forwarding_works() {
        use std::os::unix::process::ExitStatusExt;
        // 持ち主が呼んだスレッドであることは、子の側(`interrupt_child_mode`)の assert が固定する。
        let run = interrupt_run("forward", false);
        assert_eq!(run.status.signal(), Some(libc::SIGINT), "{}", run.stderr);
        assert!(
            run.stderr.trim_end().ends_with("KeyboardInterrupt"),
            "{}",
            run.stderr
        );
        assert_child_gone(run.child_pid);
    }

    #[test]
    fn interrupt_kills_the_direct_child_and_ends_by_the_signal_with_keyboard_interrupt() {
        use std::os::unix::process::ExitStatusExt;
        let run = interrupt_run("run", false);
        assert_eq!(run.status.signal(), Some(libc::SIGINT), "{}", run.stderr);
        assert!(
            run.stderr.trim_end().ends_with("KeyboardInterrupt"),
            "{}",
            run.stderr
        );
        // 直接の子は回収済みで、残っていない(0.25 秒待っても終わらないので kill される)。
        assert_child_gone(run.child_pid);
    }

    /// 旧 `Popen.communicate` は、SIGINT の後に子の終了を最大 0.25 秒待つ。その間に自分で片付けて終われば、kill されない。
    #[test]
    fn interrupt_waits_briefly_for_a_child_that_cleans_up_on_its_own() {
        use std::os::unix::process::ExitStatusExt;
        let run = interrupt_run("trap-fast", true);
        assert_eq!(run.status.signal(), Some(libc::SIGINT), "{}", run.stderr);
        assert!(
            run.scratch.0.join("notify.cleaned").exists(),
            "子の片付けが終わる前に kill された"
        );
        assert_child_gone(run.child_pid);
    }

    /// 片付けが 0.25 秒を超える子は kill される(片付けは終わらない)。
    #[test]
    fn interrupt_kills_a_child_whose_cleanup_takes_longer_than_the_wait() {
        use std::os::unix::process::ExitStatusExt;
        let run = interrupt_run("trap-slow", true);
        assert_eq!(run.status.signal(), Some(libc::SIGINT), "{}", run.stderr);
        assert!(!run.scratch.0.join("notify.cleaned").exists());
        assert_child_gone(run.child_pid);
    }

    /// SIGINT を無視して起動されたら、旧 Python は無視のまま(KeyboardInterrupt にならない)。子の Git も無視を受け継ぐ。
    #[test]
    fn an_ignored_sigint_stays_ignored_and_the_run_finishes() {
        let run = interrupt_run("ignored", true);
        assert!(run.status.success(), "{:?} {}", run.status, run.stderr);
        assert!(run.scratch.0.join("notify.parent-done").exists());
        assert!(run.scratch.0.join("notify.finished").exists());
    }

    #[test]
    fn protected_interrupt_only_leaves_a_mark() {
        assert!(!interrupt_protection(true));
        assert!(!interrupted());
        on_interrupt(libc::SIGINT);
        assert!(interrupted());
        assert!(interrupt_protection(false));
        assert!(!interrupted());
    }

    #[test]
    fn child_deadline_kills_only_the_direct_child() {
        let scratch = Scratch::new("deadline");
        let dir = scratch.dir();
        let started = Instant::now();
        let result = run_in_dir(
            &dir,
            &[
                b"/bin/sh".to_vec(),
                b"-c".to_vec(),
                b"exec sleep 30".to_vec(),
            ],
            &[(
                b"PATH".to_vec(),
                std::env::var_os("PATH")
                    .map(|v| std::os::unix::ffi::OsStrExt::as_bytes(v.as_os_str()).to_vec())
                    .unwrap(),
            )],
            Duration::from_millis(300),
        );
        assert!(matches!(result, Err(ChildError::Timeout)));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
