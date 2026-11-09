//! 直接子1個と親側pipeを所有する。群・子孫の監督は行わない。
use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
use std::io::{self, Read, Write};
use std::os::fd::AsFd;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::time::Duration;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Exited(u8),
    Signaled(i32),
}

/// Dropは回収を保証しない。回収不能時はinto_childで所有権を返す。
pub struct DirectChild {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: Option<ChildStdout>,
    stderr: Option<ChildStderr>,
}

impl DirectChild {
    pub fn spawn(command: &mut Command) -> io::Result<Self> {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        Ok(Self {
            stdin: child.stdin.take(),
            stdout: child.stdout.take(),
            stderr: child.stderr.take(),
            child,
        })
    }

    pub fn setup(&self) -> io::Result<()> {
        for fd in [
            self.stdin.as_ref().unwrap().as_fd(),
            self.stdout.as_ref().unwrap().as_fd(),
            self.stderr.as_ref().unwrap().as_fd(),
        ] {
            fcntl_setfl(fd, fcntl_getfl(fd)? | OFlags::NONBLOCK)?;
        }
        Ok(())
    }

    pub fn open_streams(&self) -> [bool; 3] {
        [
            self.stdin.is_some(),
            self.stdout.is_some(),
            self.stderr.is_some(),
        ]
    }

    pub fn close(&mut self, stream: usize) {
        match stream {
            0 => {
                self.stdin.take();
            }
            1 => {
                self.stdout.take();
            }
            2 => {
                self.stderr.take();
            }
            _ => unreachable!("invalid stream"),
        }
    }

    pub fn poll(&self, timeout: Duration) -> io::Result<[bool; 3]> {
        let mut fds = Vec::with_capacity(3);
        let mut indices = Vec::with_capacity(3);
        if let Some(stream) = &self.stdin {
            fds.push(PollFd::new(stream, PollFlags::OUT));
            indices.push(0);
        }
        if let Some(stream) = &self.stdout {
            fds.push(PollFd::new(stream, PollFlags::IN));
            indices.push(1);
        }
        if let Some(stream) = &self.stderr {
            fds.push(PollFd::new(stream, PollFlags::IN));
            indices.push(2);
        }
        let timeout = Timespec::try_from(timeout)
            .map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        poll(&mut fds, Some(&timeout))?;
        let mut ready = [false; 3];
        for (fd, index) in fds.iter().zip(indices) {
            ready[index] = !fd.revents().is_empty();
        }
        Ok(ready)
    }

    pub fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.stdin.as_mut().unwrap().write(bytes)
    }

    pub fn read(&mut self, stream: usize, bytes: &mut [u8]) -> io::Result<usize> {
        match stream {
            1 => self.stdout.as_mut().unwrap().read(bytes),
            2 => self.stderr.as_mut().unwrap().read(bytes),
            _ => unreachable!("invalid output stream"),
        }
    }

    pub fn try_wait(&mut self) -> io::Result<Option<Status>> {
        self.child
            .try_wait()?
            .map(|status| {
                if let Some(code) = status.code() {
                    u8::try_from(code)
                        .map(Status::Exited)
                        .map_err(|_| io::Error::from(io::ErrorKind::InvalidData))
                } else {
                    status
                        .signal()
                        .map(Status::Signaled)
                        .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidData))
                }
            })
            .transpose()
    }

    pub fn kill(&mut self) -> io::Result<()> {
        self.child.kill()
    }

    pub fn into_child(mut self) -> Child {
        for stream in 0..3 {
            self.close(stream);
        }
        self.child
    }
}
