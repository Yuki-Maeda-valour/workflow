//! S2L開発用入口。製品登録・配布には接続しない。
mod args;
mod budget;
mod compare;
mod config;
#[cfg(test)]
mod config_graph_tests;
#[cfg(test)]
mod config_tests;
mod control;
#[cfg(test)]
mod decision_tests;
mod error;
mod interrupt;
mod json;
mod legacy_file;
mod legacy_tempfile;
mod observe;
#[cfg(all(test, target_os = "linux"))]
mod os_watch_tests;
mod output;
mod preflight;
mod process;
mod realpath;
mod runtime;
#[cfg(test)]
mod safety_tests;
mod scan;
mod snapshot;
mod state;
mod walk_recursion;
mod walk_regex;
use std::ffi::OsString;
pub fn main(args: &[OsString]) -> u8 {
    // 旧CPythonの起動時の信号の初期化(SIGXFSZ を無視・SIGINT は既定のときだけ受ける)。
    dev_workflow_platform::loop_state::signal::startup();
    let runtime = match runtime::LegacyRuntime::from_environment() {
        Ok(v) => v,
        Err(runtime::StartupFailure(raw)) => return emit_startup(&raw),
    };
    let args = match args::parse(args, runtime.int_limit) {
        Ok(v) => v,
        Err(out) => return output::emit(None, &runtime, &out.stdout, &out.stderr, out.code, None),
    };
    let mut stdout = Vec::new();
    let mut active = None;
    // 待ちの点の外で受けた SIGINT も、普通の完了へ進めない(次の区切りで止める)。
    let result = run(&args, &runtime, &mut stdout,&mut active).and_then(|code| interrupt::check().map(|()| code)).map_err(|e|{let(line,source)=match args.command.as_str(){"snapshot"=>(2147,"snapshot(args)"),"stop-control" if e.location()=="output"=>(2155,"sys.stdout.buffer.write(value.encode(\"utf-8\", \"surrogateescape\") + b\"\\0\")"),"stop-control"=>(2152,"control = held_stop_control(args.state, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)"),"preflight"=>(2160,"preflight(args)"),"derive-removed-worktree"=>(2165,"derive_removed_worktree(args)"),"state-digest"=>(2170,"print(state_digest(args.state, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds))"),"state-digests"=>(2180,"lines.append(state_digest(path, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds))"),_=>(2194,"return compare(args)")};e.at("main",line,source).at("<module>",2201,"raise SystemExit(main())")});
    match result {
        Ok(code) => output::emit(Some(&args), &runtime, &stdout, &[], code, active.as_ref()),
        Err(e) if e.stop_reason().is_some() => output::emit(
            Some(&args),
            &runtime,
            &stdout,
            format!("ERROR [loop-state] {}\n", e.stop_reason().unwrap()).as_bytes(),
            20,
            Some(&e),
        ),
        Err(e) => {
            let code = output::emit(
                Some(&args),
                &runtime,
                &stdout,
                error::render(&e).as_bytes(),
                1,
                None,
            );
            if interrupt::is_interrupt(&e) {
                // CPython の exit_sigint: 出力の後に SIGINT を既定へ戻して自分へ送る。
                return dev_workflow_platform::loop_state::signal::exit_by_interrupt();
            }
            code
        }
    }
}
fn run(
    args: &args::Arguments,
    runtime: &runtime::LegacyRuntime,
    stdout: &mut Vec<u8>,
    active: &mut Option<error::LegacyException>,
) -> error::Result<u8> {
    use error::LegacyException as E;
    use std::path::Path;
    let limits = &args.limits;
    if [&limits.items, &limits.total, &limits.one, &limits.seconds]
        .iter()
        .any(|n| !n.positive())
    {
        return Err(E::stop("状態観察の上限は正の整数でなければならない"));
    }
    match args.command.as_str() {
        "snapshot" => snapshot::run(args, runtime.int_limit)?,
        "preflight" => preflight::run(args, runtime.int_limit)?,
        "state-digest" => {
            stdout.extend(
                format!("{}\n", state::digest(Path::new(args.raw("state")), limits)?).as_bytes(),
            );
        }
        "state-digests" => {
            let mut failure = None;
            let mut lines: Vec<u8> = Vec::new();
            for path in args.many("state") {
                match state::digest(Path::new(path), limits) {
                    Ok(d) => lines.extend(format!("{d}\n").as_bytes()),
                    Err(e) if e.stop_reason().is_some() => {
                        lines.extend(b"-\n");
                        if failure.is_none() {
                            failure = Some(e);
                        }
                    }
                    Err(e) if e.is_os() => {
                        lines.extend(b"-\n");
                        if failure.is_none() {
                            failure = Some(E::stop("状態を読めない"));
                        }
                    }
                    Err(e) => return Err(e),
                }
            }
            stdout.extend(lines);
            if let Some(error) = failure {
                return Err(error);
            }
        }
        "derive-removed-worktree" => control::derive(args, runtime.int_limit)?,
        "stop-control" => control::held(args, runtime.int_limit, stdout)?,
        "compare" => {
            let (code, out) = compare::compare(args, runtime.int_limit, active, stdout)?;
            stdout.extend(out);
            return Ok(code);
        }
        _ => unreachable!(),
    }
    Ok(0)
}
fn emit_startup(raw: &[u8]) -> u8 {
    use std::io::Write;
    // 標準エラーがディレクトリなら書けない。旧版と同じく失敗を無視して終了値1にする。
    let _ = dev_workflow_platform::loop_state::RawOutput::Stderr(std::io::stderr()).write_all(raw);
    1
}
