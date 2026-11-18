use super::{
    budget::Budget,
    error::{LegacyException, Result},
    interrupt,
};
use dev_workflow_platform::loop_state::{InheritedInput, LoopArg, LoopChild};
use std::{
    ffi::{OsStr, OsString},
    process::Command,
    time::{Duration, Instant},
};
pub const SAFE_GIT: &[&str] = &[
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.quotePath=false",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.ignoreCase=false",
    "-c",
    "core.splitIndex=false",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "push.gpgSign=false",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.clean=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
];
pub fn strings(args: &[&str]) -> Vec<OsString> {
    args.iter().map(OsString::from).collect()
}
fn command(top: &OsStr, extra: &[(OsString, OsString)]) -> Command {
    let mut command = Command::new("git");
    command
        .arg("-C")
        .arg(top)
        .args(SAFE_GIT)
        .env("GIT_NO_LAZY_FETCH", "1");
    for (key, value) in extra {
        command.env(key, value);
    }
    command
}
pub fn git(top: &OsStr, args: &[OsString], budget: &mut Budget) -> Result<Vec<u8>> {
    let refs = args.iter().map(|s| LoopArg::Raw(s)).collect::<Vec<_>>();
    Ok(run(top, &refs, &[], budget, false, &[])?.1)
}
pub fn run(
    top: &OsStr,
    args: &[LoopArg<'_>],
    inputs: &[InheritedInput<'_>],
    budget: &mut Budget,
    allow: bool,
    extra: &[(OsString, OsString)],
) -> Result<(i32, Vec<u8>)> {
    super::observe::budget("git", budget)?;
    let observed_args = args
        .iter()
        .map(|arg| match arg {
            LoopArg::Raw(value) => (*value).to_owned(),
            LoopArg::InputPath(_) => OsString::from("[held-input]"),
        })
        .collect::<Vec<_>>();
    super::observe::spawn(top, &observed_args, false)?;
    let mut child = LoopChild::spawn(command(top, extra), args, inputs)
        .map_err(|_| LegacyException::stop("git の状態読取に失敗した"))?;
    super::observe::spawned(child.id(), false)?;
    let started = Instant::now();
    let time = budget.left().min(10.0);
    let mut output = Vec::new();
    let result = (|| {
        let mut buffer = [0; 65536];
        loop {
            let left = time - started.elapsed().as_secs_f64();
            if left <= 0.0 {
                return Err(LegacyException::stop("git の状態読取が時間内に終わらない"));
            }
            // selector.select の中で受けた SIGINT。旧版は except OSError/Stop に当たらず、
            // finally で stdout を閉じるだけで、子を kill も wait もしない。
            interrupt::check().map_err(reading_frames)?;
            let ready = match child.poll(Duration::from_secs_f64(left)) {
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {
                    interrupt::check().map_err(reading_frames)?;
                    continue;
                }
                other => other.map_err(LegacyException::from)?,
            };
            // 待ちが終わった直後に受けた SIGINT も、旧版は次の命令で KeyboardInterrupt にする。
            interrupt::check().map_err(reading_frames)?;
            if !ready {
                continue;
            }
            let count = child.read(&mut buffer).map_err(LegacyException::from)?;
            if count == 0 {
                break;
            }
            if !budget.fits_chunk(count) {
                return Err(LegacyException::stop("状態の観察の総読取量が上限を超えた"));
            }
            budget.read(count)?;
            output.extend_from_slice(&buffer[..count]);
        }
        let minimum = 0.01f64.max(time - started.elapsed().as_secs_f64());
        let wait_start = Instant::now();
        loop {
            if interrupt::pending() {
                // Popen.wait の except KeyboardInterrupt: 最大 min(0.25, 残り)秒だけ待って上げ直す。
                let left = minimum - wait_start.elapsed().as_secs_f64();
                sigint_wait(&mut child, 0.25f64.min(left))?;
                return Err(interrupt::exception()
                    .external("subprocess.py", "_wait", 1953, "time.sleep(delay)")
                    .external(
                        "subprocess.py",
                        "wait",
                        1209,
                        "return self._wait(timeout=timeout)",
                    )
                    .trace(
                        "run_git",
                        545,
                        "rc = proc.wait(timeout=max(0.01, deadline - time.monotonic()))",
                    ));
            }
            if let Some(code) = child.try_wait().map_err(LegacyException::from)? {
                if code != 0 && !allow {
                    return Err(LegacyException::stop("git の状態読取に失敗した"));
                }
                return Ok((code, output));
            }
            if wait_start.elapsed().as_secs_f64() >= minimum {
                return Err(LegacyException::stop("git の状態読取が時間内に終わらない"));
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    })();
    let result = match result {
        Err(error) if error.is_os() => {
            Err(LegacyException::stop("git の状態読取に失敗した").contextual(error, true))
        }
        other => {
            if other
                .as_ref()
                .err()
                .is_some_and(|e| e.stop_reason().is_some())
                && child.try_wait()?.is_none()
            {
                child.kill_wait()?;
            }
            other
        }
    };
    child.close_stdout();
    #[cfg(test)]
    super::observe::returned(&mut child, false)?;
    result
}
fn reading_frames(error: LegacyException) -> LegacyException {
    error
        .external(
            "selectors.py",
            "select",
            469,
            "fd_event_list = self._selector.poll(timeout, max_ev)",
        )
        .trace("run_git", 532, "for key, _ in selector.select(left):")
}
/// Popen._wait(timeout) の短い待ち。子が終われば回収する。時間内に終わらなければ何もしない。
fn sigint_wait(child: &mut LoopChild, seconds: f64) -> Result<()> {
    let start = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            return Ok(());
        }
        if seconds <= 0.0 || start.elapsed().as_secs_f64() >= seconds {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}
/// index_snapshot の subprocess.run が KeyboardInterrupt を受けたときの後始末。
/// Popen.wait が最大 min(0.25, 残り)秒待ち、communicate は 0 秒で一度だけ確かめ、
/// run の except が kill する。__exit__ は待たないので回収しない。
fn index_interrupt(child: &mut LoopChild, left: f64) -> LegacyException {
    let cleanup = sigint_wait(child, 0.25f64.min(left))
        .and_then(|()| sigint_wait(child, 0.0))
        .and_then(|()| child.kill_if_running().map_err(LegacyException::from));
    if let Err(error) = cleanup {
        return error;
    }
    interrupt::exception()
        .external("subprocess.py", "_wait", 1953, "time.sleep(delay)")
        .external(
            "subprocess.py",
            "wait",
            1209,
            "return self._wait(timeout=timeout)",
        )
        .external(
            "subprocess.py",
            "_communicate",
            2047,
            "self.wait(timeout=self._remaining_time(endtime))",
        )
        .external(
            "subprocess.py",
            "communicate",
            1154,
            "stdout, stderr = self._communicate(input, endtime, timeout)",
        )
        .external(
            "subprocess.py",
            "run",
            505,
            "stdout, stderr = process.communicate(input, timeout=timeout)",
        )
        .trace("index_snapshot", 1702, INDEX_LINE)
}
const INDEX_LINE: &str = r#"proc = subprocess.run(["git", "-C", worktree, *SAFE_GIT_PREFIX,"#;
pub fn index(top: &OsStr, budget: &Budget) -> Result<bool> {
    let args = strings(&["diff-index", "--quiet", "HEAD", "--"]);
    let refs = args.iter().map(|s| LoopArg::Raw(s)).collect::<Vec<_>>();
    // timeout=min(10, budget.remaining())。
    let seconds = budget.remaining()?.min(10.0);
    super::observe::spawn(top, &args, true)?;
    let mut child = LoopChild::spawn_quiet(command(top, &[]), &refs)?;
    super::observe::spawned(child.id(), true)?;
    let start = Instant::now();
    // communicate が endtime を決めた後、Popen.wait へ渡す残り(float)が TimeoutExpired の秒になる。
    let shown = super::json::dump(&super::json::PyValue::Float(
        seconds - start.elapsed().as_secs_f64(),
    ));
    loop {
        if interrupt::pending() {
            return Err(index_interrupt(
                &mut child,
                seconds - start.elapsed().as_secs_f64(),
            ));
        }
        if let Some(code) = child.try_wait()? {
            return match code {
                0 => Ok(true),
                1 => Ok(false),
                _ => Err(LegacyException::stop("index の整合を読めない")),
            };
        }
        if start.elapsed().as_secs_f64() >= seconds {
            child.kill_wait()?;
            return Err(LegacyException::Python {
                kind: "subprocess.TimeoutExpired",
                reason: format!(
                    "Command '{}' timed out after {shown} seconds",
                    command_repr(top, &args)
                ),
                location: "index_snapshot",
            }
            .external(
                "subprocess.py",
                "_wait",
                1951,
                "raise TimeoutExpired(self.args, timeout)",
            )
            .external(
                "subprocess.py",
                "wait",
                1209,
                "return self._wait(timeout=timeout)",
            )
            .external(
                "subprocess.py",
                "_communicate",
                2047,
                "self.wait(timeout=self._remaining_time(endtime))",
            )
            .external(
                "subprocess.py",
                "communicate",
                1154,
                "stdout, stderr = self._communicate(input, endtime, timeout)",
            )
            .external(
                "subprocess.py",
                "run",
                505,
                "stdout, stderr = process.communicate(input, timeout=timeout)",
            )
            .trace("index_snapshot", 1702, INDEX_LINE));
        }
        std::thread::sleep(Duration::from_millis(1));
    }
}
fn command_repr(top: &OsStr, args: &[OsString]) -> String {
    use std::os::unix::ffi::OsStrExt;
    [OsString::from("git"), OsString::from("-C"), top.to_owned()]
        .into_iter()
        .chain(SAFE_GIT.iter().map(OsString::from))
        .chain(args.iter().cloned())
        .map(|v| super::args::repr(v.as_bytes()))
        .collect::<Vec<_>>()
        .join(", ")
        .pipe_brackets()
}
trait Brackets {
    fn pipe_brackets(self) -> String;
}
impl Brackets for String {
    fn pipe_brackets(self) -> String {
        format!("[{self}]")
    }
}
