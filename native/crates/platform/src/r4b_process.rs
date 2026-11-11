//! R4B専用。直接子だけを回収し、時間切れ後は子孫が持つpipeを待たない。
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
use std::io::{self, Read};
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub struct Captured {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}
#[derive(Debug)]
pub enum Failure {
    Timeout,
    Io(io::Error),
}
impl From<io::Error> for Failure {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

pub fn capture(command: &mut Command, timeout: Option<Duration>) -> Result<Captured, Failure> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let result = (|| {
        let mut stdout = child.stdout.take();
        let mut stderr = child.stderr.take();
        for stream in [
            stdout.as_ref().map(std::os::fd::AsFd::as_fd),
            stderr.as_ref().map(std::os::fd::AsFd::as_fd),
        ]
        .into_iter()
        .flatten()
        {
            fcntl_setfl(
                stream,
                fcntl_getfl(stream).map_err(io::Error::from)? | OFlags::NONBLOCK,
            )
            .map_err(io::Error::from)?;
        }
        let started = Instant::now();
        let mut out = Vec::new();
        let mut err = Vec::new();
        let mut status = None;
        loop {
            if let Some(limit) = timeout
                && started.elapsed() >= limit
            {
                return Err(Failure::Timeout);
            }
            if status.is_none() {
                status = child.try_wait()?;
            }
            if stdout.is_none() && stderr.is_none() && status.is_some() {
                break;
            }
            let wait = timeout
                .map(|limit| limit.saturating_sub(started.elapsed()))
                .unwrap_or(Duration::from_millis(50))
                .min(Duration::from_millis(50));
            let mut fds = Vec::new();
            if let Some(s) = stdout.as_ref() {
                fds.push(PollFd::new(s, PollFlags::IN));
            }
            if let Some(s) = stderr.as_ref() {
                fds.push(PollFd::new(s, PollFlags::IN));
            }
            match poll(&mut fds, Some(&Timespec::try_from(wait).unwrap())) {
                Ok(_) => (),
                Err(e) if e == rustix::io::Errno::INTR => continue,
                Err(e) => return Err(Failure::Io(e.into())),
            }
            drop(fds);
            fn drain<R: Read>(pipe: &mut Option<R>, output: &mut Vec<u8>) -> io::Result<()> {
                let mut buf = [0; 65536];
                // One bounded read per turn keeps the other stream and deadline observable.
                if let Some(stream) = pipe {
                    match stream.read(&mut buf) {
                        Ok(0) => {
                            pipe.take();
                        }
                        Ok(n) => output.extend_from_slice(&buf[..n]),
                        Err(e)
                            if matches!(
                                e.kind(),
                                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                            ) => {}
                        Err(e) => return Err(e),
                    }
                }
                Ok(())
            }
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
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}

pub fn is_directory(path: &std::path::Path) -> bool {
    path.is_dir()
}
pub fn metadata(path: &std::path::Path, follow: bool) -> io::Result<Option<std::fs::Metadata>> {
    let result = if follow {
        std::fs::metadata(path)
    } else {
        std::fs::symlink_metadata(path)
    };
    match result {
        Ok(value) => Ok(Some(value)),
        Err(e) if matches!(e.raw_os_error(), Some(2 | 20)) => Ok(None),
        Err(e) => Err(e),
    }
}
pub fn read_link(path: &std::path::Path) -> io::Result<std::path::PathBuf> {
    std::fs::read_link(path)
}

/// Pythonのget_terminal_sizeと同じく、標準出力の実端末を調べる。
/// 端末でない場合や取得失敗だけ80へ戻し、取得した幅はそのまま返す。
pub fn stdout_columns() -> usize {
    rustix::termios::tcgetwinsize(std::io::stdout())
        .map(|size| usize::from(size.ws_col))
        .unwrap_or(80)
}
