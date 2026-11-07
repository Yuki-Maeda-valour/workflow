use dev_workflow_core::{CommandError, CommandResult};
use std::ffi::OsString;

// Task 2.1 がこの関数を実装する。引数にはコマンド名を含めない。
pub fn run(_args: &[OsString]) -> CommandResult {
    Err(CommandError::runtime("未実装"))
}
