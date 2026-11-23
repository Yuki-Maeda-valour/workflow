//! Development-only contract executable; deliberately absent from the product CLI.
#![deny(unsafe_code)]
#[path = "../src/commands/diff_snapshot/mod.rs"]
mod diff_snapshot;
// Only this executable records the pre-main SIGPIPE and descriptor state. The
// product executable links the same platform crate without this initializer.
// The loader-initializer attribute is the only unsafe item of this file.
#[allow(unsafe_code)]
#[used]
#[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
#[cfg_attr(target_os = "macos", unsafe(link_section = "__DATA,__mod_init_func"))]
static REMEMBER_STARTUP_STATE: extern "C" fn() =
    dev_workflow_platform::diff_snapshot::signals::remember_startup_state;
fn main() -> std::process::ExitCode {
    diff_snapshot::main_entry().into()
}
