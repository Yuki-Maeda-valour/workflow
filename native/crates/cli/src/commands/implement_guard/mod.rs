//! S3I-A: `implement-guard.sh`(以下「旧版」。sha256 `b958cc12…`)のうち、Git を呼ばない部分の内部部品。
//! 製品のコマンド登録には接続しない(検査専用入口 `implement_guard_contract` だけが呼ぶ)。
//! `take`・`config-check`・`compare` の本体は S3I-B #475・S3I-C #476 で移す。検査専用入口は、引数が
//! 正しい `take` と、記録の読み取りまで通った `config-check`・`compare` を、境界の印(stderr の
//! `ERROR [s3i-a-boundary] …` と終了値 99)で止める。
//!
//! # 旧版の関数との対応
//!
//! | 旧版(行) | Rust |
//! |---|---|
//! | 起動 :204-220(`set -eEuo pipefail`・export・unset・`exec </dev/null`・`umask 077`) | `start` |
//! | bash の起動時の警告(`SHLVL`・`BASH_XTRACEFD`・`BASH_COMPAT`・setlocale) | `startup_messages` |
//! | `cleanup_tmp` :299-313 | `Shell::cleanup_tmp` |
//! | `on_signal` :315-321・罠 :322-324 | `Shell::on_signal`・`Shell::install_traps`・`Shell::err_trap` |
//! | `usage` :327 | `args::usage` |
//! | `fail_usage` :329・`fail_internal` :330 | `state::fail_usage`・`state::fail_internal` |
//! | `arg_error` :350・`need_val` :356-366 | `args::arg_error`・`args::parse` |
//! | `resolve_tools` :422-427 | `prelude`(道具を使わないので確かめない。`stat` の地点だけ) |
//! | `needs_quote`・`cquote`・`path_field` :429-462 | `text::needs_quote`・`text::cquote`・`text::path_field` |
//! | `cunquote` :469-491・`field_value` :492 | `text::cunquote_*`・`meta::field_value_*` |
//! | `pwd_of`・`split_path` :502-509 | `paths::phys_dir`・`paths::split_path` |
//! | `abs_existing_dir`・`real_path` :521-540 | `paths::realpath` |
//! | `real_dir`・`under_dir`・`init_excl` :549-564 | `paths::phys_dir`・`state::init_excl` |
//! | `norm_rest`・`resolve_future_path` :571-614 | `paths::norm_rest`・`paths::resolve_future_path` |
//! | `check_state_dir` :644-671 | `state::check_state_dir` |
//! | `snapshot_digest` :673-684 | `state::snapshot_digest` |
//! | `state_fail`・`verify_digests`・`open_state` :2253-2280 | `state::state_fail`・`state::open_state` |
//! | `load_meta`・`set_taskmd_rel` :2281-2329 | `meta::load_meta`・`meta::Meta::rel` |
//! | `verify_taskmd_body` :2330-2346 | `meta::verify_taskmd_body` |
//! | `name_absent` :2362-2372・`owner_mark_of` | `cleanup::name_absent`・`cleanup::owner_mark_of` |
//! | `remove_owner_mark` :2521-2543 | `cleanup::remove_owner_mark` |
//! | `emit_incomparable` :2677-2686 | `state::emit_incomparable` |
//! | `cmd_config_check` :2714-2720・`cmd_compare` :2877-2885 | `boundary_after_meta` |
//! | `cmd_taskmd_diff` :2928-2975 | `taskmd_diff::run` |
//! | `restore_fail`・`cmd_restore_taskmd` :2977-3038 | `restore::run`(exec の先は R4R の部品) |
//! | `cmd_cleanup` :3041-3053 | `cleanup::run`(`rm -rf` は `cleanup::remove_tree`) |
//! | 引数の解析 :3055-3105 | `args::parse`・`main_entry` |
pub mod args;
pub mod childenv;
pub mod cleanup;
pub mod meta;
pub mod paths;
pub mod restore;
pub mod shell;
pub mod sites;
pub mod state;
pub mod stdio;
pub mod taskmd_diff;
#[cfg(test)]
mod tests;
pub mod text;

use args::Args;
use childenv::Startup;
use dev_workflow_platform::implement_guard as os;
use shell::{Exit, Flow, Shell, Sub};
use state::{Checked, Excluded, Places};
use std::ffi::OsString;

/// errno の文(R4R の `errno_text`。Linux は glibc 2.39 の生成した表、Mac は libSystem)。
pub fn strerror(number: i32) -> String {
    super::restore_taskmd::errno_text::strerror(number)
}

/// サブコマンドの間で持つ値。
#[derive(Default)]
pub struct Context {
    pub args: Args,
    pub places: Places,
    pub excluded: Excluded,
    pub checked: Option<Checked>,
    /// bash の `PATH` の値(受け継がなければ bash の既定。`None` は受け継いでいない)。
    pub path: Option<Vec<u8>>,
    pub startup: Startup,
}

/// `resolve_tools`(`stat -c %a -- /` の地点)と `init_excl`。
pub fn prelude(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    prelude_tools(shell)?;
    context.excluded = state::init_excl(&context.places);
    Ok(())
}

/// `resolve_stat` の地点(Rust は道具を使わないので確かめない)。
pub fn prelude_tools(shell: &mut Shell) -> Flow<()> {
    shell.tool_point("resolve-stat")
}

/// 境界の印(検査専用入口だけの動き)。`WORK` は EXIT の罠で消す。
fn boundary(shell: &mut Shell, sub: Sub) -> Exit {
    let mut text = b"ERROR [s3i-a-boundary] ".to_vec();
    text.extend_from_slice(sub.name());
    text.extend_from_slice(" の本体は S3I-B・S3I-C で移す".as_bytes());
    shell.boundary_message(&text);
    shell.finish(99)
}

/// `config-check`・`compare`: `load_meta` までを旧版と同じに行い、境界で止める。
fn boundary_after_meta(shell: &mut Shell, context: &mut Context, sub: Sub) -> Flow<()> {
    prelude(shell, context)?;
    let checked = state::open_state(
        shell,
        &context.args.state,
        &context.places,
        &context.excluded,
        &context.args.manifest_sha256,
        &context.args.snapshot_sha256,
    )?;
    let state = checked.state.clone();
    context.checked = Some(checked);
    state::make_work(shell, &state)?;
    meta::load_meta(shell, &state, &context.args.cwd)?;
    Err(boundary(shell, sub))
}

/// bash の起動時の警告(この順。基準の容器の bash 5.2.21 で測った)。
fn startup_messages(shell: &mut Shell, imported: &[(Vec<u8>, Vec<u8>)], startup: &mut Startup) {
    let get = |name: &str| childenv::lookup(imported, name);
    // set_pwd: getcwd が失敗した。
    let (pwd, failed) = childenv::pwd_value(get("PWD"));
    startup.pwd = pwd;
    if let Some(errno) = failed {
        let mut text = b"shell-init: error retrieving current directory: getcwd: cannot access parent directories: ".to_vec();
        text.extend_from_slice(strerror(errno).as_bytes());
        shell.raw_stderr_line(&text);
    }
    // SHLVL。
    let (level, warned) = childenv::adjust_shell_level(get("SHLVL"), 1);
    startup.shell_level = level;
    if let Some(value) = warned {
        shell.startup_message(
            format!("warning: shell level ({value}) too high, resetting to 1").as_bytes(),
        );
    }
    // BASH_XTRACEFD(数でないか、開いている fd でないか、書き込みで開けない)。
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
    // BASH_COMPAT(`D.D` か `DD` で 31〜52 のほかは範囲の外)。
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
    // setlocale(LC_ALL)。
    if let Some(value) = get("LC_ALL").filter(|v| !v.is_empty()) {
        let locpath = get("LOCPATH");
        let result = os::lc_all(value, locpath);
        let mut text = b"warning: setlocale: LC_ALL: cannot change locale (".to_vec();
        text.extend_from_slice(value);
        text.push(b')');
        match result {
            os::Locale::Found => {}
            os::Locale::Missing => shell.startup_message(&text),
            os::Locale::Failed(errno) => {
                text.extend_from_slice(b": ");
                text.extend_from_slice(strerror(errno).as_bytes());
                shell.startup_message(&text);
            }
        }
    }
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
    // strtol はあふれると LONG_MAX・LONG_MIN。int へ切り詰める。
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

/// 検査専用入口の本体。argv はプログラム名を除いたもの。終了値を返す(信号で終わるときは戻らない)。
pub fn main_entry(argv: &[OsString]) -> u8 {
    // 起動の前に記録した SIGPIPE の扱いへ戻す。
    if let Some(disposition) = os::inherited_pipe() {
        let _ = os::set_disposition(os::SIGPIPE, disposition);
    }
    let mut shell = Shell::new(Box::new(stdio::RealIo));
    shell.pipe = os::disposition(os::SIGPIPE).unwrap_or(os::Disposition::Default);
    shell.file_size = os::disposition(os::SIGXFSZ).unwrap_or(os::Disposition::Default);
    let raw = os::raw_environment();
    let imported = childenv::imported(&raw);
    let mut context = Context {
        path: childenv::lookup(&imported, "PATH").map(<[u8]>::to_vec),
        ..Context::default()
    };
    context.startup.raw = raw;
    context.startup.root = os::user_ids().1 == 0;
    startup_messages(&mut shell, &imported, &mut context.startup);
    context.startup.cwd = context.startup.pwd.clone();
    let path_list = context
        .path
        .clone()
        .unwrap_or_else(|| paths::DEFAULT_PATH.to_vec());
    context.startup.bash = childenv::bash_value(
        &path_list,
        context.startup.cwd.as_deref(),
        os::login_shell().unwrap_or_else(|| b"/bin/sh".to_vec()),
    );
    let get = |name: &str| childenv::lookup(&imported, name).map(<[u8]>::to_vec);
    paths::remember_oldpwd(get("OLDPWD").as_deref());
    context.places = Places {
        xdg_state_home: get("XDG_STATE_HOME"),
        home: get("HOME"),
        tmpdir: get("TMPDIR"),
    };
    shell.restore_temp_state = get("RESTORE_TEMP_STATE");
    // exec </dev/null(罠を置く前なので、失敗は errexit で終了値 1)。
    if let Err(error) = os::redirect_stdin_to_dev_null() {
        let errno = error.raw_os_error().unwrap_or(os::errno::EIO);
        shell.redirect_error(sites::DEV_NULL.line, b"/dev/null", errno);
        shell.flush_all();
        return 1;
    }
    os::set_umask_077();
    shell.install_traps();
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
    let (sub, args) = args::parse(shell, argv)?;
    context.args = args;
    match sub {
        Sub::Take => Err(boundary(shell, sub)),
        Sub::ConfigCheck | Sub::Compare => {
            boundary_after_meta(shell, context, sub)?;
            Ok(0)
        }
        Sub::TaskmdDiff => {
            taskmd_diff::run(shell, context)?;
            Err(shell.finish(0))
        }
        Sub::RestoreTaskmd => restore::run(shell, context),
        Sub::Cleanup => {
            cleanup::run(shell, context)?;
            Err(shell.finish(0))
        }
    }
}
