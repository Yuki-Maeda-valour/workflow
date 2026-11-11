//! S2L専用。旧CPython3.10の起動時の信号の初期化と、起動の失敗の診断の材料。
//!
//! CPython は起動時に SIGPIPE・SIGXFSZ を無視にし、SIGINT は起動時に既定
//! (SIG_DFL)のときだけ自分の処理へ替える。無視(SIG_IGN)を受け継いだ SIGINT は
//! 無視のままにする。子は exec で処理を既定へ戻し、無視は受け継ぐ。
//! unsafe と libc の宣言はこの部品に閉じる。
use std::sync::atomic::{AtomicBool, Ordering};

const SIGINT: i32 = 2;
const SIGXFSZ: i32 = 25;
const SIG_DFL: usize = 0;
const SIG_IGN: usize = 1;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);
static HANDLED: AtomicBool = AtomicBool::new(false);

unsafe extern "C" {
    #[link_name = "signal"]
    fn c_signal(signal: i32, handler: usize) -> usize;
    #[link_name = "siginterrupt"]
    fn c_siginterrupt(signal: i32, flag: i32) -> i32;
    #[link_name = "sigaction"]
    fn c_sigaction(signal: i32, action: *const u8, old: *mut u8) -> i32;
    #[link_name = "pthread_self"]
    fn c_pthread_self() -> usize;
    #[link_name = "kill"]
    fn c_kill(pid: i32, signal: i32) -> i32;
    #[link_name = "getpid"]
    fn c_getpid() -> i32;
}

// 信号の処理の中では、原子的な旗を立てるだけにする(確保・ロックをしない)。
extern "C" fn interrupt(_signal: i32) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}

/// 起動時のSIGINTの扱い。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Interrupt {
    /// 既定だったので、旗を立てる処理へ替えた。
    Handled,
    /// 無視を受け継いだので、無視のままにした。
    Ignored,
    /// 既定でも無視でもない(exec を経た起動では起きない)。触らない。
    Other,
}

/// 現在の SIGINT の扱いを、変えずに読む。
fn current_interrupt() -> usize {
    // struct sigaction の先頭は、どの対象 OS でも処理関数の欄である。
    // OS ごとの大きさ(Linux 152・Mac 16 byte)より大きい領域を渡す。
    let mut old = [0u64; 32];
    // SAFETY: action が NULL の読取りだけ。old は sigaction の大きさより大きい。
    let status = unsafe { c_sigaction(SIGINT, std::ptr::null(), old.as_mut_ptr().cast()) };
    if status != 0 {
        return usize::MAX;
    }
    old[0] as usize
}

/// 旧CPythonの起動時と同じ順で、SIGXFSZを無視にし、SIGINTの扱いを決める。
/// 一度だけ呼ぶ(開発用入口の先頭)。
pub fn startup() -> Interrupt {
    // SAFETY: 定数の信号番号と SIG_IGN。戻り値は使わない。
    unsafe {
        c_signal(SIGXFSZ, SIG_IGN);
    }
    match current_interrupt() {
        SIG_DFL => {
            // SAFETY: 原子的な旗だけを立てる extern "C" の処理。SA_RESTART を外し、
            // 待ちの系呼出しが EINTR で戻るようにする(CPython の sa_flags と同じく再開しない)。
            unsafe {
                c_signal(SIGINT, interrupt as extern "C" fn(i32) as usize);
                c_siginterrupt(SIGINT, 1);
            }
            HANDLED.store(true, Ordering::SeqCst);
            Interrupt::Handled
        }
        SIG_IGN => Interrupt::Ignored,
        _ => Interrupt::Other,
    }
}

/// SIGINT を受けたか。受ける設定のときだけ真になりうる。
pub fn interrupted() -> bool {
    HANDLED.load(Ordering::SeqCst) && INTERRUPTED.load(Ordering::SeqCst)
}

/// 未捕捉の KeyboardInterrupt の後の CPython の終わり方(exit_sigint)。
/// SIGINT を既定へ戻して自分へ送る。止まらなければ 130 を返す。
pub fn exit_by_interrupt() -> u8 {
    // SAFETY: 定数の信号番号。自分のプロセスへだけ送る。
    unsafe {
        if c_signal(SIGINT, SIG_DFL) != usize::MAX {
            c_kill(c_getpid(), SIGINT);
        }
    }
    130
}

/// faulthandler が出す `Current thread` の値(主スレッドの pthread_self)。
pub fn current_thread() -> usize {
    // SAFETY: 引数の無い読取り。
    unsafe { c_pthread_self() }
}

/// 標準入出力の番号が開いたディレクトリか(CPython の init_sys_streams の検査)。
pub fn is_directory(fd: i32) -> bool {
    use std::os::fd::BorrowedFd;
    if !(0..=2).contains(&fd) {
        return false;
    }
    // SAFETY: 0〜2 の標準の番号を、この呼出しの間だけ借りる。閉じない。
    let borrowed = unsafe { BorrowedFd::borrow_raw(fd) };
    rustix::fs::fstat(borrowed).is_ok_and(|st| {
        rustix::fs::FileType::from_raw_mode(st.st_mode) == rustix::fs::FileType::Directory
    })
}

#[cfg(test)]
pub fn reset_for_test(handled: bool) {
    INTERRUPTED.store(false, Ordering::SeqCst);
    HANDLED.store(handled, Ordering::SeqCst);
}

/// 検査用。自分のプロセスへ SIGINT を送る(端末の Ctrl-C と同じ信号)。
pub fn raise_interrupt() {
    // SAFETY: 定数の信号番号。自分のプロセスへだけ送る。
    unsafe {
        c_kill(c_getpid(), SIGINT);
    }
}
