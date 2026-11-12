#![forbid(unsafe_code)]
//! S2E専用の検査入口。製品登録表を変更しない。
//! 9 コマンド(`bootstrap`・`verify`・`read`・`exec`・`hook`・`verify-pair`・`host-paths`・`host-settings`・`host-allowlist`)と、
//! 比較用の内部 reader の隠しコマンド(`reader-host-settings`・`reader-components`・`reader-policy-digest`)を動かす。
//! 旧 Python が起動の時点で止まる経路(不正な `PYTHONHASHSEED`・標準の fd がディレクトリ)も、本体の前に再現する。
#[path = "../src/commands/environment_guard/mod.rs"]
mod environment_guard;

fn main() -> std::process::ExitCode {
    // 比較用の隠しコマンド: 返した値を型の印つきの JSON で 1 行出す。環境は、通常のコマンドと同じに整える。
    let args: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if args
        .first()
        .is_some_and(|name| name.to_string_lossy().starts_with("reader-"))
    {
        environment_guard::prepare_process();
        if let Some(line) = environment_guard::reader::run_hidden_os(
            &environment_guard::dispatch::ProcessHost,
            &args,
        ) {
            // 読む側が先に閉じても(`| head`)、旧版の比較の出力として黙って終わる。
            let _ =
                std::io::Write::write_all(&mut std::io::stdout(), format!("{line}\n").as_bytes());
            return std::process::ExitCode::SUCCESS;
        }
    }
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
