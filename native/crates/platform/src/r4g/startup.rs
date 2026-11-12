//! 起動前の状態の記録と戻し。
//!
//! Rustの標準ライブラリは`main`の前に、閉じたfd 0〜2を`/dev/null`で開き、`SIGPIPE`を無視へ変える。
//! 旧Pythonと旧bashは`SIGPIPE`を変えないので、比べるには変える前の信号の扱いが要る。
//!
//! 閉じたfd 0〜2は、標準ライブラリの初期化のとおり`/dev/null`で開いたまま使う(2026-11-12 08:55の決定。
//! 旧版との差を認める)。fdの起動前の状態は記録しない。
//!
//! 方式: [`r4g_record_startup_state!`](crate::r4g_record_startup_state)を展開した実行ファイルだけが、
//! ローダーが`main`より前に呼ぶ初期化の表(Linuxは`.init_array`、Macは`__mod_init_func`)へ
//! [`record_before_std`]を載せる。表の関数は、標準ライブラリの初期化(`main`の中の`lang_start`)より前に動く。
//!
//! 製品の実行ファイル(`dev-workflow`)はこのマクロを展開しない。表へ載せる`static`はマクロを
//! 展開した側のcrateにだけ生まれるので、platformを結合しても製品の起動前の関数は増えない。
//! 記録した値は、展開した実行ファイルの中でだけ意味を持つ。

use super::Errno;
use std::os::raw::c_int;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU8, Ordering};

static RECORDED: AtomicBool = AtomicBool::new(false);
static SIGPIPE: AtomicI32 = AtomicI32::new(0);

/// 記録する信号の数(Linuxは`_NSIG`、Macは`NSIG`)。番号0は使わない。
#[cfg(target_os = "linux")]
pub(super) const SIGNAL_SLOTS: usize = 65;
#[cfg(not(target_os = "linux"))]
pub(super) const SIGNAL_SLOTS: usize = 32;

/// 信号ごとの起動前の扱い。0は読めなかった(libcの内部用の信号など)、1は既定、2は無視、3は関数。
static SIGNALS: [AtomicU8; SIGNAL_SLOTS] = [const { AtomicU8::new(0) }; SIGNAL_SLOTS];

const SIGPIPE_DEFAULT: i32 = 0;
const SIGPIPE_IGNORE: i32 = 1;
const SIGPIPE_HANDLER: i32 = 2;

/// 信号の扱い。`execve`を越えて継げるのは既定と無視だけ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Default,
    Ignore,
    Handler,
}

/// 標準ライブラリが変える前の状態。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StartupState {
    /// 親から継いだ`SIGPIPE`の扱い。
    pub sigpipe: Disposition,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StartupError {
    /// 起動前の記録が動いていない(マクロを展開していない・対応外のOS)。
    NotRecorded,
    /// `SIGPIPE`の扱いを読めなかった。
    SigpipeQuery(Errno),
}

fn errno_value(error: Errno) -> i32 {
    error.raw_os_error()
}

/// 起動前の状態を記録する。ローダーが`main`より前に呼ぶ。
///
/// 2回目からは何もしない。確保をせず、libcの読取りだけを使う。
///
/// # Safety
///
/// 標準ライブラリの初期化より前(初期化の表から)にだけ呼ぶ。`main`の後に呼ぶと、
/// 標準ライブラリが変えた後の状態を、起動前の状態として記録してしまう。
#[doc(hidden)]
pub unsafe extern "C" fn record_before_std() {
    if RECORDED.load(Ordering::Acquire) {
        return;
    }
    // SAFETY: zeroedのsigactionは出力先として有効である。
    let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: 新しい扱いにnullを渡すので、扱いを読むだけで変えない。
    let sigpipe = if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut old) } == 0 {
        match old.sa_sigaction {
            libc::SIG_DFL => SIGPIPE_DEFAULT,
            libc::SIG_IGN => SIGPIPE_IGNORE,
            _ => SIGPIPE_HANDLER,
        }
    } else {
        -errno_value(super::last_errno())
    };
    SIGPIPE.store(sigpipe, Ordering::Relaxed);
    // 1〜(信号の数-1)の扱いを読むだけで記録する。旧bashは起動時の扱いを子へ渡すので、
    // 入口が後で変えた扱い(Pythonと同じ SIGXFSZ の無視など)に左右されないようにする。
    for signal in 1..SIGNAL_SLOTS as c_int {
        // SAFETY: zeroedのsigactionは出力先として有効である。
        let mut old: libc::sigaction = unsafe { std::mem::zeroed() };
        // SAFETY: 新しい扱いにnullを渡すので、読むだけで変えない。
        let value = if unsafe { libc::sigaction(signal, std::ptr::null(), &mut old) } != 0 {
            0
        } else {
            match old.sa_sigaction {
                libc::SIG_DFL => 1,
                libc::SIG_IGN => 2,
                _ => 3,
            }
        };
        SIGNALS[signal as usize].store(value, Ordering::Relaxed);
    }
    RECORDED.store(true, Ordering::Release);
}

/// 起動前の記録を読む。記録が無ければ`NotRecorded`を返し、既定値を作らない。
pub fn recorded() -> Result<StartupState, StartupError> {
    if !RECORDED.load(Ordering::Acquire) {
        return Err(StartupError::NotRecorded);
    }
    let sigpipe = match SIGPIPE.load(Ordering::Relaxed) {
        SIGPIPE_DEFAULT => Disposition::Default,
        SIGPIPE_IGNORE => Disposition::Ignore,
        SIGPIPE_HANDLER => Disposition::Handler,
        negative => {
            return Err(StartupError::SigpipeQuery(Errno::from_raw_os_error(
                -negative,
            )));
        }
    };
    Ok(StartupState { sigpipe })
}

/// 信号1つの起動前の扱い。libcが扱いを読ませなかった信号と範囲の外の番号は`None`。
pub fn recorded_disposition(signal: c_int) -> Result<Option<Disposition>, StartupError> {
    if !RECORDED.load(Ordering::Acquire) {
        return Err(StartupError::NotRecorded);
    }
    let Some(slot) = usize::try_from(signal)
        .ok()
        .filter(|index| *index > 0)
        .and_then(|index| SIGNALS.get(index))
    else {
        return Ok(None);
    };
    Ok(match slot.load(Ordering::Relaxed) {
        1 => Some(Disposition::Default),
        2 => Some(Disposition::Ignore),
        3 => Some(Disposition::Handler),
        _ => None,
    })
}

/// `SIGPIPE`を、起動前の記録の扱い(既定か無視)へ戻す。記録が無ければ何もせず`NotRecorded`。
///
/// 旧bashのように、継いだ扱いで閉じたpipeへの書込みを扱う(既定なら信号で終わる)ときに使う。
/// 呼出側が作った値ではなく、記録だけを読む。
pub fn restore_sigpipe() -> Result<(), RestoreError> {
    let state = recorded().map_err(RestoreError::Startup)?;
    let to = match state.sigpipe {
        Disposition::Ignore => Disposition::Ignore,
        Disposition::Default | Disposition::Handler => Disposition::Default,
    };
    super::process::set_disposition(libc::SIGPIPE, to)
        .map(|_| ())
        .map_err(RestoreError::Os)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RestoreError {
    Startup(StartupError),
    Os(Errno),
}

// ---------------------------------------------------------------- 旧Pythonの起動時の信号

/// 旧Pythonの「`SIGINT`を受けた印」(`signal_handler`が立て、評価の合間に読む)。
static PYTHON_INTERRUPTED: AtomicBool = AtomicBool::new(false);

extern "C" fn python_interrupt_handler(_signal: c_int) {
    // 信号の中で安全な操作(原子的な書込み)だけをする。
    PYTHON_INTERRUPTED.store(true, Ordering::SeqCst);
}

/// 旧Python 3.10の`signal_install_handlers`と同じ信号の設定をする。
///
/// - `SIGPIPE`・`SIGXFSZ`を無視にする。
/// - `SIGINT`は、起動前の記録で既定だったときだけ、印を立てる関数を置く(旧Pythonの
///   `default_int_handler`の登録)。`SA_ONSTACK`だけを付け、`SA_RESTART`を付けない(`PyOS_setsig`)。
///   起動時に無視だったときは無視のまま。
///
/// 戻り値は`SIGINT`の関数を置いたか。記録が無ければ何も変えずに`NotRecorded`を返す。
pub fn install_python_signals() -> Result<bool, PythonSignalError> {
    let sigint = recorded_disposition(libc::SIGINT).map_err(PythonSignalError::Startup)?;
    for signal in [libc::SIGPIPE, libc::SIGXFSZ] {
        super::process::set_disposition(signal, Disposition::Ignore)
            .map_err(PythonSignalError::Os)?;
    }
    if sigint != Some(Disposition::Default) {
        return Ok(false);
    }
    // SAFETY: zeroedのsigactionに、原子的な書込みだけをする関数と空のmaskを入れる。
    let mut new: libc::sigaction = unsafe { std::mem::zeroed() };
    new.sa_sigaction = python_interrupt_handler as extern "C" fn(c_int) as libc::sighandler_t;
    new.sa_flags = libc::SA_ONSTACK;
    // SAFETY: sa_maskは有効な集合を指す。
    unsafe { libc::sigemptyset(&mut new.sa_mask) };
    // SAFETY: 有効なsigactionを渡し、前の扱いは受け取らない。
    if unsafe { libc::sigaction(libc::SIGINT, &new, std::ptr::null_mut()) } != 0 {
        return Err(PythonSignalError::Os(super::last_errno()));
    }
    Ok(true)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PythonSignalError {
    Startup(StartupError),
    Os(Errno),
}

/// 印を読み、立っていれば下ろして`true`を返す(旧Pythonが`KeyboardInterrupt`を投げる時点)。
pub fn take_python_interrupt() -> bool {
    PYTHON_INTERRUPTED.swap(false, Ordering::SeqCst)
}

/// 自分へ信号を送る(`kill(getpid(), signal)`)。検査専用の注入が、決めた時点の信号に使う。
pub fn signal_self(signal: c_int) -> Result<(), Errno> {
    // SAFETY: killは引数の値だけを読む。
    if unsafe { libc::kill(libc::getpid(), signal) } != 0 {
        return Err(super::last_errno());
    }
    Ok(())
}

/// 旧Pythonの`exit_sigint`: `SIGINT`を既定へ戻して自分へ送る。終わらなければ`128 + SIGINT`で終える。
pub fn exit_by_python_interrupt() -> ! {
    if super::process::set_disposition(libc::SIGINT, Disposition::Default).is_ok() {
        let _ = signal_self(libc::SIGINT);
    }
    // SAFETY: Cの`exit`。旧Pythonの`exit(SIGINT + 128)`と同じ。
    unsafe { libc::exit(128 + libc::SIGINT) }
}

/// 実行ファイルに1回だけ展開し、起動前の状態を記録させる。
///
/// R4Gの「unsafe(安全検査を外す書き方)はplatformの中だけ」の、ただ1つの例外である。
/// 展開した側のcrateに`#[unsafe(link_section)]`の`static`が生まれる(外部のマクロの展開なので、
/// 展開した側の`#![forbid(unsafe_code)]`には当たらない)。展開してよいのは検査専用の入口
/// `examples/r4g_contract.rs`の1か所と、このモジュールのテストだけ。
///
/// 展開した実行ファイルの初期化の表へ[`record_before_std`]を載せる。
/// 製品の実行ファイル(`dev-workflow`)では展開しない。対応外のOSではコンパイルを止める。
#[macro_export]
macro_rules! r4g_record_startup_state {
    () => {
        #[cfg(not(any(target_os = "linux", target_os = "android", target_vendor = "apple")))]
        compile_error!("R4G の起動前の記録は Linux と Mac だけに対応する");
        const _: () = {
            #[used]
            #[cfg_attr(
                any(target_os = "linux", target_os = "android"),
                unsafe(link_section = ".init_array")
            )]
            #[cfg_attr(
                target_vendor = "apple",
                unsafe(link_section = "__DATA,__mod_init_func,mod_init_funcs")
            )]
            static RECORD_BEFORE_STD: unsafe extern "C" fn() =
                $crate::r4g::startup::record_before_std;
        };
    };
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    crate::r4g_record_startup_state!();

    const REPORT: &str = "R4G_STARTUP_REPORT";

    fn scratch(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("r4g-startup-{}-{name}", std::process::id()))
    }

    /// 自分自身のテスト1件を、シェルで状態を変えてから起動し、報告のファイルを読む。
    fn child_report(prefix: &str, name: &str) -> String {
        let report = scratch(name);
        let _ = std::fs::remove_file(&report);
        let status = Command::new("/bin/sh")
            .arg("-c")
            .arg(format!("{prefix}\nexec \"$0\" \"$@\""))
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "r4g::startup::tests::startup_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(REPORT, &report)
            .stdout(Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "{name}: {status}");
        let text = std::fs::read_to_string(&report).unwrap();
        std::fs::remove_file(&report).unwrap();
        text
    }

    fn getfd(fd: i32) -> bool {
        // SAFETY: F_GETFDは記述子の表を読むだけである。
        unsafe { libc::fcntl(fd, libc::F_GETFD) != -1 }
    }

    /// 報告のための子。環境変数が無い通常の実行では何もしない。
    #[test]
    fn startup_child() {
        let Some(path) = std::env::var_os(REPORT) else {
            return;
        };
        let state = recorded().unwrap();
        let mut text = format!("{:?}", state.sigpipe);
        // 標準ライブラリが閉じたfd 0〜2を開いた後の状態(2026-11-12 08:55の決定: そのまま使う)と、今の SIGPIPE。
        text.push(' ');
        for fd in 0..3 {
            text.push(if getfd(fd) { 'o' } else { 'c' });
        }
        text.push(' ');
        text.push_str(&format!(
            "{:?}",
            crate::r4g::process::disposition(libc::SIGPIPE).unwrap()
        ));
        // 戻した後の SIGPIPE。
        restore_sigpipe().unwrap();
        text.push(' ');
        text.push_str(&format!(
            "{:?}",
            crate::r4g::process::disposition(libc::SIGPIPE).unwrap()
        ));
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn the_record_precedes_the_standard_library() {
        // 閉じたfdは、標準ライブラリが開いたまま(ooo)。SIGPIPE は起動前の扱いを記録し、戻せる。
        let cases = [
            ("open", ":", "Default ooo Ignore Default"),
            (
                "stdin-stdout-closed",
                "exec 0<&- 1>&-",
                "Default ooo Ignore Default",
            ),
            (
                "all-closed",
                "exec 0<&- 1>&- 2>&-",
                "Default ooo Ignore Default",
            ),
            (
                "sigpipe-ignored",
                "trap '' PIPE",
                "Ignore ooo Ignore Ignore",
            ),
            (
                "stderr-closed-sigpipe-ignored",
                "trap '' PIPE; exec 2>&-",
                "Ignore ooo Ignore Ignore",
            ),
        ];
        for (name, prefix, expected) in cases {
            assert_eq!(child_report(prefix, name), expected, "{name}");
        }
    }

    /// 信号の記録の報告のための子。環境変数が無い通常の実行では何もしない。
    #[test]
    fn signal_record_child() {
        let Some(path) = std::env::var_os("R4G_SIGNAL_REPORT") else {
            return;
        };
        // 記録の後に扱いを変えても、記録は起動前のまま。
        crate::r4g::process::set_disposition(libc::SIGXFSZ, Disposition::Default).unwrap();
        crate::r4g::process::set_disposition(libc::SIGTERM, Disposition::Ignore).unwrap();
        let text = [
            libc::SIGUSR1,
            libc::SIGXFSZ,
            libc::SIGTERM,
            libc::SIGPIPE,
            0,
            999,
        ]
        .map(|signal| format!("{:?}", recorded_disposition(signal).unwrap()))
        .join(" ");
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn every_signal_is_recorded_before_the_standard_library() {
        let report = scratch("signals");
        let _ = std::fs::remove_file(&report);
        let status = Command::new("/bin/sh")
            .arg("-c")
            .arg("trap '' USR1 XFSZ\nexec \"$0\" \"$@\"")
            .arg(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "r4g::startup::tests::signal_record_child",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("R4G_SIGNAL_REPORT", &report)
            .stdout(Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
        // SIGPIPE は標準ライブラリが無視へ変える前の既定。範囲の外は None。
        assert_eq!(
            std::fs::read_to_string(&report).unwrap(),
            "Some(Ignore) Some(Ignore) Some(Default) Some(Default) None None"
        );
        std::fs::remove_file(&report).unwrap();
    }

    #[test]
    fn this_test_binary_has_a_record_and_the_library_changed_sigpipe() {
        // 記録がある。標準ライブラリは SIGPIPE を無視へ変えている。
        recorded().unwrap();
        assert_eq!(
            crate::r4g::process::disposition(libc::SIGPIPE).unwrap(),
            Disposition::Ignore
        );
    }
}
