#![forbid(unsafe_code)]
//! 未登録のloop-state専用検査入口。
#[path = "../src/commands/loop_state/mod.rs"]
mod loop_state;
fn main() -> std::process::ExitCode {
    loop_state::main(&std::env::args_os().skip(1).collect::<Vec<_>>()).into()
}
