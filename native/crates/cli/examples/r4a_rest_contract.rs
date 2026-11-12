#![forbid(unsafe_code)]
//! R4A残り5本の開発専用入口。製品コマンド表へは登録しない。
//!
//! 入出力は platform の `r4a_rest`(実fd・端末幅・スレッド値・SIGINT・permlog)と C0a の `CommandIo` を使う。
//! 起動時に閉じた標準fd(0〜2)は、Rust標準の実行時の初期化(`/dev/null` で開き直す)をそのまま使う。
#![allow(dead_code, clippy::duplicate_mod)]

#[path = "../src/commands/candidate_keys/mod.rs"]
pub mod candidate_keys;
#[path = "../src/commands/host_argv/mod.rs"]
pub mod host_argv;
#[path = "../src/commands/loop_child_output/mod.rs"]
pub mod loop_child_output;
#[path = "../src/commands/loop_task_text/mod.rs"]
pub mod loop_task_text;
#[path = "../src/commands/r4a_rest_support/mod.rs"]
pub mod r4a_rest_support;
#[path = "../src/commands/resolve_task_store/mod.rs"]
pub mod resolve_task_store;

#[path = "../src/commands/resolve_task_dir/mod.rs"]
pub mod resolve_task_dir_definition;
#[path = "../src/commands/task_digest/mod.rs"]
pub mod task_digest_definition;

// 製品の `commands` の形を、専用の入口の中に作る(登録表には載せない)。既存の定義を、そのまま取り込む。
mod commands {
    pub use super::r4a_rest_support;

    pub use super::resolve_task_dir_definition as resolve_task_dir;
    pub use super::task_digest_definition as task_digest;

    pub mod r4a_platform {
        pub use dev_workflow_platform::r4a_rest::{
            PermlogError, PermlogHooks, read_file, read_permlog, take_sigint,
        };
    }
}

use dev_workflow_core::{CommandOutput, ExitCode};
use dev_workflow_platform::r4a_rest as platform;
use platform::StandardFd;
use r4a_rest_support::args::Env;
use r4a_rest_support::chars::{self, PyStr};
use r4a_rest_support::runtime::{Buffering, Sink, TextOutput};
use r4a_rest_support::stream::{Errors, Launch, Program, PyStream, Source};
use std::io::{self, Read, Write};
use std::os::unix::ffi::OsStrExt;

/// platform の `write_once`(生の write 1 回。EAGAIN は `None`)。
struct PlatformSink {
    fd: StandardFd,
}

impl Sink for PlatformSink {
    fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
        platform::write_once(self.fd, bytes)
            .map_err(|errno| io::Error::from_raw_os_error(errno.raw_os_error()))
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn interrupted(&mut self) -> bool {
        platform::take_sigint()
    }
}

/// platform の `read_once`(標準入力からの生の read 1 回)。SIGINT での中断は `Interrupted`。
struct PlatformStdin;

impl Read for PlatformStdin {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        match platform::read_once(buffer) {
            Ok(Some(count)) => Ok(count),
            Ok(None) => Err(io::Error::from(io::ErrorKind::WouldBlock)),
            // EINTR(4)は SIGINT による中断(`install_sigint_handler` の後で、届いていれば read_once が返す)。
            Err(errno) if errno.raw_os_error() == 4 => {
                Err(io::Error::from(io::ErrorKind::Interrupted))
            }
            Err(errno) => Err(io::Error::from_raw_os_error(errno.raw_os_error())),
        }
    }
}

/// C0a の `CommandIo::read_input`(先読みなしの 1 回の読取り)を、`Program` の標準入力の読み手にする。
struct IoSource<'a, 'b> {
    io: &'a mut dev_workflow_core::CommandIo<'b>,
}

impl Source for IoSource<'_, '_> {
    fn read_once(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
        match self.io.read_input(buffer) {
            Ok(count) => Ok(Some(count)),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => Ok(None),
            Err(error) => Err(error),
        }
    }
}

/// permlog の競合・close の失敗を、決まった出来事で差し込む検査専用の `PermlogHooks`
/// (旧版側は `fault_shim.c` の `R4A_RACE`・`R4A_CLOSE_FAIL`)。
struct RaceHooks {
    race: Option<Race>,
}

impl platform::PermlogHooks for RaceHooks {
    fn before_parent_open(&mut self, name: &[u8]) {
        if let Some(race) = self.race.as_mut() {
            race.run_if(&format!(
                "before-parent-open:{}",
                String::from_utf8_lossy(name)
            ));
        }
    }

    fn before_leaf_open(&mut self) {
        if let Some(race) = self.race.as_mut() {
            race.run_if("before-leaf-open");
        }
    }

    fn before_read(&mut self, n: u32) {
        if n == 1
            && let Some(race) = self.race.as_mut()
        {
            race.run_if("before-first-read");
        }
    }

    fn after_read(&mut self, n: u32) {
        if n == 1
            && let Some(race) = self.race.as_mut()
        {
            race.run_if("after-first-read");
        }
    }

    fn close_error(&mut self) -> Option<i32> {
        std::env::var("R4A_CLOSE_FAIL")
            .ok()
            .and_then(|v| v.parse().ok())
    }
}

fn stream(fd: StandardFd, mode: Buffering, standard_error: bool) -> Option<PyStream> {
    // 起動時に閉じていた fd も、実行時が `/dev/null` で開き直しているので、取得に失敗しない(失敗したら None の stream)。
    let info = platform::standard_fd_info(fd).ok()?;
    let capacity = if info.blksize > 1 {
        info.blksize as usize
    } else {
        8192
    };
    let sink: Box<dyn Sink> =
        match FaultSink::from_env(if standard_error { 2 } else { 1 }, PlatformSink { fd }) {
            Ok(faulty) => Box::new(faulty),
            Err(plain) => Box::new(plain),
        };
    let out = TextOutput::with_capacity(sink, mode, capacity, platform::again_errno())
        .line_buffered(standard_error || info.is_tty);
    Some(PyStream {
        out,
        errors: if standard_error {
            Errors::BackslashReplace
        } else {
            Errors::SurrogateEscape
        },
        repr: if standard_error {
            "<_io.TextIOWrapper name='<stderr>' mode='w' encoding='utf-8'>"
        } else {
            "<_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>"
        },
    })
}

fn build_program<'a>(launch: Launch) -> Program<'a> {
    let unbuffered =
        r4a_rest_support::unbuffered_from(std::env::var_os("PYTHONUNBUFFERED").as_deref());
    let mode = if unbuffered {
        Buffering::Unbuffered
    } else {
        Buffering::Buffered
    };
    Program::new(
        stream(StandardFd::Stdout, mode, false),
        stream(StandardFd::Stderr, mode, true),
        launch,
    )
}

fn startup_fds() -> r4a_rest_support::startup::StartupFds {
    use platform::FdKind;
    use r4a_rest_support::startup::StreamKind;
    let kind = |fd| match platform::standard_fd_kind(fd) {
        Ok(FdKind::Directory) => StreamKind::Directory,
        _ => StreamKind::Other,
    };
    r4a_rest_support::startup::StartupFds {
        stdin: kind(StandardFd::Stdin),
        stdout: kind(StandardFd::Stdout),
        stderr: kind(StandardFd::Stderr),
    }
}

fn args_env() -> Env {
    let to_py = |name: &str| std::env::var_os(name).map(|v| chars::from_os_bytes(v.as_bytes()));
    Env {
        columns: to_py("COLUMNS"),
        lines: to_py("LINES"),
        terminal: platform::terminal_size(),
        int_max_digits: r4a_rest_support::int_max_digits(
            std::env::var_os("PYTHONINTMAXSTRDIGITS").as_deref(),
        ),
        optimize: r4a_rest_support::optimize_level(std::env::var_os("PYTHONOPTIMIZE").as_deref()),
    }
}

/// 本体を C0a の `execute_to`(入力は platform の生の read・出力は専用処理が直接書くので空)で動かし、終了値を返す。
fn run_helper(
    launch: Launch,
    body: impl FnOnce(&mut Program<'_>) -> Result<i32, r4a_rest_support::stream::Raised>,
) -> u8 {
    let interrupted = std::cell::Cell::new(false);
    let code =
        dev_workflow_core::execute_to(&mut PlatformStdin, &mut io::sink(), &mut io::sink(), |io| {
            let mut program = build_program(launch).with_stdin(Some(Box::new(IoSource { io })));
            // 本体の終わりの切れ目でも、届いている SIGINT を KeyboardInterrupt にする(旧 Python の次のバイトコードの確認)。
            let outcome = match body(&mut program) {
                Ok(code) => match r4a_rest_support::stream::interrupt(&[]) {
                    Some(exception) => Err(r4a_rest_support::stream::Raised::Exception(exception)),
                    None => Ok(code),
                },
                other => other,
            };
            let (code, signal) = program.finish_with_signal(outcome);
            interrupted.set(signal);
            Ok(CommandOutput {
                stdout: Vec::new(),
                stderr: Vec::new(),
                code: ExitCode::new(code),
            })
        });
    if interrupted.get() {
        platform::exit_by_sigint();
    }
    code.as_u8()
}

/// 検査専用の故障の差込み(製品の入口には入れない)。旧版側は `fault_shim.c` が同じ規則を
/// `write(2)` に当てる。`R4A_FAULT="<fd>:<mode>:<n>"`(`budget`・`cut-first`・`short-fail`)。
struct FaultSink {
    inner: PlatformSink,
    mode: String,
    limit: usize,
    accepted: usize,
    sequence: usize,
    failing: bool,
}

impl FaultSink {
    fn from_env(fd: u32, inner: PlatformSink) -> Result<Self, PlatformSink> {
        let Ok(spec) = std::env::var("R4A_FAULT") else {
            return Err(inner);
        };
        let mut parts = spec.splitn(3, ':');
        let (Some(target), Some(mode), Some(limit)) = (parts.next(), parts.next(), parts.next())
        else {
            return Err(inner);
        };
        match (target.parse::<u32>(), limit.parse::<usize>()) {
            (Ok(target), Ok(limit)) if target == fd => Ok(Self {
                inner,
                mode: mode.to_owned(),
                limit,
                accepted: 0,
                sequence: 0,
                failing: false,
            }),
            _ => Err(inner),
        }
    }
}

impl Sink for FaultSink {
    fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
        self.sequence += 1;
        let full = io::Error::from_raw_os_error(28);
        match self.mode.as_str() {
            "budget" => {
                let remaining = self.limit.saturating_sub(self.accepted);
                if remaining == 0 {
                    return Err(full);
                }
                let result = self
                    .inner
                    .write_once(&bytes[..bytes.len().min(remaining)])?;
                self.accepted += result.unwrap_or(0);
                Ok(result)
            }
            "fail-nth" | "fail-first-and-nth" => {
                // n 番目(`fail-first-and-nth` は 1 番目と n 番目)の書込みだけが失敗する(再現の回数を固定する検査専用)。
                if self.sequence == self.limit
                    || (self.sequence == 1 && self.mode == "fail-first-and-nth")
                {
                    return Err(io::Error::from_raw_os_error(28));
                }
                self.inner.write_once(bytes)
            }
            "fail-first" | "fail-second" => {
                let which = if self.mode == "fail-first" { 1 } else { 2 };
                if self.sequence == which {
                    let errno = if self.limit == 0 {
                        28
                    } else {
                        self.limit as i32
                    };
                    return Err(io::Error::from_raw_os_error(errno));
                }
                self.inner.write_once(bytes)
            }
            "cut-first" => {
                let count = if self.sequence == 1 && bytes.len() > self.limit {
                    self.limit
                } else {
                    bytes.len()
                };
                self.inner.write_once(&bytes[..count])
            }
            "short-fail" => {
                if self.failing {
                    return Err(full);
                }
                if self.sequence == 1 && bytes.len() > self.limit {
                    self.failing = true;
                    return self.inner.write_once(&bytes[..self.limit]);
                }
                self.inner.write_once(bytes)
            }
            _ => self.inner.write_once(bytes),
        }
    }

    fn interrupted(&mut self) -> bool {
        self.inner.interrupted()
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// 検査専用の競合の差込み(旧版側は `fault_shim.c` の `R4A_RACE`)。
struct Race {
    event: String,
    action: String,
    path: String,
    arg: String,
    done: bool,
}

impl Race {
    fn from_env() -> Option<Self> {
        let spec = std::env::var("R4A_RACE").ok()?;
        let fields: Vec<&str> = spec.split(':').collect();
        let shift = usize::from(fields.first() == Some(&"before-parent-open"));
        if fields.len() < 3 + shift {
            return None;
        }
        let event = if shift == 1 {
            format!("{}:{}", fields[0], fields[1])
        } else {
            fields[0].to_owned()
        };
        Some(Self {
            event,
            action: fields[1 + shift].to_owned(),
            path: fields[2 + shift].to_owned(),
            arg: fields.get(3 + shift).copied().unwrap_or("").to_owned(),
            done: false,
        })
    }

    fn run_if(&mut self, event: &str) {
        if self.done || self.event != event {
            return;
        }
        self.done = true;
        use std::io::Write;
        match self.action.as_str() {
            "truncate" => {
                if let Ok(file) = std::fs::OpenOptions::new().write(true).open(&self.path) {
                    let _ = file.set_len(self.arg.parse().unwrap_or(0));
                }
            }
            "append" => {
                if let Ok(mut file) = std::fs::OpenOptions::new().append(true).open(&self.path) {
                    let _ = file.write_all(self.arg.as_bytes());
                }
            }
            "rewrite" => {
                if let Ok(mut file) = std::fs::OpenOptions::new().write(true).open(&self.path) {
                    let _ = file.write_all(self.arg.as_bytes());
                    let _ = file.set_modified(
                        std::time::SystemTime::now() + std::time::Duration::from_secs(5),
                    );
                }
            }
            "swap" => {
                let _ = std::fs::rename(&self.path, format!("{}.orig", self.path));
                let _ = std::os::unix::fs::symlink(&self.arg, &self.path);
            }
            _ => {}
        }
    }
}

fn main() {
    let mut args = std::env::args_os().skip(1);
    let Some(command) = args.next() else {
        eprintln!("r4a_rest_contract: command is required");
        std::process::exit(2);
    };
    let argv: Vec<PyStr> = args.map(|a| chars::from_os_bytes(a.as_bytes())).collect();
    // 起動前の拒否の順: PYTHONHASHSEED(Current thread の行なし)→ PYTHONINTMAXSTRDIGITS → 標準の fd。
    let int_error =
        r4a_rest_support::hash_seed_startup_error(std::env::var_os("PYTHONHASHSEED").as_deref())
            .or_else(|| {
                r4a_rest_support::int_limit_startup_error(
                    std::env::var_os("PYTHONINTMAXSTRDIGITS").as_deref(),
                )
            });
    if let Some(diagnostic) =
        r4a_rest_support::startup::decide(int_error, startup_fds(), platform::pthread_identity())
    {
        let _ = io::stderr().write_all(&diagnostic);
        std::process::exit(1);
    }
    platform::install_sigint_handler();
    let env = args_env();
    let code = match command.to_str() {
        Some("resolve-task-store") => run_helper(Launch::FileScript, |program| {
            resolve_task_store::main(program, &env, &argv)
        }),
        Some("host-argv") => run_helper(Launch::FileScript, |program| {
            host_argv::main(program, &argv)
        }),
        Some("candidate-keys") => run_helper(Launch::FileScript, |program| {
            candidate_keys::main(program, &env, &argv)
        }),
        Some("loop-task-text") => run_helper(Launch::HeldCode, |program| {
            loop_task_text::main(program, &argv)
        }),
        Some("loop-child-output") => run_helper(Launch::HeldCode, |program| {
            let mut hooks = RaceHooks {
                race: Race::from_env(),
            };
            loop_child_output::main(program, &argv, env.int_max_digits, &mut hooks)
        }),

        // 検査専用: errno 0〜299 と -1 の `strerror` の文(Python の `os.strerror` との照合に使う)。
        Some("strerror") => {
            let mut out = String::new();
            for number in (-1..300).chain([9999, 100_000]) {
                out.push_str(&format!(
                    "{number}\t{}\n",
                    r4a_rest_support::exception::strerror(number)
                ));
            }
            let _ = io::stdout().write_all(out.as_bytes());
            0
        }
        // 検査専用: 全文字番号の文字の性質(bit0=isprintable・bit1=isspace・上位=10 進数字の値+1)を 1 バイトずつ出す。
        Some("chartable") => {
            let mut out = Vec::with_capacity(0x110000);
            for code in 0..0x110000_u32 {
                let digit = chars::decimal_value(code).map_or(0, |d| d + 1) as u8;
                out.push(
                    u8::from(chars::is_printable(code))
                        | (u8::from(chars::is_space(code)) << 1)
                        | (digit << 2),
                );
            }
            let _ = io::stdout().write_all(&out);
            0
        }
        // 検査専用: 標準入力の各行(厳密 UTF-8)を NFC にして出す。Python の結果との比較に使う。
        Some("nfc") => {
            use std::io::Read;
            let mut data = Vec::new();
            let _ = io::stdin().read_to_end(&mut data);
            let mut out = Vec::new();
            for line in data.split(|b| *b == b'\n') {
                let text = chars::from_os_bytes(line);
                out.extend(
                    chars::to_os_bytes(&r4a_rest_support::nfc::nfc(&text)).unwrap_or_default(),
                );
                out.push(b'\n');
            }
            let _ = io::stdout().write_all(&out);
            0
        }
        _ => {
            eprintln!("r4a_rest_contract: unknown command");
            std::process::exit(2);
        }
    };
    std::process::exit(code.into());
}
