mod probe;
mod resolve_task_dir;

use dev_workflow_core::CommandResult;
use std::ffi::OsString;

pub struct Command {
    pub name: &'static str,
    pub run: fn(&[OsString]) -> CommandResult,
}

// 移植する機能は commands/<名前>/mod.rs に置き、この固定表に加える。
pub const COMMANDS: &[Command] = &[
    Command {
        name: "probe",
        run: probe::run,
    },
    Command {
        name: "resolve-task-dir",
        run: resolve_task_dir::run,
    },
];
