//! 同じ予算と絶対期限を借りる単一子実行。出力の用途別規則はconsumerへ残す。
use crate::ExitCode;
use crate::budget::Budget;
use dev_workflow_platform::process::{DirectChild, Status};
use std::io;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrokenPipePolicy {
    Reject,
    Close,
}
#[derive(Clone, Copy, Debug)]
pub struct ChildInput<'a> {
    pub bytes: &'a [u8],
    pub broken_pipe: BrokenPipePolicy,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildStream {
    Stdin,
    Stdout,
    Stderr,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutputStream {
    Stdout,
    Stderr,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChildStatus {
    Exited(ExitCode),
    Signaled(i32),
}
impl From<Status> for ChildStatus {
    fn from(value: Status) -> Self {
        match value {
            Status::Exited(code) => Self::Exited(ExitCode::new(code)),
            Status::Signaled(n) => Self::Signaled(n),
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct ChildDeadlines {
    pub execution: Instant,
    pub cleanup: Instant,
}
#[derive(Debug, PartialEq, Eq)]
pub struct ChildOutcome {
    pub status: ChildStatus,
    pub input_written: usize,
    pub input_closed_early: bool,
}
#[derive(Debug)]
pub enum RunCause<E> {
    InvalidDeadlines,
    Cancelled,
    Deadline,
    Spawn(io::Error),
    Setup(io::Error),
    Poll(io::Error),
    InputClosed,
    Io {
        stream: ChildStream,
        error: io::Error,
    },
    Wait(io::Error),
    Consumer(E),
}
#[derive(Debug)]
#[must_use = "未回収のpending_childは呼出元が保持して回収する必要があります"]
pub struct RunFailure<E> {
    pub cause: RunCause<E>,
    pub status: Option<ChildStatus>,
    pub input_written: usize,
    pub input_closed_early: bool,
    pub kill_error: Option<io::Error>,
    pub wait_error: Option<io::Error>,
    pub cleanup_expired: bool,
    pub pending_child: Option<Child>,
}
impl<E> RunFailure<E> {
    fn new(cause: RunCause<E>) -> Self {
        Self {
            cause,
            status: None,
            input_written: 0,
            input_closed_early: false,
            kill_error: None,
            wait_error: None,
            cleanup_expired: false,
            pending_child: None,
        }
    }
}
const TICK: Duration = Duration::from_millis(20);
fn retry(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
    )
}
fn copy_error(error: &io::Error) -> io::Error {
    match error.raw_os_error() {
        Some(code) => io::Error::from_raw_os_error(code),
        None => io::Error::new(error.kind(), error.to_string()),
    }
}
fn remaining<E>(
    budget: &Budget,
    execution: Instant,
    cancel: &AtomicBool,
) -> Result<Duration, RunCause<E>> {
    if cancel.load(Ordering::Relaxed) {
        return Err(RunCause::Cancelled);
    }
    let left = budget.remaining().map_err(|_| RunCause::Deadline)?;
    let execution = execution
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or(RunCause::Deadline)?;
    Ok(left.min(execution).min(TICK))
}

/// 固定stdinと両出力を同時に進める。consumerの停止やpanicは強制中断しない。
/// 回収失敗時はpipeを閉じたChildを返す。親signalの捕捉や子孫監督は行わない。
pub fn run_child<E>(
    command: &mut Command,
    input: ChildInput<'_>,
    budget: &mut Budget,
    deadlines: ChildDeadlines,
    cancel: &AtomicBool,
    mut consumer: impl FnMut(OutputStream, &[u8], &mut Budget) -> Result<(), E>,
) -> Result<ChildOutcome, RunFailure<E>> {
    if deadlines.cleanup < deadlines.execution {
        return Err(RunFailure::new(RunCause::InvalidDeadlines));
    }
    remaining(budget, deadlines.execution, cancel).map_err(RunFailure::new)?;
    let mut process =
        DirectChild::spawn(command).map_err(|e| RunFailure::new(RunCause::Spawn(e)))?;
    let result = drive(
        &mut process,
        input,
        budget,
        deadlines,
        cancel,
        &mut consumer,
    );
    return_child(process, result)
}

fn return_child<E>(
    process: DirectChild,
    result: Result<ChildOutcome, RunFailure<E>>,
) -> Result<ChildOutcome, RunFailure<E>> {
    match result {
        Ok(outcome) => Ok(outcome),
        Err(mut failure) => {
            if failure.status.is_none() {
                failure.pending_child = Some(process.into_child());
            }
            Err(failure)
        }
    }
}

// 故障注入は非公開の操作境界で行う。製品の環境・引数に解除口を設けない。
trait Operations {
    fn setup(&mut self) -> io::Result<()>;
    fn open_streams(&self) -> [bool; 3];
    fn close(&mut self, stream: usize);
    fn poll(&mut self, timeout: Duration) -> io::Result<[bool; 3]>;
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize>;
    fn read(&mut self, stream: usize, bytes: &mut [u8]) -> io::Result<usize>;
    fn try_wait(&mut self) -> io::Result<Option<Status>>;
    fn kill(&mut self) -> io::Result<()>;
    fn now(&self) -> Instant {
        Instant::now()
    }
    fn pause(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}
impl Operations for DirectChild {
    fn setup(&mut self) -> io::Result<()> {
        DirectChild::setup(self)
    }
    fn open_streams(&self) -> [bool; 3] {
        DirectChild::open_streams(self)
    }
    fn close(&mut self, stream: usize) {
        DirectChild::close(self, stream);
    }
    fn poll(&mut self, timeout: Duration) -> io::Result<[bool; 3]> {
        DirectChild::poll(self, timeout)
    }
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        DirectChild::write(self, bytes)
    }
    fn read(&mut self, stream: usize, bytes: &mut [u8]) -> io::Result<usize> {
        DirectChild::read(self, stream, bytes)
    }
    fn try_wait(&mut self) -> io::Result<Option<Status>> {
        DirectChild::try_wait(self)
    }
    fn kill(&mut self) -> io::Result<()> {
        DirectChild::kill(self)
    }
}
fn drive<E>(
    p: &mut impl Operations,
    input: ChildInput<'_>,
    budget: &mut Budget,
    deadlines: ChildDeadlines,
    cancel: &AtomicBool,
    consumer: &mut impl FnMut(OutputStream, &[u8], &mut Budget) -> Result<(), E>,
) -> Result<ChildOutcome, RunFailure<E>> {
    let mut status = None;
    let mut written = 0;
    let mut early = false;
    let result = (|| {
        p.setup().map_err(RunCause::Setup)?;
        if input.bytes.is_empty() {
            p.close(0);
        }
        let mut buffer = [0; 65536];
        loop {
            let timeout = remaining(budget, deadlines.execution, cancel)?;
            if status.is_none() {
                match p.try_wait() {
                    Ok(found) => status = found.map(Into::into),
                    Err(e) if retry(&e) => continue,
                    Err(e) => return Err(RunCause::Wait(e)),
                }
            }
            if status.is_some() && p.open_streams()[0] {
                p.close(0);
                early = true;
                if input.broken_pipe == BrokenPipePolicy::Reject {
                    return Err(RunCause::InputClosed);
                }
            }
            if p.open_streams() == [false; 3]
                && let Some(status) = status
            {
                remaining(budget, deadlines.execution, cancel)?;
                return Ok(ChildOutcome {
                    status,
                    input_written: written,
                    input_closed_early: early,
                });
            }
            let ready = match p.poll(timeout) {
                Ok(ready) => ready,
                Err(e) if retry(&e) => continue,
                Err(e) => return Err(RunCause::Poll(e)),
            };
            remaining(budget, deadlines.execution, cancel)?;
            if ready[0] && p.open_streams()[0] {
                let end = written.saturating_add(65536).min(input.bytes.len());
                match p.write(&input.bytes[written..end]) {
                    Ok(0) => {
                        return Err(RunCause::Io {
                            stream: ChildStream::Stdin,
                            error: io::ErrorKind::WriteZero.into(),
                        });
                    }
                    Ok(n) => {
                        written += n;
                        if written == input.bytes.len() {
                            p.close(0);
                        }
                    }
                    Err(e) if retry(&e) => (),
                    Err(e) if e.kind() == io::ErrorKind::BrokenPipe => {
                        early = true;
                        p.close(0);
                        if input.broken_pipe == BrokenPipePolicy::Reject {
                            return Err(RunCause::InputClosed);
                        }
                    }
                    Err(e) => {
                        return Err(RunCause::Io {
                            stream: ChildStream::Stdin,
                            error: e,
                        });
                    }
                }
            }
            for stream in [1, 2] {
                remaining(budget, deadlines.execution, cancel)?;
                if !ready[stream] || !p.open_streams()[stream] {
                    continue;
                }
                match p.read(stream, &mut buffer) {
                    Ok(0) => p.close(stream),
                    Ok(n) => {
                        consumer(
                            if stream == 1 {
                                OutputStream::Stdout
                            } else {
                                OutputStream::Stderr
                            },
                            &buffer[..n],
                            budget,
                        )
                        .map_err(RunCause::Consumer)?;
                        remaining(budget, deadlines.execution, cancel)?;
                    }
                    Err(e) if retry(&e) => (),
                    Err(e) => {
                        return Err(RunCause::Io {
                            stream: if stream == 1 {
                                ChildStream::Stdout
                            } else {
                                ChildStream::Stderr
                            },
                            error: e,
                        });
                    }
                }
            }
        }
    })();
    result.map_err(|cause| {
        let mut failure = RunFailure::new(cause);
        failure.status = status;
        failure.input_written = written;
        failure.input_closed_early = early;
        if let RunCause::Wait(error) = &failure.cause {
            failure.wait_error = Some(copy_error(error));
        }
        cleanup(p, &mut failure, deadlines.cleanup);
        failure
    })
}
fn cleanup<E>(p: &mut impl Operations, failure: &mut RunFailure<E>, deadline: Instant) {
    for stream in 0..3 {
        p.close(stream);
    }
    if failure.status.is_some() {
        return;
    }
    if failure.wait_error.is_some() {
        failure.cleanup_expired = p.now() >= deadline;
        return;
    }
    let mut kill_attempted = false;
    let mut initial_check = true;
    loop {
        // 期限後の例外は初回とKILL直後の非待機確認だけ。待機後は必ず期限を見直す。
        if !initial_check && p.now() >= deadline {
            failure.cleanup_expired = true;
            return;
        }
        initial_check = false;
        match p.try_wait() {
            Ok(Some(status)) => {
                failure.status = Some(status.into());
                return;
            }
            Ok(None) if !kill_attempted => {
                failure.kill_error = p.kill().err();
                kill_attempted = true;
                initial_check = true;
                continue; // 期限後もKILL直後の非待機確認は1回行う。
            }
            Ok(None) => (),
            Err(e) if retry(&e) => (),
            Err(e) => {
                failure.wait_error = Some(e);
                failure.cleanup_expired = p.now() >= deadline;
                return;
            }
        }
        let Some(left) = deadline
            .checked_duration_since(p.now())
            .filter(|d| !d.is_zero())
        else {
            failure.cleanup_expired = true;
            return;
        };
        p.pause(left.min(TICK));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::BudgetLimits;
    use std::collections::VecDeque;

    #[derive(Default)]
    struct Fake {
        open: [bool; 3],
        events: Vec<&'static str>,
        waits: VecDeque<Result<Option<Status>, io::Error>>,
        polls: VecDeque<io::Result<[bool; 3]>>,
        setup_error: Option<io::Error>,
        write_error: Option<io::Error>,
        write_error_after: usize,
        read_error: Option<(usize, io::Error)>,
        kill_error: Option<io::Error>,
        killed: bool,
        stuck: bool,
        emitted: [bool; 3],
        clock: Option<Instant>,
        wait_advances: VecDeque<Duration>,
    }
    impl Fake {
        fn new() -> Self {
            Self {
                open: [true; 3],
                ..Self::default()
            }
        }
    }
    impl Operations for Fake {
        fn setup(&mut self) -> io::Result<()> {
            self.events.push("setup");
            self.setup_error.take().map_or(Ok(()), Err)
        }
        fn open_streams(&self) -> [bool; 3] {
            self.open
        }
        fn close(&mut self, n: usize) {
            self.events.push("close");
            self.open[n] = false;
        }
        fn poll(&mut self, _: Duration) -> io::Result<[bool; 3]> {
            self.events.push("poll");
            self.polls.pop_front().unwrap_or(Ok(self.open))
        }
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.events.push("write");
            if self.write_error_after > 0 {
                self.write_error_after -= 1;
                Ok(bytes.len().min(1))
            } else {
                self.write_error.take().map_or(Ok(bytes.len().min(1)), Err)
            }
        }
        fn read(&mut self, n: usize, bytes: &mut [u8]) -> io::Result<usize> {
            self.events.push("read");
            if self
                .read_error
                .as_ref()
                .is_some_and(|(stream, _)| *stream == n)
            {
                return Err(self.read_error.take().unwrap().1);
            }
            if self.emitted[n] {
                Ok(0)
            } else {
                self.emitted[n] = true;
                bytes[0] = n as u8;
                Ok(1)
            }
        }
        fn try_wait(&mut self) -> io::Result<Option<Status>> {
            self.events.push("wait");
            if let Some(clock) = &mut self.clock {
                *clock += self.wait_advances.pop_front().unwrap_or_default();
            }
            self.waits.pop_front().unwrap_or_else(|| {
                if self.stuck {
                    Ok(None)
                } else if self.killed {
                    Ok(Some(Status::Signaled(9)))
                } else if self.open == [false; 3] && self.emitted[1] && self.emitted[2] {
                    Ok(Some(Status::Exited(237)))
                } else {
                    Ok(None)
                }
            })
        }
        fn kill(&mut self) -> io::Result<()> {
            self.events.push("kill");
            self.killed = true;
            self.kill_error.take().map_or(Ok(()), Err)
        }
        fn now(&self) -> Instant {
            self.clock.unwrap_or_else(Instant::now)
        }
        fn pause(&mut self, duration: Duration) {
            self.events.push("pause");
            if let Some(clock) = &mut self.clock {
                *clock += duration;
            }
        }
    }
    fn budget() -> Budget {
        Budget::new(
            BudgetLimits {
                items: 20,
                total_bytes: 20,
                file_bytes: 20,
            },
            Instant::now() + Duration::from_secs(5),
        )
    }
    fn run(fake: &mut Fake) -> Result<ChildOutcome, RunFailure<&'static str>> {
        let mut b = budget();
        let deadlines = ChildDeadlines {
            execution: b.deadline(),
            cleanup: b.deadline(),
        };
        drive(
            fake,
            ChildInput {
                bytes: b"abc",
                broken_pipe: BrokenPipePolicy::Reject,
            },
            &mut b,
            deadlines,
            &AtomicBool::new(false),
            &mut |_, _, _| Ok(()),
        )
    }
    #[test]
    fn simultaneous_io_short_writes_and_status() {
        let mut f = Fake::new();
        let out = run(&mut f).unwrap();
        assert_eq!(
            out,
            ChildOutcome {
                status: ChildStatus::Exited(ExitCode::new(237)),
                input_written: 3,
                input_closed_early: false
            }
        );
        assert!(!f.events.contains(&"kill"));
        assert_eq!(f.open, [false; 3]);
    }
    #[test]
    fn poll_failure_is_not_a_stream_or_consumer_failure() {
        let mut f = Fake::new();
        f.polls.push_back(Err(io::Error::from_raw_os_error(5)));
        let e = run(&mut f).unwrap_err();
        assert!(matches!(e.cause, RunCause::Poll(ref e) if e.raw_os_error() == Some(5)));
        assert_eq!(e.status, Some(ChildStatus::Signaled(9)));
    }
    #[test]
    fn transient_poll_wait_read_and_write_errors_retry() {
        for kind in [io::ErrorKind::Interrupted, io::ErrorKind::WouldBlock] {
            for operation in 0..4 {
                let mut f = Fake::new();
                match operation {
                    0 => f.polls.push_back(Err(kind.into())),
                    1 => f.waits.push_back(Err(kind.into())),
                    2 => f.read_error = Some((1, kind.into())),
                    _ => f.write_error = Some(kind.into()),
                }
                assert!(run(&mut f).is_ok());
                assert!(!f.events.contains(&"kill"));
            }
        }
    }
    #[test]
    fn transient_poll_returns_to_cancellation_check() {
        let mut f = Fake::new();
        let mut b = budget();
        let cancel = AtomicBool::new(false);
        let d = ChildDeadlines {
            execution: b.deadline(),
            cleanup: b.deadline(),
        };
        let mut calls = 0;
        let e = drive(
            &mut f,
            ChildInput {
                bytes: b"",
                broken_pipe: BrokenPipePolicy::Close,
            },
            &mut b,
            d,
            &cancel,
            &mut |_, _, _| {
                calls += 1;
                cancel.store(true, Ordering::Relaxed);
                Ok::<_, ()>(())
            },
        )
        .unwrap_err();
        assert!(matches!(e.cause, RunCause::Cancelled));
        assert_eq!(calls, 1);
        assert_eq!(e.status, Some(ChildStatus::Signaled(9)));
    }
    #[test]
    fn permanent_wait_failure_stops_all_operations_except_close() {
        for code in [5, 10] {
            let mut f = Fake::new();
            f.waits.push_back(Err(io::Error::from_raw_os_error(code)));
            let e = run(&mut f).unwrap_err();
            assert!(matches!(e.cause, RunCause::Wait(ref x) if x.raw_os_error() == Some(code)));
            assert_eq!(e.wait_error.as_ref().unwrap().raw_os_error(), Some(code));
            assert!(e.status.is_none());
            assert!(!e.cleanup_expired);
            let at = f.events.iter().position(|event| *event == "wait").unwrap();
            assert!(f.events[at + 1..].iter().all(|event| *event == "close"));
        }
    }
    #[test]
    fn cleanup_wait_failure_retains_primary_and_prior_kill_failure() {
        for after_kill in [false, true] {
            let mut f = Fake::new();
            f.setup_error = Some(io::Error::from_raw_os_error(5));
            if after_kill {
                f.waits.push_back(Ok(None));
                f.kill_error = Some(io::Error::from_raw_os_error(1));
            }
            f.waits.push_back(Err(io::Error::from_raw_os_error(10)));
            let e = run(&mut f).unwrap_err();
            assert!(matches!(e.cause, RunCause::Setup(_)));
            assert_eq!(e.wait_error.as_ref().unwrap().raw_os_error(), Some(10));
            assert_eq!(e.kill_error.is_some(), after_kill);
            assert!(e.status.is_none());
            let at = f.events.iter().rposition(|event| *event == "wait").unwrap();
            assert!(f.events[at + 1..].is_empty());
        }
    }
    #[test]
    fn expired_cleanup_still_kills_once_and_returns_unreaped() {
        let mut f = Fake::new();
        f.stuck = true;
        let mut e = RunFailure::<()>::new(RunCause::Deadline);
        cleanup(&mut f, &mut e, Instant::now() - Duration::from_secs(1));
        assert!(e.cleanup_expired);
        assert!(e.status.is_none());
        assert_eq!(
            f.events,
            ["close", "close", "close", "wait", "kill", "wait"]
        );
    }
    #[test]
    fn expired_cleanup_wait_error_does_not_kill_or_retry() {
        let mut f = Fake::new();
        f.waits.push_back(Err(io::Error::from_raw_os_error(10)));
        let mut e = RunFailure::<()>::new(RunCause::Deadline);
        cleanup(&mut f, &mut e, Instant::now() - Duration::from_secs(1));
        assert!(e.cleanup_expired);
        assert!(e.wait_error.is_some());
        assert_eq!(f.events, ["close", "close", "close", "wait"]);
    }
    #[test]
    fn cleanup_rechecks_absolute_deadline_after_pause() {
        let mut f = Fake::new();
        let now = Instant::now();
        f.clock = Some(now);
        f.stuck = true;
        let mut e = RunFailure::<()>::new(RunCause::Deadline);
        cleanup(&mut f, &mut e, now + Duration::from_millis(5));
        assert!(e.cleanup_expired);
        assert!(e.status.is_none());
        assert_eq!(
            f.events,
            ["close", "close", "close", "wait", "kill", "wait", "pause"]
        );
    }
    #[test]
    fn cleanup_rechecks_absolute_deadline_after_transient_wait() {
        for kind in [io::ErrorKind::Interrupted, io::ErrorKind::WouldBlock] {
            for after_kill in [false, true] {
                let mut f = Fake::new();
                let now = Instant::now();
                f.clock = Some(now);
                if after_kill {
                    f.waits.push_back(Ok(None));
                    f.wait_advances.push_back(Duration::ZERO);
                }
                f.waits.push_back(Err(kind.into()));
                f.wait_advances.push_back(Duration::from_millis(10));
                let mut e = RunFailure::<()>::new(RunCause::Cancelled);
                cleanup(&mut f, &mut e, now + Duration::from_millis(5));
                assert!(e.cleanup_expired);
                assert!(e.status.is_none());
                assert!(e.wait_error.is_none());
                assert_eq!(
                    f.events,
                    if after_kill {
                        vec!["close", "close", "close", "wait", "kill", "wait"]
                    } else {
                        vec!["close", "close", "close", "wait"]
                    }
                );
            }
        }
    }
    #[test]
    fn already_reaped_child_is_never_signalled() {
        let mut f = Fake::new();
        let mut e = RunFailure::<()>::new(RunCause::Cancelled);
        e.status = Some(ChildStatus::Exited(ExitCode::SUCCESS));
        cleanup(&mut f, &mut e, Instant::now());
        assert_eq!(f.events, ["close", "close", "close"]);
    }
    #[test]
    fn setup_io_and_kill_errors_keep_their_identity() {
        for stream in 0..3 {
            let mut f = Fake::new();
            if stream == 0 {
                f.write_error = Some(io::Error::from_raw_os_error(5));
            } else {
                f.read_error = Some((stream, io::Error::from_raw_os_error(5)));
            }
            f.kill_error = Some(io::Error::from_raw_os_error(1));
            let e = run(&mut f).unwrap_err();
            assert!(
                matches!(e.cause, RunCause::Io { error, .. } if error.raw_os_error() == Some(5))
            );
            assert_eq!(e.kill_error.unwrap().raw_os_error(), Some(1));
            assert!(e.status.is_some());
        }
    }
    #[test]
    fn consumer_rejection_keeps_budget_and_stops_later_chunks() {
        let mut f = Fake::new();
        let mut b = budget();
        b.add_bytes(7).unwrap();
        let deadline = b.deadline();
        let mut calls = 0;
        let e = drive(
            &mut f,
            ChildInput {
                bytes: b"",
                broken_pipe: BrokenPipePolicy::Close,
            },
            &mut b,
            ChildDeadlines {
                execution: deadline,
                cleanup: deadline,
            },
            &AtomicBool::new(false),
            &mut |_, bytes, b| {
                calls += 1;
                b.add_bytes(bytes.len() as u64).unwrap();
                Err("refused")
            },
        )
        .unwrap_err();
        assert!(matches!(e.cause, RunCause::Consumer("refused")));
        assert_eq!(calls, 1);
        assert_eq!(b.bytes(), 8);
        assert_eq!(b.deadline(), deadline);
    }
    #[test]
    fn broken_pipe_keeps_exact_successful_short_writes() {
        for policy in [BrokenPipePolicy::Reject, BrokenPipePolicy::Close] {
            let mut f = Fake::new();
            f.write_error = Some(io::ErrorKind::BrokenPipe.into());
            f.write_error_after = 2;
            let mut b = budget();
            let d = ChildDeadlines {
                execution: b.deadline(),
                cleanup: b.deadline(),
            };
            let result = drive(
                &mut f,
                ChildInput {
                    bytes: b"abc",
                    broken_pipe: policy,
                },
                &mut b,
                d,
                &AtomicBool::new(false),
                &mut |_, _, _| Ok::<_, ()>(()),
            );
            match result {
                Ok(out) => {
                    assert_eq!(policy, BrokenPipePolicy::Close);
                    assert_eq!(out.input_written, 2);
                    assert!(out.input_closed_early);
                }
                Err(error) => {
                    assert_eq!(policy, BrokenPipePolicy::Reject);
                    assert!(matches!(error.cause, RunCause::InputClosed));
                    assert_eq!(error.input_written, 2);
                    assert!(error.input_closed_early);
                }
            }
        }
    }
    #[test]
    fn broken_pipe_policies_are_explicit() {
        for policy in [BrokenPipePolicy::Reject, BrokenPipePolicy::Close] {
            let mut f = Fake::new();
            f.write_error = Some(io::ErrorKind::BrokenPipe.into());
            let mut b = budget();
            let d = ChildDeadlines {
                execution: b.deadline(),
                cleanup: b.deadline(),
            };
            let r = drive(
                &mut f,
                ChildInput {
                    bytes: b"data",
                    broken_pipe: policy,
                },
                &mut b,
                d,
                &AtomicBool::new(false),
                &mut |_, _, _| Ok::<_, ()>(()),
            );
            match r {
                Ok(out) => {
                    assert_eq!(policy, BrokenPipePolicy::Close);
                    assert!(out.input_closed_early);
                    assert_eq!(out.input_written, 0);
                }
                Err(e) => {
                    assert_eq!(policy, BrokenPipePolicy::Reject);
                    assert!(matches!(e.cause, RunCause::InputClosed));
                    assert!(e.input_closed_early);
                }
            }
        }
    }

    #[test]
    fn unreaped_failure_returns_real_child_with_closed_parent_pipes() {
        for wait_failed in [false, true] {
            let mut command = Command::new("/bin/sh");
            command.args(["-c", "printf '%s\\n' $$; read ignored; exit 237"]);
            let mut process = DirectChild::spawn(&mut command).unwrap();
            process.setup().unwrap();
            let mut pid_bytes = [0; 32];
            let read_deadline = Instant::now() + Duration::from_secs(5);
            let pid_result = loop {
                match process.poll(Duration::from_millis(1)) {
                    Ok(ready) if ready[1] => break process.read(1, &mut pid_bytes),
                    Err(error) => break Err(error),
                    _ if Instant::now() >= read_deadline => {
                        break Err(io::ErrorKind::TimedOut.into());
                    }
                    _ => std::thread::sleep(Duration::from_millis(1)),
                }
            };
            let mut failure = RunFailure::<()>::new(if wait_failed {
                RunCause::Wait(io::Error::from_raw_os_error(10))
            } else {
                RunCause::Deadline
            });
            failure.cleanup_expired = !wait_failed;
            if wait_failed {
                failure.wait_error = Some(io::Error::from_raw_os_error(10));
            }
            let mut returned = return_child(process, Err(failure)).unwrap_err();
            let mut child = returned.pending_child.take().unwrap();
            let actual_pid = child.id();
            let pipes_closed =
                child.stdin.is_none() && child.stdout.is_none() && child.stderr.is_none();
            let deadline = Instant::now() + Duration::from_secs(5);
            let reaped = loop {
                match child.try_wait() {
                    Ok(Some(status)) => break Some(status),
                    Ok(None) if Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    _ => break None,
                }
            };
            if reaped.is_none() {
                let _ = child.kill();
            }
            let explicitly_waited = child.wait().unwrap();
            let n = pid_result.unwrap();
            let expected_pid: u32 = std::str::from_utf8(&pid_bytes[..n])
                .unwrap()
                .trim()
                .parse()
                .unwrap();
            assert_eq!(actual_pid, expected_pid);
            assert!(pipes_closed);
            assert_eq!(reaped.unwrap().code(), Some(237));
            assert_eq!(explicitly_waited.code(), Some(237));
            assert_eq!(returned.cleanup_expired, !wait_failed);
            assert_eq!(returned.wait_error.is_some(), wait_failed);
        }
    }
}
