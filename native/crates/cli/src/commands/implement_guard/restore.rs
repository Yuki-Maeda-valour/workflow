//! `restore-taskmd` :2977-3038。旧版が exec で旧 helper へ渡す所から先は、R4R の部品を同じプロセスで
//! 呼ぶ(`examples/r4r_contract.rs` と同じ順)。exec の時点の状態(罠の戻し・`SIGCHLD`・環境・
//! `SIGPIPE`・`SIGINT`・stdout と stderr の種類)は、旧 bash と CPython 3.10 の起動を再現する。
use super::childenv::{self, ScriptValues};
use super::meta::{field_value_errexit, fields, first_line, load_meta};
use super::paths;
use super::shell::{Exit, Flow, Part, Shell};
use super::sites;
use super::state::open_state;
use super::{Context, prelude};
use dev_workflow_platform::implement_guard as os;
use std::ffi::{OsStr, OsString};
use std::os::unix::ffi::OsStrExt;

use super::super::restore_taskmd::{self as r4r, ARGUMENT_RESPONSE, Output, Restore};

/// `restore_fail`: `echo "ERROR [$reason] $*" >&2; printf 'RESTORED=no\nTOUCHED=%s\nTEMP=%s\nREASON=%s\n' …; exit 33`。
fn restore_fail(shell: &mut Shell, reason: &str, text: &str) -> Exit {
    match restore_fail_output(shell, reason, text) {
        Ok(()) => shell.finish(33),
        Err(exit) => exit,
    }
}

fn restore_fail_output(shell: &mut Shell, reason: &str, text: &str) -> Flow<()> {
    let mut line = format!("ERROR [{reason}] ").into_bytes();
    line.extend_from_slice(text.as_bytes());
    shell.echo_err(sites::RESTORE_FAIL_ECHO, &line)?;
    let touched = shell.touched_state.clone();
    let temp = match shell.restore_temp_state.clone() {
        Some(value) if !value.is_empty() => value,
        _ => b"none".to_vec(),
    };
    shell.printf(
        sites::RESTORE_FAIL_PRINTF,
        &[
            Part::Text(b"RESTORED=no\nTOUCHED="),
            Part::Arg(&touched),
            Part::Text(b"\nTEMP="),
            Part::Arg(&temp),
            Part::Text(b"\nREASON="),
            Part::Arg(reason.as_bytes()),
            Part::Text(b"\n"),
        ],
    )?;
    Ok(())
}

fn mode_ok(mode: &[u8]) -> bool {
    !mode.is_empty() && mode.len() <= 4 && mode.iter().all(|b| (b'0'..=b'7').contains(b))
}

fn sha_ok(sha: &[u8]) -> bool {
    sha.len() == 64
        && sha
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

/// `cmd_restore_taskmd`。戻り値は、exec の後(R4R の部品)の終了値。
pub fn run(shell: &mut Shell, context: &mut Context) -> Flow<u8> {
    prelude(shell, context)?;
    let checked = open_state(
        shell,
        &context.args.state,
        &context.places,
        &context.excluded,
        &context.args.manifest_sha256,
        &context.args.snapshot_sha256,
    )?;
    let state = checked.state.clone();
    context.checked = Some(checked.clone());
    let meta = load_meta(shell, &state, &context.args.cwd)?;
    let manifest = [state.as_slice(), b"/manifest.tsv"].concat();
    let line = match first_line(shell, &manifest, sites::RESTORE_READ.line) {
        Ok(Some(line)) => line,
        Ok(None) => {
            return Err(restore_fail(
                shell,
                "taskmd-body",
                "退避した本文の記録を読めない",
            ));
        }
        Err(errno) => {
            shell.redirect_error(sites::RESTORE_READ.line, &manifest, errno);
            return Err(restore_fail(
                shell,
                "taskmd-body",
                "退避した本文の記録を読めない",
            ));
        }
    };
    let parts = fields(&line, 5);
    let get = |i: usize| parts.get(i).cloned().unwrap_or_default();
    let (kind, mode, val, pf, extra) = (get(0), get(1), get(2), get(3), get(4));
    if kind != b"T" || !extra.is_empty() || !mode_ok(&mode) || !sha_ok(&val) {
        return Err(restore_fail(
            shell,
            "taskmd-body",
            "退避した本文の記録形式が不正",
        ));
    }
    let t_path = field_value_errexit(shell, &pf)?;
    if t_path != meta.real {
        return Err(restore_fail(
            shell,
            "taskmd-body",
            "退避した本文の実体パスが記録と一致しない",
        ));
    }
    // Python と同梱の helper は探さない(決定 D5)。cp だけを旧版と同じに求める。
    let path_list = context
        .path
        .clone()
        .unwrap_or_else(|| paths::DEFAULT_PATH.to_vec());
    let found = paths::command_v(b"cp", &path_list, context.startup.cwd.as_deref());
    shell.checkpoint()?;
    let Some(found) = found else {
        return Err(restore_fail(
            shell,
            "runtime-unavailable",
            "復元に必要な GNU cp が無い",
        ));
    };
    let cp = paths::realpath(&found).filter(|p| !p.is_empty());
    shell.tool_point("realpath-cp")?;
    let Some(cp) = cp else {
        return Err(restore_fail(
            shell,
            "runtime-unavailable",
            "cp の実体を取得できない",
        ));
    };
    if !paths::is_file(&cp) || !paths::executable(&cp) {
        return Err(restore_fail(
            shell,
            "runtime-unavailable",
            "必要な実行物を起動できない",
        ));
    }
    // exec の直前: ここまでの罠の信号を確かめてから、`trap - EXIT ERR TERM INT HUP`。
    shell.checkpoint()?;
    shell.reset_traps();
    let mut script = ScriptValues::default();
    let args = &context.args;
    let excluded = &context.excluded;
    let (rel, sel_rel) = meta.rel();
    let push = |script: &mut ScriptValues, name: &'static str, value: &[u8]| {
        script.values.push((name, value.to_vec()));
    };
    push(
        &mut script,
        "ALLOWED",
        &super::args::allowed_text(super::shell::Sub::RestoreTaskmd),
    );
    push(&mut script, "GIVEN", &args.given_text());
    push(&mut script, "CWD", &args.cwd);
    push(&mut script, "STATE_ARG", &args.state);
    push(&mut script, "MAN_SHA", &args.manifest_sha256);
    push(&mut script, "SNAP_SHA", &args.snapshot_sha256);
    push(&mut script, "CWD_PHYS", &meta.cwd);
    push(&mut script, "EXCL", &meta.exclude);
    push(&mut script, "EXCL_TMP", &excluded.tmp);
    push(&mut script, "EXCL_TMPDIR", &excluded.tmpdir);
    push(&mut script, "NREST", &checked.nrest);
    push(&mut script, "STATE", &state);
    push(&mut script, "TOP", &meta.toplevel);
    push(&mut script, "TASKMD_SEL", &meta.selected);
    push(&mut script, "TASKMD_REAL", &meta.real);
    push(&mut script, "TASKMD_KIND", &meta.kind);
    push(&mut script, "TASKMD_REL", &rel);
    push(&mut script, "TASKMD_SEL_REL", &sel_rel);
    push(&mut script, "T_MODE", &mode);
    push(&mut script, "T_SHA", &val);
    push(&mut script, "T_PATH", &t_path);
    push(&mut script, "UNQ", &t_path);
    push(&mut script, "mode", &mode);
    push(&mut script, "val", &val);
    push(&mut script, "pf", &pf);
    push(&mut script, "cp_path", &cp);
    let source = [state.as_slice(), b"/taskmd-body"].concat();
    Ok(exec_helper(
        shell,
        context,
        &script,
        &[source, meta.real.clone(), mode, val, cp],
    ))
}

/// exec の時点(旧 bash の `trap -` と exec、CPython 3.10 の起動)から、R4R の部品の終わりまで。
fn exec_helper(
    shell: &mut Shell,
    context: &Context,
    script: &ScriptValues,
    values: &[Vec<u8>; 5],
) -> u8 {
    let [source, destination, mode, sha, cp] = values;
    shell.flush_all();
    // 1. SIGCHLD の止めだけを外す。扱い(既定か無視)は引き継いだまま(TERM・INT・HUP は
    //    reset_traps で戻した)。
    let _ = os::unblock_child_signal();
    // 2. プロセスの環境を子の環境に置き換える。
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let entries = childenv::child_environment(
        &context.startup,
        script,
        (now.as_secs(), now.subsec_micros()),
    );
    let _ = os::replace_environment(&entries);
    // 3. SIGPIPE・SIGXFSZ を無視にする(CPython の init_signals)。
    let _ = os::set_disposition(os::SIGPIPE, os::Disposition::Ignore);
    r4r::start();
    // 4. 引き継いだ SIGINT が既定なら、KeyboardInterrupt の再現(R4R が処理を置くまで)。
    let int_default = shell
        .inherited
        .get(1)
        .is_some_and(|d| *d == os::Disposition::Default);
    os::clear_traps();
    if int_default {
        let _ = os::install_trap(os::SIGINT);
    }
    // 5. stdout か stderr がフォルダなら、CPython の init_sys_streams の失敗。
    if os::fd_kind(1) == Some(os::FdKind::Directory)
        || os::fd_kind(2) == Some(os::FdKind::Directory)
    {
        if os::fd_kind(1) == Some(os::FdKind::Directory)
            && os::fd_kind(2) != Some(os::FdKind::Directory)
        {
            let eisdir = os::errno::EISDIR;
            let text = format!(
                "Fatal Python error: init_sys_streams: can't initialize sys standard streams\n\
                 Python runtime state: core initialized\n\
                 {}: [Errno {eisdir}] {}: 1\n\n\
                 Current thread 0x{:016x} (most recent call first):\n  <no Python frame>\n",
                super::super::restore_taskmd::errno_text::exception_type(eisdir),
                super::strerror(eisdir),
                os::thread_id()
            );
            r4r::write_stderr_lines(&[text.into_bytes()]);
        }
        return 1;
    }
    let argv: Vec<OsString> = [
        b"restore".as_slice(),
        b"--source",
        source,
        b"--destination",
        destination,
        b"--expected-mode",
        mode,
        b"--expected-sha256",
        sha,
        b"--cp",
        cp,
    ]
    .iter()
    .map(|item| OsStr::from_bytes(item).to_os_string())
    .collect();
    let keyboard = |_: &mut Shell| keyboard_interrupt(int_default);
    let mut out: Output<r4r::Stdout> = Output::stdout();
    let (code, handlers_installed) = match r4r::parse_request(&argv) {
        Err(r4r::ArgumentError) => {
            if keyboard(shell) {
                return 130;
            }
            let (code, _) = r4r::emit(&mut out, None, 33, ARGUMENT_RESPONSE);
            (code, false)
        }
        Ok(request) => {
            let request: r4r::Request = request;
            if keyboard(shell) {
                return 130;
            }
            let mut operation = Restore::new(request);
            let (rc, result) = operation.run();
            operation.checkpoint_before_output();
            let (code, _) = r4r::emit(&mut out, Some(&mut operation), rc, &result);
            (code, operation.handlers_installed())
        }
    };
    // 旧 helper の `emit_result` は応答を書く間 TERM・INT・HUP を止め、書き終えて止めを戻した所で、
    // 届いていた SIGINT が `KeyboardInterrupt` になる(helper の処理を置く前だけ。決定の表の
    // 「旧 helper の引数の誤りの応答を書く間の SIGINT」)。
    if !handlers_installed && keyboard(shell) {
        return 130;
    }
    let mut code = code;
    let (_flush_io, flush_std_files) = r4r::exit_flush(&mut out);
    if let Err(error) = flush_std_files {
        r4r::write_stderr_lines(&r4r::unraisable_lines(&error));
        code = 120;
    }
    let _ = r4r::exit_close(&mut out, handlers_installed);
    code
}

/// CPython の `default_int_handler` が置かれた後、R4R の部品が処理を置く前に SIGINT を受けていれば、
/// `KeyboardInterrupt` の呼出履歴(途中の行は比べない)を出して SIGINT で終わる。
fn keyboard_interrupt(int_default: bool) -> bool {
    if int_default && os::pending_traps() & (1u32 << os::SIGINT) != 0 {
        r4r::write_stderr_lines(&[
            b"Traceback (most recent call last):\n".to_vec(),
            b"KeyboardInterrupt\n".to_vec(),
        ]);
        os::die_by_signal(os::SIGINT);
        return true;
    }
    false
}
