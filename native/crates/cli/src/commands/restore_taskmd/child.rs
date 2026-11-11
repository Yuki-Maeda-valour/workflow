//! 直接の子の状態(Python 3.10 の `subprocess.Popen` の `poll`・`wait`・`terminate`・`kill`)。
use dev_workflow_platform::restore_taskmd::{self as os, WaitStatus};
use std::io;

/// 子の操作。検査では偽の子に差し替える。
pub(super) trait ChildProcess {
    /// `Popen.poll()`。終わっていれば終了値。
    fn poll(&mut self) -> Option<i32>;
    /// `Popen.wait()`。
    fn wait(&mut self) -> io::Result<i32>;
    /// `Popen.terminate()`。
    fn terminate(&mut self) -> io::Result<()>;
    /// `Popen.kill()`。
    fn kill(&mut self) -> io::Result<()>;
}

/// 実際の子。
pub(super) struct RealChild {
    pid: i32,
    returncode: Option<i32>,
}

impl RealChild {
    pub(super) fn new(pid: i32) -> Self {
        Self {
            pid,
            returncode: None,
        }
    }

    /// `Popen.send_signal`。終わったと分かっている子へは送らない。`ESRCH` は無視する。
    fn send(&mut self, number: i32) -> io::Result<()> {
        self.poll();
        if self.returncode.is_some() {
            return Ok(());
        }
        match os::send_signal(self.pid, number) {
            Err(error) if error.raw_os_error() == Some(os::errno::ESRCH) => Ok(()),
            other => other,
        }
    }
}

impl ChildProcess for RealChild {
    fn poll(&mut self) -> Option<i32> {
        if self.returncode.is_none() {
            loop {
                match os::wait_nohang(self.pid) {
                    Ok(WaitStatus::Exited(code)) => self.returncode = Some(code),
                    Ok(WaitStatus::Running) => {}
                    // os.waitpid は EINTR を再試行する(PEP 475)。
                    Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => continue,
                    // SIGCHLD を無視した起動などで回収できない。終了値 0 で終わったと扱う。
                    Err(error) if error.raw_os_error() == Some(os::errno::ECHILD) => {
                        self.returncode = Some(0)
                    }
                    // ほかの失敗は「まだ動いている」。
                    Err(_) => {}
                }
                break;
            }
        }
        self.returncode
    }

    fn wait(&mut self) -> io::Result<i32> {
        while self.returncode.is_none() {
            match os::wait_blocking(self.pid) {
                Ok(WaitStatus::Exited(code)) => self.returncode = Some(code),
                Ok(WaitStatus::Running) => {}
                Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => {}
                Err(error) if error.raw_os_error() == Some(os::errno::ECHILD) => {
                    self.returncode = Some(0)
                }
                Err(error) => return Err(error),
            }
        }
        Ok(self.returncode.unwrap_or(0))
    }

    fn terminate(&mut self) -> io::Result<()> {
        self.send(os::SIGTERM)
    }

    fn kill(&mut self) -> io::Result<()> {
        self.send(os::SIGKILL)
    }
}
