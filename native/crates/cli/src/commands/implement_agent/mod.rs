//! R4X-1: `implement-agent.sh`(以下「旧版」。sha256 `9486ca07…`)の 1〜714 行(引数・使い方・値の検査・
//! `--log-file` の語彙の正規化・既定表・指標・保護領域の候補の解決の始め)と、候補が 1 つも残らないときの
//! 725 行、この区間で届いた TERM・HUP・INT の扱いの内部部品。製品のコマンド登録には接続しない
//! (検査専用入口 `implement_agent_contract` だけが呼ぶ)。最初の候補の `mkdir -p`(715 行)の手前で、
//! 境界の印(stderr の `ERROR [r4x-1-boundary] …` と終了値 99)で止める。
//!
//! # 旧版の行との対応
//!
//! | 旧版(行) | Rust |
//! |---|---|
//! | bash の起動(`PWD`・`OLDPWD`・起動時の警告) | `startup` |
//! | 使い方 2〜98・`usage` 308 | `args::USAGE`(`usage.txt`)・`args::usage` |
//! | `set +o posix`・`set -eEuo pipefail`・`unset CDPATH` 103〜107 | `childenv` の表(子へ渡さない名前) |
//! | 変数の初期化 108〜135 | `args::Args`・`args::Args::script_values` |
//! | `exec 9>&1 8>&2` 136 | (Rust は fd 2 へ書く。同じ書き先) |
//! | `LOG_SWAP_PY` 146〜147 | 再現しない(14:24 の決定。受け継いだ値を子へ渡す) |
//! | `cleanup`・`trap cleanup EXIT` 149〜152 | `Shell::run_exit_trap`・`Shell::set_exit_trap` |
//! | `log_line`〜`log_model_info` 154〜267 | `shell::LogState`(この区間ではログを開かない) |
//! | `die` 269〜278 | `Shell::die` |
//! | `on_err`・ERR の罠 282〜305 | `Shell::on_err`・`Shell::fail` |
//! | `need_val`・引数 310〜332 | `args::parse` |
//! | `fail_usage` 334 | `Shell::fail_usage` |
//! | 値の検査 336〜365 | `args::check` |
//! | `grep` 352〜354 | `args`(`-` で始まるパスだけ `launch` で起動) |
//! | `lex_norm`・`--log-file` 372〜398 | `state::lex_norm`・`state::log_file`(`dirname`・`basename` は `tools`) |
//! | 既定表 404〜455 | `state::TABLE`・`state::default_table`・`state::check_shape` |
//! | `host_cli_candidates` 465〜470 | `host_cli_candidates` |
//! | timeout の確かめ 472〜477 | 行わない(決定。この区間では結果を使わない) |
//! | `on_signal`・罠 496〜519 | `Shell::on_signal`・`Shell::set_signal_traps` |
//! | `initialize_shell_signals`(SIGQUIT を無視・SIGCHLD を回収) | `main_entry` |
//! | `real_dir`・候補 693〜714・725 | `paths::real_dir`・`state::candidates` |
//!
//! 390・391 行の道具の再現(`tools`)は、基準の容器で本物の道具を測った固定期待
//! (`native/ci/implement_agent_fixtures/tool_outputs.json`)と単体テストで照らす。
pub mod args;
pub mod childenv;
pub mod launch;
pub mod locale;
pub mod paths;
pub mod shell;
pub mod sites;
pub mod state;
pub mod stdio;
#[cfg(test)]
mod tests;
pub mod tools;

use args::Args;
use childenv::{ScriptValues, Startup};
use dev_workflow_platform::implement_agent as os;
use shell::{Exit, Flow, Point, Shell, StopHook};
use std::ffi::OsString;

/// errno の文(R4R の `errno_text`。Linux は glibc 2.39 の生成した表、Mac は libSystem)。
pub fn strerror(number: i32) -> String {
    super::restore_taskmd::errno_text::strerror(number)
}

/// 保護領域の候補を決める環境の値。
#[derive(Clone, Debug, Default)]
pub struct Places {
    pub xdg_state_home: Option<Vec<u8>>,
    pub home: Option<Vec<u8>>,
    pub tmpdir: Option<Vec<u8>>,
}

/// 区間の間で持つ値。
pub struct Context {
    pub args: Args,
    pub startup: Startup,
    /// 受け継いだ環境(取り込みの後)。
    pub imported: Vec<(Vec<u8>, Vec<u8>)>,
    /// bash の `PATH` の値(受け継がなければ `None`。探すときは bash の既定の値)。
    pub path: Option<Vec<u8>>,
    /// 論理の cwd と `cd` が見る値。
    pub logical: paths::Logical,
    pub places: Places,
    /// 旧 bash の setlocale の状態。
    pub bash_locale: locale::BashLocale,
    /// 旧 bash が取り込んだ `LC_ALL`。
    pub startup_lc_all: Option<Vec<u8>>,
    /// 387 行より後で決まるスクリプトの変数(`PHYS_PWD`・`LOG_DIR` など)。
    pub extra: ScriptValues,
    pub log_dir: Vec<u8>,
    pub log_name: Vec<u8>,
    pub log_norm: Vec<u8>,
    pub shape: Option<state::Shape>,
    pub excl_tmp: Vec<u8>,
    pub excl_tmpdir: Vec<u8>,
    pub excl_cwd: Vec<u8>,
    pub state_tried: Vec<u8>,
}

impl Context {
    fn new(raw: Vec<Vec<u8>>) -> Self {
        let imported = childenv::imported(&raw);
        let get = |name: &str| childenv::lookup(&imported, name).map(<[u8]>::to_vec);
        let first = |name: &[u8]| childenv::first_value(&raw, name);
        let startup_lc_all = get("LC_ALL");
        let bash_locale = locale::BashLocale::start(&first);
        Self {
            path: get("PATH"),
            places: Places {
                xdg_state_home: get("XDG_STATE_HOME"),
                home: get("HOME"),
                tmpdir: get("TMPDIR"),
            },
            logical: paths::Logical {
                cwd: None,
                home: get("HOME"),
                oldpwd: get("OLDPWD").filter(|value| paths::is_dir(value)),
            },
            bash_locale,
            startup_lc_all,
            startup: Startup {
                raw,
                ..Startup::default()
            },
            imported,
            args: Args::default(),
            extra: ScriptValues::default(),
            log_dir: Vec::new(),
            log_name: Vec::new(),
            log_norm: Vec::new(),
            shape: None,
            excl_tmp: Vec::new(),
            excl_tmpdir: Vec::new(),
            excl_cwd: Vec::new(),
            state_tried: Vec::new(),
        }
    }

    /// 子を起動する時点のスクリプトの変数。
    pub fn script_values(&self) -> ScriptValues {
        let mut values = self.args.script_values();
        for (name, value) in &self.extra.values {
            values.set(name, value);
        }
        values
    }

    /// 探すときの `PATH`(受け継がなければ bash の既定の値)。
    pub fn path_value(&self) -> Vec<u8> {
        self.path
            .clone()
            .unwrap_or_else(|| paths::DEFAULT_PATH.to_vec())
    }

    /// `cd` が見る値(コマンド置換の中のサブシェルも同じ値を持つ)。
    pub fn cd_state(&self) -> paths::Logical {
        self.logical.clone()
    }
}

/// `host_cli_candidates`(465〜470 行。R4X-2 の判定 2 で使う。この区間では呼ばない): 立っている指標を全部候補に入れる。
/// `DEV_WORKFLOW_HOST_CLI` は最優先で、指定されたらそれだけ。
#[allow(dead_code)]
pub fn host_cli_candidates(imported: &[(Vec<u8>, Vec<u8>)]) -> Vec<Vec<u8>> {
    let set = |name: &str| childenv::lookup(imported, name).filter(|v| !v.is_empty());
    if let Some(value) = set("DEV_WORKFLOW_HOST_CLI") {
        return vec![value.to_vec()];
    }
    let mut out = Vec::new();
    if set("CLAUDECODE").is_some() {
        out.push(b"claude".to_vec());
    }
    if set("CODEX_SANDBOX").is_some() {
        out.push(b"codex".to_vec());
    }
    if set("CURSOR_AGENT").is_some() {
        out.push(b"cursor-agent".to_vec());
    }
    out
}

/// bash の起動時の警告(この順。基準の容器の bash 5.2.21 で測った)と、起動で決める値。
fn startup(shell: &mut Shell, context: &mut Context) {
    let imported = context.imported.clone();
    let get = |name: &str| childenv::lookup(&imported, name);
    // set_pwd。
    let (pwd, logical, failed) = childenv::pwd_value(get("PWD"));
    let posix = get("POSIXLY_CORRECT").is_some() || get("POSIX_PEDANTIC").is_some();
    context.startup.pwd = if posix && failed.is_none() && get("PWD").is_some() {
        logical.clone().or(pwd)
    } else {
        pwd
    };
    context.logical.cwd = logical;
    if let Some(errno) = failed {
        let mut text = b"shell-init: error retrieving current directory: getcwd: cannot access parent directories: ".to_vec();
        text.extend_from_slice(strerror(errno).as_bytes());
        shell.raw_stderr_line(&text);
    }
    // SHLVL。
    let (level, warned) = childenv::adjust_shell_level(get("SHLVL"), 1);
    context.startup.shell_level = level;
    if let Some(value) = warned {
        shell.startup_message(
            format!("warning: shell level ({value}) too high, resetting to 1").as_bytes(),
        );
    }
    // BASH_XTRACEFD。
    if let Some(value) = get("BASH_XTRACEFD").filter(|v| !v.is_empty()) {
        let message = match strtol_int(value) {
            Some(fd) => match os::xtrace_fd(fd) {
                Some(true) => None,
                Some(false) => Some("cannot open as FILE"),
                None => Some("invalid value for trace file descriptor"),
            },
            None => Some("invalid value for trace file descriptor"),
        };
        if let Some(message) = message {
            let mut text = b"BASH_XTRACEFD: ".to_vec();
            text.extend_from_slice(value);
            text.extend_from_slice(b": ");
            text.extend_from_slice(message.as_bytes());
            shell.startup_message(&text);
        }
    }
    // BASH_COMPAT。
    if let Some(value) = get("BASH_COMPAT").filter(|v| !v.is_empty()) {
        let level = match value {
            [a, b'.', b] if a.is_ascii_digit() && b.is_ascii_digit() => {
                Some((a - b'0') * 10 + (b - b'0'))
            }
            [a, b] if a.is_ascii_digit() && b.is_ascii_digit() => {
                Some((a - b'0') * 10 + (b - b'0'))
            }
            _ => None,
        };
        if !level.is_some_and(|l| (31..=52).contains(&l)) {
            let mut text = b"BASH_COMPAT: ".to_vec();
            text.extend_from_slice(value);
            text.extend_from_slice(b": compatibility value out of range");
            shell.startup_message(&text);
        }
    }
    // set_default_lang の setlocale(LC_ALL)。
    if let Some(value) = context.startup_lc_all.clone()
        && let Some((text, errno)) = context.bash_locale.set_lc_all(&value)
    {
        let mut text = [b"warning: ".as_slice(), &text].concat();
        if errno != 0 {
            text.extend_from_slice(b": ");
            text.extend_from_slice(strerror(errno).as_bytes());
        }
        shell.startup_message(&text);
    }
    let path_list = context.path_value();
    context.startup.root = os::user_ids().1 == 0;
    context.startup.bash = childenv::bash_value(
        &path_list,
        context.startup.pwd.as_deref(),
        os::login_shell().unwrap_or_else(|| b"/bin/sh".to_vec()),
    );
}

/// `(int)strtol(t, &e, 10)` で、全部が数として読めたら値。
fn strtol_int(value: &[u8]) -> Option<i32> {
    let start = value
        .iter()
        .position(|b| !matches!(b, b' ' | b'\t' | b'\n' | 11 | 12 | b'\r'))
        .unwrap_or(value.len());
    let mut rest = value.get(start..).unwrap_or_default();
    let mut negative = false;
    if let Some((sign, tail)) = rest.split_first()
        && (*sign == b'+' || *sign == b'-')
    {
        negative = *sign == b'-';
        rest = tail;
    }
    if rest.is_empty() || !rest.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let mut magnitude: i128 = 0;
    for byte in rest {
        magnitude = (magnitude * 10 + i128::from(byte - b'0')).min(i128::from(i64::MAX) + 1);
    }
    let long = if negative {
        (-magnitude).max(i128::from(i64::MIN))
    } else {
        magnitude.min(i128::from(i64::MAX))
    };
    Some(long as i64 as i32)
}

/// 境界の印の文。
pub fn boundary_text(cand: &[u8]) -> Vec<u8> {
    let mut text = "ERROR [r4x-1-boundary] 保護領域の候補 ".as_bytes().to_vec();
    text.extend_from_slice(cand);
    text.extend_from_slice(" の mkdir から後は R4X-2 で移す".as_bytes());
    text
}

/// 境界: 止め所の呼び返し → 罠のある信号を受けていれば `on_signal`(境界の行は出さない) →
/// 受けていなければ境界の行と終了値 99(書き込みに失敗しても 99)。
fn boundary(shell: &mut Shell, cand: &[u8]) -> Exit {
    if let Err(exit) = shell.stop_point(Point::Boundary, false) {
        return exit;
    }
    shell.flush_all();
    shell.boundary_message(&boundary_text(cand));
    shell.exit(99)
}

/// 検査専用入口の本体。argv はプログラム名を除いたもの(旧版と同じ並び)。終了値を返す(信号で
/// 終わるときは戻らない)。`stop` は止め所の呼び返し(製品の入口は `None`)。
pub fn main_entry(argv: &[OsString], stop: Option<StopHook>) -> u8 {
    // 起動の前に記録した SIGPIPE の扱いへ戻す(Rust の実行時が無視にしたもの)。
    if let Some(disposition) = os::inherited_disposition(os::SIGPIPE) {
        let _ = os::set_disposition(os::SIGPIPE, disposition);
    }
    // bash の initialize_shell_signals: 自分は SIGQUIT を無視にし、SIGCHLD には自分の処理を置く(起動時に無視でも
    // 子を回収できる)。子には、起動時に受け継いだ扱いを戻す(platform の spawn)。
    let _ = os::set_disposition(os::SIGQUIT, os::Disposition::Ignore);
    let _ = os::set_disposition(os::SIGCHLD, os::Disposition::Default);
    let mut shell = Shell::new(Box::new(stdio::RealIo));
    shell.stop = stop;
    let mut context = Context::new(os::raw_environment());
    startup(&mut shell, &mut context);
    shell.job_cwd_unknown = context.startup.pwd.is_none() && context.logical.cwd.is_none();
    // 152 行: trap cleanup EXIT。305 行: ERR の罠。
    shell.set_exit_trap();
    shell.err_trap = true;
    let code = match run(&mut shell, &mut context, argv) {
        Ok(code) => code,
        Err(Exit::Code(code)) => code,
        // 信号で終われなかったとき(止められていたなど)は、bash と同じ 128 + 番号。
        Err(Exit::Signal(signal)) => u8::try_from(128 + signal).unwrap_or(255),
    };
    shell.flush_all();
    code
}

fn run(shell: &mut Shell, context: &mut Context, argv: &[OsString]) -> Flow<u8> {
    context.args = args::parse(shell, argv)?;
    args::check(shell, context)?;
    state::log_file(shell, context)?;
    state::default_table(shell, context)?;
    shell.checkpoint()?;
    // 517〜519 行。
    shell.set_signal_traps();
    let cand = state::candidates(shell, context)?;
    Err(boundary(shell, &cand))
}
