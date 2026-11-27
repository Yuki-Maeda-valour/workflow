//! 旧`subprocess`(Python 3.10)と旧bashと同じ子の実行。
//!
//! 子は`fork`と`execve`で起動する。`std::process::Command`は子の信号のmaskを空にし、
//! 継いだfdを閉じず、PATHの探索の規則も旧版と違うので使わない。
//! forkの後の子は、確保もpanicもせず、libcの呼出しだけで`execve`か`_exit`へ進む。
//!
//! - 旧Python: 標準入力`/dev/null`・標準出力と標準エラーのpipe・3以上のfdを閉じる・
//!   `SIGPIPE`と`SIGXFSZ`を既定へ戻す・関数を登録した信号を既定へ戻す・maskは継ぐ・
//!   PATHの各候補へ順に`execve`する。回収できない子(`ECHILD`)は終了値0とする。
//! - 旧bash: 0〜2は呼出側の指定・ほかの継いだfdはそのまま渡す・全部の信号を起動時の扱い
//!   ([`BashSignals`])へ戻す・maskは継ぐ・探索は呼出側が[`bash_find_command`]で先に決める。
//!   `execve`の失敗は、子からエラー番号だけを受け取り、[`bash_exec_failure`]で旧bashの経路を決める。

use super::startup::Disposition;
use super::{Errno, last_errno};
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int};
use std::time::{Duration, Instant};

// ---------------------------------------------------------------- 信号

/// 信号の数(Linuxは`_NSIG`、Macは`NSIG`)。
#[cfg(target_os = "linux")]
const SIGNAL_LIMIT: c_int = 65;
#[cfg(not(target_os = "linux"))]
const SIGNAL_LIMIT: c_int = 32;
const _: () = assert!(SIGNAL_LIMIT as usize == super::startup::SIGNAL_SLOTS);

fn disposition_of(handler: libc::sighandler_t) -> Disposition {
    match handler {
        libc::SIG_DFL => Disposition::Default,
        libc::SIG_IGN => Disposition::Ignore,
        _ => Disposition::Handler,
    }
}

/// 信号の今の扱いを読む。
pub fn disposition(signal: c_int) -> Result<Disposition, Errno> {
    // SAFETY: zeroedのsigactionは出力先として有効である。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい扱いにnullを渡すので、読むだけで変えない。
    if unsafe { libc::sigaction(signal, std::ptr::null(), &mut old) } != 0 {
        return Err(last_errno());
    }
    Ok(disposition_of(old.sa_sigaction))
}

/// 信号の扱いを既定か無視にして、前の扱いを返す。関数を登録する扱いは受けない。
///
/// 旧Pythonの起動時の`SIGPIPE`・`SIGXFSZ`の無視や、旧bashの継いだ`SIGPIPE`への戻しに使う。
pub fn set_disposition(signal: c_int, to: Disposition) -> Result<Disposition, Errno> {
    let handler = match to {
        Disposition::Default => libc::SIG_DFL,
        Disposition::Ignore => libc::SIG_IGN,
        Disposition::Handler => return Err(Errno::INVAL),
    };
    // SAFETY: zeroedのsigactionに既定か無視だけを入れる。関数を登録しないので、
    // 信号の中で動くコードは増えない。
    let mut new: libc::sigaction = unsafe { std::mem::zeroed() };
    new.sa_sigaction = handler;
    // SAFETY: zeroedのsigactionは出力先として有効である。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 有効なsigactionの組を渡す。
    if unsafe { libc::sigaction(signal, &new, &mut old) } != 0 {
        return Err(last_errno());
    }
    Ok(disposition_of(old.sa_sigaction))
}

/// 旧bashが起動時に覚える信号の扱いの表(`restore_original_signals`の元)。
///
/// 旧bashは、自分が変えた信号(関数の登録・`SIGCHLD`)を、外部コマンドの子で起動時の扱いへ戻す。
/// 起動時に無視だった信号は、子でも無視のままになる。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BashSignals {
    original: [libc::sighandler_t; SIGNAL_LIMIT as usize],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BashSignalsError {
    /// 起動前の記録が無い(`SIGPIPE`の起動時の扱いが分からない)。
    Startup(super::startup::StartupError),
    /// 親の`SIGCHLD`を既定へ戻せなかった。
    Os(Errno),
}

impl BashSignals {
    /// 表を作り、親の`SIGCHLD`を既定へ戻す。1回目の結果を覚え、2回目からはそれを返す。
    ///
    /// 表は起動前の記録([`super::startup::recorded_disposition`])だけから作るので、呼ぶ前に入口が
    /// 信号の扱いを変えても(Pythonと同じ起動の処理で`SIGXFSZ`を無視にするなど)左右されない。
    /// 起動前に関数が登録されていた扱いは`execve`を越えて継げないので、既定とみなす。
    /// libcが扱いを読ませない内部用の信号は既定とみなし、子でも設定を試みない(設定は失敗する)。
    /// 親の`SIGCHLD`が無視なら既定へ戻す。無視で起動しても子の終了値を受け取るため
    /// (旧bashは`SIGCHLD`へ自分の関数を登録する)。
    pub fn capture_and_prepare() -> Result<Self, BashSignalsError> {
        static CAPTURED: std::sync::OnceLock<Result<BashSignals, BashSignalsError>> =
            std::sync::OnceLock::new();
        CAPTURED
            .get_or_init(|| {
                let mut original = [libc::SIG_DFL; SIGNAL_LIMIT as usize];
                for signal in 1..SIGNAL_LIMIT {
                    let recorded = super::startup::recorded_disposition(signal)
                        .map_err(BashSignalsError::Startup)?;
                    if recorded == Some(Disposition::Ignore) {
                        original[signal as usize] = libc::SIG_IGN;
                    }
                }
                if disposition(libc::SIGCHLD) == Ok(Disposition::Ignore) {
                    set_disposition(libc::SIGCHLD, Disposition::Default)
                        .map_err(BashSignalsError::Os)?;
                }
                Ok(Self { original })
            })
            .clone()
    }

    /// 起動時の扱い(既定か無視)。範囲の外の番号は既定。
    pub fn original(&self, signal: c_int) -> Disposition {
        match usize::try_from(signal)
            .ok()
            .and_then(|index| self.original.get(index))
        {
            Some(&libc::SIG_IGN) => Disposition::Ignore,
            _ => Disposition::Default,
        }
    }
}

// ---------------------------------------------------------------- EXITの罠を置いた旧bashの信号

/// 受けた終わらせる信号(0は無し。旧bashの`terminating_signal`)。
static BASH_TERMINATING: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);
/// 前景の子を待っている間(旧bashの`waiting_for_child`。この間の`SIGINT`は`wait_sigint_handler`が受ける)。
static BASH_WAITING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// 子を待つ間に受けた`SIGINT`(旧bashの`wait_sigint_received`)。
static BASH_WAIT_SIGINT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// 終わらせる信号を処理している間(旧bashの`handling_termsig`)。この間に来た終わらせる信号は、
/// 信号の関数の中で直ちにシェルを終える(旧bashの`termsig_sighandler`の`kill_shell`)。
static BASH_HANDLING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// 旧bashの`terminating_signals`(EXITの罠を置いた非対話のbashが関数を置く信号)。
#[cfg(target_os = "linux")]
pub const BASH_TERMINATING_SIGNALS: &[c_int] = &[
    libc::SIGHUP,
    libc::SIGINT,
    libc::SIGILL,
    libc::SIGTRAP,
    libc::SIGABRT,
    libc::SIGFPE,
    libc::SIGBUS,
    libc::SIGSEGV,
    libc::SIGSYS,
    libc::SIGPIPE,
    libc::SIGALRM,
    libc::SIGTERM,
    libc::SIGXCPU,
    libc::SIGXFSZ,
    libc::SIGVTALRM,
    libc::SIGUSR1,
    libc::SIGUSR2,
];
#[cfg(not(target_os = "linux"))]
pub const BASH_TERMINATING_SIGNALS: &[c_int] = &[
    libc::SIGHUP,
    libc::SIGINT,
    libc::SIGILL,
    libc::SIGTRAP,
    libc::SIGABRT,
    libc::SIGEMT,
    libc::SIGFPE,
    libc::SIGBUS,
    libc::SIGSEGV,
    libc::SIGSYS,
    libc::SIGPIPE,
    libc::SIGALRM,
    libc::SIGTERM,
    libc::SIGXCPU,
    libc::SIGXFSZ,
    libc::SIGVTALRM,
    libc::SIGUSR1,
    libc::SIGUSR2,
];

/// 2回目で直ちに終わる信号(旧bashの`termsig_sighandler`の、例外の一覧に無い信号)。
fn bash_fatal_twice(signal: c_int) -> bool {
    !matches!(
        signal,
        libc::SIGHUP
            | libc::SIGINT
            | libc::SIGPIPE
            | libc::SIGALRM
            | libc::SIGTERM
            | libc::SIGXCPU
            | libc::SIGXFSZ
            | libc::SIGVTALRM
            | libc::SIGUSR1
            | libc::SIGUSR2
    )
}

/// 信号の関数の中の`kill_shell`: 既定へ戻し、maskから外して自分へ送る(信号の中で安全な呼出しだけ)。
fn kill_shell_now(signal: c_int) {
    // SAFETY: sigaction・sigprocmask・kill・getpid・_exitは、信号の中で呼べる。
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = libc::SIG_DFL;
        libc::sigaction(signal, &action, std::ptr::null_mut());
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, signal);
        libc::sigprocmask(libc::SIG_UNBLOCK, &set, std::ptr::null_mut());
        libc::kill(libc::getpid(), signal);
        libc::_exit(128 + signal);
    }
}

extern "C" fn bash_termsig_handler(signal: c_int) {
    use std::sync::atomic::Ordering;
    if BASH_HANDLING.load(Ordering::SeqCst) {
        // 旧bashの termsig_sighandler: 終わらせる信号を処理している間の2つ目の信号は、直ちに終える。
        kill_shell_now(signal);
        return;
    }
    if signal == libc::SIGINT && BASH_WAITING.load(Ordering::SeqCst) {
        BASH_WAIT_SIGINT.store(true, Ordering::SeqCst);
        return;
    }
    if bash_fatal_twice(signal) && BASH_TERMINATING.load(Ordering::SeqCst) == signal {
        // 同じ信号が処理の前にもう一度来た(本物の故障など)。既定へ戻して返り、もう一度で終わる。
        // SAFETY: zeroedのsigactionに既定だけを入れる。信号の中で安全な呼出し。
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = libc::SIG_DFL;
            libc::sigaction(signal, &action, std::ptr::null_mut());
        }
        return;
    }
    BASH_TERMINATING.store(signal, Ordering::SeqCst);
}

/// 旧bashの`initialize_terminating_signals`(非対話のbashが`trap ... EXIT`を置いた時点)。
///
/// 起動時に無視だった信号は無視のまま。ほかは、受けた信号を覚える関数を`SA_RESTART`なしで置く
/// (子を待つ`waitpid`などが`EINTR`で戻り、旧bashと同じ所で信号を確かめられる)。
pub fn install_bash_exit_trap_signals(signals: &BashSignals) -> Result<(), Errno> {
    for &signal in BASH_TERMINATING_SIGNALS {
        if signals.original(signal) == Disposition::Ignore {
            set_disposition(signal, Disposition::Ignore)?;
            continue;
        }
        // SAFETY: zeroedのsigactionに、原子的な書込みだけをする関数と空のmaskを入れる。
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = bash_termsig_handler as extern "C" fn(c_int) as libc::sighandler_t;
        // SAFETY: sa_maskは有効な集合を指す。
        unsafe { libc::sigemptyset(&mut action.sa_mask) };
        // SAFETY: 有効なsigactionを渡す。
        if unsafe { libc::sigaction(signal, &action, std::ptr::null_mut()) } != 0 {
            return Err(last_errno());
        }
    }
    Ok(())
}

/// 旧bashの`termsig_handler`の始め: 処理中の印を立て、受けた信号を下ろす(`terminating_signal = 0`)。
/// この後に来る終わらせる信号は、信号の関数の中でシェルを終える。
pub fn bash_begin_termsig() {
    use std::sync::atomic::Ordering;
    BASH_TERMINATING.store(0, Ordering::SeqCst);
    BASH_HANDLING.store(true, Ordering::SeqCst);
}

/// 受けた終わらせる信号を読む(旧bashの`CHECK_TERMSIG`の判定。下ろさない)。
pub fn bash_terminating_signal() -> Option<c_int> {
    match BASH_TERMINATING.load(std::sync::atomic::Ordering::SeqCst) {
        0 => None,
        signal => Some(signal),
    }
}

/// 前景の子を待ち始める・待ち終える(旧bashの`waiting_for_child`)。終えるときは、待つ間に受けた
/// `SIGINT`の有無を返して下ろす。
pub fn bash_set_waiting(waiting: bool) -> bool {
    use std::sync::atomic::Ordering;
    BASH_WAITING.store(waiting, Ordering::SeqCst);
    if waiting {
        false
    } else {
        BASH_WAIT_SIGINT.swap(false, Ordering::SeqCst)
    }
}

/// 子を待つ間に`SIGINT`を受けたか(旧bashの`wait_sigint_received`。下ろさない)。
pub fn bash_wait_sigint_received() -> bool {
    BASH_WAIT_SIGINT.load(std::sync::atomic::Ordering::SeqCst)
}

/// 旧bashの`waitchld`の`waitpid(-1, &status, 0)`: どれかの子を1つ、終わった順に回収する。
/// 番号と生の終わり方を返す([`Child::record_reaped`]で子へ渡す)。信号で中断したら`Err(INTR)`。
pub fn bash_wait_any() -> Result<(libc::pid_t, c_int), Errno> {
    let mut raw = 0;
    // SAFETY: 有効な出力先を渡す。自分の子だけを回収する。
    let pid = unsafe { libc::waitpid(-1, &mut raw, 0) };
    if pid < 0 {
        return Err(last_errno());
    }
    Ok((pid, raw))
}

/// 子を待つ間に受けた`SIGINT`を、終わらせる信号として扱う(子が`SIGINT`で終わったとき)。
pub fn bash_raise_terminating(signal: c_int) {
    BASH_TERMINATING.store(signal, std::sync::atomic::Ordering::SeqCst);
}

/// プロセスのスレッドの数(Linux は`/proc/self/stat`の`num_threads`、Mac は`proc_pidinfo`の
/// `pti_threadnum`)。
#[cfg(target_os = "linux")]
fn thread_count() -> Result<i64, Errno> {
    let text = std::fs::read("/proc/self/stat")
        .map_err(|error| Errno::from_raw_os_error(error.raw_os_error().unwrap_or(libc::EIO)))?;
    num_threads_from_stat(&text).ok_or(Errno::IO)
}

/// `/proc/<pid>/stat`の`num_threads`(20番目の欄)。名前の欄は括弧の中に空白や括弧を含みうるので、
/// 最後の`) `の後から数える(状態が1つ目、num_threads は18個目)。
#[cfg(any(target_os = "linux", test))]
fn num_threads_from_stat(text: &[u8]) -> Option<i64> {
    let close = text.windows(2).rposition(|pair| pair == b") ")?;
    let field = text[close + 2..].split(|byte| *byte == b' ').nth(17)?;
    std::str::from_utf8(field).ok()?.trim().parse().ok()
}

#[cfg(target_vendor = "apple")]
fn thread_count() -> Result<i64, Errno> {
    // SAFETY: zeroedのproc_taskinfoは出力先として有効で、大きさを正しく渡す。
    unsafe {
        let mut info: libc::proc_taskinfo = std::mem::zeroed();
        let size = std::mem::size_of::<libc::proc_taskinfo>() as c_int;
        let written = libc::proc_pidinfo(
            libc::getpid(),
            libc::PROC_PIDTASKINFO,
            0,
            (&mut info as *mut libc::proc_taskinfo).cast(),
            size,
        );
        if written != size {
            return Err(last_errno());
        }
        Ok(i64::from(info.pti_threadnum))
    }
}

#[cfg(not(any(target_os = "linux", target_vendor = "apple")))]
fn thread_count() -> Result<i64, Errno> {
    Err(Errno::NOSYS)
}

/// 旧bashのサブシェル(コマンド置換)と、診断だけを書く子を作る`fork`。`Ok(None)`は子の側。
///
/// 子は親の状態の写しで続き(確保もする)、[`bash_exit_subshell`]で終える。子では受けた信号の印を
/// 下ろす([`bash_subshell_reset`])。
///
/// 1つのスレッドで動く旧bashの再現(reviews-dir の経路)だけが使う包み。呼出側はスレッドを作らない
/// (ほかのスレッドが確保の鍵を持ったまま写ると、子が止まりうる)。この関数を platform の外で呼ぶのは
/// `reviews_dir/shell.rs` だけ(`scripts/test_native_r4g.py` が呼出元を確かめる)。スレッドの数を読める
/// とき(Linux の`/proc`・Mac の`proc_pidinfo`)は確かめ、2つ以上なら`EAGAIN`を返す(forkしない)。
/// 読めないとき(`/proc`の無い環境)は確かめずに進む(旧bashと同じく`/proc`に頼らない)。
pub fn bash_fork_subshell() -> Result<Option<Child>, Errno> {
    if let Ok(count) = thread_count()
        && count != 1
    {
        return Err(Errno::AGAIN);
    }
    // SAFETY: 呼出側(reviews-dir の経路)はスレッドを作らない1つのスレッドのプロセスで、数を読めたときは
    // 直前に確かめた。子は写した状態で続く。
    let pid = unsafe { libc::fork() };
    match pid {
        -1 => Err(last_errno()),
        0 => Ok(None),
        pid => Ok(Some(Child {
            pid,
            status: None,
            core: false,
            flavor: Flavor::Bash,
        })),
    }
}

/// サブシェルの始め: 受けた信号の印と処理中の印を下ろし、`SIGQUIT`を起動時の扱いへ戻す
/// (旧bashの`reset_signal_handlers`。終わらせる信号の関数は残る)。
pub fn bash_subshell_reset(signals: &BashSignals) {
    use std::sync::atomic::Ordering;
    BASH_TERMINATING.store(0, Ordering::SeqCst);
    BASH_HANDLING.store(false, Ordering::SeqCst);
    BASH_WAITING.store(false, Ordering::SeqCst);
    BASH_WAIT_SIGINT.store(false, Ordering::SeqCst);
    let _ = set_disposition(libc::SIGQUIT, signals.original(libc::SIGQUIT));
}

/// 旧bashの`initialize_shell_signals`の`SIGQUIT`: シェル自身は無視する(子は起動時の扱いへ戻す)。
pub fn bash_ignore_sigquit() -> Result<(), Errno> {
    set_disposition(libc::SIGQUIT, Disposition::Ignore).map(|_| ())
}

/// サブシェルや診断の子の終わり(`_exit`。親の後始末を動かさない)。
pub fn bash_exit_subshell(code: u8) -> ! {
    // SAFETY: _exitは戻らない。
    unsafe { libc::_exit(c_int::from(code)) }
}

/// 旧bashの`hangup_all_jobs`(職の制御の無いbashの仕事の群は、シェルの群)。
pub fn bash_hangup_group() {
    // SAFETY: 自分の群へ送るだけ。
    unsafe {
        libc::killpg(libc::getpgrp(), libc::SIGHUP);
    }
}

/// 信号のmaskの控え(旧bashの`BLOCK_SIGNAL`と`UNBLOCK_SIGNAL`)。
pub struct SignalMask {
    old: libc::sigset_t,
}

/// `SIGINT`を止める(旧bashがコマンド置換の出力を読む間)。戻り値で前のmaskへ戻す。
pub fn bash_block_sigint() -> Result<SignalMask, Errno> {
    // SAFETY: zeroedのsigset_tへsigemptyset・sigaddsetで書き、有効な集合を渡す。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        let mut old: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGINT);
        if libc::sigprocmask(libc::SIG_BLOCK, &set, &mut old) != 0 {
            return Err(last_errno());
        }
        Ok(SignalMask { old })
    }
}

impl SignalMask {
    /// 前のmaskへ戻す。止めていた間に受けた信号は、ここで関数へ渡る。
    pub fn restore(self) {
        // SAFETY: 控えた有効な集合を渡す。
        unsafe {
            libc::sigprocmask(libc::SIG_SETMASK, &self.old, std::ptr::null_mut());
        }
    }
}

/// 旧bashの`kill_shell`: 信号を既定へ戻して自分へ送る。終わらなければ`128 + 信号`で終える。
pub fn bash_kill_shell(signal: c_int) -> ! {
    let _ = set_disposition(signal, Disposition::Default);
    // SAFETY: 信号のmaskから外すだけ。
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, signal);
        libc::sigprocmask(libc::SIG_UNBLOCK, &set, std::ptr::null_mut());
        libc::kill(libc::getpid(), signal);
        libc::exit(128 + signal)
    }
}

// ---------------------------------------------------------------- 環境

#[cfg(target_os = "linux")]
unsafe extern "C" {
    static environ: *const *const c_char;
}

#[cfg(target_os = "linux")]
fn environ_pointer() -> *const *const c_char {
    // SAFETY: libcの大域変数のポインタの値を写すだけである。
    unsafe { environ }
}

#[cfg(target_vendor = "apple")]
fn environ_pointer() -> *const *const c_char {
    // SAFETY: _NSGetEnvironは、environの場所を返すだけである。
    unsafe { (*libc::_NSGetEnviron()).cast_const().cast() }
}

/// 今の`environ`の項目を、並びのまま生のバイト列で返す(`=`で分けない)。
///
/// 標準ライブラリの`vars_os`は、名前が`=`で始まる項目の分け方が旧Pythonと違うので使わない。
/// 起動の直後、ほかのスレッドが環境を変えない間に呼ぶ。
pub fn environ_entries() -> Vec<Vec<u8>> {
    let mut entries = Vec::new();
    let mut cursor = environ_pointer();
    if cursor.is_null() {
        return entries;
    }
    // SAFETY: environはNULで終わる文字列へのポインタの、nullで終わる並びである。
    // このモジュールは環境を変えないので、読む間に並びは変わらない(呼出側の約束)。
    unsafe {
        while !(*cursor).is_null() {
            entries.push(CStr::from_ptr(*cursor).to_bytes().to_vec());
            cursor = cursor.add(1);
        }
    }
    entries
}

/// 旧Pythonの`dict(os.environ)`と同じ、挿入の順を保つ環境の辞書。名前と値は生のバイト列。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PythonEnviron {
    entries: Vec<(Vec<u8>, Vec<u8>)>,
}

impl PythonEnviron {
    /// `os.environ`の作り方: 最初の`=`で分け、`=`の無い項目は捨て、同じ名前は最初の値を残す。
    pub fn from_entries<I, T>(entries: I) -> Self
    where
        I: IntoIterator<Item = T>,
        T: AsRef<[u8]>,
    {
        let mut result = Self::default();
        for entry in entries {
            let entry = entry.as_ref();
            let Some(position) = entry.iter().position(|byte| *byte == b'=') else {
                continue;
            };
            let (key, value) = (&entry[..position], &entry[position + 1..]);
            if result.get(key).is_none() {
                result.entries.push((key.to_vec(), value.to_vec()));
            }
        }
        result
    }

    /// 今のプロセスの環境から作る。
    pub fn from_process() -> Self {
        Self::from_entries(environ_entries())
    }

    pub fn get(&self, key: &[u8]) -> Option<&[u8]> {
        self.entries
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_slice())
    }

    /// `env[key] = value`。あれば同じ位置で置き換え、無ければ末尾に足す。
    pub fn insert(&mut self, key: &[u8], value: &[u8]) {
        match self.entries.iter_mut().find(|(name, _)| name == key) {
            Some((_, old)) => *old = value.to_vec(),
            None => self.entries.push((key.to_vec(), value.to_vec())),
        }
    }

    /// `env.pop(key, None)`。
    pub fn remove(&mut self, key: &[u8]) -> Option<Vec<u8>> {
        let position = self.entries.iter().position(|(name, _)| name == key)?;
        Some(self.entries.remove(position).1)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&[u8], &[u8])> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_slice(), value.as_slice()))
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

// ---------------------------------------------------------------- PATHの探索

fn join(directory: &[u8], name: &[u8]) -> Vec<u8> {
    let mut path = directory.to_vec();
    if !path.is_empty() && !path.ends_with(b"/") {
        path.push(b'/');
    }
    path.extend_from_slice(name);
    path
}

/// 旧`subprocess`が順に`execve`する候補(`os.get_exec_path(env)`と`os.path.join`)。
///
/// `/`を含む名前はそのまま1件。ほかは環境の`PATH`(無ければ`/bin:/usr/bin`)を`:`で分ける。
pub fn python_executable_list(program: &[u8], env: &PythonEnviron) -> Vec<Vec<u8>> {
    if program.contains(&b'/') {
        return vec![program.to_vec()];
    }
    env.get(b"PATH")
        .unwrap_or(b"/bin:/usr/bin")
        .split(|byte| *byte == b':')
        .map(|directory| join(directory, program))
        .collect()
}

/// 旧bashの探索の結果。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BashLookup {
    /// このパスで`execve`する(実行できるファイル・`/`を含む名前・`PATH`が空か無い)。
    Found(Vec<u8>),
    /// 実行できるファイルは無く、最初に見つかった実行できないファイル。`execve`は失敗する。
    NotExecutable(Vec<u8>),
    /// 見つからない。
    NotFound,
}

struct FileStatus {
    exists: bool,
    directory: bool,
    executable: bool,
}

/// 旧bashの`file_status`(`stat`と`eaccess`)。
fn file_status(path: &[u8]) -> FileStatus {
    let missing = FileStatus {
        exists: false,
        directory: false,
        executable: false,
    };
    let Ok(path) = CString::new(path) else {
        return missing;
    };
    let Ok(info) = super::fs::stat_at(None, &path, true) else {
        return missing;
    };
    if info.is_dir() {
        return FileStatus {
            exists: true,
            directory: true,
            executable: false,
        };
    }
    FileStatus {
        exists: true,
        directory: false,
        executable: super::fs::access(&path, rustix::fs::Access::EXEC_OK, true).is_ok(),
    }
}

/// 旧bashの`extract_colon_unit`と同じ分け方。空の要素は`.`にする。
fn bash_path_elements(path: &[u8]) -> Vec<Vec<u8>> {
    let mut elements = Vec::new();
    let mut index = 0;
    while index < path.len() {
        if index > 0 && path[index] == b':' {
            index += 1;
        }
        let start = index;
        while index < path.len() && path[index] != b':' {
            index += 1;
        }
        if index == start {
            if index < path.len() {
                index += 1;
            }
            elements.push(b".".to_vec());
        } else {
            elements.push(path[start..index].to_vec());
        }
    }
    elements
}

/// 旧bashの`find_user_command_in_path`(`FS_EXEC_PREFERRED|FS_NODIRS`)と同じ探索。
///
/// `path`はbashの`PATH`変数の値。
/// - 旧bashは、環境に`PATH`が無いと、起動時に組込みの既定値(ビルドごとに違う)を入れて探す。
///   その既定値は呼出側が決めて`Some`で渡す。
/// - `None`は、bashの中で`PATH`変数が無い状態(`unset PATH`の後)だけを表す。旧bashと同じく、
///   名前をそのまま返す(今のディレクトリからの相対パスとして`execve`する)。空の値も同じ。
///
/// `~`で始まる要素は、`expand_tilde`へ`~`の後から最初の`/`までの名前(空なら今の利用者)を渡し、
/// `Some`の値で置き換える。`None`なら旧bashと同じく展開しない。名前から家のディレクトリを
/// 引く方法(`HOME`・利用者の情報)は呼出側が決める。
///
/// 扱わないもの(呼出側の責務): 環境変数`EXECIGNORE`による除外、bashの探索結果の記憶(`hash`。
/// 2回目からは探さずに覚えたパスを使い、消えたファイルも探し直さない)。
pub fn bash_find_command(
    name: &[u8],
    path: Option<&[u8]>,
    expand_tilde: &mut dyn FnMut(&[u8]) -> Option<Vec<u8>>,
) -> BashLookup {
    if name.contains(&b'/') {
        return BashLookup::Found(name.to_vec());
    }
    let Some(path) = path.filter(|path| !path.is_empty()) else {
        return BashLookup::Found(name.to_vec());
    };
    let mut first_existing: Option<Vec<u8>> = None;
    for element in bash_path_elements(path) {
        let element = if element.starts_with(b"~") {
            let end = element.iter().position(|byte| *byte == b'/');
            let user = &element[1..end.unwrap_or(element.len())];
            match expand_tilde(user) {
                Some(mut home) => {
                    if let Some(end) = end {
                        home.extend_from_slice(&element[end..]);
                    }
                    home
                }
                None => element,
            }
        } else {
            element
        };
        let full = join(&element, name);
        let status = file_status(&full);
        if !status.exists {
            continue;
        }
        if status.executable && !status.directory {
            return BashLookup::Found(full);
        }
        if first_existing.is_none() {
            first_existing = Some(full);
        }
    }
    match first_existing {
        Some(path) if !file_status(&path).directory => BashLookup::NotExecutable(path),
        _ => BashLookup::NotFound,
    }
}

// ---------------------------------------------------------------- 旧bashのexecの失敗

/// `#!`で始まるファイルで、2進とみなすNULを探す範囲。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NulScope {
    FirstLine,
    Anywhere,
    FirstTwoLines,
}

/// 旧bashの`shell_execve`の、版ごとの規則。bash 5.1・5.2・5.3の原本と5.2の修正で確かめた。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BashExecRules {
    elf_is_binary: bool,
    hash_bang_nul: NulScope,
    required_file_not_found: bool,
}

impl BashExecRules {
    /// R4G の基準の版(CI の Ubuntu 24.04 の bash 5.2.21。2026-11-12 00:35 の利用者決定)の規則。
    /// Rust が作る旧bashの起動の失敗の文と終了値は、この規則に固定する。
    pub fn baseline() -> Self {
        Self::for_version(5, 2, 21)
    }

    /// bashの版(`BASH_VERSINFO`の0〜2)から規則を選ぶ。
    ///
    /// - 5.1以前: ELFの印を見ない。NULは1行目だけを見る。`required file not found`は無い。
    ///   (5.1より前は未確認で、5.1と同じとみなす)
    /// - 5.2.0〜5.2.9: ELFの印は2進。`#!`のファイルはNULを全体で探す。ENOENTは`required file not found`。
    /// - 5.2.10以上(修正 bash52-010): `#!`のファイルのNULを2行目まで探す。ほかは5.2.0と同じ。
    /// - 5.3以上: 5.2.10と同じ2進の判定。`required file not found`は無い(5.1と同じ経路)。
    pub fn for_version(major: u32, minor: u32, patch: u32) -> Self {
        let version = (major, minor);
        if version < (5, 2) {
            Self {
                elf_is_binary: false,
                hash_bang_nul: NulScope::FirstLine,
                required_file_not_found: false,
            }
        } else if version == (5, 2) {
            Self {
                elf_is_binary: true,
                hash_bang_nul: if patch < 10 {
                    NulScope::Anywhere
                } else {
                    NulScope::FirstTwoLines
                },
                required_file_not_found: true,
            }
        } else {
            Self {
                elf_is_binary: true,
                hash_bang_nul: NulScope::FirstTwoLines,
                required_file_not_found: false,
            }
        }
    }

    /// 旧bashの`check_binary_file`。
    fn is_binary(&self, sample: &[u8]) -> bool {
        if self.elf_is_binary && sample.starts_with(b"\x7fELF") {
            return true;
        }
        let hash_bang = sample.starts_with(b"#!");
        let mut lines = match (hash_bang, self.hash_bang_nul) {
            (true, NulScope::Anywhere) => return sample.contains(&0),
            (true, NulScope::FirstTwoLines) => 2,
            _ => 1,
        };
        for byte in sample {
            if *byte == b'\n' {
                lines -= 1;
                if lines == 0 {
                    return false;
                }
            }
            if *byte == 0 {
                return true;
            }
        }
        false
    }
}

/// 旧bashの`shell_execve`が、`execve`の失敗の後に選ぶ経路。
///
/// 診断の文(翻訳・`strerror`の字面・行番号の前置き)と、その出力は呼出側(単位3)が作る。
/// 文の形は版で違う(`required file not found`は5.2だけ)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BashExecFailure {
    /// `<名前>: Is a directory`。
    IsDirectory { status: u8 },
    /// `file_error`(`<名前>: <strerror(errno)>`)。実行できないファイル・`E2BIG`・`ENOMEM`・ほか。
    FileError { errno: Errno, status: u8 },
    /// 5.2だけ: `<名前>: cannot execute: required file not found`(127)。
    RequiredFileNotFound,
    /// `<名前>: <解釈系>: bad interpreter: <strerror(errno)>`(126)。
    /// 解釈系は、旧bashと同じく末尾の`\r`を`^M`に置き換えた後の字面。
    BadInterpreter { errno: Errno, interpreter: Vec<u8> },
    /// `ENOEXEC`で中身が空。旧bashは何もせず終了値0。
    EmptyFile,
    /// `ENOEXEC`で2進。`<名前>: cannot execute binary file: <strerror(ENOEXEC)>`(126)。
    BinaryFile,
    /// `ENOEXEC`で文字。旧bashは、自分の別の子としてファイルをシェルのスクリプトとして実行する。
    /// Rustでは再現できないので、扱いは呼出側が決める。
    ShellScript,
}

impl BashExecFailure {
    /// 旧bashの子の終了値。`ShellScript`はスクリプトの結果なので`None`。
    pub fn status(&self) -> Option<u8> {
        match self {
            Self::IsDirectory { status } | Self::FileError { status, .. } => Some(*status),
            Self::RequiredFileNotFound => Some(127),
            Self::BadInterpreter { .. } | Self::BinaryFile => Some(126),
            Self::EmptyFile => Some(0),
            Self::ShellScript => None,
        }
    }
}

/// 旧bashの`READ_SAMPLE_BUF`: リンクを辿って開き(書き手を待つ)、128バイトまで1回読む。失敗は`None`。
fn read_sample(path: &CStr) -> Option<Vec<u8>> {
    let fd = super::fs::open_at(None, path, rustix::fs::OFlags::RDONLY).ok()?;
    let mut sample = vec![0u8; 128];
    let length = rustix::io::read(&fd, &mut sample).ok()?;
    sample.truncate(length);
    Some(sample)
}

/// 旧bashの`getinterp`: `#!`の後の空白を飛ばし、空白・改行までを取る。Cの文字列と同じくNULで切る。
fn interpreter(sample: &[u8]) -> Vec<u8> {
    let mut index = 2;
    while index < sample.len() && matches!(sample[index], b' ' | b'\t') {
        index += 1;
    }
    let start = index;
    while index < sample.len() && !matches!(sample[index], b' ' | b'\t' | b'\n') {
        index += 1;
    }
    let mut name = sample[start..index].to_vec();
    if let Some(end) = name.iter().position(|byte| *byte == 0) {
        name.truncate(end);
    }
    if name.last() == Some(&b'\r') {
        name.pop();
        name.extend_from_slice(b"^M");
    }
    name
}

/// [`bash_spawn`]が返した`execve`のエラー番号から、旧bashの`shell_execve`の経路を決める。
///
/// 旧bashと同じく、`stat`・`eaccess`・ファイルの先頭128バイトを調べる(`path`を開くので、
/// 名前付きpipeなら書き手を待つ)。`rules`は比べる旧bashの版から作る。
pub fn bash_exec_failure(path: &[u8], errno: Errno, rules: BashExecRules) -> BashExecFailure {
    let Ok(c_path) = CString::new(path) else {
        return BashExecFailure::FileError {
            errno,
            status: if errno == Errno::NOENT { 127 } else { 126 },
        };
    };
    if errno != Errno::NOEXEC {
        let status = if errno == Errno::NOENT { 127 } else { 126 };
        let file = file_status(path);
        if file.directory {
            return BashExecFailure::IsDirectory { status };
        }
        if !file.executable || errno == Errno::TOOBIG || errno == Errno::NOMEM {
            return BashExecFailure::FileError { errno, status };
        }
        if rules.required_file_not_found && errno == Errno::NOENT {
            return BashExecFailure::RequiredFileNotFound;
        }
        if let Some(mut sample) = read_sample(&c_path)
            && let Some(last) = sample.last_mut()
        {
            *last = 0;
            if sample.len() > 2 && sample.starts_with(b"#!") {
                return BashExecFailure::BadInterpreter {
                    errno,
                    interpreter: interpreter(&sample),
                };
            }
        }
        return BashExecFailure::FileError { errno, status };
    }
    match read_sample(&c_path) {
        Some(sample) if sample.is_empty() => BashExecFailure::EmptyFile,
        Some(sample) if rules.is_binary(&sample) => BashExecFailure::BinaryFile,
        _ => BashExecFailure::ShellScript,
    }
}

// ---------------------------------------------------------------- 子の回収

/// 子の終わり方。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Exited(u8),
    Signaled(c_int),
}

impl Status {
    /// 旧`subprocess`の`returncode`。信号で終わった子は負の数。
    pub fn returncode(self) -> i32 {
        match self {
            Self::Exited(code) => i32::from(code),
            Self::Signaled(signal) => -signal,
        }
    }

    fn from_raw(raw: c_int) -> Self {
        if libc::WIFSIGNALED(raw) {
            Self::Signaled(libc::WTERMSIG(raw))
        } else {
            Self::Exited(libc::WEXITSTATUS(raw) as u8)
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Flavor {
    Python,
    Bash,
}

/// 起動した直接の子1個。`Drop`は回収しない(旧Pythonの割込みの経路と同じく、残りうる)。
#[derive(Debug)]
pub struct Child {
    pid: libc::pid_t,
    status: Option<Status>,
    /// 信号で終わったときに`core`を書いたか(`WCOREDUMP`。旧bashの知らせの`(core dumped)`)。
    core: bool,
    flavor: Flavor,
}

impl Child {
    pub fn pid(&self) -> i32 {
        self.pid
    }

    /// 回収済みなら、その終わり方。
    pub fn status(&self) -> Option<Status> {
        self.status
    }

    /// 回収済みで、信号で終わって`core`を書いたか。
    pub fn core_dumped(&self) -> bool {
        self.core
    }

    fn waitpid(&mut self, options: c_int) -> Result<Option<Status>, Errno> {
        if let Some(status) = self.status {
            return Ok(Some(status));
        }
        let mut raw = 0;
        // SAFETY: 自分が起動した子の番号と、有効な出力先を渡す。
        let result = unsafe { libc::waitpid(self.pid, &mut raw, options) };
        if result == 0 {
            return Ok(None);
        }
        let status = if result < 0 {
            let error = last_errno();
            // 旧`subprocess`だけが、回収できない子(SIGCHLDを無視した起動など)を終了値0とする。
            // 旧bashの子では誤りのまま返す([`BashSignals::capture_and_prepare`]で起きなくなる)。
            if error != Errno::CHILD || self.flavor != Flavor::Python {
                return Err(error);
            }
            Status::Exited(0)
        } else {
            self.core = libc::WIFSIGNALED(raw) && libc::WCOREDUMP(raw);
            Status::from_raw(raw)
        };
        self.status = Some(status);
        Ok(Some(status))
    }

    /// [`bash_wait_any`]で回収した終わり方を、この子のものなら覚える(覚えたら`true`)。
    pub fn record_reaped(&mut self, pid: libc::pid_t, raw: c_int) -> bool {
        if pid != self.pid || self.status.is_some() {
            return false;
        }
        self.core = libc::WIFSIGNALED(raw) && libc::WCOREDUMP(raw);
        self.status = Some(Status::from_raw(raw));
        true
    }

    /// `waitpid(pid, WNOHANG)`。
    pub fn try_wait(&mut self) -> Result<Option<Status>, Errno> {
        self.waitpid(libc::WNOHANG)
    }

    /// `waitpid(pid, 0)`。信号で中断したら`Err(INTR)`を返し、呼出側に信号を確かめさせる。
    pub fn wait(&mut self) -> Result<Status, Errno> {
        self.waitpid(0)
            .map(|status| status.expect("blocking waitpid"))
    }

    /// 旧`Popen.send_signal`: まず回収を試み、終わっていなければ送る。`ESRCH`は無視する。
    pub fn send_signal(&mut self, signal: c_int) -> Result<(), Errno> {
        if self.try_wait()?.is_some() {
            return Ok(());
        }
        self.kill(signal)
    }

    /// 回収済みでなければ`kill(pid, signal)`する(回収を試みない。旧bashの`kill`の向き)。
    pub fn kill(&mut self, signal: c_int) -> Result<(), Errno> {
        if self.status.is_some() {
            return Ok(());
        }
        // SAFETY: 自分が起動し、まだ回収していない子の番号へ送る。
        if unsafe { libc::kill(self.pid, signal) } == 0 {
            return Ok(());
        }
        match last_errno() {
            Errno::SRCH => Ok(()),
            error => Err(error),
        }
    }
}

// ---------------------------------------------------------------- forkとexec

/// 子の中の失敗の段階。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecStage {
    /// fdや信号の準備の失敗(旧Pythonの`noexec`。例外にファイル名を添えない)。
    BeforeExec,
    /// `execve`の失敗(旧Pythonは`args[0]`を添える)。
    Exec,
}

/// 子の中の失敗。子は報告の後に終わり、親が回収する。子の終了値は使わない。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecFailure {
    pub stage: ExecStage,
    pub errno: Errno,
}

/// 子で設定する信号。
enum SignalPlan<'a> {
    /// 旧Python: `SIGPIPE`・`SIGXFSZ`を既定へ戻し、maskで止めていない信号の関数を既定へ戻す。
    Python,
    /// 旧bash: 全部の信号を起動時の扱いへ戻す。
    Bash(&'a [libc::sighandler_t; SIGNAL_LIMIT as usize]),
}

struct Plan<'a> {
    candidates: Vec<CString>,
    argv: Vec<CString>,
    envp: Vec<CString>,
    targets: [Option<RawFd>; 3],
    close_inherited: bool,
    signals: SignalPlan<'a>,
    flavor: Flavor,
}

enum ForkError {
    Os(Errno),
    Child(ExecFailure),
}

/// テスト専用の切替え。`close_range`が使えない経路(1)と、fdの列挙にも失敗する経路(2)を流す。
/// 製品と検査専用の入口のビルドには無い。
#[cfg(test)]
static TEST_CLOSE_PATH: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

#[cfg(test)]
fn test_close_path() -> u8 {
    TEST_CLOSE_PATH.load(std::sync::atomic::Ordering::SeqCst)
}

#[cfg(not(test))]
fn test_close_path() -> u8 {
    0
}

/// テスト専用。最後の起動で選んだ閉じ方(1: close_range・2: 列挙・3: 総当たり)。
#[cfg(test)]
static TEST_LAST_CLOSE: std::sync::atomic::AtomicU8 = std::sync::atomic::AtomicU8::new(0);

/// `close_range(CLOSE_RANGE_CLOEXEC)`が使えるか(Linux 5.11以上)。親で1回だけ確かめる。
#[cfg(target_os = "linux")]
fn close_range_cloexec_works() -> bool {
    static WORKS: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    test_close_path() == 0
        && *WORKS.get_or_init(|| {
            // 開いていない最大の番号だけを範囲にするので、何も変わらない。
            // SAFETY: 引数の型をc_uintにそろえた、可変長の引数のシステムコール。
            unsafe {
                libc::syscall(
                    libc::SYS_close_range,
                    libc::c_uint::MAX,
                    libc::c_uint::MAX,
                    libc::CLOSE_RANGE_CLOEXEC,
                ) == 0
            }
        })
}

#[cfg(not(target_os = "linux"))]
fn close_range_cloexec_works() -> bool {
    false
}

/// 開いているfdの番号を親で列挙する(Linuxは`/proc/self/fd`、Macは`/dev/fd`)。
fn open_descriptors() -> Result<Vec<RawFd>, Errno> {
    if test_close_path() == 2 {
        return Err(Errno::NOENT);
    }
    let directory = if cfg!(target_os = "linux") {
        c"/proc/self/fd"
    } else {
        c"/dev/fd"
    };
    let fd = super::fs::open_at(
        None,
        directory,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::DIRECTORY,
    )?;
    let mut list = Vec::new();
    super::fs::scandir::<()>(fd.as_fd(), |name| {
        if let Some(number) = std::str::from_utf8(name)
            .ok()
            .and_then(|text| text.parse::<RawFd>().ok())
        {
            list.push(number);
        }
        Ok(())
    })
    .map_err(|error| match error {
        super::fs::ScanError::Os(error) => error,
        super::fs::ScanError::Stopped(()) => Errno::IO,
    })?;
    Ok(list)
}

/// 旧Pythonの`safe_get_max_fd`: `sysconf(_SC_OPEN_MAX)`、取れなければ256。
fn max_descriptor() -> RawFd {
    // SAFETY: 設定値を読むだけである。
    let limit = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
    if limit < 0 {
        256
    } else {
        RawFd::try_from(limit).unwrap_or(RawFd::MAX)
    }
}

/// 子で3以上のfdに`FD_CLOEXEC`を付ける方法(旧Pythonの`close_fds=True`と同じ結果)。
enum CloseInherited {
    /// 継いだfdを閉じない(旧bash)。
    Keep,
    /// `close_range`。失敗したら上限までの総当たりへ切り替える。
    CloseRange { limit: RawFd },
    /// 親で列挙した番号。
    Listed(Vec<RawFd>),
    /// 列挙にも失敗したときの、上限までの総当たり(旧Pythonの`_close_range_brute_force`)。
    UpTo(RawFd),
}

fn errno_value() -> c_int {
    last_errno().raw_os_error()
}

/// 子の中で、失敗の段階とエラー番号を報告のpipeへ書き、終わる。確保をしない。
///
/// # Safety
///
/// forkの後の子の中でだけ呼ぶ。
unsafe fn child_fail(report: RawFd, stage: ExecStage, errno: c_int) -> ! {
    let mut record = [0u8; 5];
    record[0] = match stage {
        ExecStage::BeforeExec => 0,
        ExecStage::Exec => 1,
    };
    record[1..].copy_from_slice(&errno.to_le_bytes());
    let mut written = 0;
    while written < record.len() {
        // SAFETY: 有効な受け皿の残りを渡す。
        let count = unsafe {
            libc::write(
                report,
                record[written..].as_ptr().cast(),
                record.len() - written,
            )
        };
        if count > 0 {
            written += count as usize;
        } else if count < 0 && errno_value() == libc::EINTR {
            continue;
        } else {
            break;
        }
    }
    // 終了値は使わない(旧Pythonの子と同じ255)。旧bashの終了値は呼出側が bash_exec_failure で決める。
    // SAFETY: 子を終える。atexitの処理もバッファのflushもしない。
    unsafe { libc::_exit(255) }
}

struct ChildArgs<'a> {
    candidates: &'a [*const c_char],
    argv: *const *const c_char,
    envp: *const *const c_char,
    targets: [Option<RawFd>; 3],
    close: &'a CloseInherited,
    signals: &'a SignalPlan<'a>,
}

/// 子で`sigaction`を1つ設定する。失敗は見ない(旧Pythonの PyOS_setsig・旧bashと同じ)。
///
/// # Safety
///
/// forkの後の子の中でだけ呼ぶ。
unsafe fn child_set_signal(signal: c_int, handler: libc::sighandler_t) {
    // SAFETY: zeroedのsigactionへ既定か無視を入れる。
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = handler;
    // SAFETY: 有効なsigactionを渡す。
    unsafe { libc::sigaction(signal, &action, std::ptr::null_mut()) };
}

/// 子で3以上の番号へ`FD_CLOEXEC`を付ける。閉じた番号のEBADFは無視する(旧Pythonと同じ)。
///
/// # Safety
///
/// forkの後の子の中でだけ呼ぶ。
unsafe fn child_cloexec(fd: RawFd) {
    // SAFETY: 番号の印を付けるだけである。
    unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
}

/// forkの後の子。確保・panic・ロックを使わず、libcの呼出しだけで進む。
///
/// # Safety
///
/// forkの直後の子の中でだけ呼ぶ。`args`の指す値は、forkの前に親が作って生かしたもの。
unsafe fn child_main(args: &ChildArgs<'_>, mask: &libc::sigset_t, report: RawFd) -> ! {
    // 1. 標準入出力。0〜2にある元のfdは、上書きされないよう先に3以上へ移す。
    let mut targets = args.targets;
    for (index, target) in targets.iter_mut().enumerate() {
        if let Some(source) = *target
            && source < 3
            && source != index as RawFd
        {
            // SAFETY: 開いた番号を複製するだけである。
            let moved = unsafe { libc::fcntl(source, libc::F_DUPFD_CLOEXEC, 3) };
            if moved < 0 {
                // SAFETY: この関数は子の中でだけ動く。
                unsafe { child_fail(report, ExecStage::BeforeExec, errno_value()) };
            }
            *target = Some(moved);
        }
    }
    for (index, target) in targets.iter().enumerate() {
        let index = index as RawFd;
        match *target {
            None => {}
            Some(source) if source == index => {
                // dup2が何もしない場合は、旧Pythonと同じくFD_CLOEXECを外す。
                // SAFETY: 番号の印を読み書きするだけである。
                let flags = unsafe { libc::fcntl(index, libc::F_GETFD) };
                if flags < 0
                    || unsafe { libc::fcntl(index, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } < 0
                {
                    // SAFETY: この関数は子の中でだけ動く。
                    unsafe { child_fail(report, ExecStage::BeforeExec, errno_value()) };
                }
            }
            // SAFETY: 開いた番号を0〜2へ複製する。
            Some(source) if unsafe { libc::dup2(source, index) } < 0 => {
                // SAFETY: この関数は子の中でだけ動く。
                unsafe { child_fail(report, ExecStage::BeforeExec, errno_value()) };
            }
            Some(_) => {}
        }
    }
    // 2. 信号の扱い。
    match args.signals {
        SignalPlan::Python => {
            // SAFETY: 子の中である。
            unsafe {
                child_set_signal(libc::SIGPIPE, libc::SIG_DFL);
                child_set_signal(libc::SIGXFSZ, libc::SIG_DFL);
            }
            // 関数を登録した信号を既定へ戻す(旧Pythonの reset_signal_handlers)。
            // maskで止めたままexecveを越える信号は、カーネルが戻すので触れない。
            for signal in 1..SIGNAL_LIMIT {
                if signal == libc::SIGKILL || signal == libc::SIGSTOP {
                    continue;
                }
                // SAFETY: maskは親が渡した有効なsigset_tである。
                if unsafe { libc::sigismember(mask, signal) } == 1 {
                    continue;
                }
                // SAFETY: zeroedのsigactionは出力先として有効である。
                let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
                // SAFETY: 読むだけの呼出し。内部用の信号ではEINVALになるので飛ばす。
                if unsafe { libc::sigaction(signal, std::ptr::null(), &mut old) } != 0 {
                    continue;
                }
                if old.sa_sigaction == libc::SIG_DFL || old.sa_sigaction == libc::SIG_IGN {
                    continue;
                }
                // SAFETY: 子の中である。
                unsafe { child_set_signal(signal, libc::SIG_DFL) };
            }
        }
        SignalPlan::Bash(original) => {
            // 旧bashの restore_original_signals と同じく、起動時の扱いへ戻す。
            for signal in 1..SIGNAL_LIMIT {
                if signal == libc::SIGKILL || signal == libc::SIGSTOP {
                    continue;
                }
                // SAFETY: 子の中である。内部用の信号は失敗するだけである。
                unsafe { child_set_signal(signal, original[signal as usize]) };
            }
        }
    }
    // 3. 親が起動の前に持っていたmaskへ戻す。
    // SAFETY: maskは親が渡した有効なsigset_tである。
    let result = unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, mask, std::ptr::null_mut()) };
    if result != 0 {
        // SAFETY: この関数は子の中でだけ動く。
        unsafe { child_fail(report, ExecStage::BeforeExec, result) };
    }
    // 4. 3以上のfdにFD_CLOEXECを付ける。
    match args.close {
        CloseInherited::Keep => {}
        CloseInherited::CloseRange { limit } => {
            #[cfg(target_os = "linux")]
            // SAFETY: 引数の型をc_uintにそろえた、可変長の引数のシステムコール。
            let done = unsafe {
                libc::syscall(
                    libc::SYS_close_range,
                    3 as libc::c_uint,
                    libc::c_uint::MAX,
                    libc::CLOSE_RANGE_CLOEXEC,
                )
            } == 0;
            #[cfg(not(target_os = "linux"))]
            let done = false;
            if !done {
                for fd in 3..*limit {
                    // SAFETY: 子の中である。
                    unsafe { child_cloexec(fd) };
                }
            }
        }
        CloseInherited::Listed(list) => {
            for fd in list.iter().filter(|fd| **fd >= 3) {
                // SAFETY: 子の中である。
                unsafe { child_cloexec(*fd) };
            }
        }
        CloseInherited::UpTo(limit) => {
            for fd in 3..*limit {
                // SAFETY: 子の中である。
                unsafe { child_cloexec(fd) };
            }
        }
    }
    // 5. 候補へ順にexecveする。報告するのは、ENOENT・ENOTDIRでない最初の失敗か、最後の失敗。
    let mut saved = 0;
    let mut last = libc::ENOENT;
    for candidate in args.candidates {
        // SAFETY: 親が作ったNUL終端の文字列と、nullで終わる並びを渡す。
        unsafe { libc::execve(*candidate, args.argv, args.envp) };
        last = errno_value();
        if last != libc::ENOENT && last != libc::ENOTDIR && saved == 0 {
            saved = last;
        }
    }
    let errno = if saved != 0 { saved } else { last };
    // SAFETY: この関数は子の中でだけ動く。
    unsafe { child_fail(report, ExecStage::Exec, errno) }
}

fn c_strings(items: &[Vec<u8>]) -> Option<Vec<CString>> {
    items
        .iter()
        .map(|item| CString::new(item.clone()).ok())
        .collect()
}

fn pointers(items: &[CString]) -> Vec<*const c_char> {
    items
        .iter()
        .map(|item| item.as_ptr())
        .chain(std::iter::once(std::ptr::null()))
        .collect()
}

/// 両端とも3以上で`FD_CLOEXEC`付きのpipe。0〜2が閉じた起動でも、標準入出力の番号を使わない。
pub fn pipe() -> Result<(OwnedFd, OwnedFd), Errno> {
    let (reader, writer) = std::io::pipe()
        .map_err(|error| Errno::from_raw_os_error(error.raw_os_error().unwrap_or(libc::EIO)))?;
    Ok((high(OwnedFd::from(reader))?, high(OwnedFd::from(writer))?))
}

/// 旧bashのコマンド置換の`pipe()`: 番号を動かさない。close-on-exec は付ける(子の側では、書く端を
/// fd 1 へ複製して渡す)。標準の番号(0〜2)は標準ライブラリが開いたままなので、ここには当たらない。
pub fn bash_pipe() -> Result<(OwnedFd, OwnedFd), Errno> {
    let (reader, writer) = std::io::pipe()
        .map_err(|error| Errno::from_raw_os_error(error.raw_os_error().unwrap_or(libc::EIO)))?;
    Ok((OwnedFd::from(reader), OwnedFd::from(writer)))
}

fn high(fd: OwnedFd) -> Result<OwnedFd, Errno> {
    if fd.as_raw_fd() >= 3 {
        Ok(fd)
    } else {
        rustix::io::fcntl_dupfd_cloexec(&fd, 3)
    }
}

fn fork_exec(plan: &Plan<'_>, report: (OwnedFd, OwnedFd)) -> Result<Child, ForkError> {
    let candidates: Vec<*const c_char> = plan.candidates.iter().map(|c| c.as_ptr()).collect();
    let argv = pointers(&plan.argv);
    let envp = pointers(&plan.envp);
    let close = if !plan.close_inherited {
        CloseInherited::Keep
    } else if close_range_cloexec_works() {
        CloseInherited::CloseRange {
            limit: max_descriptor(),
        }
    } else {
        match open_descriptors() {
            Ok(list) => CloseInherited::Listed(list),
            Err(_) => CloseInherited::UpTo(max_descriptor()),
        }
    };
    #[cfg(test)]
    TEST_LAST_CLOSE.store(
        match close {
            CloseInherited::Keep => 0,
            CloseInherited::CloseRange { .. } => 1,
            CloseInherited::Listed(_) => 2,
            CloseInherited::UpTo(_) => 3,
        },
        std::sync::atomic::Ordering::SeqCst,
    );
    let args = ChildArgs {
        candidates: &candidates,
        argv: argv.as_ptr(),
        envp: envp.as_ptr(),
        targets: plan.targets,
        close: &close,
        signals: &plan.signals,
    };
    let (report_read, report_write) = report;
    // 子が信号の関数を動かさないよう、forkの前後は全部の信号を止める(旧Pythonのvforkと同じ)。
    // SAFETY: zeroedのsigset_tへsigfillsetで書く。
    let mut all: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: zeroedのsigset_tは出力先として有効である。
    let mut old: libc::sigset_t = unsafe { std::mem::zeroed() };
    // SAFETY: 有効なsigset_tを渡す。
    let result = unsafe {
        libc::sigfillset(&mut all);
        libc::pthread_sigmask(libc::SIG_BLOCK, &all, &mut old)
    };
    if result != 0 {
        return Err(ForkError::Os(Errno::from_raw_os_error(result)));
    }
    // SAFETY: 子はchild_mainへ進み、確保もpanicもせずにexecveか_exitで終わる。
    let pid = unsafe { libc::fork() };
    if pid == 0 {
        // SAFETY: forkの直後の子である。
        unsafe { child_main(&args, &old, report_write.as_raw_fd()) };
    }
    let fork_error = (pid < 0).then(last_errno);
    // SAFETY: 起動の前のmaskへ戻す。
    unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &old, std::ptr::null_mut()) };
    drop(report_write);
    if let Some(error) = fork_error {
        return Err(ForkError::Os(error));
    }
    let mut child = Child {
        pid,
        status: None,
        core: false,
        flavor: plan.flavor,
    };
    let mut record = [0u8; 5];
    let mut received = 0;
    while received < record.len() {
        match rustix::io::read(&report_read, &mut record[received..]) {
            Ok(0) => break,
            Ok(count) => received += count,
            Err(Errno::INTR) => continue,
            Err(error) => return Err(ForkError::Os(error)),
        }
    }
    if received == 0 {
        return Ok(child);
    }
    let failure = if received == record.len() {
        ExecFailure {
            stage: if record[0] == 1 {
                ExecStage::Exec
            } else {
                ExecStage::BeforeExec
            },
            errno: Errno::from_raw_os_error(i32::from_le_bytes([
                record[1], record[2], record[3], record[4],
            ])),
        }
    } else {
        ExecFailure {
            stage: ExecStage::BeforeExec,
            errno: Errno::IO,
        }
    };
    // 報告の後に終わった子を回収する。回収できない子(ECHILD)は、もう居ないので待たない。
    loop {
        match child.wait() {
            Ok(_) | Err(Errno::CHILD) => break,
            Err(Errno::INTR) => continue,
            Err(error) => return Err(ForkError::Os(error)),
        }
    }
    Err(ForkError::Child(failure))
}

// ---------------------------------------------------------------- 旧Pythonの子

/// 旧Pythonの`ValueError`になる入力。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ValueError {
    /// `illegal environment variable name`
    IllegalEnvironmentName,
    /// `embedded null byte`
    EmbeddedNull,
}

/// 旧`Popen`の起動の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SpawnError {
    /// `os.open(os.devnull, O_RDWR)`の失敗。旧版の例外は`/dev/null`を添える。
    DevNull(Errno),
    /// `os.pipe`・forkなど、ファイル名を添えない`OSError`。
    Os(Errno),
    Value(ValueError),
    /// 子の中の失敗。`Exec`なら旧版の例外は`args[0]`を添える。
    Child(ExecFailure),
}

/// 旧`subprocess.run`へ渡す内容。標準入力は`/dev/null`、標準出力と標準エラーは捕捉する。
pub struct PythonCommand<'a> {
    /// `args`。先頭が実行するプログラムの名前で、子の`argv[0]`にもなる。
    pub args: &'a [Vec<u8>],
    pub env: &'a PythonEnviron,
}

/// 旧`TimeoutExpired`が出た段階。
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum TimeoutStage {
    /// 出力を読む間に期限が来た。旧版の文は元の期限の秒数を出す。
    Reading,
    /// 出力が閉じた後、子の終わりを待つ間に期限が来た。旧版の文は待ち始めの残り秒数を出す。
    Waiting { remaining_seconds: f64 },
}

#[derive(Debug, PartialEq)]
pub enum RunError {
    Spawn(SpawnError),
    /// 子へ`SIGKILL`を送り、回収した後。
    Timeout {
        stage: TimeoutStage,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// 読取りや回収の`OSError`。子へ`SIGKILL`を送り、回収した後。
    Os(Errno),
    /// 呼出側の割込みの確かめが真を返した(旧Pythonの`KeyboardInterrupt`)。
    /// 旧版と同じく子を最大0.25秒待ち、終わっていなければ`SIGKILL`を送る。回収はしない。
    Interrupted,
}

/// 旧`subprocess.run`の結果。
#[derive(Debug, PartialEq, Eq)]
pub struct Completed {
    pub returncode: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// 旧`Popen._sigint_wait_secs`。
const SIGINT_WAIT_SECONDS: f64 = 0.25;

fn seconds_until(deadline: Instant) -> f64 {
    let now = Instant::now();
    if now <= deadline {
        (deadline - now).as_secs_f64()
    } else {
        -(now - deadline).as_secs_f64()
    }
}

/// 信号で中断できる眠り(旧`time.sleep`)。中断したら`false`。
fn sleep_interruptible(seconds: f64) -> bool {
    let duration = Duration::from_secs_f64(seconds.max(0.0));
    let Ok(timeout) = Timespec::try_from(duration) else {
        return true;
    };
    !matches!(poll(&mut [], Some(&timeout)), Err(Errno::INTR))
}

enum WaitOutcome {
    Done(Status),
    Timeout,
    /// 割込み。値は、その時点の待ちの期限までの残り秒数。
    Interrupted(f64),
}

/// 旧`Popen._wait(timeout)`の、期限付きの待ち。眠る前と、眠りが中断した後に割込みを確かめる。
fn wait_with_timeout(
    child: &mut Child,
    timeout: f64,
    interrupted: &mut dyn FnMut() -> bool,
) -> Result<WaitOutcome, Errno> {
    let started = Instant::now();
    let remaining = |started: Instant| timeout - started.elapsed().as_secs_f64();
    let mut delay = 0.0005_f64;
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(WaitOutcome::Done(status));
        }
        let left = remaining(started);
        if left <= 0.0 {
            return Ok(WaitOutcome::Timeout);
        }
        delay = (delay * 2.0).min(left).min(0.05);
        if interrupted() || (!sleep_interruptible(delay) && interrupted()) {
            return Ok(WaitOutcome::Interrupted(remaining(started)));
        }
    }
}

/// 旧`Popen.wait()`(期限なし)。待つ前と、待ちが中断した後に割込みを確かめる。
fn wait_blocking(
    child: &mut Child,
    interrupted: &mut dyn FnMut() -> bool,
) -> Result<WaitOutcome, Errno> {
    loop {
        if interrupted() {
            return Ok(WaitOutcome::Interrupted(f64::INFINITY));
        }
        match child.wait() {
            Ok(status) => return Ok(WaitOutcome::Done(status)),
            Err(Errno::INTR) => {
                if interrupted() {
                    return Ok(WaitOutcome::Interrupted(f64::INFINITY));
                }
            }
            Err(error) => return Err(error),
        }
    }
}

/// 割込みの後の旧版の流れ。
///
/// 最大0.25秒(待ちの期限の残りが短ければその分)子を待つ。待つ間の新しい割込みで待ちを打ち切る。
/// `extra_poll`なら、さらに1回だけ回収を試みる(`wait`の中の割込みが`communicate`へ伝わる経路)。
/// `kill`なら、終わっていない子へ`SIGKILL`を送る。
fn after_interrupt(
    child: &mut Child,
    limit: f64,
    extra_poll: bool,
    kill: bool,
    interrupted: &mut dyn FnMut() -> bool,
) -> RunError {
    let _ = wait_with_timeout(child, SIGINT_WAIT_SECONDS.min(limit), interrupted);
    if extra_poll {
        let _ = child.try_wait();
    }
    if kill {
        let _ = child.send_signal(libc::SIGKILL);
    }
    RunError::Interrupted
}

/// 期限切れか読取りの失敗の後の旧版の流れ: `SIGKILL`を送り(回収済みなら送らない)、回収する。
/// 回収を待つ間の割込みは、旧版と同じく0.25秒だけ待って、`SIGKILL`を送り直さずに返す。
fn kill_and_reap(
    child: &mut Child,
    interrupted: &mut dyn FnMut() -> bool,
    error: RunError,
) -> RunError {
    if let Err(kill_error) = child.send_signal(libc::SIGKILL) {
        return RunError::Os(kill_error);
    }
    match wait_blocking(child, interrupted) {
        Ok(WaitOutcome::Interrupted(limit)) => {
            after_interrupt(child, limit, false, false, interrupted)
        }
        Ok(_) => error,
        Err(wait_error) => RunError::Os(wait_error),
    }
}

/// 旧`Popen(args, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)`。
fn python_popen(command: &PythonCommand<'_>) -> Result<(Child, OwnedFd, OwnedFd), SpawnError> {
    let devnull = super::fs::open_at(None, c"/dev/null", rustix::fs::OFlags::RDWR)
        .map_err(SpawnError::DevNull)?;
    let devnull = high(devnull).map_err(SpawnError::DevNull)?;
    let (out_read, out_write) = pipe().map_err(SpawnError::Os)?;
    let (err_read, err_write) = pipe().map_err(SpawnError::Os)?;
    let report = pipe().map_err(SpawnError::Os)?;
    let mut envp = Vec::with_capacity(command.env.len());
    for (key, value) in command.env.iter() {
        if key.contains(&b'=') {
            return Err(SpawnError::Value(ValueError::IllegalEnvironmentName));
        }
        let mut entry = key.to_vec();
        entry.push(b'=');
        entry.extend_from_slice(value);
        envp.push(entry);
    }
    let program = command.args.first().map(Vec::as_slice).unwrap_or_default();
    let candidates = python_executable_list(program, command.env);
    let (Some(candidates), Some(argv), Some(envp)) = (
        c_strings(&candidates),
        c_strings(command.args),
        c_strings(&envp),
    ) else {
        return Err(SpawnError::Value(ValueError::EmbeddedNull));
    };
    let plan = Plan {
        candidates,
        argv,
        envp,
        targets: [
            Some(devnull.as_raw_fd()),
            Some(out_write.as_raw_fd()),
            Some(err_write.as_raw_fd()),
        ],
        close_inherited: true,
        signals: SignalPlan::Python,
        flavor: Flavor::Python,
    };
    let child = fork_exec(&plan, report).map_err(|error| match error {
        ForkError::Os(error) => SpawnError::Os(error),
        ForkError::Child(failure) => SpawnError::Child(failure),
    })?;
    // 旧版の _close_pipe_fds と同じく、子の側の端を閉じる。
    drop((devnull, out_write, err_write));
    Ok((child, out_read, err_read))
}

enum ReadOutcome {
    Done(Vec<u8>, Vec<u8>),
    Timeout(Vec<u8>, Vec<u8>),
    Interrupted,
    Os(Errno),
}

/// 旧`Popen._communicate`の読取りの段階(PollSelectorと32768バイトずつの読取り)。
///
/// 読み終えたpipeだけをここで閉じる。期限切れ・失敗・割込みのときは開いたまま返し、
/// 呼出側が旧版と同じ時点(回収の前か後)で閉じる。割込みは、待つ前と中断した後に確かめる。
fn read_outputs(
    streams: &mut [Option<OwnedFd>; 2],
    deadline: Instant,
    interrupted: &mut dyn FnMut() -> bool,
) -> ReadOutcome {
    let mut outputs = [Vec::new(), Vec::new()];
    while streams.iter().any(Option::is_some) {
        let remaining = seconds_until(deadline);
        if remaining < 0.0 {
            let [out, err] = outputs;
            return ReadOutcome::Timeout(out, err);
        }
        if interrupted() {
            return ReadOutcome::Interrupted;
        }
        // 旧版の PollSelector と同じく、ミリ秒へ切り上げる。
        let milliseconds = (remaining * 1e3).ceil();
        let timeout =
            Timespec::try_from(Duration::from_millis(milliseconds as u64)).unwrap_or(Timespec {
                tv_sec: i64::MAX,
                tv_nsec: 0,
            });
        let mut ready = [false; 2];
        {
            let mut fds = Vec::with_capacity(2);
            let mut indices = Vec::with_capacity(2);
            for (index, stream) in streams.iter().enumerate() {
                if let Some(stream) = stream {
                    fds.push(PollFd::new(stream, PollFlags::IN));
                    indices.push(index);
                }
            }
            match poll(&mut fds, Some(&timeout)) {
                Ok(_) => {}
                Err(Errno::INTR) => {
                    if interrupted() {
                        return ReadOutcome::Interrupted;
                    }
                    continue;
                }
                Err(error) => return ReadOutcome::Os(error),
            }
            for (fd, index) in fds.iter().zip(indices) {
                ready[index] = !fd.revents().is_empty();
            }
        }
        if seconds_until(deadline) < 0.0 {
            let [out, err] = outputs;
            return ReadOutcome::Timeout(out, err);
        }
        for index in 0..2 {
            if !ready[index] {
                continue;
            }
            let stream = streams[index].as_ref().expect("ready stream");
            let mut buffer = [0u8; 32768];
            let count = loop {
                match rustix::io::read(stream, &mut buffer) {
                    Ok(count) => break count,
                    Err(Errno::INTR) => {
                        if interrupted() {
                            return ReadOutcome::Interrupted;
                        }
                    }
                    Err(error) => return ReadOutcome::Os(error),
                }
            };
            if count == 0 {
                streams[index] = None;
            } else {
                outputs[index].extend_from_slice(&buffer[..count]);
            }
        }
    }
    let [out, err] = outputs;
    ReadOutcome::Done(out, err)
}

/// 旧`subprocess.run(args, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, timeout=timeout)`。
///
/// 期限は起動の後から数える。期限までは孫が持つpipeの終わりも待ち、期限の後は待たない。
///
/// `interrupted`は、旧Pythonの「信号を受けた印」を読む関数。poll・眠り・回収の待ちの前と、
/// それらが信号で中断した後に呼ぶ。真を返すときは印を消す(1回の信号で1回だけ真を返す)。
/// 真なら旧版の`KeyboardInterrupt`の流れへ進み、その後の0.25秒の待ちの間も呼ぶ。
pub fn python_run(
    command: &PythonCommand<'_>,
    timeout: Duration,
    interrupted: &mut dyn FnMut() -> bool,
) -> Result<Completed, RunError> {
    let (mut child, stdout, stderr) = python_popen(command).map_err(RunError::Spawn)?;
    let deadline = Instant::now() + timeout;
    let mut streams = [Some(stdout), Some(stderr)];
    let (out, err) = match read_outputs(&mut streams, deadline, interrupted) {
        ReadOutcome::Done(out, err) => (out, err),
        ReadOutcome::Timeout(stdout, stderr) => {
            // run の except TimeoutExpired: kill と wait の後に、__exit__ がpipeを閉じる。
            let error = RunError::Timeout {
                stage: TimeoutStage::Reading,
                stdout,
                stderr,
            };
            let error = kill_and_reap(&mut child, interrupted, error);
            drop(streams);
            return Err(error);
        }
        ReadOutcome::Interrupted => {
            // communicate の except KeyboardInterrupt と run の kill の後に、__exit__ が閉じる。
            let limit = seconds_until(deadline);
            let error = after_interrupt(&mut child, limit, false, true, interrupted);
            drop(streams);
            return Err(error);
        }
        ReadOutcome::Os(error) => {
            // run の except: kill の後、__exit__ がpipeを閉じてから wait する。
            if let Err(kill_error) = child.send_signal(libc::SIGKILL) {
                return Err(RunError::Os(kill_error));
            }
            drop(streams);
            return Err(match wait_blocking(&mut child, interrupted) {
                Ok(WaitOutcome::Interrupted(limit)) => {
                    after_interrupt(&mut child, limit, false, false, interrupted)
                }
                Ok(_) => RunError::Os(error),
                Err(wait_error) => RunError::Os(wait_error),
            });
        }
    };
    let remaining_seconds = seconds_until(deadline);
    match wait_with_timeout(&mut child, remaining_seconds, interrupted) {
        Ok(WaitOutcome::Done(status)) => Ok(Completed {
            returncode: status.returncode(),
            stdout: out,
            stderr: err,
        }),
        Ok(WaitOutcome::Timeout) => {
            let error = RunError::Timeout {
                stage: TimeoutStage::Waiting { remaining_seconds },
                stdout: out,
                stderr: err,
            };
            Err(kill_and_reap(&mut child, interrupted, error))
        }
        Ok(WaitOutcome::Interrupted(limit)) => {
            Err(after_interrupt(&mut child, limit, true, true, interrupted))
        }
        Err(error) => Err(kill_and_reap(&mut child, interrupted, RunError::Os(error))),
    }
}

// ---------------------------------------------------------------- 旧bashの子

/// 子の0〜2に置くもの。
///
/// 3つの指定は、子で並べ替える前の親の番号で読む。`>file 2>&1`は、1と2の両方に同じファイルを渡して表す。
#[derive(Clone, Copy, Debug)]
pub enum StdTarget<'a> {
    /// 親のその番号をそのまま渡す(閉じていれば閉じたまま)。
    Inherit,
    /// このfdを複製して置く。
    Fd(BorrowedFd<'a>),
}

/// 旧bashが外部コマンドを起動する内容。
pub struct BashCommand<'a> {
    /// [`bash_find_command`]で決めた、`execve`へ渡すパス。
    pub path: &'a [u8],
    /// 子の`argv`。旧bashは先頭にコマンドの語をそのまま置く。
    pub argv: &'a [Vec<u8>],
    /// 子の環境(`名前=値`の並び)。
    pub env: &'a [Vec<u8>],
    pub stdio: [StdTarget<'a>; 3],
    /// 子へ渡す信号の扱い。[`BashSignals::capture_and_prepare`]で作った表。
    pub signals: &'a BashSignals,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BashSpawnError {
    EmbeddedNull,
    Os(Errno),
    /// 子の中の失敗。子は回収済み。旧bashの経路と終了値は[`bash_exec_failure`]で決め、
    /// 診断は呼出側が出す。
    Child(ExecFailure),
}

/// 旧bashの外部コマンドの起動。継いだfdを閉じず、信号は起動時の扱いへ戻し、maskを継ぐ。
pub fn bash_spawn(command: &BashCommand<'_>) -> Result<Child, BashSpawnError> {
    let report = pipe().map_err(BashSpawnError::Os)?;
    let (Some(candidates), Some(argv), Some(envp)) = (
        c_strings(&[command.path.to_vec()]),
        c_strings(command.argv),
        c_strings(command.env),
    ) else {
        return Err(BashSpawnError::EmbeddedNull);
    };
    let target = |stdio: StdTarget<'_>| match stdio {
        StdTarget::Inherit => None,
        StdTarget::Fd(fd) => Some(fd.as_raw_fd()),
    };
    let plan = Plan {
        candidates,
        argv,
        envp,
        targets: [
            target(command.stdio[0]),
            target(command.stdio[1]),
            target(command.stdio[2]),
        ],
        close_inherited: false,
        signals: SignalPlan::Bash(&command.signals.original),
        flavor: Flavor::Bash,
    };
    fork_exec(&plan, report).map_err(|error| match error {
        ForkError::Os(error) => BashSpawnError::Os(error),
        ForkError::Child(failure) => BashSpawnError::Child(failure),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::atomic::{AtomicBool, Ordering};

    struct Tree(PathBuf);
    impl Tree {
        fn new(name: &str) -> Self {
            Self::new_in(&std::env::temp_dir(), name)
        }
        /// 短いパスが要るとき(`#!` の行に置くパスなど)は、`/tmp` の下に作る。Mac の一時フォルダ
        /// (`/private/var/folders/…`)は長く、`#!` の行が旧bashの読む先頭のバイト数を超えるため。
        fn new_in(base: &Path, name: &str) -> Self {
            let path = base.join(format!("r4g-process-{}-{name}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn bytes(&self, name: &str) -> Vec<u8> {
            self.0.join(name).as_os_str().as_bytes().to_vec()
        }
        fn file(&self, name: &str, body: &[u8], mode: u32) {
            let path = self.0.join(name);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, body).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn os(bytes: &[u8]) -> &std::ffi::OsStr {
        std::ffi::OsStr::from_bytes(bytes)
    }

    fn python(script: &str, args: &[&[u8]], env: &[(&str, &[u8])]) -> Vec<u8> {
        let mut command = Command::new("python3");
        command.arg("-I").arg("-c").arg(script);
        command.args(args.iter().map(|arg| os(arg)));
        for (key, value) in env {
            command.env(key, os(value));
        }
        let output = command.output().unwrap();
        assert!(output.status.success(), "{output:?}");
        output.stdout
    }

    fn never() -> bool {
        false
    }

    /// `num_threads`の欄の読み方(名前に`) `や空白があっても、最後の`) `から数える)。
    #[test]
    fn num_threads_is_read_after_the_last_parenthesis() {
        let tail = b"S 1 2 3 0 -1 4194560 10 0 0 0 1 2 0 0 20 0 7 0 100 2000 30\n";
        for name in [&b"bash"[..], b"a) b", b"x) (y) z", b") "] {
            let mut text = b"123 (".to_vec();
            text.extend_from_slice(name);
            text.extend_from_slice(b") ");
            text.extend_from_slice(tail);
            assert_eq!(num_threads_from_stat(&text), Some(7), "{name:?}");
        }
        assert_eq!(num_threads_from_stat(b"123 (bash) S 1 2"), None);
        assert_eq!(num_threads_from_stat(b"no parenthesis"), None);
    }

    /// `waitpid(-1)`で回収した終わり方は、その番号の子にだけ、1回だけ覚える(信号と core の印を含む)。
    #[test]
    fn reaped_status_is_recorded_only_for_the_same_child() {
        let mut child = Child {
            pid: 4242,
            status: None,
            core: false,
            flavor: Flavor::Bash,
        };
        assert!(!child.record_reaped(4243, 0));
        assert_eq!(child.status(), None);
        assert!(child.record_reaped(4242, libc::SIGINT | 0x80));
        assert_eq!(child.status(), Some(Status::Signaled(libc::SIGINT)));
        assert!(child.core_dumped());
        assert!(!child.record_reaped(4242, 3 << 8));
        assert_eq!(child.status(), Some(Status::Signaled(libc::SIGINT)));
        let mut exited = Child {
            pid: 7,
            status: None,
            core: false,
            flavor: Flavor::Bash,
        };
        assert!(exited.record_reaped(7, 3 << 8));
        assert_eq!(exited.status(), Some(Status::Exited(3)));
        assert!(!exited.core_dumped());
    }

    /// ほかのスレッドがあるときは、サブシェルの fork をしない(EAGAIN。子を作らない)。
    #[test]
    fn subshell_fork_refuses_when_other_threads_exist() {
        let (stop, wait) = std::sync::mpsc::channel::<()>();
        let (ready, started) = std::sync::mpsc::channel::<()>();
        let worker = std::thread::spawn(move || {
            ready.send(()).unwrap();
            let _ = wait.recv();
        });
        started.recv().unwrap();
        // 検査の枠もスレッドを使うので、数は2以上(ちょうどの数は、並んで動く検査で変わる)。
        assert!(thread_count().unwrap() >= 2);
        assert_eq!(bash_fork_subshell().err(), Some(Errno::AGAIN));
        stop.send(()).unwrap();
        worker.join().unwrap();
    }

    /// テストを動かしている PATH で道具を探す(子へ別の PATH を渡しても、比較元の起動を変えない)。
    /// その機械の bash の版(`BASH_VERSINFO`の0〜2)。
    fn bash_version(bash: &Path) -> Vec<u32> {
        let output = Command::new(bash)
            .args([
                "-c",
                "echo ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]} ${BASH_VERSINFO[2]}",
            ])
            .env_remove("BASH_ENV")
            .output()
            .unwrap()
            .stdout;
        String::from_utf8(output)
            .unwrap()
            .split_whitespace()
            .map(|part| part.parse().unwrap())
            .collect()
    }

    /// その機械の bash が基準の版(5.2.21。2026-11-12 00:35 の決定)なら、そのパス。違えば、その bash とは
    /// 比べなかったことを出して`None`(Mac の /bin/bash 3.2 などは、文も扱いも基準と違う)。
    fn baseline_bash(test: &str) -> Option<PathBuf> {
        let bash = which("bash");
        let version = bash_version(&bash);
        if version == [5, 2, 21] {
            Some(bash)
        } else {
            eprintln!(
                "{test}: その機械の bash とは比べなかった(この機械の bash は {version:?}。基準は 5.2.21)"
            );
            None
        }
    }

    fn which(name: &str) -> PathBuf {
        let path = std::env::var_os("PATH").unwrap_or_default();
        match bash_find_command(name.as_bytes(), Some(path.as_bytes()), &mut |_| None) {
            BashLookup::Found(found) => PathBuf::from(os(&found)),
            other => panic!("{name}: {other:?}"),
        }
    }

    /// `SigBlk`・`SigIgn`・`SigCgt`の行から、libcの内部用の信号(32〜34)の印を消す。
    ///
    /// glibcの posix_spawn(テストの`Command`)は子の内部用の信号を無視にし、多スレッドのglibcは
    /// 33に関数を登録する。どちらもテストの起動の仕方による差で、libcの外からは設定できない。
    fn mask_internal_signals(text: &str) -> String {
        text.split_inclusive('\n')
            .map(|line| {
                let Some((name, value)) = line.trim_end().split_once(":\t") else {
                    return line.to_string();
                };
                if !name.starts_with("Sig") {
                    return line.to_string();
                }
                let bits = u64::from_str_radix(value, 16).unwrap() & !(0b111 << 31);
                format!("{name}:\t{bits:016x}\n")
            })
            .collect()
    }

    /// 印字できるASCIIと改行だけを含む bytes の、旧Pythonの repr。
    fn bytes_repr(bytes: &[u8]) -> String {
        let mut text = String::from("b'");
        for byte in bytes {
            match byte {
                b'\n' => text.push_str("\\n"),
                b'\'' => text.push_str("\\'"),
                b'\\' => text.push_str("\\\\"),
                0x20..=0x7e => text.push(char::from(*byte)),
                other => text.push_str(&format!("\\x{other:02x}")),
            }
        }
        text.push('\'');
        text
    }

    #[test]
    fn python_environ_keeps_the_first_value_and_the_insertion_order() {
        let entries: [&[u8]; 7] = [
            b"A=1",
            b"B=2",
            b"A=3",
            b"no-equals",
            b"=leading",
            b"C=x=y",
            b"raw\xff=\xfe",
        ];
        let mut env = PythonEnviron::from_entries(entries);
        // 旧Pythonへ同じ生の環境を渡し、os.environ の並びと値を比べる。
        let tree = Tree::new("environ");
        let report = tree.0.join("report");
        // 旧Pythonは、受け継いだ PATH で探す(CI では比較用の 3.10.11)。Mac の /usr/bin/python3 は Xcode の
        // 仲介(xcrun)で、SDKROOT などを自分の環境へ足すので使わない。子の環境の PATH は下のとおり渡す。
        let python3 = which("python3").as_os_str().as_bytes().to_vec();
        let child = bash_spawn(&BashCommand {
            path: &python3,
            // argv[0] も同じパスにする(子の PATH に python3 が無い機械でも、旧Pythonが自分の置き場所を決められる)。
            argv: &[
                python3.clone(),
                b"-I".to_vec(),
                b"-c".to_vec(),
                b"import os, sys\nopen(sys.argv[1], 'wb').write(b''.join(k + b'\\0' + v + b'\\0' for k, v in os.environb.items() if k not in (b'PATH', b'LC_CTYPE')))".to_vec(),
                report.as_os_str().as_bytes().to_vec(),
            ],
            env: &entries
                .iter()
                .map(|entry| entry.to_vec())
                .chain([b"PATH=/usr/bin:/bin".to_vec(), b"LC_ALL=C.UTF-8".to_vec()])
                .collect::<Vec<_>>(),
            stdio: [StdTarget::Inherit; 3],
            signals: &BashSignals::capture_and_prepare().unwrap(),
        });
        assert_eq!(child.unwrap().wait(), Ok(Status::Exited(0)));
        let mut ours = Vec::new();
        for (key, value) in env.iter() {
            ours.extend_from_slice(key);
            ours.push(0);
            ours.extend_from_slice(value);
            ours.push(0);
        }
        ours.extend_from_slice(b"LC_ALL\0C.UTF-8\0");
        let raw = std::fs::read(&report).unwrap();
        // Mac の旧Pythonは、起動の中で CoreFoundation が自分の環境へ __CF_USER_TEXT_ENCODING を足す
        // (2026-11-12 02:41 の決定。Rust は足さない)。Mac ではその1項目がちょうど1つあることを確かめて
        // から除き、ほかを全部比べる。Linux では無いこと。
        // 項目(名前\0値\0)に分けて、その1項目だけを除く(位置は問わない。ほかの項目は全部比べる)。
        let mut fields: Vec<&[u8]> = raw.split(|byte| *byte == 0).collect();
        assert_eq!(fields.pop(), Some(&b""[..]), "{raw:?}");
        assert_eq!(fields.len() % 2, 0, "{raw:?}");
        let pairs: Vec<(&[u8], &[u8])> = fields.chunks(2).map(|pair| (pair[0], pair[1])).collect();
        let found = pairs
            .iter()
            .filter(|(key, _)| *key == b"__CF_USER_TEXT_ENCODING")
            .count();
        assert_eq!(found, usize::from(cfg!(target_os = "macos")), "{raw:?}");
        let theirs: Vec<u8> = pairs
            .iter()
            .filter(|(key, _)| *key != b"__CF_USER_TEXT_ENCODING")
            .flat_map(|(key, value)| [*key, &b"\0"[..], *value, &b"\0"[..]].concat())
            .collect();
        assert_eq!(theirs, ours);
        // 辞書の代入は位置を保ち、新しい名前は末尾へ足す。pop は並びを詰める。
        env.insert(b"B", b"9");
        env.insert(b"D", b"4");
        assert_eq!(env.remove(b"A"), Some(b"1".to_vec()));
        assert_eq!(env.remove(b"missing"), None);
        let keys: Vec<&[u8]> = env.iter().map(|(key, _)| key).collect();
        assert_eq!(keys, [&b"B"[..], b"", b"C", b"raw\xff", b"D"]);
        assert_eq!(env.get(b"B"), Some(&b"9"[..]));
    }

    const CHILD: &str = "R4G_PROCESS_CHILD";
    const REPORT: &str = "R4G_PROCESS_REPORT";

    /// 自分自身のテスト1件を、旧版の比較元と同じ起動の状態で動かす。
    fn launch_child(launcher: &[&str], action: &str, report: &Path) {
        let exe = std::env::current_exe().unwrap();
        let output = Command::new(which("python3"))
            .args(["-I", "-c", LAUNCHER])
            .args(launcher)
            .arg(&exe)
            .args([
                "--exact",
                "r4g::process::tests::scenario_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD, action)
            .env(REPORT, report)
            .output()
            .unwrap();
        assert!(output.status.success(), "{action}: {output:?}");
    }

    /// 比較元と子のテストを同じ状態で起動する。引数は SIGPIPE・SIGINT・SIGCHLD の扱い、
    /// SIGUSR1 を止めるか、fd 7 を開いて渡すか。その後に exec するコマンドが続く。
    const LAUNCHER: &str = "import os, signal, sys\n\
signal.signal(signal.SIGPIPE, getattr(signal, sys.argv[1]))\n\
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n\
signal.signal(signal.SIGINT, getattr(signal, sys.argv[2]))\n\
signal.signal(signal.SIGCHLD, getattr(signal, sys.argv[3]))\n\
if sys.argv[4] == '1':\n    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})\n\
if sys.argv[5] == '1':\n    fd = os.open('/dev/null', os.O_RDONLY)\n    os.dup2(fd, 7)\n    os.close(fd)\n\
os.execvp(sys.argv[6], sys.argv[6:])\n";

    /// 旧Pythonの比較元。各調べ方を subprocess.run で動かし、終了値・標準出力・標準エラーを書く。
    const PYTHON_ORACLE: &str = "import subprocess, sys\n\
out = open(sys.argv[1], 'wb')\n\
env = {'PATH': '/usr/bin:/bin', 'B': '2', 'A': '1', 'LC_ALL': 'C'}\n\
for probe in sys.argv[2:]:\n    args = probe.split('\\x1f')\n    r = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)\n    out.write(b'%d\\0' % r.returncode + r.stdout + b'\\0' + r.stderr + b'\\0')\n";

    fn probes() -> Vec<Vec<&'static str>> {
        let mut probes = vec![
            vec!["ls", "/dev/fd"],
            vec!["env"],
            vec!["sh", "-c", "echo out; echo err >&2; exit 3"],
            vec!["sh", "-c", "kill -TERM $$"],
        ];
        if cfg!(target_os = "linux") {
            probes.push(vec![
                "grep",
                "-E",
                "^(SigBlk|SigIgn|SigCgt)",
                "/proc/self/status",
            ]);
            probes.push(vec!["cat", "/proc/self/cmdline"]);
        }
        probes
    }

    /// 旧bashの比較元。各調べ方の出力をファイルへ向けて、bash の子として動かす。
    fn bash_oracle_script(probes: &[Vec<&str>], report: &Path) -> String {
        let mut script = String::new();
        for (index, probe) in probes.iter().enumerate() {
            let words: Vec<String> = probe
                .iter()
                .map(|word| format!("'{}'", word.replace('\'', "'\\''")))
                .collect();
            script.push_str(&format!(
                "{} >'{}.{index}' 2>&1; echo \"$?\" >>'{}.{index}'\n",
                words.join(" "),
                report.display(),
                report.display()
            ));
        }
        script.push_str(":\n");
        script
    }

    /// 報告のための子。環境変数が無い通常の実行では何もしない。
    #[test]
    fn scenario_child() {
        let Some(action) = std::env::var_os(CHILD) else {
            return;
        };
        let report = PathBuf::from(std::env::var_os(REPORT).unwrap());
        let probes = probes();
        let action = action.to_str().unwrap();
        if action == "bash-order" {
            // 入口が先に信号を無視へ変えても、子へ渡す表は起動前の記録から作る。
            set_disposition(libc::SIGXFSZ, Disposition::Ignore).unwrap();
            set_disposition(libc::SIGINT, Disposition::Ignore).unwrap();
            let signals = BashSignals::capture_and_prepare().unwrap();
            let mut text = format!(
                "{:?} {:?} {:?}",
                signals.original(libc::SIGXFSZ),
                signals.original(libc::SIGINT),
                signals.original(libc::SIGUSR1)
            );
            if cfg!(target_os = "linux") {
                let output = std::fs::File::create(report.with_extension("status")).unwrap();
                let fd = OwnedFd::from(output);
                let argv = [
                    b"grep".to_vec(),
                    b"^SigIgn".to_vec(),
                    b"/proc/self/status".to_vec(),
                ];
                let mut child = bash_spawn(&BashCommand {
                    path: b"/bin/grep",
                    argv: &argv,
                    env: &[],
                    stdio: [
                        StdTarget::Inherit,
                        StdTarget::Fd(fd.as_fd()),
                        StdTarget::Inherit,
                    ],
                    signals: &signals,
                })
                .unwrap();
                assert_eq!(child.wait(), Ok(Status::Exited(0)));
                drop(fd);
                let line = std::fs::read_to_string(report.with_extension("status")).unwrap();
                let bits =
                    u64::from_str_radix(line.trim().trim_start_matches("SigIgn:\t"), 16).unwrap();
                text.push_str(&format!(
                    " xfsz={} int={} usr1={}",
                    bits >> (libc::SIGXFSZ - 1) & 1,
                    bits >> (libc::SIGINT - 1) & 1,
                    bits >> (libc::SIGUSR1 - 1) & 1
                ));
            }
            std::fs::write(&report, text).unwrap();
            return;
        }
        if action == "bash-echild" {
            // 起動時の表を作らない(親の SIGCHLD を無視のまま)と、旧bashの子の回収は ECHILD になる。
            let env = [b"PATH=/usr/bin:/bin".to_vec()];
            let argv = [b"sh".to_vec(), b"-c".to_vec(), b"exit 5".to_vec()];
            let unprepared = BashSignals {
                original: [libc::SIG_DFL; SIGNAL_LIMIT as usize],
            };
            let spawn = |signals: &BashSignals| {
                bash_spawn(&BashCommand {
                    path: b"/bin/sh",
                    argv: &argv,
                    env: &env,
                    stdio: [StdTarget::Inherit; 3],
                    signals,
                })
                .unwrap()
                .wait()
            };
            let before = spawn(&unprepared);
            let signals = BashSignals::capture_and_prepare().unwrap();
            let after = spawn(&signals);
            let text = format!(
                "{before:?} {after:?} {:?} {:?}",
                signals.original(libc::SIGCHLD),
                disposition(libc::SIGCHLD).unwrap()
            );
            std::fs::write(&report, text).unwrap();
            return;
        }
        if let Some(path) = action.strip_prefix("python-close-") {
            // テスト専用の切替えで、close_range が使えない経路と、列挙にも失敗する経路を流す。
            TEST_CLOSE_PATH.store(path.parse().unwrap(), std::sync::atomic::Ordering::SeqCst);
        }
        if action.starts_with("python") {
            // 旧Pythonは起動時に SIGPIPE と SIGXFSZ を無視する。
            set_disposition(libc::SIGXFSZ, Disposition::Ignore).unwrap();
            let mut env = PythonEnviron::default();
            for (key, value) in [
                ("PATH", "/usr/bin:/bin"),
                ("B", "2"),
                ("A", "1"),
                ("LC_ALL", "C"),
            ] {
                env.insert(key.as_bytes(), value.as_bytes());
            }
            let mut text = Vec::new();
            for probe in &probes {
                let args: Vec<Vec<u8>> =
                    probe.iter().map(|word| word.as_bytes().to_vec()).collect();
                let result = python_run(
                    &PythonCommand {
                        args: &args,
                        env: &env,
                    },
                    Duration::from_secs(30),
                    &mut never,
                )
                .unwrap();
                text.extend_from_slice(format!("{}\0", result.returncode).as_bytes());
                text.extend_from_slice(&result.stdout);
                text.push(0);
                text.extend_from_slice(&result.stderr);
                text.push(0);
            }
            std::fs::write(&report, text).unwrap();
            let kind = TEST_LAST_CLOSE.load(std::sync::atomic::Ordering::SeqCst);
            std::fs::write(report.with_extension("close"), kind.to_string()).unwrap();
        } else {
            // 旧bashと同じく、起動の直後に信号の表を作り、親の SIGCHLD を既定へ戻す。
            let signals = BashSignals::capture_and_prepare().unwrap();
            let env = [b"PATH=/usr/bin:/bin".to_vec(), b"LC_ALL=C".to_vec()];
            for (index, probe) in probes.iter().enumerate() {
                let argv: Vec<Vec<u8>> =
                    probe.iter().map(|word| word.as_bytes().to_vec()).collect();
                let path =
                    match bash_find_command(argv[0].as_slice(), Some(b"/usr/bin:/bin"), &mut |_| {
                        None
                    }) {
                        BashLookup::Found(path) => path,
                        other => panic!("{other:?}"),
                    };
                let output =
                    std::fs::File::create(format!("{}.{index}", report.display())).unwrap();
                let fd = OwnedFd::from(output);
                let mut child = bash_spawn(&BashCommand {
                    path: &path,
                    argv: &argv,
                    env: &env,
                    stdio: [
                        StdTarget::Inherit,
                        StdTarget::Fd(fd.as_fd()),
                        StdTarget::Fd(fd.as_fd()),
                    ],
                    signals: &signals,
                })
                .unwrap();
                let status = loop {
                    match child.wait() {
                        Err(Errno::INTR) => continue,
                        other => break other.unwrap(),
                    }
                };
                drop(fd);
                // 旧bashの $? と同じ表し方(信号は 128 + 番号)。
                let code = match status {
                    Status::Exited(code) => i32::from(code),
                    Status::Signaled(signal) => 128 + signal,
                };
                let mut file = std::fs::OpenOptions::new()
                    .append(true)
                    .open(format!("{}.{index}", report.display()))
                    .unwrap();
                std::io::Write::write_all(&mut file, format!("{code}\n").as_bytes()).unwrap();
            }
        }
    }

    #[test]
    fn python_run_matches_subprocess_for_fds_signals_env_and_argv() {
        let tree = Tree::new("python-scenarios");
        let probes = probes();
        let joined: Vec<String> = probes.iter().map(|probe| probe.join("\x1f")).collect();
        for (launcher, action) in [
            (["SIG_DFL", "SIG_DFL", "SIG_DFL", "0", "0"], "python"),
            (["SIG_IGN", "SIG_IGN", "SIG_DFL", "1", "1"], "python"),
            (["SIG_DFL", "SIG_IGN", "SIG_IGN", "1", "0"], "python"),
            (
                ["SIG_DFL", "SIG_DFL", "SIG_DFL", "0", "1"],
                "python-close-1",
            ),
            (
                ["SIG_DFL", "SIG_DFL", "SIG_DFL", "0", "1"],
                "python-close-2",
            ),
        ] {
            let oracle = tree
                .0
                .join(format!("oracle-{}-{action}", launcher.join("-")));
            let ours = tree.0.join(format!("ours-{}-{action}", launcher.join("-")));
            let output = Command::new(which("python3"))
                .args(["-I", "-c", LAUNCHER])
                .args(launcher)
                .arg(which("python3"))
                .args(["-I", "-c", PYTHON_ORACLE])
                .arg(&oracle)
                .args(&joined)
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            launch_child(&launcher, action, &ours);
            let expected =
                mask_internal_signals(&String::from_utf8_lossy(&std::fs::read(&oracle).unwrap()));
            let actual =
                mask_internal_signals(&String::from_utf8_lossy(&std::fs::read(&ours).unwrap()));
            assert_eq!(actual, expected, "{launcher:?} {action}");
            // 切替えが効いて、意図した閉じ方を通った(Mac は close_range が無く、いつも列挙)。
            let kind = std::fs::read_to_string(ours.with_extension("close")).unwrap();
            let expected_kind = match action {
                "python-close-1" => "2",
                "python-close-2" => "3",
                _ if cfg!(target_os = "linux") => "1",
                _ => "2",
            };
            assert_eq!(kind, expected_kind, "{action}");
            // 調べ方が空振りしていない(fd 7 を渡しても子には無い・環境の順・終了値)。
            // SIGCHLD を無視した起動では、Linux は子を自動で回収するので、旧Pythonは回収できない子の終了値を
            // 0 にする。Mac のカーネルは自動で回収しないので、無視した起動でも本当の終了値になる(上の旧版との
            // 比較は、どちらの OS でも全部同じであることを求める)。
            let text = actual;
            assert!(text.starts_with("0\0"), "{text}");
            assert!(!text.contains("\n7\n"), "{text}");
            assert!(
                text.contains("PATH=/usr/bin:/bin\nB=2\nA=1\nLC_ALL=C\n"),
                "{text}"
            );
            if launcher[2] == "SIG_DFL" || !cfg!(target_os = "linux") {
                assert!(text.contains("3\0out\n\0err\n\0-15\0\0\0"), "{text}");
            } else {
                assert!(
                    text.contains(concat!("0\0out\n\0err\n\0", "0\0\0\0")),
                    "{text}"
                );
            }
        }
    }

    #[test]
    fn bash_spawn_matches_bash_for_inherited_fds_and_signals() {
        let tree = Tree::new("bash-scenarios");
        let probes = probes();
        // その機械の bash と比べるのは、基準の版のときだけ。Rust の子の確かめ(下の fd 7 と終了値)は
        // どの機械でも行う。
        let bash = baseline_bash("bash_spawn_matches_bash_for_inherited_fds_and_signals");
        for launcher in [
            ["SIG_DFL", "SIG_DFL", "SIG_DFL", "0", "1"],
            ["SIG_IGN", "SIG_IGN", "SIG_DFL", "1", "1"],
            ["SIG_IGN", "SIG_DFL", "SIG_IGN", "0", "0"],
        ] {
            let oracle = tree.0.join(format!("oracle-{}", launcher.join("-")));
            let ours = tree.0.join(format!("ours-{}", launcher.join("-")));
            launch_child(&launcher, "bash", &ours);
            for (index, probe) in probes.iter().enumerate() {
                let Some(bash) = &bash else {
                    break;
                };
                if index == 0 {
                    let output = Command::new(which("python3"))
                        .args(["-I", "-c", LAUNCHER])
                        .args(launcher)
                        .arg(bash)
                        .args(["-c", &bash_oracle_script(&probes, &oracle)])
                        .env("PATH", "/usr/bin:/bin")
                        .env("LC_ALL", "C")
                        .env_remove("BASH_ENV")
                        .output()
                        .unwrap();
                    assert!(output.status.success(), "{output:?}");
                }
                let expected = std::fs::read(format!("{}.{index}", oracle.display())).unwrap();
                let actual = std::fs::read(format!("{}.{index}", ours.display())).unwrap();
                // env の出力は bash が足す PWD・SHLVL・_ を含むので、名前の集合ではなく
                // 子へ渡した2つの値が両方にあることだけを比べる。
                if probe == &["env"] {
                    for line in ["PATH=/usr/bin:/bin\n", "LC_ALL=C\n"] {
                        assert!(String::from_utf8_lossy(&actual).contains(line));
                        assert!(String::from_utf8_lossy(&expected).contains(line));
                    }
                    continue;
                }
                assert_eq!(
                    mask_internal_signals(&String::from_utf8_lossy(&actual)),
                    mask_internal_signals(&String::from_utf8_lossy(&expected)),
                    "{launcher:?} {probe:?}"
                );
            }
            let fds = std::fs::read_to_string(format!("{}.0", ours.display())).unwrap();
            assert_eq!(fds.contains("\n7\n"), launcher[4] == "1", "{fds}");
            let env = std::fs::read_to_string(format!("{}.1", ours.display())).unwrap();
            for line in ["PATH=/usr/bin:/bin\n", "LC_ALL=C\n"] {
                assert!(env.contains(line), "{env}");
            }
            // SIGCHLD を無視した起動でも、旧bashと同じく子の終了値 3 を受け取る。
            let code = std::fs::read_to_string(format!("{}.2", ours.display())).unwrap();
            assert!(code.ends_with("\n3\n"), "{code}");
        }
    }

    #[test]
    fn bash_signal_table_comes_from_the_startup_record_not_the_call_time() {
        let tree = Tree::new("bash-order");
        let report = tree.0.join("report");
        // SIGUSR1 は起動時に無視。SIGXFSZ・SIGINT は起動時に既定で、入口が後で無視にする。
        let output = Command::new(which("python3"))
            .args([
                "-I", "-c", LAUNCHER, "SIG_DFL", "SIG_DFL", "SIG_DFL", "0", "0",
            ])
            .arg(which("sh"))
            .arg("-c")
            .arg("trap '' USR1; exec \"$0\" \"$@\"")
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "r4g::process::tests::scenario_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD, "bash-order")
            .env(REPORT, &report)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        let expected = if cfg!(target_os = "linux") {
            "Default Default Ignore xfsz=0 int=0 usr1=1"
        } else {
            "Default Default Ignore"
        };
        assert_eq!(std::fs::read_to_string(&report).unwrap(), expected);
    }

    #[test]
    fn bash_children_need_the_prepared_signal_table_to_be_reaped() {
        let tree = Tree::new("bash-echild");
        let report = tree.0.join("report");
        launch_child(
            &["SIG_DFL", "SIG_DFL", "SIG_IGN", "0", "0"],
            "bash-echild",
            &report,
        );
        // 表を作る前は、Linux は ECHILD を返し(旧Pythonの子のように 0 にしない)、作った後は 5 を受け取る。
        // Mac のカーネルは、起動の前から SIGCHLD を無視していても子を自動では回収しないので、作る前も 5 を
        // 受け取る(CI の macOS 15 で確かめた。旧版との差ではなく OS の違い)。
        // 子へは起動時の無視を渡し、親の SIGCHLD は既定へ戻っている。
        let before = if cfg!(target_os = "linux") {
            format!("Err({:?})", Errno::CHILD)
        } else {
            "Ok(Exited(5))".to_string()
        };
        assert_eq!(
            std::fs::read_to_string(&report).unwrap(),
            format!("{before} Ok(Exited(5)) Ignore Default")
        );
    }

    #[test]
    fn python_executable_list_follows_get_exec_path_and_join() {
        let script = "import os, sys\nfor case in sys.argv[1:]:\n    env = {} if case == '<none>' else {'PATH': case}\n    for d in os.get_exec_path(env):\n        sys.stdout.buffer.write(os.path.join(os.fsencode(d), b'prog') + b'\\0')\n    sys.stdout.buffer.write(b'|')";
        let cases: [&[u8]; 6] = [b"<none>", b"", b"/a/:/b::", b":", b"rel:/x/y", b"/\xff"];
        let expected = python(script, &cases, &[]);
        let mut ours = Vec::new();
        for case in cases {
            let mut env = PythonEnviron::default();
            if case != b"<none>" {
                env.insert(b"PATH", case);
            }
            for candidate in python_executable_list(b"prog", &env) {
                ours.extend_from_slice(&candidate);
                ours.push(0);
            }
            ours.push(b'|');
        }
        assert_eq!(ours, expected);
        assert_eq!(
            python_executable_list(b"./prog", &PythonEnviron::default()),
            [b"./prog".to_vec()]
        );
    }

    #[test]
    fn python_run_reports_the_same_exec_errno_as_subprocess() {
        let tree = Tree::new("exec-errno");
        std::fs::create_dir_all(tree.0.join("d_dir/prog")).unwrap();
        tree.file("d_noexec/prog", b"#!/bin/sh\necho noexec\n", 0o644);
        tree.file("d_exec/prog", b"#!/bin/sh\necho ran\n", 0o755);
        tree.file("d_noshebang/prog", b"\x7fELF\x00garbage", 0o755);
        tree.file("plain", b"", 0o644);
        let dir = |name: &str| String::from_utf8(tree.bytes(name)).unwrap();
        let cases = [
            format!("{}:{}", dir("d_dir"), dir("d_exec")),
            format!("{}:{}", dir("d_noexec"), dir("d_exec")),
            dir("d_noexec"),
            format!("{}:{}", dir("d_dir"), dir("missing")),
            format!("{}:{}", dir("missing"), dir("plain")),
            format!("{}:{}", dir("plain"), dir("missing")),
            dir("d_noshebang"),
            dir("missing"),
        ];
        let script = "import os, subprocess, sys\nfor path in sys.argv[1:]:\n    try:\n        r = subprocess.run(['prog'], env={'PATH': path}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n        sys.stdout.write('ok %d %r\\n' % (r.returncode, r.stdout))\n    except OSError as e:\n        sys.stdout.write('err %d %r\\n' % (e.errno, e.filename))\n";
        let args: Vec<&[u8]> = cases.iter().map(|case| case.as_bytes()).collect();
        let expected = String::from_utf8(python(script, &args, &[])).unwrap();
        let mut ours = String::new();
        for case in &cases {
            let mut env = PythonEnviron::default();
            env.insert(b"PATH", case.as_bytes());
            let args = [b"prog".to_vec()];
            match python_run(
                &PythonCommand {
                    args: &args,
                    env: &env,
                },
                Duration::from_secs(30),
                &mut never,
            ) {
                Ok(done) => ours.push_str(&format!(
                    "ok {} {}\n",
                    done.returncode,
                    bytes_repr(&done.stdout)
                )),
                Err(RunError::Spawn(SpawnError::Child(failure))) => {
                    assert_eq!(failure.stage, ExecStage::Exec);
                    ours.push_str(&format!("err {} 'prog'\n", failure.errno.raw_os_error()));
                }
                Err(other) => panic!("{other:?}"),
            }
        }
        assert_eq!(ours, expected);
        // 空振りしていない: 成功・EACCES・ENOENT・ENOTDIR・ENOEXEC がそろう。
        for errno in [13, 2, 20, 8] {
            assert!(expected.contains(&format!("err {errno} ")), "{expected}");
        }
        assert!(expected.contains("ok 0 b'ran\\n'"), "{expected}");
    }

    #[test]
    fn python_run_value_errors_precede_the_fork() {
        let mut env = PythonEnviron::default();
        env.insert(b"PATH", b"/usr/bin:/bin");
        let args = [b"tr\0ue".to_vec()];
        assert_eq!(
            python_run(
                &PythonCommand {
                    args: &args,
                    env: &env
                },
                Duration::from_secs(5),
                &mut never
            ),
            Err(RunError::Spawn(SpawnError::Value(ValueError::EmbeddedNull)))
        );
        env.insert(b"BAD=NAME", b"1");
        let args = [b"true".to_vec()];
        assert_eq!(
            python_run(
                &PythonCommand {
                    args: &args,
                    env: &env
                },
                Duration::from_secs(5),
                &mut never
            ),
            Err(RunError::Spawn(SpawnError::Value(
                ValueError::IllegalEnvironmentName
            )))
        );
    }

    #[test]
    fn python_run_timeout_stages_match_subprocess() {
        let cases = [
            "sleep 4",
            "exec >&- 2>&-; sleep 4",
            "sleep 3 & echo started",
        ];
        let script = "import subprocess, sys\nfor case in sys.argv[1:]:\n    try:\n        subprocess.run(['sh', '-c', case], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1.0)\n        print('done')\n    except subprocess.TimeoutExpired as e:\n        print('reading' if e.timeout == 1.0 else 'waiting', repr(e.output))\n";
        let args: Vec<&[u8]> = cases.iter().map(|case| case.as_bytes()).collect();
        let expected = String::from_utf8(python(script, &args, &[])).unwrap();
        let mut env = PythonEnviron::default();
        env.insert(b"PATH", b"/usr/bin:/bin");
        let mut ours = String::new();
        for case in cases {
            let args = [b"sh".to_vec(), b"-c".to_vec(), case.as_bytes().to_vec()];
            let started = Instant::now();
            let result = python_run(
                &PythonCommand {
                    args: &args,
                    env: &env,
                },
                Duration::from_secs(1),
                &mut never,
            );
            // 期限の後は孫が持つpipeを待たない。
            assert!(started.elapsed() < Duration::from_millis(2000), "{case}");
            match result {
                Err(RunError::Timeout { stage, stdout, .. }) => {
                    let output = if stdout.is_empty() {
                        "None".to_string()
                    } else {
                        format!(
                            "b'{}'",
                            String::from_utf8(stdout).unwrap().replace('\n', "\\n")
                        )
                    };
                    match stage {
                        TimeoutStage::Reading => ours.push_str(&format!("reading {output}\n")),
                        TimeoutStage::Waiting { remaining_seconds } => {
                            assert!(remaining_seconds > 0.0 && remaining_seconds <= 1.0);
                            ours.push_str(&format!("waiting {output}\n"));
                        }
                    }
                }
                other => panic!("{other:?}"),
            }
        }
        assert_eq!(ours, expected);
        assert_eq!(
            expected,
            "reading None\nwaiting None\nreading b'started\\n'\n"
        );
    }

    static INTERRUPTED: AtomicBool = AtomicBool::new(false);

    extern "C" fn record_interrupt(_: c_int) {
        INTERRUPTED.store(true, Ordering::SeqCst);
    }

    /// 待っているスレッドへだけ信号を送り、poll などを EINTR で戻す。
    // pthread_t の整数型は Linux と Mac で違う。
    #[allow(clippy::unnecessary_cast)]
    fn interrupt_later(thread: libc::pthread_t, after: Duration) -> std::thread::JoinHandle<()> {
        let thread = thread as usize;
        std::thread::spawn(move || {
            std::thread::sleep(after);
            // SAFETY: 生きているテストのスレッドへ、関数を登録した信号を送る。
            unsafe { libc::pthread_kill(thread as libc::pthread_t, libc::SIGUSR2) };
        })
    }

    #[test]
    fn interrupts_follow_the_keyboard_interrupt_flow_only_when_requested() {
        // SAFETY: SA_RESTART を付けず、記録だけをする関数を登録する。
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = record_interrupt as extern "C" fn(c_int) as libc::sighandler_t;
            assert_eq!(
                libc::sigaction(libc::SIGUSR2, &action, std::ptr::null_mut()),
                0
            );
        }
        let mut env = PythonEnviron::default();
        env.insert(b"PATH", b"/usr/bin:/bin");
        // SAFETY: 自分のスレッドの識別子を読むだけである。
        let me = unsafe { libc::pthread_self() };
        // 割込みを求めない中断は、読取りを続ける。
        let args = [
            b"sh".to_vec(),
            b"-c".to_vec(),
            b"sleep 0.5; echo done".to_vec(),
        ];
        let sender = interrupt_later(me, Duration::from_millis(100));
        let result = python_run(
            &PythonCommand {
                args: &args,
                env: &env,
            },
            Duration::from_secs(10),
            &mut || false,
        );
        sender.join().unwrap();
        assert_eq!(
            result,
            Ok(Completed {
                returncode: 0,
                stdout: b"done\n".to_vec(),
                stderr: Vec::new()
            })
        );
        // 割込みを求める中断は、0.25 秒まで待ってから SIGKILL を送り、回収せずに返す。
        INTERRUPTED.store(false, Ordering::SeqCst);
        let args = [b"sleep".to_vec(), b"5".to_vec()];
        let sender = interrupt_later(me, Duration::from_millis(100));
        let started = Instant::now();
        let result = python_run(
            &PythonCommand {
                args: &args,
                env: &env,
            },
            Duration::from_secs(10),
            &mut || INTERRUPTED.swap(false, Ordering::SeqCst),
        );
        sender.join().unwrap();
        assert_eq!(result, Err(RunError::Interrupted));
        let elapsed = started.elapsed();
        assert!(
            elapsed >= Duration::from_millis(300) && elapsed < Duration::from_secs(2),
            "{elapsed:?}"
        );
        // poll の前に届いていた信号(印が立っている)も、待つ前の確かめで取りこぼさない。
        // 印は1回だけ真を返して消えるので、その後の0.25秒の待ちは打ち切られない。
        INTERRUPTED.store(true, Ordering::SeqCst);
        let mut calls = 0;
        let started = Instant::now();
        let result = python_run(
            &PythonCommand {
                args: &args,
                env: &env,
            },
            Duration::from_secs(10),
            &mut || {
                calls += 1;
                INTERRUPTED.swap(false, Ordering::SeqCst)
            },
        );
        assert_eq!(result, Err(RunError::Interrupted));
        let elapsed = started.elapsed();
        assert!(
            elapsed >= Duration::from_millis(240) && elapsed < Duration::from_secs(2),
            "{elapsed:?}"
        );
        assert!(calls > 1, "{calls}");
    }

    #[test]
    fn bash_lookup_matches_bash() {
        let tree = Tree::new("bash-lookup");
        std::fs::create_dir_all(tree.0.join("d_dir/prog")).unwrap();
        tree.file("d_noexec/prog", b"x\n", 0o644);
        tree.file("d_exec/prog", b"#!/bin/sh\necho ran\n", 0o755);
        tree.file("home/bin/prog", b"#!/bin/sh\necho home\n", 0o755);
        let dir = |name: &str| String::from_utf8(tree.bytes(name)).unwrap();
        let cases = [
            format!("{}:{}", dir("d_dir"), dir("d_exec")),
            format!("{}:{}", dir("d_noexec"), dir("d_exec")),
            dir("d_noexec"),
            format!("{}:{}", dir("d_dir"), dir("d_noexec")),
            format!("{}:{}", dir("d_noexec"), dir("d_dir")),
            dir("missing"),
            "~/bin".to_string(),
        ];
        // どの機械でも: Rust の探し方(基準の bash 5.2.21 の規則)の固定の期待。ディレクトリの後の実行できない
        // ファイルは、旧bashと同じく「見つからない」(127)になる。
        let fixed = [
            BashLookup::Found(tree.bytes("d_exec/prog")),
            BashLookup::Found(tree.bytes("d_exec/prog")),
            BashLookup::NotExecutable(tree.bytes("d_noexec/prog")),
            BashLookup::NotFound,
            BashLookup::NotExecutable(tree.bytes("d_noexec/prog")),
            BashLookup::NotFound,
            BashLookup::Found(tree.bytes("home/bin/prog")),
        ];
        for (case, wanted) in cases.iter().zip(fixed) {
            let home = tree.bytes("home");
            let lookup = bash_find_command(b"prog", Some(case.as_bytes()), &mut |user| {
                user.is_empty().then(|| home.clone())
            });
            assert_eq!(lookup, wanted, "{case}");
        }
        // 基準の版の bash があれば、その bash の探し方とも比べる。
        let Some(bash) = baseline_bash("bash_lookup_matches_bash") else {
            return;
        };
        for case in &cases {
            let output = Command::new(&bash)
                .args([
                    "-c",
                    "command -v prog; echo \"cv=$?\"; prog >/dev/null 2>&1; echo \"rc=$?\"",
                ])
                .env("PATH", case)
                .env("HOME", tree.0.join("home"))
                .env("LC_ALL", "C")
                .env_remove("BASH_ENV")
                .output()
                .unwrap();
            let expected = String::from_utf8(output.stdout).unwrap();
            let home = tree.bytes("home");
            let lookup = bash_find_command(b"prog", Some(case.as_bytes()), &mut |user| {
                user.is_empty().then(|| home.clone())
            });
            let ours = match lookup {
                BashLookup::Found(path) => {
                    format!("{}\ncv=0\nrc=0\n", String::from_utf8(path).unwrap())
                }
                BashLookup::NotExecutable(path) => {
                    format!("{}\ncv=0\nrc=126\n", String::from_utf8(path).unwrap())
                }
                BashLookup::NotFound => "cv=1\nrc=127\n".to_string(),
            };
            assert_eq!(ours, expected, "{case}");
        }
        assert_eq!(
            bash_path_elements(b":a::b:"),
            [&b"."[..], b"a", b".", b"b", b"."]
        );
        assert_eq!(bash_path_elements(b":"), [&b"."[..]]);
        assert_eq!(
            bash_find_command(b"x/y", Some(b"/bin"), &mut |_| None),
            BashLookup::Found(b"x/y".to_vec())
        );
        assert_eq!(
            bash_find_command(b"prog", Some(b""), &mut |_| None),
            BashLookup::Found(b"prog".to_vec())
        );
    }

    #[test]
    fn bash_exec_rules_follow_each_version() {
        let rules = [
            BashExecRules::for_version(5, 1, 16),
            BashExecRules::for_version(5, 2, 0),
            BashExecRules::for_version(5, 2, 37),
            BashExecRules::for_version(5, 3, 0),
        ];
        // 5.1・5.2.0〜5.2.9・5.2.10以上・5.3 の check_binary_file(原本と修正 bash52-010)。
        let cases: [(&[u8], [bool; 4]); 7] = [
            (b"ab\0cd\n", [true; 4]),
            (b"\x7fELF\x02\x01\x01garbage\n", [false, true, true, true]),
            (b"#!\nexit 5\n\0x\n", [false, true, false, false]),
            (b"#!\nexit\0 6\n", [false, true, true, true]),
            (b"echo\n\0", [false; 4]),
            (b"#!/bin/sh\n", [false; 4]),
            (b"no newline", [false; 4]),
        ];
        for (sample, expected) in cases {
            let actual = rules.map(|rule| rule.is_binary(sample));
            assert_eq!(actual, expected, "{sample:?}");
        }
        assert_eq!(
            rules.map(|rule| rule.required_file_not_found),
            [false, true, true, false]
        );
        assert_eq!(BashExecRules::for_version(4, 4, 23), rules[0]);
        assert_eq!(BashExecRules::for_version(5, 2, 9), rules[1]);
        assert_eq!(BashExecRules::for_version(5, 2, 10), rules[2]);
        assert_eq!(BashExecRules::for_version(6, 0, 0), rules[3]);
        // getinterp: #! の後の空白を飛ばし、空白・改行・NULまで。末尾の \r は ^M。
        assert_eq!(interpreter(b"#!  /x -y\n"), b"/x");
        assert_eq!(interpreter(b"#!/x\r\nmore"), b"/x^M");
        assert_eq!(interpreter(b"#!/x\0y z"), b"/x");
        assert_eq!(interpreter(b"#!\n"), b"");
    }

    fn strerror(errno: Errno) -> String {
        let text = std::io::Error::from_raw_os_error(errno.raw_os_error()).to_string();
        text.split(" (os error").next().unwrap().to_string()
    }

    #[test]
    fn bash_exec_failure_matches_bash() {
        // 解釈系のパスは、旧bashが読む先頭(5.2 は 128 バイト、Mac の /bin/bash 3.2 は 80 バイト)に
        // 収まる短さにする(長い `#!` の行の切り方は、下で基準の版だけ確かめる)。
        let tree = Tree::new_in(Path::new("/tmp"), "bash-exec");
        tree.file("plain", b"x\n", 0o644);
        std::fs::create_dir(tree.0.join("dir")).unwrap();
        let interp_noexec = format!("#!{}\necho hi\n", tree.0.join("plain").display());
        // 名前・中身・スクリプトとして実行されたときの終了値。
        let files: [(&str, &[u8], Option<i32>); 11] = [
            ("badinterp", b"#!/nonexistent-interp\necho hi\n", None),
            ("badinterp_cr", b"#!/nonexistent-interp\r\necho hi\n", None),
            (
                "badinterp_arg",
                b"#!  /nonexistent-interp -x\necho hi\n",
                None,
            ),
            ("interp_noexec", interp_noexec.as_bytes(), None),
            ("noshebang", b"echo from-text\nexit 7\n", Some(7)),
            ("binary_nul", b"ab\0cd\nexit 3\n", Some(3)),
            (
                "elf_nonul",
                b"\x7fELF\x02\x01\x01garbage\nexit 4\n",
                Some(4),
            ),
            ("hashbang_empty_nul3", b"#!\nexit 5\n\0x\n", Some(5)),
            ("hashbang_empty_nul2", b"#!\nexit\0 6\n", Some(6)),
            ("empty", b"", None),
            ("noexec", b"echo x\n", None),
        ];
        for (name, body, _) in &files {
            tree.file(name, body, if *name == "noexec" { 0o644 } else { 0o755 });
        }
        // Rust が作る経路と終了値は、基準の版(bash 5.2.21。2026-11-12 00:35 の決定)の規則に固定する。
        // その機械の bash と比べるのは、基準の版のときだけ(Mac の /bin/bash 3.2 などは文も扱いも違う)。
        let baseline = baseline_bash("bash_exec_failure_matches_bash");
        let compare = baseline.is_some();
        let bash = baseline.clone().unwrap_or_else(|| which("bash"));
        let rules = BashExecRules::baseline();
        let kind_of = |outcome: &BashExecFailure| {
            format!("{outcome:?}")
                .split([' ', '{'])
                .next()
                .unwrap()
                .to_string()
        };
        let signals = BashSignals::capture_and_prepare().unwrap();
        let mut kinds = std::collections::BTreeSet::new();
        let names = files
            .iter()
            .map(|(name, _, script)| (*name, *script))
            .chain([("dir", None), ("missing", None)]);
        for (name, script) in names {
            let path = tree.bytes(name);
            // どの機械でも: 起動は exec の段で失敗し、その errno から経路を決める。
            let error = bash_spawn(&BashCommand {
                path: &path,
                argv: std::slice::from_ref(&path),
                env: &[b"PATH=/usr/bin:/bin".to_vec()],
                stdio: [StdTarget::Inherit; 3],
                signals: &signals,
            })
            .unwrap_err();
            let BashSpawnError::Child(failure) = error else {
                panic!("{name}: {error:?}");
            };
            assert_eq!(failure.stage, ExecStage::Exec, "{name}");
            let outcome = bash_exec_failure(&path, failure.errno, rules);
            kinds.insert(kind_of(&outcome));
            if !compare {
                continue;
            }
            // 単純なコマンド1つの -c は、bash 自身が execve し、失敗の経路を同じ形で通る。
            let output = Command::new(&bash)
                .args(["-c", "\"$0\""])
                .arg(os(&path))
                .env("LC_ALL", "C")
                .env("PATH", "/usr/bin:/bin")
                .env_remove("BASH_ENV")
                .output()
                .unwrap();
            let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
            let marker = match &outcome {
                BashExecFailure::IsDirectory { .. } => "Is a directory".to_string(),
                BashExecFailure::FileError { errno, .. } => strerror(*errno),
                BashExecFailure::RequiredFileNotFound => {
                    "cannot execute: required file not found".to_string()
                }
                BashExecFailure::BadInterpreter { errno, interpreter } => format!(
                    "{}: bad interpreter: {}",
                    String::from_utf8_lossy(interpreter),
                    strerror(*errno)
                ),
                BashExecFailure::BinaryFile => {
                    "cannot execute binary file: ".to_string() + &strerror(Errno::NOEXEC)
                }
                BashExecFailure::EmptyFile | BashExecFailure::ShellScript => String::new(),
            };
            let expected = outcome.status().map(i32::from).or(script);
            assert_eq!(
                output.status.code(),
                expected,
                "{name} {outcome:?} {stderr}"
            );
            if marker.is_empty() {
                for word in ["bad interpreter", "cannot execute", "Is a directory"] {
                    assert!(!stderr.contains(word), "{name} {outcome:?} {stderr}");
                }
            } else {
                assert!(stderr.contains(&marker), "{name} {outcome:?} {stderr}");
            }
        }
        // どの機械でも: Rust 自身の分類(基準の規則)。Linux の execve が返す errno を渡したときの経路を、
        // ファイルの中身と状態から決めることを確かめる(機械の bash も機械の errno も使わない)。
        let classified: [(&str, Errno, &str); 13] = [
            ("badinterp", Errno::NOENT, "RequiredFileNotFound"),
            ("badinterp_cr", Errno::NOENT, "RequiredFileNotFound"),
            ("badinterp_arg", Errno::NOENT, "RequiredFileNotFound"),
            ("interp_noexec", Errno::ACCESS, "BadInterpreter"),
            ("noshebang", Errno::NOEXEC, "ShellScript"),
            ("binary_nul", Errno::NOEXEC, "BinaryFile"),
            ("elf_nonul", Errno::NOEXEC, "BinaryFile"),
            ("hashbang_empty_nul3", Errno::NOEXEC, "ShellScript"),
            ("hashbang_empty_nul2", Errno::NOEXEC, "BinaryFile"),
            ("empty", Errno::NOEXEC, "EmptyFile"),
            ("noexec", Errno::ACCESS, "FileError"),
            ("dir", Errno::ACCESS, "IsDirectory"),
            ("missing", Errno::NOENT, "FileError"),
        ];
        for (name, errno, kind) in classified {
            let outcome = bash_exec_failure(&tree.bytes(name), errno, rules);
            assert_eq!(kind_of(&outcome), kind, "{name} {outcome:?}");
        }
        assert_eq!(
            bash_exec_failure(&tree.bytes("interp_noexec"), Errno::ACCESS, rules),
            BashExecFailure::BadInterpreter {
                errno: Errno::ACCESS,
                interpreter: tree.bytes("plain"),
            }
        );
        assert_eq!(
            bash_exec_failure(&tree.bytes("missing"), Errno::NOENT, rules).status(),
            Some(127)
        );
        assert_eq!(
            bash_exec_failure(&tree.bytes("noexec"), Errno::ACCESS, rules).status(),
            Some(126)
        );
        // 長い `#!` の行: 旧bash 5.2 は先頭の 128 バイトを読み、最後のバイトを NUL にしてから解釈系の名前を
        // 取る(名前は途中で切れる)。Rust はこの基準の版(5.2.21。2026-11-12 00:35 の決定)だけを再現する。
        // 読む長さの違う版(Mac の /bin/bash 3.2 は 80 バイトで、最後のバイトを NUL にしない)では比べない
        // (比べなかったことは上で出した)。
        if compare {
            let deep = tree.0.join("d".repeat(120));
            std::fs::create_dir(&deep).unwrap();
            std::fs::write(deep.join("plain"), b"x\n").unwrap();
            let line = format!("#!{}\necho hi\n", deep.join("plain").display());
            assert!(line.len() > 130, "{line}");
            tree.file("interp_long", line.as_bytes(), 0o755);
            let path = tree.bytes("interp_long");
            let output = Command::new(&bash)
                .args(["-c", "\"$0\""])
                .arg(os(&path))
                .env("LC_ALL", "C")
                .env("PATH", "/usr/bin:/bin")
                .env_remove("BASH_ENV")
                .output()
                .unwrap();
            let stderr = String::from_utf8_lossy(&output.stderr).into_owned();
            let error = bash_spawn(&BashCommand {
                path: &path,
                argv: std::slice::from_ref(&path),
                env: &[b"PATH=/usr/bin:/bin".to_vec()],
                stdio: [StdTarget::Inherit; 3],
                signals: &signals,
            })
            .unwrap_err();
            let BashSpawnError::Child(failure) = error else {
                panic!("interp_long: {error:?}");
            };
            let outcome = bash_exec_failure(&path, failure.errno, rules);
            let BashExecFailure::BadInterpreter { errno, interpreter } = &outcome else {
                panic!("interp_long: {outcome:?}");
            };
            assert_eq!(interpreter.len(), 125, "{outcome:?}");
            let marker = format!(
                "{}: bad interpreter: {}",
                String::from_utf8_lossy(interpreter),
                strerror(*errno)
            );
            assert!(stderr.contains(&marker), "{outcome:?} {stderr}");
            assert_eq!(output.status.code(), Some(126), "{stderr}");
        }
        // 空振りしていない: 基準の機械の errno で、6つの経路と required file not found を通った。
        if compare {
            for kind in [
                "IsDirectory",
                "FileError",
                "EmptyFile",
                "BinaryFile",
                "ShellScript",
                "BadInterpreter",
                "RequiredFileNotFound",
            ] {
                assert!(kinds.contains(kind), "{kinds:?}");
            }
        }
        let (read, write) = pipe().unwrap();
        assert!(read.as_raw_fd() >= 3 && write.as_raw_fd() >= 3);
    }
}
