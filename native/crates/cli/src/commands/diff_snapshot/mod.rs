//! Development-only snapshot implementation. The public product CLI is unchanged.
mod approval;
mod args;
pub mod bytes;
mod ere;
mod git;
mod helper_args;
mod legacy_text;
mod output;
mod precheck;
mod publish;
mod report;
mod secret_profiles;
mod strerror_table;
mod tools;
mod tracked;
#[cfg(test)]
mod typed_tests;
mod untracked;
use args::Args;
use bytes::{joined, os, raw, trim_lf};
use dev_workflow_core::{CommandOutput, ExitCode};
use dev_workflow_platform::diff_snapshot::{NativeOperations, NoAudit};
use dev_workflow_platform::diff_snapshot::{fs as sfs, process, signals::Signals};
use git::{Git, words};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
pub type Result<T> = std::result::Result<T, Failure>;
#[derive(Debug)]
pub struct Failure {
    pub code: u8,
    pub stderr: Vec<u8>,
}
impl Failure {
    pub fn raw(code: u8, stderr: Vec<u8>) -> Self {
        Self { code, stderr }
    }
    pub fn silent(code: u8) -> Self {
        Self::raw(code, vec![])
    }
    pub fn usage(s: &str) -> Self {
        Self::usage_bytes(s.as_bytes().to_vec())
    }
    pub fn usage_bytes(s: Vec<u8>) -> Self {
        Self::raw(2, joined(&[b"ERROR [usage] ", &s, b"\n"]))
    }
    pub fn internal(s: &str) -> Self {
        Self::raw(20, joined(&[b"ERROR [internal] ", s.as_bytes(), b"\n"]))
    }
    pub fn unexpected(code: i32, line: usize) -> Self {
        Self::internal(&format!("予期しない失敗(終了コード {code}・行 {line})"))
    }
    /// A failing command inside `$(...)` under `set -E`: the subshell's ERR trap
    /// reports the command's status, then the parent's trap reports 20.
    pub fn substitution(code: i32, line: usize) -> Self {
        Self::raw(
            20,
            joined(&[
                &Self::unexpected(code, line).stderr,
                &Self::unexpected(20, line).stderr,
            ]),
        )
    }
    /// Keep the external command's own diagnostic before the old shell's line.
    pub fn after(stderr: &[u8], failure: Self) -> Self {
        Self::raw(failure.code, joined(&[stderr, &failure.stderr]))
    }
}
fn signal_checkpoint() -> Result<()> {
    if dev_workflow_platform::diff_snapshot::signals::pending() {
        Err(Failure::silent(20))
    } else {
        Ok(())
    }
}
/// Bash (`notify_of_job_status`) reports a foreground job of the main shell
/// that a trapped signal other than SIGINT ended (SIGPIPE is not reported):
/// the old script traps TERM, INT and HUP, so `Terminated` and `Hangup`. A job
/// inside `$(…)` is not reported. For a pipeline the signal is that of its
/// first element, in order, that a signal ended (`job_signal_status`).
fn job_status(signal: Option<i32>) {
    if let Some(text) = dev_workflow_platform::diff_snapshot::signals::job_report(signal) {
        let _ = output::stderr(&[text.as_bytes(), b"\n"].concat());
    }
}
/// A foreground job of the old script (not inside `$(…)`): run it, report it as
/// Bash did, then run a pending trap. The outer error is the trap.
fn foreground(command: &mut Command, input: &[u8]) -> Result<std::io::Result<process::Output>> {
    signal_checkpoint()?;
    let result = process::capture(command, input, None);
    if let Ok(o) = &result {
        job_status(o.signal);
    }
    signal_checkpoint()?;
    Ok(result)
}
/// Bash runs a pending trap immediately after its foreground child finishes.
/// Preserve that boundary before interpreting output or starting another child.
fn capture(
    command: &mut Command,
    input: &[u8],
    timeout: Option<std::time::Duration>,
) -> std::io::Result<process::Output> {
    let cancelled = || std::io::Error::from(std::io::ErrorKind::Interrupted);
    signal_checkpoint().map_err(|_| cancelled())?;
    let result = process::capture(command, input, timeout);
    signal_checkpoint().map_err(|_| cancelled())?;
    result
}
pub fn sha(bytes: &[u8]) -> Vec<u8> {
    format!("{:x}", Sha256::digest(bytes)).into_bytes()
}
pub struct Temp(PathBuf, usize);
/// The first failing removal of the old EXIT trap `cleanup` (242・245〜247):
/// `set -e` ends the trap there, so later removals do not run.
struct Stop {
    diagnostic: Vec<u8>,
    line: usize,
    /// The removal the repeated `cleanup` of `on_signal` tries again.
    retry: (PathBuf, bool),
}
thread_local! {
    static CLEANUP_ACTIVE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
    static CLEANUP_STOP: std::cell::RefCell<Option<Stop>> = const { std::cell::RefCell::new(None) };
}
/// Run one step of the old `cleanup`. Outside a command run it only removes.
fn cleanup_step(line: usize, retry: (&Path, bool), step: impl FnOnce() -> Vec<u8>) {
    if !CLEANUP_ACTIVE.with(|a| a.get()) {
        let diagnostic = step();
        if !diagnostic.is_empty() {
            let _ = output::stderr(&diagnostic);
        }
        return;
    }
    if CLEANUP_STOP.with(|s| s.borrow().is_some()) {
        return;
    }
    let diagnostic = step();
    if !diagnostic.is_empty() {
        CLEANUP_STOP.with(|s| {
            *s.borrow_mut() = Some(Stop {
                diagnostic,
                line,
                retry: (retry.0.to_path_buf(), retry.1),
            })
        });
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        // rm -rf -- "$PUB" / "$PUB2" / "$TMPD" in the old cleanup trap.
        let path = self.0.clone();
        cleanup_step(self.1, (&path, true), || tools::remove_tree(&path));
    }
}
/// How the old script reached its EXIT trap.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Route {
    /// `exit N` with the ERR trap set: a failing removal runs the ERR trap.
    Normal,
    /// After the ERR trap or `fail_*`: `set -e` ends the trap with status 1.
    Trapped,
    /// `on_signal`: the ERR trap runs, then the EXIT trap repeats `cleanup`.
    Signal,
}
/// Apply the old cleanup outcome to the command result.
fn after_cleanup(result: Result<u8>, route: Route) -> Result<u8> {
    CLEANUP_ACTIVE.with(|a| a.set(false));
    let Some(stop) = CLEANUP_STOP.with(|s| s.borrow_mut().take()) else {
        return result;
    };
    let before = match result {
        Ok(_) => vec![],
        Err(f) => f.stderr,
    };
    let trap = Failure::unexpected(1, stop.line).stderr;
    let stderr = match route {
        Route::Normal => {
            return Err(Failure::raw(
                20,
                joined(&[&before, &stop.diagnostic, &trap]),
            ));
        }
        Route::Trapped => joined(&[&before, &stop.diagnostic]),
        Route::Signal => {
            let again = match &stop.retry {
                (path, true) => tools::remove_tree(path),
                (path, false) => tools::remove_file(path).err().unwrap_or_default(),
            };
            joined(&[&before, &stop.diagnostic, &trap, &again])
        }
    };
    Err(Failure::raw(1, stderr))
}
/// `VAR="$(mktemp -d [TEMPLATE])"` at the given old line. The directory exists
/// as soon as mktemp returns, so own it before acting on a pending signal.
pub fn temp(template: Option<&Path>, line: usize) -> Result<Temp> {
    signal_checkpoint()?;
    let created = tools::mktemp_dir(template.map(|p| raw(p.as_os_str())));
    // The directory exists as soon as it is made; own it before a pending signal.
    let cleanup_line = match line {
        1186 => 245,
        1190 => 246,
        _ => 247,
    };
    let created = created.map(|path| Temp(path, cleanup_line));
    signal_checkpoint()?;
    created.map_err(|diagnostic| Failure::after(&diagnostic, Failure::substitution(1, line)))
}
/// A simple command whose stderr was not redirected by the old script.
/// An in-process tool's failure at an old simple command: its diagnostic, then
/// the old ERR trap line.
pub fn tool_failure(diagnostic: &[u8], line: usize) -> Failure {
    Failure::after(diagnostic, Failure::unexpected(1, line))
}
/// `VAR="$(wc -c <"$P" | tr -d ' ')"`: Bash's own diagnostic for the failed
/// redirection, then the ERR traps of the substitution and of the assignment.
fn open_size(path: &Path, shown: &[u8], point: &'static str, line: usize) -> Result<u64> {
    sfs::open_size(path, point).map_err(|e| {
        Failure::after(
            &joined(&[
                b"diff-snapshot.sh: line ",
                line.to_string().as_bytes(),
                b": ",
                shown,
                b": ",
                tools::strerror(&e).as_bytes(),
                b"\n",
            ]),
            Failure::substitution(1, line),
        )
    })
}
/// `VAR="$(sha256_file FILE)"` at `line`: the redirection `<"$1"` at 279
/// fails (Bash's diagnostic) or sha256sum's read fails (its own), then the
/// ERR traps of the substitution (279) and of the assignment (`line`).
pub fn sha_failure(shown: &[u8], failure: sfs::ReadFailure, line: usize) -> Failure {
    let diagnostic = match failure {
        sfs::ReadFailure::Open(e) => joined(&[
            b"diff-snapshot.sh: line 279: ",
            shown,
            b": ",
            tools::strerror(&e).as_bytes(),
            b"\n",
        ]),
        sfs::ReadFailure::Read(e, _) => {
            joined(&[b"sha256sum: -: ", tools::strerror(&e).as_bytes(), b"\n"])
        }
    };
    Failure::after(
        &diagnostic,
        Failure::raw(
            20,
            joined(&[
                &Failure::unexpected(1, 279).stderr,
                &Failure::unexpected(20, line).stderr,
            ]),
        ),
    )
}
/// `if head_has_nul FILE` (1312-1317): both `head -c 8000 <"$1"` pipelines
/// (1314・1315) report the failure, the condition context keeps the ERR trap
/// quiet, and the counts of what was read decide (no NUL: not binary).
fn head_has_nul(path: &Path, shown: &[u8], phase: &'static str) -> Result<bool> {
    match sfs::read(path, Some(8000), phase, &mut NoAudit) {
        Ok(data) => Ok(data.contains(&0)),
        Err(failure) => {
            let (lines, data) = match failure {
                sfs::ReadFailure::Open(e) => {
                    let reason = tools::strerror(&e);
                    let at = |line: &[u8]| {
                        joined(&[
                            b"diff-snapshot.sh: line ",
                            line,
                            b": ",
                            shown,
                            b": ",
                            reason.as_bytes(),
                            b"\n",
                        ])
                    };
                    (joined(&[&at(b"1314"), &at(b"1315")]), vec![])
                }
                sfs::ReadFailure::Read(e, data) => {
                    let line = joined(&[
                        b"head: error reading 'standard input': ",
                        tools::strerror(&e).as_bytes(),
                        b"\n",
                    ]);
                    (joined(&[&line, &line]), data)
                }
            };
            output::stderr(&lines)?;
            Ok(data.contains(&0))
        }
    }
}
/// abs_existing_dir (`realpath --`) and abs_maybe (`realpath -m --`), with
/// their fallbacks, computed in process. Failures give an empty value.
fn external_absolute(path: &Path, missing: bool) -> Result<Option<PathBuf>> {
    let value = if missing {
        tools::realpath_missing(path)
    } else {
        path.canonicalize().ok()
    };
    Ok(value.or_else(|| path.canonicalize().ok()))
}
/// `case "$P" in "$TOP"|"$TOP"/*)`: the strings, not path components (with a
/// TOP of `/`, only `/` itself, since `//*` matches no resolved path).
fn inside(path: &Path, top: &Path) -> bool {
    let (path, top) = (raw(path.as_os_str()), raw(top.as_os_str()));
    path == top || (path.starts_with(top) && path.get(top.len()) == Some(&b'/'))
}
/// abs_from_caller: `"$CALLER_PWD/$1"` unless the value starts with `/`.
fn absolute_from(caller: &Path, value: &[u8]) -> PathBuf {
    if value.starts_with(b"/") {
        PathBuf::from(os(value))
    } else {
        PathBuf::from(os(&joined(&[raw(caller.as_os_str()), b"/", value])))
    }
}
#[derive(Clone)]
pub struct Row {
    one: Vec<u8>,
    two: Vec<u8>,
    three: Vec<u8>,
    four: Vec<u8>,
    allowed: bool,
}
pub struct Snapshot {
    args: Args,
    git: Git,
    root: PathBuf,
    temp: Temp,
    out: PathBuf,
    patch: Option<PathBuf>,
    ere: Vec<u8>,
    notes: Vec<Vec<u8>>,
    rows: Vec<Row>,
    tamper: bool,
    stop: bool,
    digest: Vec<u8>,
    show_digest: bool,
    mismatch: bool,
    included: Vec<Vec<u8>>,
    tracked: Vec<Vec<u8>>,
    untracked: Vec<Vec<u8>>,
    stage: Vec<Vec<u8>>,
    sparse: Vec<Vec<u8>>,
    filters: HashMap<Vec<u8>, Vec<u8>>,
    pre: HashSet<Vec<u8>>,
    include: HashSet<Vec<u8>>,
    sections: [Vec<u8>; 11],
    counts: [usize; 7],
    failed: usize,
    body_empty: Vec<u8>,
    head: Vec<u8>,
    ancestor: Vec<u8>,
    want_patch: bool,
    patch_bytes: Vec<u8>,
    signals: Signals,
}
impl Snapshot {
    fn tracked_changed_included(&self, path: &[u8]) -> bool {
        self.included.iter().any(|p| p == path)
    }
    fn note(&mut self, value: impl AsRef<[u8]>) {
        self.notes.push(value.as_ref().to_vec());
    }
    fn row(&mut self, one: &[u8], two: &[u8], three: &[u8], four: &[u8], allowed: bool) {
        self.rows.push(Row {
            one: one.to_vec(),
            two: two.to_vec(),
            three: three.to_vec(),
            four: four.to_vec(),
            allowed,
        });
        self.tamper = true;
    }
    fn checkpoint(&self) -> Result<()> {
        if self.signals.received().is_some() {
            Err(Failure::silent(20))
        } else {
            Ok(())
        }
    }
    fn classify(&self, path: &[u8], untracked: bool) -> Result<Class> {
        if ere::matches(path,br"(^|/)\.claude/(reviews/|grasp\.md$|settings\.local\.json$|\.understand-project-done$)")?{return Ok(Class::Default("既定除外"));}
        // The old script compares the strings "$TOP/$path" and the checked output.
        let abs = joined(&[raw(self.root.as_os_str()), b"/", path]);
        if abs == raw(self.out.as_os_str())
            || self
                .patch
                .as_ref()
                .is_some_and(|p| abs == raw(p.as_os_str()))
        {
            return Ok(Class::Default("出力先自身"));
        }
        if path != b".claude/project-profile.yml"
            && !self.ere.is_empty()
            && ere::matches(path, &self.ere)?
        {
            return Ok(Class::Secret);
        }
        if untracked && self.pre.contains(path) && !self.include.contains(path) {
            return Ok(Class::Outside);
        }
        Ok(Class::Include)
    }
}
enum Class {
    Default(&'static str),
    Secret,
    Outside,
    Include,
}
pub fn execute(input: Vec<OsString>, signals: Signals) -> CommandOutput {
    CLEANUP_STOP.with(|s| s.borrow_mut().take());
    CLEANUP_ACTIVE.with(|a| a.set(true));
    let observed = signals.clone();
    let directory = sfs::SavedDirectory::save();
    let result = run(input, signals);
    let _ = process::retry_pending();
    let signalled =
        observed.received().is_some() || dev_workflow_platform::diff_snapshot::signals::pending();
    let result = if signalled {
        Err(Failure::silent(20))
    } else {
        result
    };
    let route = match &result {
        _ if signalled => Route::Signal,
        Ok(_) => Route::Normal,
        Err(_) => Route::Trapped,
    };
    let result = after_cleanup(result, route);
    if let Some(directory) = directory {
        directory.restore();
    }
    match result {
        Ok(code) => CommandOutput {
            stdout: vec![],
            stderr: vec![],
            code: ExitCode::new(code),
        },
        Err(f) => CommandOutput {
            stdout: vec![],
            stderr: f.stderr,
            code: ExitCode::new(f.code),
        },
    }
}
pub fn main_entry() -> ExitCode {
    // Judge BASH_XTRACEFD before this process opens its own descriptors.
    *TRACE_WARNING.lock().unwrap_or_else(|p| p.into_inner()) = Some(trace_warning_now());
    let signals = match Signals::install() {
        Ok(s) => s,
        Err(_) => return ExitCode::new(20),
    };
    let output = execute(std::env::args_os().skip(1).collect(), signals.clone());
    if output::stderr(&output.stderr).is_err() {
        ExitCode::new(1)
    } else {
        output.code
    }
}
/// Bash's `legal_number`: strtoimax after optional C spaces and a sign,
/// followed only by spaces or tabs.
fn shell_number(value: &[u8]) -> Option<i64> {
    let start = value
        .iter()
        .take_while(|b| matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r'))
        .count();
    let rest = &value[start..];
    let sign = usize::from(matches!(rest.first(), Some(b'+' | b'-')));
    let digits = rest[sign..]
        .iter()
        .take_while(|b| b.is_ascii_digit())
        .count();
    if digits == 0 {
        return None;
    }
    if !rest[sign + digits..]
        .iter()
        .all(|b| matches!(b, b' ' | b'\t'))
    {
        return None;
    }
    std::str::from_utf8(&rest[..sign + digits])
        .ok()?
        .parse::<i64>()
        .ok()
}
/// The variables Bash itself exports to every child of the old script.
fn start_shell() -> Option<OsString> {
    use std::os::unix::fs::MetadataExt;
    let same = |a: &Path, b: &Path| match (std::fs::metadata(a), std::fs::metadata(b)) {
        (Ok(a), Ok(b)) => a.dev() == b.dev() && a.ino() == b.ino(),
        _ => false,
    };
    let pwd = std::env::var_os("PWD")
        .filter(|p| p.as_encoded_bytes().starts_with(b"/") && same(Path::new(p), Path::new(".")))
        .or_else(|| std::env::current_dir().ok().map(PathBuf::into_os_string));
    let oldpwd = std::env::var_os("OLDPWD").filter(|p| Path::new(p).is_dir());
    let old = std::env::var_os("SHLVL")
        .and_then(|v| shell_number(raw(&v)))
        .unwrap_or(0);
    // adjust_shell_level: intmax_t arithmetic stored in Bash's `int`.
    let level = match old.wrapping_add(1) as i32 {
        n if n < 0 => 0,
        n if n >= 1000 => {
            // bash 5.2.21 (00:35の基準): the start-up warning, with the fixed name `bash`.
            let _ = output::stderr(
                format!("bash: warning: shell level ({n}) too high, resetting to 1\n").as_bytes(),
            );
            1
        }
        n => n,
    };
    // initialize_shell_variables: sv_xtracefd, then sv_shcompat, after the SHLVL
    // warning and before the locale one. Only the warnings are reproduced; the
    // trace destination and the compatibility level are not (09:58 決定 3).
    for warning in [trace_warning(), compat_warning()].into_iter().flatten() {
        let _ = output::stderr(&warning);
    }
    setlocale_warning();
    // bash 5.2.21 with dynamic variables in its environment (observed in the
    // reference container): these six lose their value at start-up and reach no
    // child; EPOCHSECONDS and EPOCHREALTIME are regenerated; SECONDS keeps the
    // inherited text. LINENO is regenerated when Bash rebuilds the export
    // environment (`process::Lineno`): each site below names its old line.
    let mut exported: Vec<(OsString, Option<OsString>)> = vec![
        ("GIT_NO_LAZY_FETCH".into(), Some("1".into())),
        ("PWD".into(), pwd.clone()),
        ("OLDPWD".into(), oldpwd),
    ];
    for name in [
        "RANDOM",
        "SRANDOM",
        "BASHPID",
        "BASH_COMMAND",
        "BASH_SUBSHELL",
        "HISTCMD",
    ] {
        if std::env::var_os(name).is_some() {
            exported.push((name.into(), None));
        }
    }
    let epoch = ["EPOCHSECONDS", "EPOCHREALTIME"]
        .into_iter()
        .filter(|name| std::env::var_os(name).is_some())
        .map(OsString::from)
        .collect();
    process::set_shell(process::Shell {
        exported,
        level: level.to_string().into(),
        // bash 5.2.21 (00:35の基準) gives pipeline elements the same SHLVL as
        // other children (bash 5.1 gave one less).
        subshell_level: level.to_string().into(),
        epoch,
        lineno: std::env::var_os("LINENO").map(|_| process::Lineno::start()),
    });
    pwd
}
/// The BASH_XTRACEFD warning decided by `main_entry` before it opened anything
/// (`None` when the command runs in-process and judges at start-up instead).
static TRACE_WARNING: std::sync::Mutex<Option<Option<Vec<u8>>>> = std::sync::Mutex::new(None);
/// C `strtol(value, &end, 10)` that consumed the whole value: C spaces, a sign
/// and at least one digit, saturating at the `long` bounds.
fn whole_strtol(value: &[u8]) -> Option<i64> {
    let start = value
        .iter()
        .take_while(|b| matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r'))
        .count();
    let rest = &value[start..];
    let negative = rest.first() == Some(&b'-');
    let digits = &rest[usize::from(matches!(rest.first(), Some(b'+' | b'-')))..];
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let limit = if negative { i64::MIN } else { i64::MAX };
    Some(digits.iter().fold(0i64, |n, d| {
        let d = i64::from(d - b'0');
        n.checked_mul(10)
            .and_then(|n| {
                if negative {
                    n.checked_sub(d)
                } else {
                    n.checked_add(d)
                }
            })
            .unwrap_or(limit)
    }))
}
/// bash 5.2.21 sv_xtracefd at start-up: a value strtol does not read whole, or
/// whose number (cast to `int`) is not an open descriptor, gives the invalid
/// value warning; a read-only descriptor gives the fdopen warning. The text is
/// the `LC_ALL=C` English one, with the fixed name `bash`.
fn trace_warning_now() -> Option<Vec<u8>> {
    trace_text(raw(&std::env::var_os("BASH_XTRACEFD")?))
}
fn trace_text(value: &[u8]) -> Option<Vec<u8>> {
    use dev_workflow_platform::diff_snapshot::fs::TraceDescriptor;
    if value.is_empty() {
        return None;
    }
    let reason: &[u8] = match whole_strtol(value).map(|n| sfs::trace_descriptor(n as i32)) {
        Some(TraceDescriptor::Writable) => return None,
        Some(TraceDescriptor::ReadOnly) => b"cannot open as FILE",
        Some(TraceDescriptor::Closed) | None => b"invalid value for trace file descriptor",
    };
    Some(joined(&[
        b"bash: BASH_XTRACEFD: ",
        value,
        b": ",
        reason,
        b"\n",
    ]))
}
fn trace_warning() -> Option<Vec<u8>> {
    let recorded = TRACE_WARNING
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .take();
    recorded.unwrap_or_else(trace_warning_now)
}
/// bash 5.2.21 sv_shcompat at start-up: a non-empty value that is not `d.d` or
/// `dd` from 31 to 52 gives the out of range warning (fixed name `bash`).
fn compat_warning() -> Option<Vec<u8>> {
    compat_text(raw(&std::env::var_os("BASH_COMPAT")?))
}
fn compat_text(value: &[u8]) -> Option<Vec<u8>> {
    let level = match value {
        [] => return None,
        [a, b'.', b] | [a, b] if a.is_ascii_digit() && b.is_ascii_digit() => {
            (a - b'0') * 10 + (b - b'0')
        }
        _ => 0,
    };
    (!(31..=52).contains(&level)).then(|| {
        joined(&[
            b"bash: BASH_COMPAT: ",
            value,
            b": compatibility value out of range\n",
        ])
    })
}
/// bash 5.2.21 (00:35の基準) warns at start-up when it cannot switch to the
/// inherited `LC_ALL` (not `LANG` nor the other `LC_*`), after the SHLVL warning.
/// The text is the `LC_ALL=C` English one, with the fixed name `bash`.
fn setlocale_warning() {
    use dev_workflow_platform::diff_snapshot::locale::{self, Locale};
    let Some(value) = std::env::var_os("LC_ALL").filter(|v| !v.is_empty()) else {
        return;
    };
    let locpath = std::env::var_os("LOCPATH");
    let reason = match locale::lc_all(raw(&value), locpath.as_deref().map(raw)) {
        Locale::Found => return,
        Locale::Missing => vec![],
        Locale::Failed(code) => joined(&[
            b": ",
            tools::strerror(&std::io::Error::from_raw_os_error(code)).as_bytes(),
        ]),
    };
    let _ = output::stderr(&joined(&[
        b"bash: warning: setlocale: LC_ALL: cannot change locale (",
        raw(&value),
        b")",
        &reason,
        b"\n",
    ]));
}
fn run(input: Vec<OsString>, signals: Signals) -> Result<u8> {
    let caller_pwd = start_shell();
    let a = Args::parse(input.iter().map(|s| raw(s).to_vec()).collect())?;
    let ere = if (!a.globs.is_empty() || !a.eres.is_empty()) && a.refs.is_empty() {
        ere::build(&a.globs, &a.eres)?
    } else {
        vec![]
    };
    if a.print {
        output::stdout(&joined(&[&ere, b"\n"]), 567)?;
        return Ok(0);
    }
    let cwd = PathBuf::from(os(&a.cwd));
    if !cwd.is_dir() {
        return Err(Failure::usage_bytes(joined(&[
            "--cwd が存在しない: ".as_bytes(),
            &a.cwd,
        ])));
    }
    // CALLER_PWD="$PWD" (601): Bash's logical directory, as start_shell found it.
    let caller = caller_pwd
        .clone()
        .map(PathBuf::from)
        .ok_or_else(|| Failure::unexpected(1, 601))?;
    let mut initial = Git::new(caller.clone());
    let top = initial.at(576).run(
        &[
            words(&["-C"]),
            vec![a.cwd.clone()],
            words(&["rev-parse", "--show-toplevel"]),
        ]
        .concat(),
        b"",
    )?;
    let topraw = trim_lf(top.stdout);
    if top.code != 0 || topraw.is_empty() {
        return Err(Failure::usage_bytes(joined(&[
            "--cwd が git リポジトリの中でない: ".as_bytes(),
            &a.cwd,
        ])));
    }
    let root = external_absolute(Path::new(&os(&topraw)), false)?.ok_or_else(|| {
        Failure::usage_bytes(joined(&[
            "toplevel の物理パスを解決できない: ".as_bytes(),
            &topraw,
        ]))
    })?;
    let cwdphys = external_absolute(&cwd, false)?;
    if !cwdphys.as_ref().is_some_and(|p| inside(p, &root)) {
        let val = initial.at(587).run(
            &[
                words(&["-C"]),
                vec![a.cwd.clone()],
                words(&["config", "--get", "core.worktree"]),
            ]
            .concat(),
            b"",
        )?;
        let val = if val.code == 0 {
            trim_lf(val.stdout)
        } else {
            vec![]
        };
        // WT_TMP="$(mktemp -d)" (589) and `rm -rf -- "$WT_TMP"` (594). The
        // value written there is hashed in memory (00:35).
        let scratch = tools::mktemp_dir(None)
            .map_err(|d| Failure::after(&d, Failure::substitution(1, 589)))?;
        let removed = tools::remove_tree(&scratch);
        if !removed.is_empty() {
            return Err(tool_failure(&removed, 594));
        }
        return Err(Failure::raw(22,joined(&[b"core.worktree\t","ローカル設定: core.worktree\t".as_bytes(),&sha(&val),b"\t",&bytes::display_value(&val),"\nWARNING: 作業ツリーが差し替えられている疑いがある(--cwd が toplevel の配下にない)\n".as_bytes()])));
    }
    initial.root = root.clone();
    // CDPATH= cd -P -- "$TOP" (610): later relative paths (a relative TMPDIR
    // for mktemp, GIT_INDEX_FILE under it) are resolved from TOP.
    if let Err(e) = std::env::set_current_dir(&root) {
        return Err(Failure::after(
            &joined(&[
                b"diff-snapshot.sh: line 610: cd: ",
                raw(root.as_os_str()),
                b": ",
                tools::strerror(&e).as_bytes(),
                b"\n",
            ]),
            Failure::unexpected(1, 610),
        ));
    }
    process::export("OLDPWD", caller_pwd);
    process::export("PWD", Some(root.clone().into_os_string()));
    // The temporary `CDPATH=` is disposed of after the command: Bash rebuilds
    // its export environment there (610).
    process::remake(610);
    let temp = temp(None, 613)?;
    if external_absolute(&temp.0, false)?.is_some_and(|p| inside(&p, &root)) {
        return Err(Failure::usage("一時ディレクトリがリポジトリ内にある"));
    }
    let mut s = Snapshot {
        args: a,
        git: initial,
        root,
        temp,
        out: PathBuf::new(),
        patch: None,
        ere,
        notes: vec![],
        rows: vec![],
        tamper: false,
        stop: false,
        digest: vec![],
        show_digest: false,
        mismatch: false,
        included: vec![],
        tracked: vec![],
        untracked: vec![],
        stage: vec![],
        sparse: vec![],
        filters: HashMap::new(),
        pre: HashSet::new(),
        include: HashSet::new(),
        sections: std::array::from_fn(|_| vec![]),
        counts: [0; 7],
        failed: 0,
        body_empty: "(差分なし)".as_bytes().to_vec(),
        head: vec![],
        ancestor: vec![],
        want_patch: false,
        patch_bytes: vec![],
        signals,
    };
    if !s.args.precheck {
        if !s.args.patch.is_empty() {
            s.patch = Some(publish::check_path(
                &s.root,
                raw(absolute_from(&caller, &s.args.patch).as_os_str()),
                true,
            )?);
        }
        s.out = publish::check_path(
            &s.root,
            raw(absolute_from(&caller, &s.args.out).as_os_str()),
            false,
        )?;
        if s.patch
            .as_ref()
            .is_some_and(|p| raw(p.as_os_str()) == raw(s.out.as_os_str()))
        {
            return Err(Failure::usage(
                "--patch-out と --out が同じパスを指している",
            ));
        }
    }
    for include in &s.args.includes {
        if include.is_empty() {
            return Err(Failure::usage("--include-untracked に空の値は指定できない"));
        }
        s.include.insert(include.clone());
    }
    if !s.args.pre.is_empty() {
        let p = absolute_from(&caller, &s.args.pre);
        let pbytes = raw(p.as_os_str());
        if !sfs::exists_or_link(&p) {
            return Err(Failure::usage_bytes(joined(&[
                "--pre-untracked が存在しない: ".as_bytes(),
                pbytes,
            ])));
        }
        if sfs::symlink(&p) || !p.is_file() || !sfs::readable(&p) {
            return Err(Failure::usage_bytes(joined(&[
                "--pre-untracked は symlink でない読み取り可能な通常ファイルに限る: ".as_bytes(),
                pbytes,
            ])));
        }
        // GOT_SHA="$(sha256_file "$PRE_LIST")" (694), then `done <"$PRE_LIST"`.
        let data = sfs::read(&p, None, "pre-list", &mut NoAudit)
            .map_err(|failure| sha_failure(pbytes, failure, 694))?;
        if sha(&data) != s.args.pre_sha {
            return Err(Failure::usage("未追跡一覧のダイジェストが一致しない"));
        }
        for p in bytes::zrecords(&data) {
            if p.is_empty() {
                return Err(Failure::usage("--pre-untracked の一覧に空のレコードがある"));
            }
            s.pre.insert(p);
        }
        // `while IFS= read …; done <"$PRE_LIST"` (696).
        process::mark();
    }
    precheck::run(&mut s)?;
    s.checkpoint()?;
    finish(s)
}
fn finish(mut s: Snapshot) -> Result<u8> {
    approval::apply(&mut s)?;
    if s.args.precheck {
        approval::notes(&s)?;
        if s.tamper {
            approval::mismatch(&s)?;
            output::stderr(&approval::rows(&s))?;
            return Ok(22);
        }
        approval::tokens(&s)?;
        return Ok(0);
    }
    report::header(&mut s)?;
    if s.tamper {
        approval::mismatch(&s)?;
        s.sections[9] = approval::rows(&s);
        output::stderr(&s.sections[9])?;
        let body = report::assemble(&s, true)?;
        publish::publish(&s, &body, &mut publish::Native)?;
        return Ok(22);
    }
    approval::tokens(&s)?;
    secret_profiles::union(&mut s)?;
    s.checkpoint()?;
    tracked::run(&mut s)?;
    s.checkpoint()?;
    untracked::run(&mut s)?;
    s.checkpoint()?;
    report::finish_notes(&mut s)?;
    tracked::patch(&mut s)?;
    report::binary_tamper(&mut s)?;
    let body = report::assemble(&s, false)?;
    publish::publish(&s, &body, &mut publish::Native)?;
    s.checkpoint()?;
    Ok(if s.tamper {
        22
    } else if s.failed > 0 {
        21
    } else {
        0
    })
}

#[cfg(test)]
mod startup_warning_tests {
    use super::{compat_text, trace_text, whole_strtol};

    #[test]
    fn strtol_reads_the_whole_value_and_saturates() {
        for (value, expected) in [
            (&b"99"[..], Some(99)),
            (b" \t99", Some(99)),
            (b"+99", Some(99)),
            (b"-1", Some(-1)),
            (b"4294967395", Some(4_294_967_395)),
            (b"99999999999999999999", Some(i64::MAX)),
            (b"-99999999999999999999", Some(i64::MIN)),
            (b"", None),
            (b"+", None),
            (b"2x", None),
            (b"2 ", None),
            (b"0x1", None),
        ] {
            assert_eq!(expected, whole_strtol(value), "{value:?}");
        }
        assert_eq!(99, 4_294_967_395_i64 as i32);
        assert_eq!(-1, i64::MAX as i32);
    }

    #[test]
    fn startup_warnings_follow_bash_5_2_21() {
        assert_eq!(None, compat_text(b""));
        for value in ["31", "3.1", "42", "5.2", "52"] {
            assert_eq!(None, compat_text(value.as_bytes()), "{value}");
        }
        for value in ["30", "3.0", "53", "5.3", "99", "5.10", "051", "abc", " 51"] {
            assert_eq!(
                Some(
                    format!("bash: BASH_COMPAT: {value}: compatibility value out of range\n")
                        .into_bytes()
                ),
                compat_text(value.as_bytes())
            );
        }
        assert_eq!(None, trace_text(b""));
        // 2 is the open standard error (4294967298 wraps to it as Bash's int cast does).
        assert_eq!(None, trace_text(b"2"));
        assert_eq!(None, trace_text(b"4294967298"));
        for value in [
            "abc",
            "2x",
            "-1",
            "99999999999999999999",
            "4294967395",
            "1048575",
        ] {
            assert_eq!(
                Some(
                    format!(
                        "bash: BASH_XTRACEFD: {value}: invalid value for trace file descriptor\n"
                    )
                    .into_bytes()
                ),
                trace_text(value.as_bytes())
            );
        }
    }
}
