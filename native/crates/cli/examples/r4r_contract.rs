#![forbid(unsafe_code)]
//! R4R専用の検査入口。製品登録表を変更せず、旧 helper(`restore-taskmd.py`)と同じ argv を受ける。
#[path = "../src/commands/restore_taskmd/mod.rs"]
mod restore_taskmd;

use restore_taskmd::{ARGUMENT_RESPONSE, Emitted, Output, Restore};

fn main() -> std::process::ExitCode {
    // 引数の検査の前に、SIGXFSZ の無視と LC_CTYPE の書き換えを行う(Python の起動時と同じ)。
    restore_taskmd::start();
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let mut out: Output<restore_taskmd::Stdout> = Output::stdout();
    let (code, handlers_installed) = match restore_taskmd::parse_request(&args) {
        Err(restore_taskmd::ArgumentError) => {
            let (code, _) = restore_taskmd::emit(&mut out, None, 33, ARGUMENT_RESPONSE);
            (code, false)
        }
        Ok(request) => {
            let request: restore_taskmd::Request = request;
            let mut operation = Restore::new(request);
            let (rc, result) = operation.run();
            operation.checkpoint_before_output();
            let (code, state) = restore_taskmd::emit(&mut out, Some(&mut operation), rc, &result);
            debug_assert!(state != Emitted::MaskFailed || code == 33);
            (code, operation.handlers_installed())
        }
    };
    let mut code = code;
    let (_flush_io, flush_std_files) = restore_taskmd::exit_flush(&mut out);
    if let Err(error) = flush_std_files {
        // Python の flush_std_files は、失敗したらすぐに 2 行を出し、終了値を 120 に決める。
        restore_taskmd::write_stderr_lines(&restore_taskmd::unraisable_lines(&error));
        code = 120;
    }
    let _ = restore_taskmd::exit_close(&mut out, handlers_installed);
    std::process::ExitCode::from(code)
}
