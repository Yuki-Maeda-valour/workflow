//! Signal handlers only store an integer; cleanup remains in ordinary Rust code.
use std::io;
use std::sync::atomic::{AtomicI32, AtomicU8, AtomicUsize, Ordering};
// The Rust runtime changes SIGPIPE before main. Only the development S3D
// executable registers `remember_startup_state` as a loader initializer, so the
// product executable has no code before main. No allocation or process-wide
// change happens here.
static CLOSED_STANDARD: AtomicU8 = AtomicU8::new(0);
static INHERITED_PIPE: AtomicUsize = AtomicUsize::new(libc::SIG_DFL);
/// Record the closed standard descriptors and the inherited SIGPIPE
/// disposition. Register it only from an S3D executable's loader initializer.
pub extern "C" fn remember_startup_state() {
    let mut closed = 0;
    for fd in 0..=2 {
        if rustix::io::fcntl_getfd(super::fs::standard(fd)).is_err() {
            closed |= 1 << fd;
        }
    }
    CLOSED_STANDARD.store(closed, Ordering::Relaxed);
    // SAFETY: sigaction only reads the current disposition into a zeroed structure.
    let mut old = unsafe { std::mem::zeroed::<libc::sigaction>() };
    if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut old) } == 0 {
        INHERITED_PIPE.store(old.sa_sigaction, Ordering::Relaxed);
    }
}
pub(crate) fn preserve_child_pipe(command: &mut std::process::Command) {
    use std::os::unix::process::CommandExt;
    let disposition = INHERITED_PIPE.load(Ordering::Relaxed);
    // SAFETY: the pre-exec hook performs only the async-signal-safe signal call.
    unsafe {
        command.pre_exec(move || {
            if libc::signal(libc::SIGPIPE, disposition) == libc::SIG_ERR {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
}
static RECEIVED: AtomicI32 = AtomicI32::new(0);
/// The signal description Bash prints (`strsignal`): the glibc 2.39 text on
/// Linux (the static musl build has its own), the system's on macOS.
pub fn description(signal: i32) -> String {
    #[cfg(target_os = "macos")]
    {
        // SAFETY: strsignal returns a C string valid until the next call.
        let text = unsafe { std::ffi::CStr::from_ptr(libc::strsignal(signal)) };
        text.to_string_lossy().into_owned()
    }
    #[cfg(not(target_os = "macos"))]
    {
        match signal {
            libc::SIGHUP => "Hangup".into(),
            libc::SIGINT => "Interrupt".into(),
            libc::SIGTERM => "Terminated".into(),
            libc::SIGPIPE => "Broken pipe".into(),
            other => format!("Unknown signal {other}"),
        }
    }
}
/// The line Bash prints for its foreground job ended by `signal`, when the old
/// script traps that signal and Bash reports it (TERM and HUP; not INT, and
/// SIGPIPE is never reported).
pub fn job_report(signal: Option<i32>) -> Option<String> {
    match signal {
        Some(s @ (libc::SIGTERM | libc::SIGHUP)) => Some(description(s)),
        _ => None,
    }
}
pub fn pending() -> bool {
    RECEIVED.load(Ordering::SeqCst) != 0
}
extern "C" fn notify(signal: libc::c_int) {
    RECEIVED.store(signal, Ordering::SeqCst);
}
#[derive(Clone)]
pub struct Signals(std::sync::Arc<SignalGuard>);
struct SignalGuard {
    previous: Vec<(libc::c_int, libc::sigaction)>,
}
impl Signals {
    pub fn install() -> io::Result<Self> {
        RECEIVED.store(0, Ordering::SeqCst);
        let mut guard = SignalGuard {
            previous: Vec::new(),
        };
        // SAFETY: sigaction is a plain C structure for which all-zero bytes are a valid value.
        let mut old = unsafe { std::mem::zeroed::<libc::sigaction>() };
        // SAFETY: a null new action only reads the current disposition into the valid `old` structure.
        if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut old) } != 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: signal takes a valid signal number and a handler value saved from this process.
        if unsafe { libc::signal(libc::SIGPIPE, INHERITED_PIPE.load(Ordering::Relaxed)) }
            == libc::SIG_ERR
        {
            return Err(io::Error::last_os_error());
        }
        guard.previous.push((libc::SIGPIPE, old));
        for signal in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
            // SAFETY: sigaction receives initialized, correctly sized native structures.
            let mut old = unsafe { std::mem::zeroed::<libc::sigaction>() };
            // SAFETY: a null new action only reads the current disposition into the valid `old` structure.
            if unsafe { libc::sigaction(signal, std::ptr::null(), &mut old) } != 0 {
                return Err(io::Error::last_os_error());
            }
            if old.sa_sigaction == libc::SIG_IGN {
                continue;
            }
            let mut next = old;
            next.sa_sigaction = notify as *const () as usize;
            next.sa_flags = 0;
            // SAFETY: sigemptyset writes only the signal mask inside the valid local `next` structure.
            unsafe {
                libc::sigemptyset(&mut next.sa_mask);
            }
            // SAFETY: `next` is fully initialized and `notify` is an async-signal-safe handler.
            if unsafe { libc::sigaction(signal, &next, std::ptr::null_mut()) } != 0 {
                return Err(io::Error::last_os_error());
            }
            guard.previous.push((signal, old));
        }
        Ok(Self(std::sync::Arc::new(guard)))
    }
    pub fn received(&self) -> Option<i32> {
        let _ = &self.0;
        match RECEIVED.load(Ordering::SeqCst) {
            0 => None,
            s => Some(s),
        }
    }
}
impl Drop for SignalGuard {
    fn drop(&mut self) {
        for (signal, old) in self.previous.iter().rev() {
            unsafe {
                libc::sigaction(*signal, old, std::ptr::null_mut());
            }
        }
    }
}

/// CPython 3.10's pthread backend casts pthread_self() to unsigned long.
pub fn legacy_thread_identifier() -> String {
    // SAFETY: pthread_self has no preconditions and only identifies this thread.
    let thread = unsafe { libc::pthread_self() } as libc::c_ulong;
    format!(
        "{thread:0width$x}",
        width = std::mem::size_of::<libc::c_ulong>() * 2
    )
}

/// Rust startup may replace a closed standard descriptor with /dev/null. Keep
/// the S3D write outcome without changing descriptors in unrelated commands.
pub(crate) fn initially_closed(fd: i32) -> bool {
    CLOSED_STANDARD.load(Ordering::Relaxed) & (1 << fd) != 0
}

#[cfg(test)]
mod tests {
    #[test]
    fn python_thread_field_retains_the_old_hexadecimal_format() {
        let value = super::legacy_thread_identifier();
        assert_eq!(value.len(), std::mem::size_of::<libc::c_ulong>() * 2);
        assert!(
            value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        );
    }
}
