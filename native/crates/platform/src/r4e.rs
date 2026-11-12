//! R4E(Markdown の表の XLSX 出力)専用の OS の部品。
//!
//! unsafe はこのモジュールの中だけに置き、安全な関数だけを公開する。C の型・定数・関数は、
//! platform の直接の依存の `libc` の crate のものを使う。`libc` に無いもの(`tzset`)だけを
//! `sys` に手で宣言する。
//!
//! # 起動前の状態の記録
//!
//! Rust の標準ライブラリは `main` の前に、閉じた fd 0〜2 を `/dev/null` で開き、`SIGPIPE` を
//! 無視へ変える。旧版(Python 3.10)と比べるには、その前の状態が要る。
//!
//! [`record_startup_state`] は、実行ファイルの読み込み時に `main` より前に呼ばれる関数の一覧
//! (Linux は `.init_array`、Mac は `__DATA,__mod_init_func`)に載せて使う。一覧の項目(関数を
//! 指す静的変数)は、検査専用の入口 `examples/r4e_contract.rs` にだけ置く。このモジュールは
//! 一覧の項目を持たない(テストの実行ファイルを除く)ので、platform を結合した製品の
//! `dev-workflow` の起動前の関数は増えない。
#![cfg(all(
    target_pointer_width = "64",
    any(target_arch = "x86_64", target_arch = "aarch64"),
    any(target_os = "linux", target_os = "macos"),
))]

use std::ffi::c_int;
use std::io;
use std::os::fd::{AsFd, BorrowedFd, IntoRawFd, OwnedFd};
use std::path::PathBuf;
use std::ptr;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU8, AtomicUsize, Ordering};

/// `libc` に無い C の関数だけを手で宣言する。
mod sys {
    unsafe extern "C" {
        /// `TZ` を読んで時刻帯の情報を作る(POSIX の `tzset`)。`libc` 0.2.191 は Unix 向けに
        /// `tzset` を宣言しない(Windows の `_tzset` だけ)ので、ここで宣言する。glibc・musl・
        /// Mac の C の宣言は、どれも引数も戻り値も無い `void tzset(void)`。
        pub fn tzset();
    }
}

// `current_thread_id` は `pthread_t`(glibc は `unsigned long`、musl と Mac は番地)を `usize` へ
// 変える。4 機種とも同じ 64 ビットなので、値は切れない。
const _: () = assert!(size_of::<libc::pthread_t>() == size_of::<usize>());

fn last_errno() -> i32 {
    io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

fn set_errno(value: i32) {
    // errno の置き場を返す関数(Linux は `__errno_location`、Mac は `__error`)。
    #[cfg(target_os = "linux")]
    let location = libc::__errno_location;
    #[cfg(target_os = "macos")]
    let location = libc::__error;
    // SAFETY: errno の置き場はこの thread の領域を指し、int として書ける。
    unsafe { *location() = value };
}

// ---------------------------------------------------------------- fd の種類

/// fd が指す物の種類。`fstat` の種類と、文字デバイスなら端末か(`isatty`)で分ける。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FdKind {
    RegularFile,
    Directory,
    Fifo,
    Socket,
    /// 端末の文字デバイス(`isatty` が真)。
    Terminal,
    /// 端末でない文字デバイス(`/dev/null` など)。
    CharacterDevice,
    BlockDevice,
    Symlink,
    /// `fstat` が失敗したか、種類が分からない。
    Unknown,
}

impl FdKind {
    const ALL: [Self; 9] = [
        Self::RegularFile,
        Self::Directory,
        Self::Fifo,
        Self::Socket,
        Self::Terminal,
        Self::CharacterDevice,
        Self::BlockDevice,
        Self::Symlink,
        Self::Unknown,
    ];

    fn code(self) -> u8 {
        Self::ALL.iter().position(|kind| *kind == self).unwrap_or(8) as u8
    }

    fn from_code(code: u8) -> Self {
        Self::ALL
            .get(usize::from(code))
            .copied()
            .unwrap_or(Self::Unknown)
    }

    /// 検査の出力に使う短い名前。
    pub fn name(self) -> &'static str {
        match self {
            Self::RegularFile => "regular",
            Self::Directory => "directory",
            Self::Fifo => "fifo",
            Self::Socket => "socket",
            Self::Terminal => "terminal",
            Self::CharacterDevice => "chardev",
            Self::BlockDevice => "blockdev",
            Self::Symlink => "symlink",
            Self::Unknown => "unknown",
        }
    }
}

/// fd が端末か(C の `isatty` と同じ判定)。Python の `os.isatty` と `FileIO.isatty` が使う。
///
/// Linux は glibc の `isatty` と同じく、端末の属性の取得(`tcgetattr`)の成否で決める。rustix の
/// `isatty` は Linux では窓の大きさの取得(`TIOCGWINSZ`)で決めるので使わない。Mac は OS の
/// `isatty`(rustix が呼ぶ C の関数)を使う。
pub fn is_terminal<Fd: AsFd>(fd: Fd) -> bool {
    #[cfg(target_os = "linux")]
    {
        rustix::termios::tcgetattr(fd).is_ok()
    }
    #[cfg(target_os = "macos")]
    {
        rustix::termios::isatty(fd)
    }
}

/// 開いている fd の種類を返す。起動前の記録と同じ分け方をする。
///
/// `main` の中では、fd 0〜2 は標準ライブラリが必ず開いている(閉じていたものは `/dev/null`)。
/// 起動の時点の状態は [`startup_state`] で読む。端末かどうかは [`is_terminal`] で決める。
pub fn fd_kind<Fd: AsFd>(fd: Fd) -> io::Result<FdKind> {
    use rustix::fs::FileType;
    let fd = fd.as_fd();
    let info = rustix::fs::fstat(fd)?;
    Ok(match FileType::from_raw_mode(info.st_mode) {
        FileType::RegularFile => FdKind::RegularFile,
        FileType::Directory => FdKind::Directory,
        FileType::Fifo => FdKind::Fifo,
        FileType::Socket => FdKind::Socket,
        FileType::CharacterDevice if is_terminal(fd) => FdKind::Terminal,
        FileType::CharacterDevice => FdKind::CharacterDevice,
        FileType::BlockDevice => FdKind::BlockDevice,
        FileType::Symlink => FdKind::Symlink,
        _ => FdKind::Unknown,
    })
}

/// 端末の幅(`ioctl(TIOCGWINSZ)` の列の数)。Python の `os.get_terminal_size(fd).columns` と
/// 同じ値で、0 もそのまま返す。端末でないなど取得に失敗したら `None`。
pub fn terminal_columns<Fd: AsFd>(fd: Fd) -> Option<u16> {
    rustix::termios::tcgetwinsize(fd)
        .ok()
        .map(|size| size.ws_col)
}

/// 標準出力の端末の幅(Python の `os.get_terminal_size(sys.__stdout__.fileno())`)。
///
/// 起動の時点で fd 1 が閉じていたときは、標準ライブラリが開いた `/dev/null` を調べるので
/// `None` になる(旧版の `sys.__stdout__` が `None` で幅を取れないのと同じ結果)。
pub fn stdout_terminal_columns() -> Option<u16> {
    terminal_columns(io::stdout())
}

// ---------------------------------------------------------------- 書き込み

/// `write` を 1 回だけ呼ぶ(Python の `_Py_write` の中の 1 回の `write`)。
///
/// 途中までしか書けなかったときは書けたバイト数を返す。信号で中断された `EINTR` も、書き直さずに
/// 誤りとして返す(書き直すか `KeyboardInterrupt` にするかは呼出側が決める)。
pub fn write_once<Fd: AsFd>(fd: Fd, data: &[u8]) -> io::Result<usize> {
    rustix::io::write(fd, data).map_err(io::Error::from)
}

/// fd を閉じ、`close(2)` の誤りを返す(Python の `FileIO.close` の `internal_close`)。
///
/// 標準ライブラリの `File` などは、閉じるときの誤りを捨てる。旧版は誤りを `OSError` にするので、
/// ここで受け取る。`EINTR` も閉じ直さずに返す(CPython と同じ)。誤りのときも fd はもう使えない。
///
/// Linux は、カーネルの `close` を `syscall` で直接呼ぶ(glibc の `close` と同じ結果)。静的に
/// 結合する musl の `close` は `EINTR` を成功に変えるので使わない。Mac は OS の `close` を使う。
pub fn close_checked(fd: impl Into<OwnedFd>) -> io::Result<()> {
    let raw = fd.into().into_raw_fd();
    // SAFETY: raw は、ここで所有を受け取った開いた fd である(`OwnedFd` から取り出した)。
    // ほかに持ち主はいないので、閉じた後にこの番号を使うコードは無い。
    #[cfg(target_os = "linux")]
    let result = unsafe { libc::syscall(libc::SYS_close, std::ffi::c_long::from(raw)) };
    // SAFETY: 同上。
    #[cfg(target_os = "macos")]
    let result = unsafe { libc::close(raw) };
    if result == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// fd の `st_blksize`(Python の `FileIO._blksize` の元)。`io.open` の溜めの大きさに使う。
pub fn block_size<Fd: AsFd>(fd: Fd) -> io::Result<i64> {
    // st_blksize の型は機種で違う(Linux x86_64 は long、aarch64 と Mac は int)。
    #[allow(clippy::unnecessary_cast)]
    rustix::fs::fstat(fd)
        .map(|info| info.st_blksize as i64)
        .map_err(io::Error::from)
}

// ---------------------------------------------------------------- 起動前の状態

/// 信号の扱い。`execve` を越えて継げるのは既定と無視だけ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Default,
    Ignore,
    Handler,
}

impl Disposition {
    fn code(self) -> i32 {
        match self {
            Self::Default => 1,
            Self::Ignore => 2,
            Self::Handler => 3,
        }
    }

    fn from_code(code: i32) -> Option<Self> {
        match code {
            1 => Some(Self::Default),
            2 => Some(Self::Ignore),
            3 => Some(Self::Handler),
            _ => None,
        }
    }

    /// 検査の出力に使う短い名前。
    pub fn name(self) -> &'static str {
        match self {
            Self::Default => "default",
            Self::Ignore => "ignore",
            Self::Handler => "handler",
        }
    }
}

/// 起動の時点の fd 1 本の状態。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FdState {
    /// 開いていた。種類つき。
    Open(FdKind),
    /// 閉じていた(`fcntl(F_GETFD)` が `EBADF`)。標準ライブラリが `/dev/null` で埋めた。
    Closed,
    /// `fcntl(F_GETFD)` が `EBADF` 以外の誤り(errno)で失敗した。
    Failed(i32),
}

/// 起動の時点の fd 0〜2 の 1 本の記録。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StandardFd {
    pub state: FdState,
    /// Python 3.10 の `is_valid_fd` が失敗したときの errno(Linux は `dup`、Mac は `fstat`)。
    /// `Some` なら、旧版の `sys.stdin`・`sys.stdout`・`sys.stderr` の該当するものは `None` になる。
    pub python_check_errno: Option<i32>,
}

impl StandardFd {
    /// 起動の時点で閉じていたか。
    pub fn was_closed(&self) -> bool {
        self.state == FdState::Closed
    }

    /// 旧版の Python 3.10 がこの fd を標準入出力に使うか(`is_valid_fd` が真か)。
    pub fn python_valid(&self) -> bool {
        self.python_check_errno.is_none()
    }
}

/// 標準ライブラリが変える前の状態。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StartupState {
    pub fds: [StandardFd; 3],
    /// 継いだ `SIGPIPE` の扱い。読めなかったら `None`。
    pub sigpipe: Option<Disposition>,
    /// 継いだ `SIGINT` の扱い。読めなかったら `None`。
    pub sigint: Option<Disposition>,
}

impl StartupState {
    /// 検査の出力に使う 1 行の要約。fd ごとに `<状態>/<Python の判定>`、続けて信号の扱い。
    ///
    /// 例: `chardev/valid closed/invalid:9 fifo/valid sigpipe=default sigint=ignore`
    pub fn summary(&self) -> String {
        let fds = self
            .fds
            .iter()
            .map(|fd| {
                let state = match fd.state {
                    FdState::Open(kind) => kind.name().to_owned(),
                    FdState::Closed => "closed".to_owned(),
                    FdState::Failed(errno) => format!("failed:{errno}"),
                };
                let python = match fd.python_check_errno {
                    None => "valid".to_owned(),
                    Some(errno) => format!("invalid:{errno}"),
                };
                format!("{state}/{python}")
            })
            .collect::<Vec<_>>()
            .join(" ");
        let name = |value: Option<Disposition>| value.map_or("unread", Disposition::name);
        format!(
            "{fds} sigpipe={} sigint={}",
            name(self.sigpipe),
            name(self.sigint)
        )
    }
}

const PHASE_EMPTY: u8 = 0;
const PHASE_WRITING: u8 = 1;
const PHASE_DONE: u8 = 2;

/// 1 本の fd を調べた結果。`getfd_errno` が 0 なら開いている。
#[derive(Clone, Copy)]
struct FdProbe {
    getfd_errno: i32,
    kind: FdKind,
    python_errno: i32,
}

/// 起動前の状態の置き場。原子的な値だけを持ち、確保をしない。
struct Record {
    phase: AtomicU8,
    getfd_errno: [AtomicI32; 3],
    kind: [AtomicU8; 3],
    python_errno: [AtomicI32; 3],
    /// `SIGPIPE`・`SIGINT` の順。正は [`Disposition`] の値、負は読めなかった errno、0 は未記録。
    signals: [AtomicI32; 2],
}

const RECORDED_SIGNALS: [c_int; 2] = [libc::SIGPIPE, libc::SIGINT];

impl Record {
    const fn new() -> Self {
        Self {
            phase: AtomicU8::new(PHASE_EMPTY),
            getfd_errno: [const { AtomicI32::new(0) }; 3],
            kind: [const { AtomicU8::new(0) }; 3],
            python_errno: [const { AtomicI32::new(0) }; 3],
            signals: [const { AtomicI32::new(0) }; 2],
        }
    }

    /// 最初の 1 回だけ書く。2 回目からは何もしない。
    fn write_with(
        &self,
        probe_fd: impl Fn(c_int) -> FdProbe,
        probe_signal: impl Fn(c_int) -> io::Result<Disposition>,
    ) {
        if self
            .phase
            .compare_exchange(
                PHASE_EMPTY,
                PHASE_WRITING,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        for fd in 0..3 {
            let probe = probe_fd(fd as c_int);
            self.getfd_errno[fd].store(probe.getfd_errno, Ordering::Relaxed);
            self.kind[fd].store(probe.kind.code(), Ordering::Relaxed);
            self.python_errno[fd].store(probe.python_errno, Ordering::Relaxed);
        }
        for (slot, signal) in self.signals.iter().zip(RECORDED_SIGNALS) {
            let value = match probe_signal(signal) {
                Ok(disposition) => disposition.code(),
                Err(error) => -error.raw_os_error().unwrap_or(libc::EINVAL),
            };
            slot.store(value, Ordering::Relaxed);
        }
        self.phase.store(PHASE_DONE, Ordering::Release);
    }

    fn read(&self) -> Option<StartupState> {
        if self.phase.load(Ordering::Acquire) != PHASE_DONE {
            return None;
        }
        let fd = |index: usize| {
            let getfd = self.getfd_errno[index].load(Ordering::Relaxed);
            let python = self.python_errno[index].load(Ordering::Relaxed);
            StandardFd {
                state: match getfd {
                    0 => FdState::Open(FdKind::from_code(self.kind[index].load(Ordering::Relaxed))),
                    libc::EBADF => FdState::Closed,
                    other => FdState::Failed(other),
                },
                python_check_errno: (python != 0).then_some(python),
            }
        };
        let signal =
            |index: usize| Disposition::from_code(self.signals[index].load(Ordering::Relaxed));
        Some(StartupState {
            fds: [fd(0), fd(1), fd(2)],
            sigpipe: signal(0),
            sigint: signal(1),
        })
    }
}

static RECORD: Record = Record::new();

/// 起動の時点の fd を 1 本調べる。確保をせず、標準ライブラリの初期化を要らない呼出しだけを使う。
fn probe_startup_fd(fd: c_int) -> FdProbe {
    // SAFETY: F_GETFD は記述子の表を読むだけで、閉じた番号には EBADF を返すだけである。
    if unsafe { libc::fcntl(fd, libc::F_GETFD) } == -1 {
        let errno = last_errno();
        // 閉じた番号には Python の `dup`・`fstat` も同じ errno で失敗する。
        return FdProbe {
            getfd_errno: errno,
            kind: FdKind::Unknown,
            python_errno: errno,
        };
    }
    // SAFETY: fd は標準の 0〜2 のどれかで、直前の F_GETFD で開いていると確かめた。
    // 標準の fd 0〜2 は、Rust の標準ライブラリ自身も `impl AsFd for Stdout` などで同じ番号を
    // `borrow_raw` で借りるとおり、unsafe なコードで閉じない限り開いたままと見なされる
    // (安全なコードからは閉じられない)。この借用はこの関数を出る前に終わる。
    let borrowed = unsafe { BorrowedFd::borrow_raw(fd) };
    FdProbe {
        getfd_errno: 0,
        kind: fd_kind(borrowed).unwrap_or(FdKind::Unknown),
        python_errno: python_is_valid_fd(borrowed),
    }
}

/// Python 3.10 の `is_valid_fd`。Linux は `dup` して閉じ、ほかは `fstat`。失敗の errno、成功は 0。
fn python_is_valid_fd(fd: BorrowedFd<'_>) -> i32 {
    #[cfg(target_os = "linux")]
    let result = rustix::io::dup(fd).map(drop);
    #[cfg(target_os = "macos")]
    let result = rustix::fs::fstat(fd).map(drop);
    match result {
        Ok(()) => 0,
        Err(error) => error.raw_os_error(),
    }
}

/// 起動前の状態を記録する。`main` より前に呼ばれる関数の一覧に載せて使う。
///
/// 最初の 1 回だけ記録し、2 回目からは何もしない。確保をせず、panic しない。
///
/// 一覧の項目の外(`main` の中や、`main` の後に動く thread など)から初めて呼ぶと、起動前の
/// 状態ではなく、標準ライブラリが変えた後の状態(閉じていた fd 0〜2 を埋めた `/dev/null`、
/// 無視へ変えた `SIGPIPE`)を記録してしまう。その記録は旧版の判定に使えないので、この関数は
/// 一覧の項目からだけ呼ぶ。
pub extern "C" fn record_startup_state() {
    RECORD.write_with(probe_startup_fd, signal_disposition);
}

/// 起動前の状態を読む。[`record_startup_state`] が呼ばれていなければ `None`(既定値を作らない)。
pub fn startup_state() -> Option<StartupState> {
    RECORD.read()
}

// ---------------------------------------------------------------- 信号

/// 扱いと旗だけを入れた `struct sigaction`。mask は空(全ビットが 0 は `sigemptyset` と同じ)。
///
/// 起動前の記録からも呼ぶので、確保も C の関数の呼出しもしない。
fn signal_action(handler: libc::sighandler_t, flags: c_int) -> libc::sigaction {
    // SAFETY: `struct sigaction` の欄は、整数と、mask の整数(の並び)と、Linux の `sa_restorer`
    // (関数の番地の `Option`)だけで、全ビットが 0 はどれも有効な値(扱いは SIG_DFL、mask は空、
    // 旗は無し、`sa_restorer` は `None`)。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = handler;
    action.sa_flags = flags;
    action
}

fn signal_disposition(signal: c_int) -> io::Result<Disposition> {
    let mut old = signal_action(libc::SIG_DFL, 0);
    // SAFETY: 新しい扱いに null を渡すので、扱いを読むだけで変えない。old は有効な書き込み先。
    if unsafe { libc::sigaction(signal, ptr::null(), &mut old) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(match old.sa_sigaction {
        libc::SIG_DFL => Disposition::Default,
        libc::SIG_IGN => Disposition::Ignore,
        _ => Disposition::Handler,
    })
}

/// Python の `PyOS_setsig` と同じく、空の mask と `SA_ONSTACK` だけで扱いを置く(`SA_RESTART` なし)。
fn set_signal_handler(signal: c_int, handler: libc::sighandler_t) -> io::Result<()> {
    let action = signal_action(handler, libc::SA_ONSTACK);
    // SAFETY: action は有効な値で、handler は SIG_DFL・SIG_IGN か、原子的な値だけを書く関数である。
    if unsafe { libc::sigaction(signal, &action, ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// 旧版の Python と同じく、`SIGXFSZ` を無視へ変える。`ulimit -f` を超える書き込みは `EFBIG` になる。
pub fn ignore_sigxfsz() -> io::Result<()> {
    set_signal_handler(libc::SIGXFSZ, libc::SIG_IGN)
}

/// `SIGPIPE` を既定(`ignored` が偽)か無視(真)にする。
///
/// Rust の標準ライブラリは `main` の前に `SIGPIPE` を無視へ変える。旧版の Python は、起動の設定を
/// 読む間は継いだ扱いのままで、信号の初期化(`_PySignal_Init`)で無視へ変える。その間の書き込み
/// (設定の誤りの `Fatal Python error`)を旧版と同じにするために、継いだ扱いへ戻すのに使う。
pub fn set_sigpipe_ignored(ignored: bool) -> io::Result<()> {
    let handler = if ignored {
        libc::SIG_IGN
    } else {
        libc::SIG_DFL
    };
    set_signal_handler(libc::SIGPIPE, handler)
}

static SIGINT_PENDING: AtomicBool = AtomicBool::new(false);
static SIGINT_COUNT: AtomicUsize = AtomicUsize::new(0);

extern "C" fn on_sigint(_signal: c_int) {
    // 信号の中で安全な操作(原子的な書き込み)だけをする。errno も変えない。
    SIGINT_COUNT.fetch_add(1, Ordering::SeqCst);
    SIGINT_PENDING.store(true, Ordering::SeqCst);
}

/// [`install_python_sigint`] の結果。起動時の `SIGINT` の扱いで決まる。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SigintSetup {
    /// 既定だったので、印を立てる扱いを置いた。
    Installed,
    /// 無視されていたので、無視のままにした。
    Ignored,
    /// ほかの扱いが置かれていたので、変えなかった。
    Kept,
}

/// Python 3.10 の起動時の `SIGINT` の扱い(`default_int_handler` の登録)を再現する。
///
/// 今の扱いが既定(`SIG_DFL`)のときだけ、届いた印と回数を記録する扱いを置く。`SA_RESTART` を
/// 付けないので、書き込みなどの待ちは `EINTR` で戻る。無視されていれば無視を続ける。
pub fn install_python_sigint() -> io::Result<SigintSetup> {
    match signal_disposition(libc::SIGINT)? {
        Disposition::Default => {
            set_signal_handler(libc::SIGINT, on_sigint as extern "C" fn(c_int) as usize)?;
            Ok(SigintSetup::Installed)
        }
        Disposition::Ignore => Ok(SigintSetup::Ignored),
        Disposition::Handler => Ok(SigintSetup::Kept),
    }
}

/// 届いて、まだ取り出していない `SIGINT` があるか。
pub fn sigint_pending() -> bool {
    SIGINT_PENDING.load(Ordering::SeqCst)
}

/// 届いた印を取り出す。立っていれば下ろして `true`(Python が `KeyboardInterrupt` を投げる時点)。
pub fn take_sigint() -> bool {
    SIGINT_PENDING.swap(false, Ordering::SeqCst)
}

/// 扱いを置いてから届いた `SIGINT` の回数。
pub fn sigint_count() -> usize {
    SIGINT_COUNT.load(Ordering::SeqCst)
}

/// Python 3.10 の終了処理の `_PySignal_Fini` のうち `SIGINT` の分: 置いた扱いを既定へ戻し、
/// 届いてまだ取り出していない印を捨てる。この後に届いた `SIGINT` は既定どおりに働く。
///
/// [`install_python_sigint`] が [`SigintSetup::Installed`] を返したときだけ呼ぶ(旧版も、自分の
/// 扱いを置いた信号だけを既定へ戻し、無視のものは変えない)。
pub fn restore_default_sigint() -> io::Result<()> {
    SIGINT_PENDING.store(false, Ordering::SeqCst);
    set_signal_handler(libc::SIGINT, libc::SIG_DFL)
}

/// [`resend_sigint_by_default`] の失敗。
#[derive(Debug)]
pub enum SigintResendError {
    /// 既定へ戻せなかった(Python は `perror("signal")` を出す)。
    Disposition(io::Error),
    /// 自分へ送れなかった(Python は何も出さない)。
    Kill(io::Error),
}

/// Python 3.10 の `exit_sigint`: `SIGINT` を既定へ戻し、`kill(getpid(), SIGINT)` で自分へ送る。
///
/// 送った信号が届けば、この関数は戻らずに `SIGINT` で終わる。戻ったとき(信号が塞がれている
/// など)は、呼出側が `128 + SIGINT`(130)で終える。
pub fn resend_sigint_by_default() -> Result<(), SigintResendError> {
    set_signal_handler(libc::SIGINT, libc::SIG_DFL).map_err(SigintResendError::Disposition)?;
    // SAFETY: getpid は自分の番号を返すだけで、kill は引数の値だけを読む。
    if unsafe { libc::kill(libc::getpid(), libc::SIGINT) } != 0 {
        return Err(SigintResendError::Kill(io::Error::last_os_error()));
    }
    Ok(())
}

// ---------------------------------------------------------------- 時刻

/// 年月日と時分秒。月は 1〜12、日は 1〜31。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CivilTime {
    pub year: i64,
    pub month: u8,
    pub day: u8,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
}

/// Python の `time.localtime(seconds)` と同じく、`localtime_r` で現地時刻へ分ける。
///
/// 先に `tzset` で `TZ` を読む。失敗は Python の `_PyTime_localtime` と同じく、errno が 0 なら
/// `EINVAL` とする。
pub fn local_time(seconds: i64) -> io::Result<CivilTime> {
    // SAFETY: `struct tm` の欄は整数と文字列の番地だけで、全ビットが 0(番地は null)は有効な値。
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: tzset は環境の TZ を読んで時刻帯の情報を作るだけである。環境を書く関数は
    // unsafe なので、安全なコードからは同時に書かれない。
    unsafe { sys::tzset() };
    set_errno(0);
    // SAFETY: seconds と tm は有効な読み書き先で、localtime_r は tm にだけ書く。
    let result = unsafe { libc::localtime_r(&seconds, &mut tm) };
    if result.is_null() {
        let errno = match last_errno() {
            0 => libc::EINVAL,
            other => other,
        };
        return Err(io::Error::from_raw_os_error(errno));
    }
    Ok(CivilTime {
        year: i64::from(tm.tm_year) + 1900,
        month: (tm.tm_mon + 1) as u8,
        day: tm.tm_mday as u8,
        hour: tm.tm_hour as u8,
        minute: tm.tm_min as u8,
        second: tm.tm_sec as u8,
    })
}

/// UNIX 時刻の秒を UTC の年月日と時分秒へ分ける(先発グレゴリオ暦。閏秒は数えない)。
/// Python の `datetime.datetime.now(datetime.timezone.utc)` の年月日と時分秒と同じ値。
pub fn utc_time(seconds: i64) -> CivilTime {
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    // 1970-03-01 を起点にした 400 年周期の計算(H. Hinnant の civil_from_days)。
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    CivilTime {
        year,
        month: month as u8,
        day: day as u8,
        hour: (rest / 3_600) as u8,
        minute: (rest % 3_600 / 60) as u8,
        second: (rest % 60) as u8,
    }
}

// ---------------------------------------------------------------- 実行中の thread

/// 起動の失敗の `Current thread 0x…` に出す値(Python の `PyThread_get_thread_ident`、
/// つまり `pthread_self()` の値)。
pub fn current_thread_id() -> usize {
    // SAFETY: pthread_self は呼んだ thread の識別子を返すだけで、引数も失敗も無い。
    let thread = unsafe { libc::pthread_self() };
    // glibc は整数、musl と Mac は番地。どちらも 64 ビットのまま `usize` にする(上の assert)。
    thread as usize
}

// ---------------------------------------------------------------- 現在のディレクトリ

/// 現在のディレクトリ(Python の `os.getcwd()` と同じ結果)。
///
/// Linux は glibc の `getcwd` と同じ手順で得る。まずカーネルの `getcwd` を呼び、`PATH_MAX`
/// (4096 バイト)を超えて `ENAMETOOLONG` になったときと、到達できない場所(`/` で始まらない値)
/// のときは、`..` を辿って名前を探す(glibc の `__getcwd_generic`)。静的に結合した musl の
/// `getcwd` は長いパスで失敗するので使わない。Mac は OS の `getcwd` を使う。
///
/// 失敗は errno つきの [`io::Error`] で返す。
pub fn current_dir() -> io::Result<PathBuf> {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::ffi::OsStringExt;
        linux_getcwd().map(|bytes| PathBuf::from(std::ffi::OsString::from_vec(bytes)))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::current_dir()
    }
}

#[cfg(target_os = "linux")]
fn linux_getcwd() -> io::Result<Vec<u8>> {
    let mut buffer = vec![0u8; libc::PATH_MAX as usize];
    // SAFETY: 長さ PATH_MAX の書き込み先を渡す。カーネルはその範囲にだけ書く。
    let length = unsafe { libc::syscall(libc::SYS_getcwd, buffer.as_mut_ptr(), buffer.len()) };
    if length > 0 && buffer[0] == b'/' {
        let end = buffer
            .iter()
            .position(|byte| *byte == 0)
            .unwrap_or(buffer.len());
        buffer.truncate(end);
        return Ok(buffer);
    }
    if length < 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ENAMETOOLONG) {
            return Err(error);
        }
    }
    getcwd_by_walking()
}

/// glibc の `__getcwd_generic`(fd 相対の版)と同じ手順。
///
/// 現在地から `..` を開いて上へ進み、親の中で `(st_dev, st_ino)` が一致するディレクトリの名前を
/// 探す。マウントの境目では `d_ino` を使わずに全部を `fstatat` する。`d_ino` で見つからなければ、
/// 先頭へ戻して全部を `fstatat` する。名前の `fstatat` の失敗は無視し、最後まで見つからなければ
/// `ENOENT`。
#[cfg(target_os = "linux")]
fn getcwd_by_walking() -> io::Result<Vec<u8>> {
    use rustix::fs::{AtFlags, CWD, Dir, FileType, Mode, OFlags};
    let here = rustix::fs::lstat(".")?;
    let root = rustix::fs::lstat("/")?;
    let (mut this_dev, mut this_ino) = (here.st_dev, here.st_ino);
    let mut names: Vec<Vec<u8>> = Vec::new();
    let mut current: Option<Dir> = None;
    while !(this_dev == root.st_dev && this_ino == root.st_ino) {
        let flags = OFlags::RDONLY | OFlags::CLOEXEC;
        let parent = match &current {
            None => rustix::fs::openat(CWD, "..", flags, Mode::empty())?,
            Some(dir) => rustix::fs::openat(dir.fd()?, "..", flags, Mode::empty())?,
        };
        let info = rustix::fs::fstat(&parent)?;
        // glibc と同じく、親を開いて調べた後で、前の段の流れを閉じる。
        drop(current.take());
        let (dot_dev, dot_ino) = (info.st_dev, info.st_ino);
        let mount_point = dot_dev != this_dev;
        let mut dir = Dir::new(parent)?;
        let mut use_d_ino = true;
        let name = loop {
            let entry = match dir.read() {
                None if use_d_ino => {
                    use_d_ino = false;
                    dir.rewind();
                    continue;
                }
                None => return Err(io::Error::from_raw_os_error(libc::ENOENT)),
                Some(Err(error)) => return Err(error.into()),
                Some(Ok(entry)) => entry,
            };
            let name = entry.file_name();
            if matches!(name.to_bytes(), b"." | b"..") {
                continue;
            }
            if use_d_ino && !(entry.ino() == this_ino || mount_point) {
                continue;
            }
            let found =
                rustix::fs::statat(dir.fd()?, name, AtFlags::SYMLINK_NOFOLLOW).is_ok_and(|info| {
                    FileType::from_raw_mode(info.st_mode) == FileType::Directory
                        && info.st_dev == this_dev
                        && info.st_ino == this_ino
                });
            if found {
                break name.to_bytes().to_vec();
            }
        };
        names.push(name);
        this_dev = dot_dev;
        this_ino = dot_ino;
        current = Some(dir);
    }
    if names.is_empty() {
        return Ok(b"/".to_vec());
    }
    let mut path = Vec::new();
    for name in names.iter().rev() {
        path.push(b'/');
        path.extend_from_slice(name);
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    use std::path::Path;
    use std::process::{Command, Stdio};
    use std::sync::Arc;
    use std::sync::atomic::AtomicU64;
    use std::time::{Duration, Instant};

    #[test]
    fn close_checked_closes_the_fd_and_reports_success() {
        let (mut reader, writer) = std::io::pipe().unwrap();
        close_checked(writer).unwrap();
        // 書く側を閉じたので、読む側は終わり(0 バイト)を読む。
        let mut data = Vec::new();
        std::io::Read::read_to_end(&mut reader, &mut data).unwrap();
        assert!(data.is_empty());
        let file = std::fs::File::open("/dev/null").unwrap();
        close_checked(file).unwrap();
    }

    // テストの実行ファイルにだけ、起動前の関数の一覧の項目を置く。cfg(test) なので製品には入らない。
    #[used]
    #[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
    #[cfg_attr(
        target_os = "macos",
        unsafe(link_section = "__DATA,__mod_init_func,mod_init_funcs")
    )]
    static RECORD_FOR_TESTS: extern "C" fn() = record_startup_state;

    /// `setrlimit` の資源の番号の型(glibc は `unsigned int`、musl と Mac は `int`)。
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    type Resource = libc::__rlimit_resource_t;
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    type Resource = c_int;

    const CHILD: &str = "R4E_PLATFORM_TEST_CHILD";
    const REPORT: &str = "R4E_PLATFORM_TEST_REPORT";
    const SCRATCH: &str = "R4E_PLATFORM_TEST_SCRATCH";

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4e-platform-{}-{}-{name}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    /// このテストの実行ファイルの `child` を、決めた動きで起動する。
    fn child_command(mode: &str, scratch: &Scratch) -> (Command, PathBuf) {
        let report = scratch.0.join(format!("report-{mode}"));
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "r4e::tests::child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD, mode)
            .env(REPORT, &report)
            .env(SCRATCH, &scratch.0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        (command, report)
    }

    /// 子の exec の直前に、fd を閉じ、信号の扱いと資源の上限を決める。
    fn prepare(
        command: &mut Command,
        close_fds: &'static [c_int],
        ignore: &'static [c_int],
        limits: &'static [(Resource, u64)],
    ) {
        // SAFETY: fork の後の子で、信号の中でも安全な close・sigaction・setrlimit だけを呼ぶ。
        // 標準ライブラリはこの前に SIGPIPE を既定へ戻している。
        unsafe {
            command.pre_exec(move || {
                for signal in [libc::SIGPIPE, libc::SIGINT, libc::SIGXFSZ] {
                    let handler = if ignore.contains(&signal) {
                        libc::SIG_IGN
                    } else {
                        libc::SIG_DFL
                    };
                    set_signal_handler(signal, handler)?;
                }
                for (resource, value) in limits {
                    let limit = libc::rlimit {
                        rlim_cur: *value,
                        rlim_max: *value,
                    };
                    if libc::setrlimit(*resource, &limit) != 0 {
                        return Err(io::Error::last_os_error());
                    }
                }
                for fd in close_fds {
                    libc::close(*fd);
                }
                Ok(())
            });
        }
    }

    fn read_report(path: &Path) -> String {
        std::fs::read_to_string(path).unwrap_or_else(|error| panic!("{path:?}: {error}"))
    }

    fn wait_until(condition: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !condition() {
            assert!(Instant::now() < deadline, "時間切れ");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    /// 決めた動きの子。環境変数が無い通常の実行では何もしない。
    #[test]
    fn child() {
        let Some(mode) = std::env::var_os(CHILD) else {
            return;
        };
        let report = PathBuf::from(std::env::var_os(REPORT).unwrap());
        let scratch = PathBuf::from(std::env::var_os(SCRATCH).unwrap());
        let text = match mode.to_str().unwrap() {
            "startup" => child_startup(),
            "sigint-installed" => child_sigint_installed(&report),
            "sigint-ignored" => child_sigint_ignored(),
            "sigint-eintr" => child_sigint_eintr(),
            "sigxfsz" => child_sigxfsz(&scratch, true),
            "sigxfsz-control" => child_sigxfsz(&scratch, false),
            "sigint-restored" => child_sigint_restored(&report),
            "sigpipe-default" => child_sigpipe(&report, false),
            "sigpipe-ignored" => child_sigpipe(&report, true),
            "localtime" => child_localtime(),
            #[cfg(target_os = "linux")]
            "long-cwd" => child_long_cwd(&scratch),
            #[cfg(target_os = "linux")]
            "removed-cwd" => child_removed_cwd(&scratch),
            other => panic!("知らない子の動き: {other}"),
        };
        std::fs::write(report, text).unwrap();
    }

    fn child_startup() -> String {
        let state = startup_state().expect("起動前の記録がある");
        let now = [
            fd_kind(io::stdin()),
            fd_kind(io::stdout()),
            fd_kind(io::stderr()),
        ]
        .map(|kind| kind.map_or("error", FdKind::name))
        .join(" ");
        format!("{} now={now}", state.summary())
    }

    fn child_sigint_installed(report: &Path) -> String {
        let setup = install_python_sigint().unwrap();
        for count in 1..=3 {
            // SAFETY: getpid は自分の番号を返すだけで、kill は引数の値だけを読む。
            assert_eq!(unsafe { libc::kill(libc::getpid(), libc::SIGINT) }, 0);
            wait_until(|| sigint_count() >= count);
        }
        let text = format!(
            "{setup:?} count={} pending={} take={} take={}",
            sigint_count(),
            sigint_pending(),
            take_sigint(),
            take_sigint()
        );
        // 送り直すと戻らないので、先に書いておく。
        std::fs::write(report, &text).unwrap();
        let result = resend_sigint_by_default();
        format!("{text} returned={result:?}")
    }

    /// 置いた扱いを既定へ戻すと、取り出していない印は捨てられ、次の SIGINT で終わる。
    fn child_sigint_restored(report: &Path) -> String {
        let setup = install_python_sigint().unwrap();
        // SAFETY: getpid は自分の番号を返すだけで、kill は引数の値だけを読む。
        assert_eq!(unsafe { libc::kill(libc::getpid(), libc::SIGINT) }, 0);
        wait_until(|| sigint_count() >= 1);
        restore_default_sigint().unwrap();
        let text = format!(
            "{setup:?} pending={} now={:?}",
            sigint_pending(),
            signal_disposition(libc::SIGINT).unwrap()
        );
        std::fs::write(report, &text).unwrap();
        // SAFETY: getpid は自分の番号を返すだけで、kill は引数の値だけを読む。
        unsafe { libc::kill(libc::getpid(), libc::SIGINT) };
        std::thread::sleep(Duration::from_secs(5));
        format!("{text} survived")
    }

    /// 読み手の無いパイプへ書く。既定なら SIGPIPE で終わり、無視なら EPIPE になる。
    fn child_sigpipe(report: &Path, ignored: bool) -> String {
        set_sigpipe_ignored(ignored).unwrap();
        let text = format!("now={:?}", signal_disposition(libc::SIGPIPE).unwrap());
        std::fs::write(report, &text).unwrap();
        let (reader, writer) = io::pipe().unwrap();
        drop(reader);
        let result = write_once(&writer, b"x").map_err(|error| error.raw_os_error());
        format!("{text} result={result:?}")
    }

    fn child_sigint_ignored() -> String {
        let setup = install_python_sigint().unwrap();
        // SAFETY: getpid は自分の番号を返すだけで、kill は引数の値だけを読む。
        assert_eq!(unsafe { libc::kill(libc::getpid(), libc::SIGINT) }, 0);
        std::thread::sleep(Duration::from_millis(100));
        format!(
            "{setup:?} count={} pending={} now={:?}",
            sigint_count(),
            sigint_pending(),
            signal_disposition(libc::SIGINT).unwrap()
        )
    }

    fn child_sigint_eintr() -> String {
        use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
        let setup = install_python_sigint().unwrap();
        let (_reader, mut writer) = io::pipe().unwrap();
        let flags = fcntl_getfl(&writer).unwrap();
        fcntl_setfl(&writer, flags | OFlags::NONBLOCK).unwrap();
        for chunk in [4096, 1] {
            loop {
                match writer.write(&vec![0; chunk]) {
                    Ok(_) => continue,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                    Err(error) => panic!("{error}"),
                }
            }
        }
        fcntl_setfl(&writer, flags).unwrap();
        let target = current_thread_id();
        let done = Arc::new(AtomicBool::new(false));
        let sender = {
            let done = Arc::clone(&done);
            std::thread::spawn(move || {
                while !done.load(Ordering::SeqCst) {
                    // SAFETY: 送り先の thread は、join するまで書き込みの待ちにいる。
                    unsafe { libc::pthread_kill(target as libc::pthread_t, libc::SIGINT) };
                    std::thread::sleep(Duration::from_millis(20));
                }
            })
        };
        let result = writer.write(&[0]);
        done.store(true, Ordering::SeqCst);
        sender.join().unwrap();
        format!(
            "{setup:?} write={:?} received={} take={}",
            result.map_err(|error| error.kind()),
            sigint_count() > 0,
            take_sigint()
        )
    }

    fn child_sigxfsz(scratch: &Path, ignore: bool) -> String {
        if ignore {
            ignore_sigxfsz().unwrap();
        }
        let mut file = std::fs::File::create(scratch.join("limited")).unwrap();
        // 上限は 64 バイト(報告のファイルが収まる大きさ)。上限を超える書き込みは EFBIG で止まる。
        let result = file
            .write_all(&[b'x'; 100])
            .map_err(|error| error.raw_os_error());
        format!("result={result:?}")
    }

    fn child_localtime() -> String {
        let mut lines = [0, -1, 951_782_400, 1_690_000_000, 1_700_000_000]
            .map(|seconds| {
                let time = local_time(seconds).unwrap();
                format!(
                    "{seconds}={:04}-{:02}-{:02} {:02}:{:02}:{:02}",
                    time.year, time.month, time.day, time.hour, time.minute, time.second
                )
            })
            .to_vec();
        let overflow = local_time(i64::MAX).map_err(|error| error.raw_os_error().unwrap_or(0));
        lines.push(format!(
            "overflow={}",
            overflow.is_err_and(|errno| errno != 0)
        ));
        lines.join("\n")
    }

    #[cfg(target_os = "linux")]
    fn errno_of(result: &io::Result<PathBuf>) -> String {
        match result {
            Ok(path) => format!("ok:{}", path.as_os_str().len()),
            Err(error) => format!("err:{}", error.raw_os_error().unwrap_or(0)),
        }
    }

    /// 4096 バイトを超える現在地。祖先の権限を変えた場合も、glibc(gnu の標準ライブラリ)と比べる。
    #[cfg(target_os = "linux")]
    fn child_long_cwd(scratch: &Path) -> String {
        use std::os::unix::fs::PermissionsExt;
        let base = scratch.join("long");
        std::fs::create_dir(&base).unwrap();
        std::env::set_current_dir(&base).unwrap();
        let name = "d".repeat(200);
        let mut expected = base.clone().into_os_string();
        for _ in 0..25 {
            std::fs::create_dir(&name).unwrap();
            std::env::set_current_dir(&name).unwrap();
            expected.push("/");
            expected.push(&name);
        }
        let mut lines = Vec::new();
        let ours = current_dir();
        lines.push(format!(
            "plain={} same={} over={}",
            errno_of(&ours),
            ours.as_ref().is_ok_and(|path| path.as_os_str() == expected),
            expected.len() > 4096
        ));
        if cfg!(target_env = "gnu") {
            lines.push(format!(
                "glibc={}",
                errno_of(&std::env::current_dir()) == errno_of(&ours)
            ));
        }
        for mode in [0o311, 0o611] {
            std::fs::set_permissions(&base, std::fs::Permissions::from_mode(mode)).unwrap();
            // 権限を越えられる利用者(root など)では、どちらも成功する。
            let privileged = match mode {
                0o311 => std::fs::read_dir(&base).is_ok(),
                _ => std::fs::metadata(base.join(&name)).is_ok(),
            };
            let ours = current_dir();
            let mut line = format!("mode={mode:o} {} privileged={privileged}", errno_of(&ours));
            if cfg!(target_env = "gnu") {
                line.push_str(&format!(
                    " glibc={}",
                    errno_of(&std::env::current_dir()) == errno_of(&ours)
                ));
            }
            lines.push(line);
            std::fs::set_permissions(&base, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        std::env::set_current_dir(scratch).unwrap();
        lines.join("\n")
    }

    #[cfg(target_os = "linux")]
    fn child_removed_cwd(scratch: &Path) -> String {
        let gone = scratch.join("gone");
        std::fs::create_dir(&gone).unwrap();
        std::env::set_current_dir(&gone).unwrap();
        std::fs::remove_dir(&gone).unwrap();
        let ours = current_dir();
        let mut text = errno_of(&ours);
        if cfg!(target_env = "gnu") {
            text.push_str(&format!(
                " glibc={}",
                errno_of(&std::env::current_dir()) == errno_of(&ours)
            ));
        }
        std::env::set_current_dir(scratch).unwrap();
        text
    }

    #[test]
    fn the_record_is_written_once_and_absent_before_writing() {
        let record = Record::new();
        assert_eq!(record.read(), None);
        let probe = |getfd: i32, kind: FdKind, python: i32| {
            move |_: c_int| FdProbe {
                getfd_errno: getfd,
                kind,
                python_errno: python,
            }
        };
        record.write_with(probe(libc::EBADF, FdKind::Unknown, libc::EBADF), |signal| {
            if signal == libc::SIGPIPE {
                Ok(Disposition::Ignore)
            } else {
                Err(io::Error::from_raw_os_error(libc::EINVAL))
            }
        });
        let first = record.read().unwrap();
        assert_eq!(
            first.summary(),
            "closed/invalid:9 closed/invalid:9 closed/invalid:9 sigpipe=ignore sigint=unread"
        );
        assert!(
            first
                .fds
                .iter()
                .all(|fd| fd.was_closed() && !fd.python_valid())
        );
        // 2 回目は何も書かない。
        record.write_with(probe(0, FdKind::Terminal, 0), |_| Ok(Disposition::Default));
        assert_eq!(record.read(), Some(first));
        // EBADF 以外の F_GETFD の誤りと、開いた fd の Python の判定の失敗は分けて残す。
        let other = Record::new();
        other.write_with(probe(5, FdKind::Unknown, 5), |_| Ok(Disposition::Handler));
        assert_eq!(
            other.read().unwrap().summary(),
            "failed:5/invalid:5 failed:5/invalid:5 failed:5/invalid:5 sigpipe=handler sigint=handler"
        );
        let open = Record::new();
        open.write_with(probe(0, FdKind::Socket, 24), |_| Ok(Disposition::Default));
        let state = open.read().unwrap();
        assert_eq!(state.fds[0].state, FdState::Open(FdKind::Socket));
        assert!(!state.fds[0].was_closed() && !state.fds[0].python_valid());
    }

    #[test]
    fn this_test_binary_records_before_main() {
        // 一覧の項目を置いたので、main の前に記録されている。2 回目の呼出しは何も変えない。
        let before = startup_state().expect("起動前の記録がある");
        record_startup_state();
        assert_eq!(startup_state(), Some(before));
    }

    #[test]
    fn the_record_precedes_the_standard_library() {
        let scratch = Scratch::new("startup");
        let output = std::fs::File::create(scratch.0.join("stdout")).unwrap();
        let directory = std::fs::File::open(&scratch.0).unwrap();
        type Setup = fn(&mut Command, &std::fs::File, &std::fs::File);
        type Case = (
            &'static str,
            &'static [c_int],
            &'static [c_int],
            Setup,
            &'static str,
        );
        let cases: [Case; 9] = [
            (
                "open",
                &[],
                &[],
                |command, output, _| {
                    command.stdout(output.try_clone().unwrap());
                },
                "chardev/valid regular/valid chardev/valid sigpipe=default sigint=default now=chardev regular chardev",
            ),
            (
                "fifo",
                &[],
                &[],
                |command, _, _| {
                    command.stdin(Stdio::piped()).stderr(Stdio::piped());
                },
                "fifo/valid chardev/valid fifo/valid sigpipe=default sigint=default now=fifo chardev fifo",
            ),
            (
                "closed0",
                &[0],
                &[],
                |_, _, _| {},
                "closed/invalid:9 chardev/valid chardev/valid sigpipe=default sigint=default now=chardev chardev chardev",
            ),
            (
                "closed1",
                &[1],
                &[],
                |_, _, _| {},
                "chardev/valid closed/invalid:9 chardev/valid sigpipe=default sigint=default now=chardev chardev chardev",
            ),
            (
                "closed2",
                &[2],
                &[],
                |_, _, _| {},
                "chardev/valid chardev/valid closed/invalid:9 sigpipe=default sigint=default now=chardev chardev chardev",
            ),
            (
                "closed12",
                &[1, 2],
                &[],
                |_, _, _| {},
                "chardev/valid closed/invalid:9 closed/invalid:9 sigpipe=default sigint=default now=chardev chardev chardev",
            ),
            (
                "closed012",
                &[0, 1, 2],
                &[],
                |_, _, _| {},
                "closed/invalid:9 closed/invalid:9 closed/invalid:9 sigpipe=default sigint=default now=chardev chardev chardev",
            ),
            (
                "directory1",
                &[],
                &[],
                |command, _, directory| {
                    command.stdout(directory.try_clone().unwrap());
                },
                "chardev/valid directory/valid chardev/valid sigpipe=default sigint=default now=chardev directory chardev",
            ),
            (
                "ignored",
                &[],
                &[libc::SIGPIPE, libc::SIGINT],
                |_, _, _| {},
                "chardev/valid chardev/valid chardev/valid sigpipe=ignore sigint=ignore now=chardev chardev chardev",
            ),
        ];
        for (name, close_fds, ignore, setup, expected) in cases {
            let (mut command, report) = child_command("startup", &scratch);
            setup(&mut command, &output, &directory);
            prepare(&mut command, close_fds, ignore, &[]);
            let status = command.spawn().unwrap().wait().unwrap();
            assert!(status.success(), "{name}: {status}");
            assert_eq!(read_report(&report), expected, "{name}");
        }
    }

    #[test]
    fn sigint_is_counted_and_resent_with_the_default_disposition() {
        let scratch = Scratch::new("sigint");
        let (mut command, report) = child_command("sigint-installed", &scratch);
        prepare(&mut command, &[], &[], &[]);
        let status = command.status().unwrap();
        assert_eq!(status.signal(), Some(libc::SIGINT), "{status}");
        assert_eq!(
            read_report(&report),
            "Installed count=3 pending=true take=true take=false"
        );
    }

    #[test]
    fn restoring_sigint_drops_the_pending_mark_and_restores_the_default() {
        let scratch = Scratch::new("sigint-restored");
        let (mut command, report) = child_command("sigint-restored", &scratch);
        prepare(&mut command, &[], &[], &[]);
        let status = command.status().unwrap();
        assert_eq!(status.signal(), Some(libc::SIGINT), "{status}");
        assert_eq!(read_report(&report), "Installed pending=false now=Default");
    }

    #[test]
    fn sigpipe_can_be_returned_to_the_inherited_default() {
        let scratch = Scratch::new("sigpipe");
        let (mut command, report) = child_command("sigpipe-default", &scratch);
        prepare(&mut command, &[], &[], &[]);
        let status = command.status().unwrap();
        assert_eq!(status.signal(), Some(libc::SIGPIPE), "{status}");
        assert_eq!(read_report(&report), "now=Default");
        let (mut command, report) = child_command("sigpipe-ignored", &scratch);
        prepare(&mut command, &[], &[], &[]);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        assert_eq!(read_report(&report), "now=Ignore result=Err(Some(32))");
    }

    #[test]
    fn an_ignored_sigint_stays_ignored() {
        let scratch = Scratch::new("sigint-ignored");
        let (mut command, report) = child_command("sigint-ignored", &scratch);
        prepare(&mut command, &[], &[libc::SIGINT], &[]);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        assert_eq!(
            read_report(&report),
            "Ignored count=0 pending=false now=Ignore"
        );
    }

    #[test]
    fn sigint_interrupts_a_blocked_write_without_restart() {
        let scratch = Scratch::new("sigint-eintr");
        let (mut command, report) = child_command("sigint-eintr", &scratch);
        prepare(&mut command, &[], &[], &[]);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        assert_eq!(
            read_report(&report),
            "Installed write=Err(Interrupted) received=true take=true"
        );
    }

    #[test]
    fn an_ignored_sigxfsz_turns_the_limit_into_efbig() {
        let scratch = Scratch::new("sigxfsz");
        let limits: &'static [(Resource, u64)] =
            &[(libc::RLIMIT_FSIZE, 64), (libc::RLIMIT_CORE, 0)];
        let (mut command, report) = child_command("sigxfsz", &scratch);
        prepare(&mut command, &[], &[], limits);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        assert_eq!(
            read_report(&report),
            format!("result=Err(Some({}))", libc::EFBIG)
        );
        // 無視しなければ SIGXFSZ で終わる(上の確かめが意味を持つことの対照)。
        let (mut command, report) = child_command("sigxfsz-control", &scratch);
        command.current_dir(&scratch.0);
        prepare(&mut command, &[], &[], limits);
        let status = command.status().unwrap();
        assert_eq!(status.signal(), Some(libc::SIGXFSZ), "{status}");
        assert!(!report.exists());
    }

    #[test]
    fn local_time_follows_tz_like_python() {
        let scratch = Scratch::new("localtime");
        let cases = [
            (
                "UTC0",
                "0=1970-01-01 00:00:00\n-1=1969-12-31 23:59:59\n951782400=2000-02-29 00:00:00\n1690000000=2023-07-22 04:26:40\n1700000000=2023-11-14 22:13:20",
            ),
            (
                "JST-9",
                "0=1970-01-01 09:00:00\n-1=1970-01-01 08:59:59\n951782400=2000-02-29 09:00:00\n1690000000=2023-07-22 13:26:40\n1700000000=2023-11-15 07:13:20",
            ),
            (
                "EST5EDT,M3.2.0,M11.1.0",
                "0=1969-12-31 19:00:00\n-1=1969-12-31 18:59:59\n951782400=2000-02-28 19:00:00\n1690000000=2023-07-22 00:26:40\n1700000000=2023-11-14 17:13:20",
            ),
        ];
        for (tz, expected) in cases {
            let (mut command, report) = child_command("localtime", &scratch);
            command.env("TZ", tz);
            let status = command.status().unwrap();
            assert!(status.success(), "{tz}: {status}");
            assert_eq!(
                read_report(&report),
                format!("{expected}\noverflow=true"),
                "{tz}"
            );
        }
    }

    #[test]
    fn utc_time_matches_the_proleptic_gregorian_calendar() {
        let civil = |seconds| {
            let time = utc_time(seconds);
            format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
                time.year, time.month, time.day, time.hour, time.minute, time.second
            )
        };
        for (seconds, expected) in [
            (0, "1970-01-01T00:00:00"),
            (-1, "1969-12-31T23:59:59"),
            (951_782_400, "2000-02-29T00:00:00"),
            (951_868_799, "2000-02-29T23:59:59"),
            (951_868_800, "2000-03-01T00:00:00"),
            (4_107_542_400, "2100-03-01T00:00:00"),
            (1_700_000_000, "2023-11-14T22:13:20"),
            (-62_135_596_800, "0001-01-01T00:00:00"),
            (253_402_300_799, "9999-12-31T23:59:59"),
        ] {
            assert_eq!(civil(seconds), expected, "{seconds}");
        }
        // 1 日ずつ進めて、日付が飛ばず戻らないこと(閏年と 100 年・400 年の例外を含む)。
        let mut previous = utc_time(-86_400 * 800_000);
        for day in -799_999..800_000_i64 {
            let next = utc_time(day * 86_400);
            let expected_next = if next.month == previous.month {
                (previous.year, previous.month, previous.day + 1)
            } else if next.year == previous.year {
                (previous.year, previous.month + 1, 1)
            } else {
                (previous.year + 1, 1, 1)
            };
            assert_eq!((next.year, next.month, next.day), expected_next, "{day}");
            let leap = |year: i64| year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
            if next.day == 1 {
                let length = match previous.month {
                    2 if leap(previous.year) => 29,
                    2 => 28,
                    4 | 6 | 9 | 11 => 30,
                    _ => 31,
                };
                assert_eq!(previous.day, length, "{day}");
            }
            previous = next;
        }
    }

    #[test]
    fn fd_kinds_and_terminal_width() {
        let scratch = Scratch::new("kinds");
        let file = std::fs::File::create(scratch.0.join("file")).unwrap();
        let directory = std::fs::File::open(&scratch.0).unwrap();
        let (reader, writer) = io::pipe().unwrap();
        let (left, _right) = std::os::unix::net::UnixStream::pair().unwrap();
        let null = std::fs::File::open("/dev/null").unwrap();
        assert_eq!(fd_kind(&file).unwrap(), FdKind::RegularFile);
        assert_eq!(fd_kind(&directory).unwrap(), FdKind::Directory);
        assert_eq!(fd_kind(&reader).unwrap(), FdKind::Fifo);
        assert_eq!(fd_kind(&writer).unwrap(), FdKind::Fifo);
        assert_eq!(fd_kind(&left).unwrap(), FdKind::Socket);
        assert_eq!(fd_kind(&null).unwrap(), FdKind::CharacterDevice);
        for kind in FdKind::ALL {
            assert_eq!(FdKind::from_code(kind.code()), kind);
        }
        assert_eq!(terminal_columns(&file), None);
        assert_eq!(terminal_columns(&writer), None);
        assert_eq!(terminal_columns(&null), None);
        for fd in [
            file.as_fd(),
            directory.as_fd(),
            reader.as_fd(),
            null.as_fd(),
        ] {
            assert!(!is_terminal(fd));
        }
        // 疑似端末の親の側も端末(Linux は tcgetattr、Mac は isatty が成功する)。
        let master = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open("/dev/ptmx")
            .unwrap();
        assert!(is_terminal(&master));
        assert_eq!(fd_kind(&master).unwrap(), FdKind::Terminal);
        // 1 回の write は書けた数を返し、溜めの大きさは 1 より大きい。
        assert_eq!(write_once(&writer, b"abc").unwrap(), 3);
        assert!(block_size(&writer).unwrap() > 1);
        assert!(block_size(&file).unwrap() > 1);
        // 読み手の無いパイプの EPIPE は、別の子の中で確かめる(この試験の process は、ほかの試験が
        // 起動する子へ fd を一時的に継ぐことがあるため)。
        // 端末の幅を取れる fd は、手元の端末に依る。取れたなら fd は端末である。
        if let Some(columns) = stdout_terminal_columns() {
            assert_eq!(
                fd_kind(io::stdout()).unwrap(),
                FdKind::Terminal,
                "{columns}"
            );
        }
    }

    #[test]
    fn thread_ids_are_stable_and_distinct() {
        let here = current_thread_id();
        assert_ne!(here, 0);
        assert_eq!(current_thread_id(), here);
        let other = std::thread::spawn(current_thread_id).join().unwrap();
        assert_ne!(other, 0);
        assert_ne!(other, here);
    }

    #[test]
    fn the_current_directory_matches_the_standard_library() {
        assert_eq!(current_dir().unwrap(), std::env::current_dir().unwrap());
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::ffi::OsStrExt;
            let walked = getcwd_by_walking().unwrap();
            assert_eq!(walked, current_dir().unwrap().as_os_str().as_bytes());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_long_current_directory_is_found_by_walking() {
        let scratch = Scratch::new("long-cwd");
        let (mut command, report) = child_command("long-cwd", &scratch);
        command.current_dir(&scratch.0);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        let text = read_report(&report);
        let lines: Vec<&str> = text.lines().collect();
        let length = scratch.0.join("long").as_os_str().len() + 25 * 201;
        assert_eq!(lines[0], format!("plain=ok:{length} same=true over=true"));
        let rest = if cfg!(target_env = "gnu") {
            assert_eq!(lines[1], "glibc=true");
            &lines[2..]
        } else {
            &lines[1..]
        };
        // 読めない祖先は EACCES、検索できない祖先は名前を見つけられず ENOENT(glibc と同じ)。
        assert_eq!(rest.len(), 2, "{text}");
        for (line, mode, errno) in [(rest[0], "311", 13), (rest[1], "611", 2)] {
            let expected = if line.contains("privileged=true") {
                format!("mode={mode} ok:{length} privileged=true")
            } else {
                format!("mode={mode} err:{errno} privileged=false")
            };
            let shown = line.strip_suffix(" glibc=true").unwrap_or(line);
            assert_eq!(shown, expected, "{line}");
            if cfg!(target_env = "gnu") {
                assert!(line.ends_with(" glibc=true"), "{line}");
            }
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_removed_current_directory_is_enoent() {
        let scratch = Scratch::new("removed-cwd");
        let (mut command, report) = child_command("removed-cwd", &scratch);
        command.current_dir(&scratch.0);
        let status = command.status().unwrap();
        assert!(status.success(), "{status}");
        let expected = if cfg!(target_env = "gnu") {
            "err:2 glibc=true"
        } else {
            "err:2"
        };
        assert_eq!(read_report(&report), expected);
    }
}
