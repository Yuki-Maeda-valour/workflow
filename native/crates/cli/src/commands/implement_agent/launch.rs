//! 道具の起動(決定の表の (1)(2) の場合だけ)。bash と同じ PATH の探し方で探し、bash が子へ渡す
//! 環境で起動し、bash と同じ規則で待つ(待つ間に受けた信号・ジョブの知らせの行・コマンド置換の
//! 読み取り)。exec の前に、受け継いだ信号の扱いと止めへ戻す(platform の `spawn`)。
use super::Context;
use super::childenv;
use super::shell::{Flow, Shell, Wait};
use dev_workflow_platform::implement_agent as os;
use std::os::fd::AsFd;

/// 起動する命令。
pub struct Command<'a> {
    /// 探す名前(`argv[0]` も同じ)。
    pub name: &'a [u8],
    pub argv: &'a [Vec<u8>],
    /// 旧版の行(`LINENO`)。
    pub line: u32,
    /// 一時的な代入。
    pub temporary: &'a [(&'a str, &'a [u8])],
    pub stdout: os::ChildOut,
    pub stderr: os::ChildErr,
}

/// 起動の結果。
pub enum Started {
    /// PATH に無い(bash は子で `command not found` を出して 127。stderr の向き先へ出る)。
    NotFound,
    /// 起動した。
    Child(os::Child),
    /// fork が失敗した。
    Failed,
}

/// 子の終わり方。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Status {
    pub pid: i32,
    pub status: os::ChildStatus,
    /// コマンド置換の出力(末尾の改行を消す前)。
    pub stdout: Vec<u8>,
}

impl Status {
    /// bash の `$?`(信号なら 128 + 番号)。
    pub fn code(&self) -> Option<i32> {
        Some(match self.status {
            os::ChildStatus::Exited(code) => code,
            os::ChildStatus::Signaled(signal, _) => 128 + signal,
        })
    }

    pub fn signal(&self) -> Option<i32> {
        match self.status {
            os::ChildStatus::Signaled(signal, _) => Some(signal),
            os::ChildStatus::Exited(_) => None,
        }
    }
}

/// 待つ地点の種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Foreground,
    Comsub,
}

impl Kind {
    fn wait(self) -> Wait {
        match self {
            Self::Foreground => Wait::Foreground,
            Self::Comsub => Wait::Comsub,
        }
    }
}

fn epoch() -> (u64, u32) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    (now.as_secs(), now.subsec_micros())
}

/// 子を起動する(bash の `search_for_command` と `make_child`)。
pub fn start(_shell: &mut Shell, context: &mut Context, command: &Command<'_>) -> Started {
    let path_list = context.path_value();
    let home = childenv::lookup(&context.imported, "HOME").map(<[u8]>::to_vec);
    let Some(found) = super::paths::search_for_command(command.name, &path_list, home.as_deref())
    else {
        return Started::NotFound;
    };
    let script = context.script_values();
    let env = childenv::child_environment(
        &context.startup,
        &script,
        command.line,
        &found,
        command.temporary,
        epoch(),
    );
    match os::spawn(&os::Spawn {
        path: &found,
        argv: command.argv,
        env: &env,
        stdout: command.stdout,
        stderr: command.stderr,
    }) {
        Ok(child) => Started::Child(child),
        Err(_) => Started::Failed,
    }
}

/// 子を待つ。bash が子を待たずに終わる信号を受けたら、その信号で終わる(子はそのまま残る)。
pub fn wait(shell: &mut Shell, started: Started, kind: Kind) -> Flow<Status> {
    let child = match started {
        Started::NotFound => {
            return Ok(Status {
                pid: 0,
                status: os::ChildStatus::Exited(127),
                stdout: Vec::new(),
            });
        }
        Started::Failed => {
            return Ok(Status {
                pid: 0,
                status: os::ChildStatus::Exited(126),
                stdout: Vec::new(),
            });
        }
        Started::Child(child) => child,
    };
    let mut stdout = Vec::new();
    if let Some(read) = child.stdout {
        let mut buffer = [0u8; 4096];
        loop {
            match os::read_once(read.as_fd(), &mut buffer) {
                Ok(0) => break,
                Ok(n) => stdout.extend_from_slice(buffer.get(..n).unwrap_or_default()),
                Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => {
                    if let Some(signal) = shell.dies_now_signal(kind.wait()) {
                        return Err(shell.die_by(signal));
                    }
                }
                Err(_) => break,
            }
        }
        os::close(read);
    }
    loop {
        match os::wait_once(child.pid) {
            Ok(status) => {
                return Ok(Status {
                    pid: child.pid,
                    status,
                    stdout,
                });
            }
            Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => {
                if let Some(signal) = shell.dies_now_signal(kind.wait()) {
                    return Err(shell.die_by(signal));
                }
            }
            Err(_) => {
                return Ok(Status {
                    pid: child.pid,
                    status: os::ChildStatus::Exited(127),
                    stdout,
                });
            }
        }
    }
}

/// glibc 2.39 の `strsignal`(C の地域設定)。
pub fn strsignal(signal: i32) -> String {
    let text = match signal {
        1 => "Hangup",
        2 => "Interrupt",
        3 => "Quit",
        4 => "Illegal instruction",
        5 => "Trace/breakpoint trap",
        6 => "Aborted",
        7 => "Bus error",
        8 => "Floating point exception",
        9 => "Killed",
        10 => "User defined signal 1",
        11 => "Segmentation fault",
        12 => "User defined signal 2",
        13 => "Broken pipe",
        14 => "Alarm clock",
        15 => "Terminated",
        16 => "Stack fault",
        17 => "Child exited",
        18 => "Continued",
        19 => "Stopped (signal)",
        20 => "Stopped",
        21 => "Stopped (tty input)",
        22 => "Stopped (tty output)",
        23 => "Urgent I/O condition",
        24 => "CPU time limit exceeded",
        25 => "File size limit exceeded",
        26 => "Virtual timer expired",
        27 => "Profiling timer expired",
        28 => "Window changed",
        29 => "I/O possible",
        30 => "Power failure",
        31 => "Bad system call",
        other => return format!("Unknown signal {other}"),
    };
    text.to_string()
}

/// 前面の子が信号で死んだときの bash の知らせの行(`notify_of_job_status` と `print_pipeline`)。
/// SIGINT・SIGPIPE は出さない。SIGTERM は `Terminated` の 1 行(基準の容器で測った)。
pub fn notify(shell: &mut Shell, line: u32, status: &Status, command: &[u8]) {
    let os::ChildStatus::Signaled(signal, core) = status.status else {
        return;
    };
    if signal == os::SIGINT || signal == os::SIGPIPE {
        return;
    }
    if signal == os::SIGTERM {
        shell.raw_stderr_line(b"Terminated");
        return;
    }
    let description = strsignal(signal);
    let mut text = format!("{:5} {description}", status.pid).into_bytes();
    // LONGEST_SIGNAL_DESC(24)に満たない分を空白で詰める(負なら左に寄せた 24 桁)。
    let pad = 24i64 - description.len() as i64;
    let pad = if pad < 0 {
        (-pad) as usize
    } else {
        pad as usize
    };
    text.extend(std::iter::repeat_n(b' ', pad));
    if core {
        text.extend_from_slice(b"(core dumped) ");
    }
    text.extend_from_slice(command);
    shell.builtin_error(line, b"", &text);
}
