#![forbid(unsafe_code)]

pub mod budget;
pub mod build_info;
pub mod fs;
pub mod output;

pub use output::{
    CommandError, CommandIo, CommandOutput, CommandResult, ExitCode, emit, emit_to, execute,
    execute_to,
};
