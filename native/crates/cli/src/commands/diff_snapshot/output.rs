//! S3D output preserves the Bash operation and old source line on write faults.
use super::{Failure, Result};
use dev_workflow_platform::diff_snapshot::fs::write_standard;
#[cfg(test)]
thread_local! {
    /// Typed tests: the stderr writes of this thread, kept instead of written.
    pub static CAPTURE: std::cell::RefCell<Option<Vec<u8>>> = const { std::cell::RefCell::new(None) };
}
pub fn stderr(bytes: &[u8]) -> Result<()> {
    #[cfg(test)]
    if CAPTURE
        .with(|c| c.borrow_mut().as_mut().map(|v| v.extend_from_slice(bytes)))
        .is_some()
    {
        return Ok(());
    }
    write_standard(2, bytes).map_err(|_| Failure::silent(1))
}
pub fn stdout(bytes: &[u8], line: usize) -> Result<()> {
    write_standard(1, bytes).map_err(|error| write_failure(&error, line))
}
/// Bash's `printf` write error, with the fixed `strerror` table (not the text
/// of Rust's `io::Error`, which is musl's in the static Linux build).
pub fn write_failure(error: &std::io::Error, line: usize) -> Failure {
    let reason = super::tools::strerror(error);
    let diagnostic = format!("diff-snapshot.sh: line {line}: printf: write error: {reason}\n");
    Failure::raw(
        20,
        [diagnostic.into_bytes(), Failure::unexpected(1, line).stderr].concat(),
    )
}
