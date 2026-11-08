mod probe;
mod resolve_task_dir;

use dev_workflow_core::{CommandIo, CommandResult};
use std::ffi::OsString;

pub struct Command {
    pub name: &'static str,
    pub run: fn(&[OsString], &mut CommandIo<'_>) -> CommandResult,
}

// 移植する機能は commands/<名前>/mod.rs に置き、この固定表に加える。
pub const COMMANDS: &[Command] = &[
    Command {
        name: "probe",
        run: |args, _io| probe::run(args),
    },
    Command {
        name: "resolve-task-dir",
        run: |args, _io| resolve_task_dir::run(args),
    },
];
