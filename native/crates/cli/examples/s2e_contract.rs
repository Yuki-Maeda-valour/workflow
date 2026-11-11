#![forbid(unsafe_code)]
//! S2E専用の検査入口。製品登録表を変更しない。
//! 段階 2a は、引数・出力・終了コードと `host-paths` まで。ほかの 8 コマンドの本体は段階 2b が足す。
#[path = "../src/commands/environment_guard/mod.rs"]
mod environment_guard;

fn main() -> std::process::ExitCode {
    let columns = dev_workflow_platform::r4b_process::stdout_columns();
    let code = environment_guard::run_process(columns, &mut |parsed, stdout, _stderr| {
        environment_guard::dispatch::dispatch(
            &environment_guard::dispatch::ProcessHost,
            parsed,
            stdout,
        )
    });
    std::process::ExitCode::from(code)
}
