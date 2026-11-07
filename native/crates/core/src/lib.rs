#![forbid(unsafe_code)]

pub mod build_info;
pub mod output;

pub use output::{CommandError, CommandResult, ExitCode, emit, emit_to};
