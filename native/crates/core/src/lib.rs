#![forbid(unsafe_code)]

pub mod budget;
pub mod build_info;
pub mod fs;
pub mod loop_state;
pub mod output;

pub use output::{
    CommandError, CommandIo, CommandOutput, CommandResult, ExitCode, emit, emit_to, execute,
    execute_to,
};

pub mod process;

/// Purpose-specific snapshot profile parsing and encoding.
pub mod s3d_profile;
