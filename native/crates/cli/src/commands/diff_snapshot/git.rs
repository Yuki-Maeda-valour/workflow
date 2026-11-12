use super::bytes::{joined, os};
use super::{Failure, Result};
use dev_workflow_platform::diff_snapshot::process::{self, Output};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
#[derive(Clone)]
pub struct Git {
    pub root: PathBuf,
    pub env: Vec<(OsString, OsString)>,
    pub index: Option<PathBuf>,
}
impl Git {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            env: Vec::new(),
            index: None,
        }
    }
    /// The old Git command was forked at LINE (`process::fork`): this is where
    /// a marked export environment got its LINENO.
    pub fn at(&self, line: u32) -> &Self {
        process::fork(line);
        self
    }
    /// The old command at LINE had its own temporary assignment
    /// (`GIT_INDEX_FILE="$TMPIDX" git …`): the child rebuilt its environment, so
    /// it sees LINE whatever the shell had (`process::own_line`).
    pub fn with_line(&self, line: u32) -> Self {
        process::fork(line);
        let mut git = self.clone();
        git.env.extend(process::own_line(line));
        git
    }
    pub fn command(&self, extra: &[Vec<u8>]) -> Command {
        let mut c = Command::new("git");
        c.current_dir(&self.root)
            .env("LC_ALL", "C")
            .env("GIT_NO_LAZY_FETCH", "1");
        c.args([
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
        ]);
        c.args(extra.iter().map(|v| os(v)));
        c.envs(self.env.clone());
        if let Some(path) = &self.index {
            c.env("GIT_INDEX_FILE", path);
        }
        c
    }
    pub fn run(&self, args: &[Vec<u8>], input: &[u8]) -> Result<Output> {
        super::capture(&mut self.command(args), input, None)
            .map_err(|_| Failure::internal("git の呼び出しに失敗した(終了コード 127)"))
    }
    /// `git ARGS <FILE`: the old script fed prepared files, never a pipe.
    pub fn run_file(&self, args: &[Vec<u8>], input: &Path) -> Result<Output> {
        let failed = || Failure::internal("git の呼び出しに失敗した(終了コード 127)");
        super::signal_checkpoint()?;
        let file = std::fs::File::open(input).map_err(|_| failed())?;
        let result = process::capture_stdin_file(&mut self.command(args), file);
        if let Ok(o) = &result {
            job_status(o);
        }
        super::signal_checkpoint()?;
        result.map_err(|_| failed())
    }
    /// Write the input as the old script's `$TMPD/NAME` and run with it as stdin.
    /// A failing write gives the old writer's diagnostic (`cat` or Bash's
    /// `printf`) and the ERR trap of its line, as the old script did.
    pub fn run_temp_input(
        &self,
        args: &[Vec<u8>],
        temp: &Path,
        name: &str,
        input: &[u8],
        writer: Writer,
    ) -> Result<Output> {
        let path = temp.join(name);
        dev_workflow_platform::diff_snapshot::fs::write_input(&path, input, name)
            .map_err(|e| writer.failure(&e))?;
        self.run_file(args, &path)
    }
    /// `git ARGS [<$TMPD/IN] >$TMPD/OUT 2>…`: the old script sent the output to
    /// a file in its temporary directory. Git writes the same file, so a write
    /// that fails there fails in Git as before; the file is then read back.
    pub fn to_file(
        &self,
        args: &[Vec<u8>],
        temp: &Path,
        out: &str,
        errors: Errors,
        input: Option<&Path>,
    ) -> Result<Output> {
        let failed = || Failure::internal("git の呼び出しに失敗した(終了コード 127)");
        let create = |name: &str| {
            std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(temp.join(name))
                .map_err(|_| failed())
        };
        let output = create(out)?;
        let routed = match errors {
            Errors::File(name) => process::Errors::File(create(name)?),
            Errors::Null => process::Errors::Null,
            Errors::Shown => process::Errors::Capture,
        };
        let input = match input {
            Some(path) => Some(std::fs::File::open(path).map_err(|_| failed())?),
            None => None,
        };
        super::signal_checkpoint()?;
        let result = process::capture_files(&mut self.command(args), input, output, routed);
        if let Ok(o) = &result {
            job_status(o);
        }
        super::signal_checkpoint()?;
        let mut o = result.map_err(|_| failed())?;
        o.stdout = std::fs::read(temp.join(out)).map_err(|_| failed())?;
        if let Errors::File(name) = errors {
            o.stderr = std::fs::read(temp.join(name)).map_err(|_| failed())?;
        }
        Ok(o)
    }
    /// `git ARGS >/dev/null 2>&1`: a foreground job, which Bash reports when a
    /// signal ends it (not inside `$(…)`).
    pub fn foreground(&self, args: &[Vec<u8>]) -> Result<Output> {
        super::foreground(&mut self.command(args), b"")?
            .map_err(|_| Failure::internal("git の呼び出しに失敗した(終了コード 127)"))
    }
    /// A Git command that was the first element of an old pipeline.
    pub fn run_piped(&self, args: &[Vec<u8>]) -> Result<Output> {
        let mut command = self.command(args);
        process::pipeline_element(&mut command);
        super::capture(&mut command, b"", None)
            .map_err(|_| Failure::internal("git の呼び出しに失敗した(終了コード 127)"))
    }
    pub fn words(&self, args: &[&str]) -> Result<Output> {
        self.run(&words(args), b"")
    }
    /// `git ARGS >"$TMPD/OUT" 2>"$TMPD/ERR" || rc=$?; lfs_diag_or_fail`.
    pub fn checked_to(
        &self,
        args: &[Vec<u8>],
        temp: &Path,
        out: &str,
        err: &'static str,
    ) -> Result<Vec<u8>> {
        check(self.to_file(args, temp, out, Errors::File(err), None)?)
    }
    pub fn profile(&self, args: &[Vec<u8>]) -> std::result::Result<Vec<u8>, String> {
        let mut cmd = Command::new("git");
        cmd.arg("-C")
            .arg(&self.root)
            .args([
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=",
                "-c",
                "core.hooksPath=/dev/null",
            ])
            .args(args.iter().map(|a| os(a)))
            .env("LC_ALL", "C")
            .env("GIT_NO_LAZY_FETCH", "1")
            .envs(self.env.clone());
        // These Git children belonged to `python3 secret-profiles.py`, whose
        // environment named python3 in `_`.
        // Without python3 (D5) there is no such value: `_` is not passed.
        match process::search(&cmd, std::ffi::OsStr::new("python3")) {
            Some(python) => cmd.env("_", python),
            None => cmd.env_remove("_"),
        };
        let limit = std::time::Duration::from_secs(10);
        #[cfg(test)]
        let result = if PROFILE_DEADLINE_AT_ONCE.load(std::sync::atomic::Ordering::SeqCst) {
            // The typed check: the first look at the clock is already 10 s later.
            process::profile_capture_clocked(&mut cmd, limit, |_| limit)
        } else {
            process::profile_capture(&mut cmd, limit)
        };
        #[cfg(not(test))]
        let result = process::profile_capture(&mut cmd, limit);
        if let Ok(o) = &result {
            // The old helper, in the same process group as its Git, ended by
            // the same signal: Bash reported the helper as its foreground job.
            job_status(o);
        }
        let result = result.map_err(|_| "profile の git 読取が時間内に完了しない".to_string());
        let o = result?;
        if o.code != 0 {
            Err("profile の git 読取に失敗した".into())
        } else {
            Ok(o.stdout)
        }
    }
}
fn job_status(o: &process::Output) {
    super::job_status(o.signal);
}
/// Where the old script sent Git's standard error next to `>$TMPD/OUT`.
#[derive(Clone, Copy)]
pub enum Errors {
    /// `2>"$TMPD/NAME"`.
    File(&'static str),
    /// `2>/dev/null`.
    Null,
    /// Not redirected: shown by the caller.
    Shown,
}
/// The old command that wrote a Git input file into `$TMPD` with Bash's
/// `printf '%s\0' "$p" >>"$TMPD/NAME"` (978・1248).
#[derive(Clone, Copy)]
pub enum Writer {
    Printf(usize),
}
impl Writer {
    pub fn failure(self, error: &std::io::Error) -> Failure {
        let reason = super::tools::strerror(error);
        let Writer::Printf(line) = self;
        let diagnostic = format!("diff-snapshot.sh: line {line}: printf: write error: {reason}\n");
        super::tool_failure(diagnostic.as_bytes(), line)
    }
}
/// `cat IN… >OUT` of coreutils 9.4 (the base): it copies with
/// `copy_file_range`, so a failing write names the input being copied
/// (`cat: IN: File too large`), quoted with `quotef`.
pub fn cat_into(out: &Path, parts: &[(&Path, &[u8])], line: usize) -> Result<()> {
    use std::io::Write;
    let mut file = std::fs::File::create(out)
        .map_err(|_| Failure::internal("git の呼び出しに失敗した(終了コード 127)"))?;
    for (input, data) in parts {
        file.write_all(data).map_err(|e| {
            super::tool_failure(
                &joined(&[
                    b"cat: ",
                    &super::tools::quotef(super::bytes::raw(input.as_os_str())),
                    b": ",
                    super::tools::strerror(&e).as_bytes(),
                    b"\n",
                ]),
                line,
            )
        })?;
    }
    Ok(())
}
/// Test-only: profile Git calls see their 10 s limit reached at once. Nothing
/// in the product sets it (no argument or environment variable).
#[cfg(test)]
pub static PROFILE_DEADLINE_AT_ONCE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
pub fn words(values: &[&str]) -> Vec<Vec<u8>> {
    values.iter().map(|s| s.as_bytes().to_vec()).collect()
}
pub fn check(out: Output) -> Result<Vec<u8>> {
    if out.code == 0 {
        return Ok(out.stdout);
    }
    if out.stderr.windows(7).any(|v| v == b"git-lfs")
        || out.stderr.windows(14).any(|v| v == b"filter-process")
    {
        return Err(Failure::internal(
            "git-lfs を起動できない(filter.lfs.required=true の標準構成では git-lfs が PATH に要る)",
        ));
    }
    Err(Failure::raw(
        20,
        joined(&[
            &out.stderr,
            format!(
                "ERROR [internal] git の呼び出しに失敗した(終了コード {})\n",
                out.code
            )
            .as_bytes(),
        ]),
    ))
}
pub fn working() -> Vec<Vec<u8>> {
    words(&["-c", "core.filemode=true", "-c", "core.symlinks=true"])
}
pub fn lfs_off() -> Vec<Vec<u8>> {
    words(&[
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.required=false",
    ])
}
pub fn at_path(root: &Path, path: &[u8]) -> PathBuf {
    root.join(os(path))
}
