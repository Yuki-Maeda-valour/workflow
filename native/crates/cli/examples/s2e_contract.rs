#![forbid(unsafe_code)]
//! S2E専用の検査入口。製品登録表を変更しない。
//! 段階 1 は、引数・出力・終了コードまで。9 コマンドの本体は段階 2 が足す。
#[path = "../src/commands/environment_guard/mod.rs"]
mod environment_guard;

fn main() -> std::process::ExitCode {
    let columns = dev_workflow_platform::r4b_process::stdout_columns();
    let code = environment_guard::run_process(columns, &mut |_, _, _| {
        Err(environment_guard::error::PyException::stop(
            "9 コマンドの本体は段階 2 で実装する",
        ))
    });
    std::process::ExitCode::from(code)
}
