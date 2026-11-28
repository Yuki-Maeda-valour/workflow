#![forbid(unsafe_code)]
//! 検査専用のloop-stateの入口(製品の`dev-workflow loop-state`と同じ`loop_state::main`を呼ぶ)。
// 受け継いだ`SIGPIPE`の扱いを`main`より前に控える(D2の決めたことの8。起動の失敗の診断を受け継いだ扱いで書く)。
dev_workflow_platform::s2e_record_startup_state!();
#[path = "../src/commands/loop_state/mod.rs"]
mod loop_state;
fn main() -> std::process::ExitCode {
    loop_state::main(&std::env::args_os().skip(1).collect::<Vec<_>>()).into()
}
