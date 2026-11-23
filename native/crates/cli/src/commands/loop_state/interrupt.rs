//! 旧CPythonの未捕捉 KeyboardInterrupt。受け方は platform の専用部品が決める。
//! 待ちの点では旧版の後始末を行い、ほかの所では次の区切り(予算の確認)で止める。
use super::error::{LegacyException, Result};
use dev_workflow_platform::loop_state::signal;

pub fn exception() -> LegacyException {
    LegacyException::Python {
        kind: "KeyboardInterrupt",
        reason: String::new(),
        location: "interrupt",
    }
}
pub fn pending() -> bool {
    signal::interrupted()
}
/// 区切りで受けた SIGINT を、旧版と同じ未捕捉の KeyboardInterrupt にする。
pub fn check() -> Result<()> {
    if pending() { Err(exception()) } else { Ok(()) }
}
pub fn is_interrupt(error: &LegacyException) -> bool {
    match error {
        LegacyException::Python { kind, .. } => *kind == "KeyboardInterrupt",
        LegacyException::Traced { error, .. } | LegacyException::Context { error, .. } => {
            is_interrupt(error)
        }
        _ => false,
    }
}
