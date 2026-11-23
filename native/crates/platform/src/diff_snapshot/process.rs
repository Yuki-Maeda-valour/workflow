//! Unbounded command execution; only profile Git receives its legacy timeout.
use crate::process::{DirectChild, Status};
use std::ffi::OsString;
use std::io::{self, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
#[derive(Debug)]
pub struct Output {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    /// The signal that ended the child, if any (`code` is then 128 + it).
    pub signal: Option<i32>,
}
fn code(status: Status) -> i32 {
    match status {
        Status::Exited(c) => i32::from(c),
        Status::Signaled(s) => 128 + s,
    }
}
// Failed waits retain the actual Child. Neither a diagnostic conversion nor a
// second wait failure may discard ownership. This store is private to S3D.
static PENDING: Mutex<Vec<Child>> = Mutex::new(Vec::new());
fn wait_retry(child: &mut Child) -> io::Result<ExitStatus> {
    loop {
        match child.wait() {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            result => return result,
        }
    }
}
fn retain_after_wait(
    mut child: Child,
    wait: impl FnOnce(&mut Child) -> io::Result<ExitStatus>,
) -> io::Result<ExitStatus> {
    let result = wait(&mut child);
    if let Err(error) = &result
        && error.raw_os_error() != Some(rustix::io::Errno::CHILD.raw_os_error())
    {
        PENDING
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push(child);
    }
    result
}
fn recover(child: Child, kill: bool) {
    let mut child = child;
    if kill {
        let _ = child.kill();
    }
    let _ = retain_after_wait(child, wait_retry);
}
/// Retry without an invented deadline. Children still not waitable stay owned.
pub fn retry_pending() -> usize {
    let children = std::mem::take(&mut *PENDING.lock().unwrap_or_else(|p| p.into_inner()));
    for child in children {
        let _ = retain_after_wait(child, wait_retry);
    }
    PENDING.lock().unwrap_or_else(|p| p.into_inner()).len()
}

/// The profile helper used /dev/null for both stdin and stderr. Do not capture
/// and discard stderr: that would itself add a body read absent in the legacy.
pub fn profile_capture(command: &mut Command, timeout: Duration) -> io::Result<Output> {
    profile_capture_clocked(command, timeout, |start| start.elapsed())
}
/// `profile_capture` with the elapsed time given by `elapsed(start)`. Only the
/// typed check gives a substituted time; the product passes the real clock.
pub fn profile_capture_clocked(
    command: &mut Command,
    timeout: Duration,
    mut elapsed: impl FnMut(Instant) -> Duration,
) -> io::Result<Output> {
    use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
    use std::os::fd::AsFd;
    use std::os::unix::process::ExitStatusExt;
    prepare(command);
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    observed_spawn(command, child.id());
    let mut pipe = child.stdout.take().expect("piped stdout");
    let setup = fcntl_getfl(pipe.as_fd())
        .and_then(|flags| fcntl_setfl(pipe.as_fd(), flags | OFlags::NONBLOCK));
    if let Err(error) = setup {
        recover(child, true);
        return Err(error.into());
    }
    let start = Instant::now();
    let mut output = Vec::new();
    let mut eof = false;
    let mut status = None;
    loop {
        if elapsed(start) >= timeout && (status.is_none() || !eof) {
            drop(pipe);
            recover(child, true);
            return Err(io::ErrorKind::TimedOut.into());
        }
        if !eof {
            let mut buffer = [0; 65536];
            match pipe.read(&mut buffer) {
                Ok(0) => eof = true,
                Ok(n) => output.extend_from_slice(&buffer[..n]),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => {
                    drop(pipe);
                    recover(child, true);
                    return Err(error);
                }
            }
        }
        if status.is_none() {
            match child.try_wait() {
                Ok(found) => status = found,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    drop(pipe);
                    recover(child, true);
                    return Err(error);
                }
            }
        }
        if eof && let Some(status) = status {
            return Ok(Output {
                code: status
                    .code()
                    .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)),
                signal: status.signal(),
                stdout: output,
                stderr: Vec::new(),
            });
        }
        let timeout =
            rustix::event::Timespec::try_from(Duration::from_millis(1)).expect("small duration");
        let mut fds = if eof {
            Vec::new()
        } else {
            vec![rustix::event::PollFd::new(
                &pipe,
                rustix::event::PollFlags::IN,
            )]
        };
        if let Err(error) = rustix::event::poll(&mut fds, Some(&timeout))
            && error != rustix::io::Errno::INTR
        {
            drop(pipe);
            recover(child, true);
            return Err(error.into());
        }
    }
}

pub fn capture(
    command: &mut Command,
    input: &[u8],
    timeout: Option<Duration>,
) -> io::Result<Output> {
    capture_observed(command, input, timeout, |_| {})
}
/// Typed spawn notification for observation; the ordinary entry supplies none.
pub fn capture_observed(
    command: &mut Command,
    input: &[u8],
    timeout: Option<Duration>,
    mut spawned: impl FnMut(u32),
) -> io::Result<Output> {
    prepare(command);
    let mut child = DirectChild::spawn(command)?;
    observed_spawn(command, child.id());
    spawned(child.id());
    if let Err(e) = child.setup() {
        recover(child.into_child(), true);
        return Err(e);
    }
    let started = Instant::now();
    let mut written = 0;
    let mut status = None;
    let mut streams = [Vec::new(), Vec::new()];
    if input.is_empty() {
        child.close(0);
    }
    loop {
        if timeout.is_some_and(|t| started.elapsed() >= t) && status.is_none() {
            recover(child.into_child(), true);
            return Err(io::ErrorKind::TimedOut.into());
        }
        let ready = match child.poll(Duration::from_millis(25)) {
            Ok(r) => r,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => {
                recover(child.into_child(), true);
                return Err(e);
            }
        };
        if ready[0] && child.open_streams()[0] {
            match child.write(&input[written..]) {
                Ok(n) => {
                    written += n;
                    if written == input.len() || n == 0 {
                        child.close(0);
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) => {}
                Err(e) if e.kind() == io::ErrorKind::BrokenPipe => child.close(0),
                Err(e) => {
                    recover(child.into_child(), true);
                    return Err(e);
                }
            }
        }
        for index in 1..=2 {
            if ready[index] && child.open_streams()[index] {
                let mut buf = [0; 65536];
                match child.read(index, &mut buf) {
                    Ok(0) => child.close(index),
                    Ok(n) => streams[index - 1].extend_from_slice(&buf[..n]),
                    Err(e)
                        if matches!(
                            e.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) => {
                        recover(child.into_child(), true);
                        return Err(e);
                    }
                }
            }
        }
        if status.is_none() {
            match child.try_wait() {
                Ok(found) => status = found,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    recover(child.into_child(), true);
                    return Err(error);
                }
            }
        }
        if status.is_some() {
            child.close(0);
        }
        if let Some(status) = status
            && !child.open_streams()[1]
            && !child.open_streams()[2]
        {
            return Ok(Output {
                code: code(status),
                signal: match status {
                    Status::Signaled(s) => Some(s),
                    Status::Exited(_) => None,
                },
                stdout: std::mem::take(&mut streams[0]),
                stderr: std::mem::take(&mut streams[1]),
            });
        }
    }
}
/// Connect the actual children, without materializing the producer first.
/// Returns the pipeline's exit value and the signal Bash reports for the job:
/// the first element, in pipeline order, that a signal ended (`job_signal_status`).
pub fn pipeline(producer: &mut Command, consumer: &mut Command) -> io::Result<(i32, Option<i32>)> {
    use std::os::unix::process::ExitStatusExt;
    prepare(producer);
    prepare(consumer);
    let mut first = producer
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;
    observed_spawn(producer, first.id());
    let pipe = first.stdout.take().expect("piped stdout");
    let second = consumer
        .stdin(Stdio::from(pipe))
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn();
    let second = match second {
        Ok(c) => c,
        Err(e) => {
            // A failed spawn leaves Stdio owned by Command. Close its read end
            // before waiting, otherwise a full producer pipe never completes.
            consumer.stdin(Stdio::null());
            recover(first, false);
            return Err(e);
        }
    };
    // Command keeps its configured Stdio after spawn. Release the parent's copy
    // of the read end so that a consumer exiting early lets the producer fail.
    consumer.stdin(Stdio::null());
    observed_spawn(consumer, second.id());
    let two = retain_after_wait(second, wait_retry);
    let one = retain_after_wait(first, wait_retry);
    let two = two?;
    let one = one?;
    let c = |s: std::process::ExitStatus| s.code().unwrap_or_else(|| 128 + s.signal().unwrap_or(0));
    let signal = one.signal().or_else(|| two.signal());
    Ok((if two.success() { c(one) } else { c(two) }, signal))
}

// The environment the old Bash process exports to its children, apart from
// the per-command `_`. Set by the S3D entry; empty for other callers.
static SHELL: Mutex<Option<Shell>> = Mutex::new(None);
#[derive(Clone, Debug, Default)]
pub struct Shell {
    /// Exported variables in the shell (`None` removes an inherited value).
    pub exported: Vec<(OsString, Option<OsString>)>,
    /// SHLVL seen by a command the shell forks and waits for.
    pub level: OsString,
    /// SHLVL seen by a pipeline element that the subshell execs directly.
    pub subshell_level: OsString,
    /// Inherited dynamic variables Bash regenerates whenever it rebuilds the
    /// export environment (`EPOCHSECONDS`, `EPOCHREALTIME`): each child sees
    /// the time of its start (checked only within the run).
    pub epoch: Vec<OsString>,
    /// An inherited LINENO (`None` when the environment had none).
    pub lineno: Option<Lineno>,
}
/// LINENO in the old Bash's export environment (bash 5.2.21, observed in the
/// reference container). An inherited LINENO stays exported, and Bash
/// regenerates its value only when it rebuilds the export environment: when a
/// fork finds the environment marked (`array_needs_making`), and at once after a
/// command with a temporary assignment. A child sees the line of the last
/// rebuild, not the line of its own command.
#[derive(Clone, Copy, Debug)]
pub struct Lineno {
    /// The line of the last rebuild.
    pub value: u32,
    /// Marked since then (`export`, a builtin's temporary assignment such as
    /// `IFS= read`). Bash starts marked.
    pub dirty: bool,
}
impl Lineno {
    /// Bash at start-up: marked, so its first fork rebuilds.
    pub fn start() -> Self {
        Self {
            value: 0,
            dirty: true,
        }
    }
}
fn with_lineno(change: impl FnOnce(&mut Lineno)) {
    let mut guard = SHELL.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(lineno) = guard.as_mut().and_then(|s| s.lineno.as_mut()) {
        change(lineno);
    }
}
/// `array_needs_making = 1`: an `export`, or a builtin run with a temporary
/// assignment (`while IFS= read …`, each read including the last).
pub fn mark() {
    with_lineno(|l| l.dirty = true);
}
/// `maybe_make_export_env` before Bash forks at LINE (an external command, a
/// pipeline element or a command substitution): a marked environment is
/// rebuilt with LINE.
pub fn fork(line: u32) {
    with_lineno(|l| {
        if l.dirty {
            *l = Lineno {
                value: line,
                dirty: false,
            };
        }
    });
}
/// `dispose_used_env_vars` after a command with a temporary assignment
/// (`LC_ALL=C sort`, `CDPATH= cd`): Bash marks and rebuilds at once, with the
/// line it restored after the command (the last line of the top-level command).
pub fn remake(line: u32) {
    mark();
    fork(line);
}
/// The value an own temporary assignment gives a child (`LC_ALL=C grep`,
/// `GIT_INDEX_FILE=… git`): the child marks and rebuilds at its own LINE.
pub fn own_line(line: u32) -> Option<(OsString, OsString)> {
    let guard = SHELL.lock().unwrap_or_else(|p| p.into_inner());
    guard
        .as_ref()
        .and_then(|s| s.lineno)
        .map(|_| ("LINENO".into(), line.to_string().into()))
}
/// Replace the exported shell environment applied to later children.
pub fn set_shell(shell: Shell) {
    *SHELL.lock().unwrap_or_else(|p| p.into_inner()) = Some(shell);
}
/// Update one exported variable, as `export NAME=VALUE` did (which marks the
/// export environment).
pub fn export(name: &str, value: Option<OsString>) {
    let mut guard = SHELL.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(shell) = guard.as_mut() {
        shell.exported.retain(|(k, _)| k != name);
        shell.exported.push((name.into(), value));
        if let Some(lineno) = shell.lineno.as_mut() {
            lineno.dirty = true;
        }
    }
}
/// Mark a command run as a pipeline element (Bash execs it in the subshell).
pub fn pipeline_element(command: &mut Command) {
    if let Some(shell) = SHELL.lock().unwrap_or_else(|p| p.into_inner()).as_ref() {
        command.env("SHLVL", &shell.subshell_level);
    }
}
fn explicit(command: &Command, name: &str) -> bool {
    command.get_envs().any(|(k, _)| k == name)
}
/// Bash's command search: the first executable regular file on PATH.
pub fn search(command: &Command, program: &std::ffi::OsStr) -> Option<std::path::PathBuf> {
    use std::os::unix::ffi::OsStrExt;
    if program.as_bytes().contains(&b'/') {
        return Some(program.into());
    }
    let path = command
        .get_envs()
        .find(|(k, _)| *k == "PATH")
        .map(|(_, v)| v.map(std::ffi::OsStr::to_os_string))
        .unwrap_or_else(|| std::env::var_os("PATH"))?;
    path.as_bytes().split(|b| *b == b':').find_map(|dir| {
        let dir = if dir.is_empty() { b".".as_slice() } else { dir };
        let mut candidate = dir.to_vec();
        candidate.push(b'/');
        candidate.extend(program.as_bytes());
        let candidate = std::path::PathBuf::from(std::ffi::OsStr::from_bytes(&candidate));
        let metadata = std::fs::metadata(&candidate).ok()?;
        (metadata.is_file() && rustix::fs::access(&candidate, rustix::fs::Access::EXEC_OK).is_ok())
            .then_some(candidate)
    })
}
/// The old script exports LC_ALL=C before starting any child and leaves each
/// child the SIGPIPE disposition it inherited. Children also see the variables
/// Bash itself exports (PWD, OLDPWD, SHLVL) and `_` naming the command.
fn prepare(command: &mut Command) {
    command.env("LC_ALL", "C");
    if let Some(shell) = SHELL.lock().unwrap_or_else(|p| p.into_inner()).clone() {
        for (name, value) in &shell.exported {
            if explicit(command, &name.to_string_lossy()) {
                continue;
            }
            match value {
                Some(v) => command.env(name, v),
                None => command.env_remove(name),
            };
        }
        if !explicit(command, "SHLVL") {
            command.env("SHLVL", &shell.level);
        }
        if !shell.epoch.is_empty() {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default();
            for name in &shell.epoch {
                let value = if name == "EPOCHREALTIME" {
                    format!("{}.{:06}", now.as_secs(), now.subsec_micros())
                } else {
                    now.as_secs().to_string()
                };
                if !explicit(command, &name.to_string_lossy()) {
                    command.env(name, value);
                }
            }
        }
        if let Some(lineno) = shell.lineno
            && !explicit(command, "LINENO")
        {
            command.env("LINENO", lineno.value.to_string());
        }
        if !explicit(command, "_") {
            let program = command.get_program().to_os_string();
            match search(command, &program) {
                Some(found) => command.env("_", found),
                None => command.env("_", program),
            };
        }
    }
    super::signals::preserve_child_pipe(command);
}

enum Step {
    Data,
    Closed,
    Pending,
}
fn step(stream: &mut impl Read, sink: &mut Vec<u8>) -> io::Result<Step> {
    let mut buffer = [0; 65536];
    match stream.read(&mut buffer) {
        Ok(0) => Ok(Step::Closed),
        Ok(n) => {
            sink.extend_from_slice(&buffer[..n]);
            Ok(Step::Data)
        }
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
            ) =>
        {
            Ok(Step::Pending)
        }
        Err(e) => Err(e),
    }
}
fn drain(
    mut child: Child,
    mut stdout: Option<std::process::ChildStdout>,
    mut stderr: Option<std::process::ChildStderr>,
) -> io::Result<Output> {
    use rustix::fs::{OFlags, fcntl_getfl, fcntl_setfl};
    use std::os::fd::AsFd;
    let nonblocking = |fd: std::os::fd::BorrowedFd<'_>| {
        fcntl_getfl(fd).and_then(|f| fcntl_setfl(fd, f | OFlags::NONBLOCK))
    };
    let setup = stdout
        .as_ref()
        .map_or(Ok(()), |s| nonblocking(s.as_fd()))
        .and_then(|_| stderr.as_ref().map_or(Ok(()), |s| nonblocking(s.as_fd())));
    if let Err(error) = setup {
        drop((stdout, stderr));
        recover(child, true);
        return Err(error.into());
    }
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut status = None;
    loop {
        let mut failure = None;
        if let Some(stream) = stdout.as_mut() {
            match step(stream, &mut out) {
                Ok(Step::Closed) => stdout = None,
                Ok(_) => {}
                Err(e) => failure = Some(e),
            }
        }
        if failure.is_none()
            && let Some(stream) = stderr.as_mut()
        {
            match step(stream, &mut err) {
                Ok(Step::Closed) => stderr = None,
                Ok(_) => {}
                Err(e) => failure = Some(e),
            }
        }
        if let Some(error) = failure {
            drop((stdout, stderr));
            recover(child, true);
            return Err(error);
        }
        if status.is_none() {
            match child.try_wait() {
                Ok(found) => status = found,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    drop((stdout, stderr));
                    recover(child, true);
                    return Err(error);
                }
            }
        }
        if let Some(status) = status
            && stdout.is_none()
            && stderr.is_none()
        {
            use std::os::unix::process::ExitStatusExt;
            return Ok(Output {
                code: status
                    .code()
                    .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)),
                signal: status.signal(),
                stdout: out,
                stderr: err,
            });
        }
        let timeout =
            rustix::event::Timespec::try_from(Duration::from_millis(25)).expect("small duration");
        let mut fds = Vec::new();
        if let Some(s) = &stdout {
            fds.push(rustix::event::PollFd::new(s, rustix::event::PollFlags::IN));
        }
        if let Some(s) = &stderr {
            fds.push(rustix::event::PollFd::new(s, rustix::event::PollFlags::IN));
        }
        if let Err(error) = rustix::event::poll(&mut fds, Some(&timeout))
            && error != rustix::io::Errno::INTR
        {
            drop(fds);
            drop((stdout, stderr));
            recover(child, true);
            return Err(error.into());
        }
    }
}

/// Where the old script sent a child's standard error next to `>FILE`.
pub enum Errors {
    /// `2>FILE`: the child writes the file itself.
    File(std::fs::File),
    /// `2>/dev/null`.
    Null,
    /// Not redirected: the parent shows it afterwards.
    Capture,
}
/// `command [<IN] >FILE 2>…`: the child writes the file itself, as with the
/// legacy redirection, so a failing write (`EFBIG`, `ENOSPC`) fails in the
/// child. The caller reads the file back after the child has finished.
pub fn capture_files(
    command: &mut Command,
    input: Option<std::fs::File>,
    output: std::fs::File,
    errors: Errors,
) -> io::Result<Output> {
    use std::os::unix::process::ExitStatusExt;
    prepare(command);
    let piped = matches!(errors, Errors::Capture);
    let errors = match errors {
        Errors::File(file) => Stdio::from(file),
        Errors::Null => Stdio::null(),
        Errors::Capture => Stdio::piped(),
    };
    let spawned = command
        .stdin(input.map_or_else(Stdio::null, Stdio::from))
        .stdout(Stdio::from(output))
        .stderr(errors)
        .spawn();
    // Drop the parent's copies of the descriptors kept by Command.
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let mut child = spawned?;
    observed_spawn(command, child.id());
    if piped {
        let stderr = child.stderr.take();
        return drain(child, None, stderr);
    }
    let status = retain_after_wait(child, wait_retry)?;
    Ok(Output {
        code: status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)),
        signal: status.signal(),
        stdout: vec![],
        stderr: vec![],
    })
}

/// `command <file`: the child reads a prepared file, as the legacy redirection
/// did. The parent never writes to a pipe whose reader may already be gone.
pub fn capture_stdin_file(command: &mut Command, input: std::fs::File) -> io::Result<Output> {
    prepare(command);
    let mut child = command
        .stdin(Stdio::from(input))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    // Drop the parent's copy of the input descriptor kept by Command.
    command.stdin(Stdio::null());
    observed_spawn(command, child.id());
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    drain(child, stdout, stderr)
}

/// `command 2>/dev/null | head -c LIMIT` without pipefail: keep at most LIMIT
/// bytes, then close the read end so a longer writer stops as with head(1).
/// Also returns the signal that ended the writer (Bash reports the job by it).
pub fn capture_head(command: &mut Command, limit: usize) -> io::Result<(Vec<u8>, Option<i32>)> {
    use std::os::unix::process::ExitStatusExt;
    prepare(command);
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    observed_spawn(command, child.id());
    let mut pipe = child.stdout.take().expect("piped stdout");
    let mut kept = Vec::new();
    let mut buffer = [0; 65536];
    while kept.len() < limit {
        let wanted = (limit - kept.len()).min(buffer.len());
        match pipe.read(&mut buffer[..wanted]) {
            Ok(0) => break,
            Ok(n) => kept.extend_from_slice(&buffer[..n]),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => {
                drop(pipe);
                recover(child, true);
                return Err(e);
            }
        }
    }
    drop(pipe);
    // The writer may still be running; it ends on EPIPE like head's writer.
    let signal = retain_after_wait(child, wait_retry)
        .ok()
        .and_then(|s| s.signal());
    Ok((kept, signal))
}

fn observed_spawn(command: &Command, pid: u32) {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    let program = std::path::PathBuf::from(command.get_program());
    let cwd = command
        .get_current_dir()
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_default());
    let path = if program.components().count() > 1 {
        if program.is_absolute() {
            program
        } else {
            cwd.join(program)
        }
    } else {
        let environment = command
            .get_envs()
            .find(|(k, _)| *k == "PATH")
            .and_then(|(_, v)| v.map(std::ffi::OsStr::to_os_string))
            .or_else(|| std::env::var_os("PATH"))
            .unwrap_or_default();
        std::env::split_paths(&environment)
            .map(|p| {
                if p.is_absolute() {
                    p.join(&program)
                } else {
                    cwd.join(p).join(&program)
                }
            })
            .find(|p| p.is_file() && rustix::fs::access(p, rustix::fs::Access::EXEC_OK).is_ok())
            .unwrap_or(program)
    };
    let path = path.canonicalize().unwrap_or(path);
    let metadata = std::fs::metadata(&path).ok();
    super::observe(super::AuditEvent {
        phase: "process",
        operation: super::Operation::Spawn,
        path,
        dev: metadata.as_ref().map(MetadataExt::dev),
        ino: metadata.as_ref().map(MetadataExt::ino),
        fd: None,
        bytes: 0,
        child_pid: Some(pid),
        arguments: std::iter::once(command.get_program())
            .chain(command.get_args())
            .map(|a| a.as_bytes().to_vec())
            .collect(),
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    static SERIAL: Mutex<()> = Mutex::new(());
    #[test]
    fn failed_wait_preserves_the_actual_child_until_retry() {
        let _serial = SERIAL.lock().unwrap();
        let child = Command::new("sh").args(["-c", "exit 0"]).spawn().unwrap();
        let pid = child.id();
        let error =
            retain_after_wait(child, |_| Err(io::Error::from_raw_os_error(libc::EIO))).unwrap_err();
        assert_eq!(error.raw_os_error(), Some(libc::EIO));
        assert!(
            PENDING
                .lock()
                .unwrap()
                .iter()
                .any(|child| child.id() == pid)
        );
        assert_eq!(retry_pending(), 0);
        assert!(
            !PENDING
                .lock()
                .unwrap()
                .iter()
                .any(|child| child.id() == pid)
        );
    }
    #[test]
    fn profile_stdio_and_timeout_keep_the_legacy_routes() {
        let _serial = SERIAL.lock().unwrap();
        let out = profile_capture(
            Command::new("sh").args([
                "-c",
                "test -c /dev/stdin && test -c /dev/stderr && printf OK",
            ]),
            Duration::from_secs(5),
        )
        .unwrap();
        assert_eq!(out.code, 0);
        assert_eq!(out.stdout, b"OK");
        assert!(out.stderr.is_empty());
        let error = profile_capture(
            Command::new("sh").args(["-c", "exec sleep 30"]),
            Duration::from_millis(10),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert_eq!(retry_pending(), 0);
    }
    #[test]
    fn failed_consumer_spawn_closes_the_pipe_before_waiting() {
        let _serial = SERIAL.lock().unwrap();
        let error = pipeline(
            Command::new("sh").args(["-c", "exec yes producer 2>/dev/null"]),
            &mut Command::new("/definitely-absent-s3d-consumer"),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);
        assert_eq!(retry_pending(), 0);
    }
}
