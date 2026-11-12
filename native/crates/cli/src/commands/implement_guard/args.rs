//! 引数の検査(:3055-3094)と使い方の表示(`usage` :327・`arg_error` :350・`need_val` :356-366)。
use super::shell::{Exit, Flow, Shell, Sub, write_piece};
use super::sites::{self, Site};
use super::stdio::Writer;
use dev_workflow_platform::implement_guard as os;
use std::ffi::OsString;
use std::os::unix::ffi::OsStrExt;

/// 使い方の文(旧版の 2 行目から `# --- end usage ---` の前の行まで。バイト列そのもの)。
pub const USAGE: &[u8] = include_bytes!("usage.txt");

/// 引数の名前(旧版の受け皿の順)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Name {
    Cwd,
    TaskMd,
    State,
    ManifestSha256,
    SnapshotSha256,
    RunRc,
}

impl Name {
    pub fn text(self) -> &'static [u8] {
        match self {
            Self::Cwd => b"--cwd",
            Self::TaskMd => b"--task-md",
            Self::State => b"--state",
            Self::ManifestSha256 => b"--manifest-sha256",
            Self::SnapshotSha256 => b"--snapshot-sha256",
            Self::RunRc => b"--run-rc",
        }
    }

    fn from(value: &[u8]) -> Option<Self> {
        [
            Self::Cwd,
            Self::TaskMd,
            Self::State,
            Self::ManifestSha256,
            Self::SnapshotSha256,
            Self::RunRc,
        ]
        .into_iter()
        .find(|name| name.text() == value)
    }
}

/// 検査済みの引数(旧版の変数の値)。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Args {
    pub cwd: Vec<u8>,
    pub task_md: Vec<u8>,
    pub state: Vec<u8>,
    pub manifest_sha256: Vec<u8>,
    pub snapshot_sha256: Vec<u8>,
    pub run_rc: Vec<u8>,
    /// `GIVEN`(渡した順の名前。重複も残る)。
    pub given: Vec<Name>,
}

impl Args {
    /// `GIVEN` の値(` --cwd --state …`)。
    pub fn given_text(&self) -> Vec<u8> {
        let mut out = b" ".to_vec();
        for name in &self.given {
            out.extend_from_slice(name.text());
            out.push(b' ');
        }
        out
    }

    fn slot(&mut self, name: Name) -> &mut Vec<u8> {
        match name {
            Name::Cwd => &mut self.cwd,
            Name::TaskMd => &mut self.task_md,
            Name::State => &mut self.state,
            Name::ManifestSha256 => &mut self.manifest_sha256,
            Name::SnapshotSha256 => &mut self.snapshot_sha256,
            Name::RunRc => &mut self.run_rc,
        }
    }

    fn value(&self, name: Name) -> &[u8] {
        match name {
            Name::Cwd => &self.cwd,
            Name::TaskMd => &self.task_md,
            Name::State => &self.state,
            Name::ManifestSha256 => &self.manifest_sha256,
            Name::SnapshotSha256 => &self.snapshot_sha256,
            Name::RunRc => &self.run_rc,
        }
    }
}

/// サブコマンドで使える名前(`ALLOWED` の順)。
pub fn allowed(sub: Sub) -> &'static [Name] {
    use Name::*;
    match sub {
        Sub::Take => &[Cwd, TaskMd],
        Sub::ConfigCheck | Sub::TaskmdDiff | Sub::RestoreTaskmd => {
            &[Cwd, State, ManifestSha256, SnapshotSha256]
        }
        Sub::Compare => &[Cwd, State, ManifestSha256, SnapshotSha256, RunRc],
        Sub::Cleanup => &[State],
    }
}

/// `ALLOWED` の値。
pub fn allowed_text(sub: Sub) -> Vec<u8> {
    let mut out = b" ".to_vec();
    for name in allowed(sub) {
        out.extend_from_slice(name.text());
        out.push(b' ');
    }
    out
}

/// 引数の解析(:3055-3094)。argv はプログラム名を除いたもの。
pub fn parse(shell: &mut Shell, argv: &[OsString]) -> Flow<(Sub, Args)> {
    let mut rest = argv.iter().map(|a| a.as_bytes());
    let Some(first) = rest.next() else {
        return Err(arg_error(
            shell,
            "サブコマンドが必要です(take / config-check / compare / taskmd-diff / restore-taskmd / cleanup)".as_bytes(),
        ));
    };
    let sub = match first {
        b"take" => Sub::Take,
        b"config-check" => Sub::ConfigCheck,
        b"compare" => Sub::Compare,
        b"taskmd-diff" => Sub::TaskmdDiff,
        b"restore-taskmd" => Sub::RestoreTaskmd,
        b"cleanup" => Sub::Cleanup,
        b"-h" | b"--help" => return Err(help(shell, sites::HELP_SUBCOMMAND)),
        other => {
            let mut text = "不明なサブコマンド: ".as_bytes().to_vec();
            text.extend_from_slice(other);
            return Err(arg_error(shell, &text));
        }
    };
    shell.sub = Some(sub);
    let mut args = Args::default();
    let rest: Vec<&[u8]> = rest.collect();
    let mut index = 0usize;
    while let Some(&word) = rest.get(index) {
        if let Some(name) = Name::from(word) {
            let Some(&value) = rest.get(index + 1) else {
                return Err(need_val(shell, name));
            };
            *args.slot(name) = value.to_vec();
            args.given.push(name);
            index += 2;
            continue;
        }
        if word == b"-h" || word == b"--help" {
            return Err(help(shell, sites::HELP_ARGUMENT));
        }
        let mut text = "不明な引数: ".as_bytes().to_vec();
        text.extend_from_slice(word);
        return Err(arg_error(shell, &text));
    }
    // 1. 渡した順に、使えない名前。
    for name in &args.given {
        if !allowed(sub).contains(name) {
            let mut text = sub.name().to_vec();
            text.extend_from_slice(" では ".as_bytes());
            text.extend_from_slice(name.text());
            text.extend_from_slice(" を指定できない".as_bytes());
            return Err(arg_error(shell, &text));
        }
    }
    // 2. 表の順に、必要な名前。
    for name in allowed(sub) {
        if !args.given.contains(name) {
            let mut text = sub.name().to_vec();
            text.extend_from_slice(" には ".as_bytes());
            text.extend_from_slice(name.text());
            text.extend_from_slice(" が必要です".as_bytes());
            return Err(arg_error(shell, &text));
        }
    }
    // 3. 渡した値が空。
    for name in [
        Name::Cwd,
        Name::TaskMd,
        Name::State,
        Name::ManifestSha256,
        Name::SnapshotSha256,
    ] {
        if args.given.contains(&name) && args.value(name).is_empty() {
            let mut text = name.text().to_vec();
            text.extend_from_slice(" が空です".as_bytes());
            return Err(arg_error(shell, &text));
        }
    }
    // 4. --run-rc の形(compare だけ)。
    if sub == Sub::Compare
        && (args.run_rc.is_empty() || !args.run_rc.iter().all(u8::is_ascii_digit))
    {
        let mut text = "--run-rc は 0 以上の十進整数: '".as_bytes().to_vec();
        text.extend_from_slice(&args.run_rc);
        text.push(b'\'');
        return Err(arg_error(shell, &text));
    }
    Ok((sub, args))
}

/// `arg_error` :350: `echo "ERROR [usage] $*" >&2; usage; trap - ERR; exit 2`。
pub fn arg_error(shell: &mut Shell, text: &[u8]) -> Exit {
    usage_error(shell, sites::ARG_ERROR_ECHO, sites::USAGE, text)
}

/// `need_val` :356-366。
fn need_val(shell: &mut Shell, name: Name) -> Exit {
    let mut text = name.text().to_vec();
    text.extend_from_slice(" に値が必要です".as_bytes());
    usage_error(shell, sites::NEED_VAL_ECHO, sites::USAGE, &text)
}

fn usage_error(shell: &mut Shell, echo: Site, usage_site: Site, text: &[u8]) -> Exit {
    let mut line = b"ERROR [usage] ".to_vec();
    line.extend_from_slice(text);
    if let Err(exit) = shell.echo_err(echo, &line) {
        return exit;
    }
    if let Err(exit) = usage(shell, usage_site) {
        return exit;
    }
    shell.finish(2)
}

/// `-h`・`--help`: `usage; trap - ERR; exit 0`。
fn help(shell: &mut Shell, _site: Site) -> Exit {
    if let Err(exit) = usage(shell, sites::USAGE) {
        return exit;
    }
    shell.finish(0)
}

/// `usage` :327: `sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2`。2 本目の `sed` の書き方
/// (GNU sed 4.9 は stdout を溜めて書く。溜め場所は glibc と同じく `st_blksize` と 8192 の小さい方)を
/// 再現する。`sed` は bash の罠の信号を受けないので、`EINTR` は書き直す。失敗の終了値は、`sed` の
/// `couldn't write` の 4(引き継いだ `SIGPIPE`・`SIGXFSZ` が既定なら、書き込みで信号を受けて終わる)。
pub fn usage(shell: &mut Shell, site: Site) -> Flow<()> {
    let status = write_usage(shell);
    shell.after(site, status == 0, status)?;
    Ok(())
}

fn write_usage(shell: &mut Shell) -> i32 {
    let capacity = match shell.io.blksize(2) {
        Some(size) if size > 0 && size < 8192 => size as usize,
        _ => 8192,
    };
    // sed は別のプロセスなので、bash の errno は変えない。
    let mut errno = 0;
    let mut writer = Writer {
        io: shell.io.as_mut(),
        errno: &mut errno,
    };
    // sed は行と改行を 1 回ずつ fwrite する。満杯になった所で溜め場所の全部を書く。
    let mut buffered = 0usize;
    let mut written = 0usize;
    let mut failed_items = None;
    for line in USAGE.split_inclusive(|b| *b == b'\n') {
        let body = line.strip_suffix(b"\n").unwrap_or(line);
        for item in [body, line.get(body.len()..).unwrap_or_default()] {
            if item.is_empty() {
                continue;
            }
            let mut left = item.len();
            while buffered + left > capacity {
                left -= capacity - buffered;
                buffered = capacity;
                let chunk = USAGE.get(written..written + buffered).unwrap_or_default();
                if !write_retrying(&mut writer, chunk) {
                    failed_items = Some(item.len());
                    break;
                }
                written += buffered;
                buffered = 0;
            }
            if failed_items.is_some() {
                break;
            }
            buffered += left;
        }
        if failed_items.is_some() {
            break;
        }
    }
    if failed_items.is_none() && buffered > 0 {
        // 終わる時の書き出し(ck_fclose)。失敗は `couldn't flush`(件数の無い形)だが、終了値は同じ 4。
        let chunk = USAGE.get(written..written + buffered).unwrap_or_default();
        if !write_retrying(&mut writer, chunk) {
            failed_items = Some(0);
        }
    }
    let Some(items) = failed_items else {
        return 0;
    };
    let errno = *writer.errno;
    // sed は bash の起動時の扱いを受け継ぐ。既定なら書き込みで受けた信号で終わる。
    if let Some(signal) = shell.child_died() {
        return 128 + signal;
    }
    // sed の誤りの文(stderr は同じ fd なので、ふつうは書けない)。
    let mut message = if items == 0 {
        b"sed: couldn't flush stdout: ".to_vec()
    } else {
        format!(
            "sed: couldn't write {items} item{} to stdout: ",
            if items == 1 { "" } else { "s" }
        )
        .into_bytes()
    };
    message.extend_from_slice(super::strerror(errno).as_bytes());
    message.push(b'\n');
    let mut ignored = 0;
    let mut quiet = Writer {
        io: shell.io.as_mut(),
        errno: &mut ignored,
    };
    let _ = write_piece(&mut quiet, 2, &message);
    let _ = shell.child_died();
    4
}

/// `sed` の書き込み(`EINTR` は書き直す。sed は bash の信号を受けない)。
fn write_retrying(writer: &mut Writer<'_>, data: &[u8]) -> bool {
    let mut done = 0usize;
    while done < data.len() {
        match writer.io.write(2, data.get(done..).unwrap_or_default()) {
            Ok(n) => done += n,
            Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => continue,
            Err(error) => {
                *writer.errno = error.raw_os_error().unwrap_or(os::errno::EIO);
                return false;
            }
        }
    }
    true
}
