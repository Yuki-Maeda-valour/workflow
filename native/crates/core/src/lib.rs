#![forbid(unsafe_code)]

pub mod build_info;
pub mod output;

pub use output::{
    CommandError, CommandIo, CommandOutput, CommandResult, ExitCode, emit, emit_to, execute,
    execute_to,
};
