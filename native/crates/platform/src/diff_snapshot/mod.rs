//! Operations whose legacy snapshot ordering is part of the command contract.
//!
//! libc is used only where rustix has no equivalent: `sigaction`, `signal`
//! and `sigemptyset` (the minimal signal operations), `pthread_self` (CPython's
//! thread identifier), `localtime_r` (time zone rules), `fcntl` on a
//! descriptor number that may be closed (`BASH_XTRACEFD`), and on macOS only
//! `strerror_r`, `strsignal`, `getentropy` and `newlocale`/`freelocale` (the
//! system's own texts, randomness and locale check). Descriptor writes, `fstat`, `fcntl`, `fchdir` and Linux
//! `getrandom` go through rustix.
pub mod fs;
pub mod locale;
pub mod process;
pub mod signals;
use std::path::PathBuf;
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Operation {
    Open,
    Read,
    Hash,
    Spawn,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuditEvent {
    pub phase: &'static str,
    pub operation: Operation,
    pub path: PathBuf,
    pub dev: Option<u64>,
    pub ino: Option<u64>,
    pub fd: Option<i32>,
    pub bytes: u64,
    pub child_pid: Option<u32>,
    pub arguments: Vec<Vec<u8>>,
}
pub trait Audit {
    fn record(&mut self, event: AuditEvent);
}
pub struct NoAudit;
impl Audit for NoAudit {
    fn record(&mut self, _: AuditEvent) {}
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SyncPoint {
    BeforePatch,
    AfterPatch,
    BeforeReport,
    AfterReport,
    BeforeCleanup,
    ProfileInspected,
    ProfileOpened,
    ProfileReading,
    ProfileRead,
}
pub trait Operations {
    fn sync(&mut self, point: SyncPoint) -> std::io::Result<()>;
    /// `mv -- FROM TO`; an error carries the diagnostic to print.
    fn rename(&mut self, from: &std::path::Path, to: &std::path::Path) -> Result<(), Vec<u8>> {
        std::fs::rename(from, to).map_err(|e| e.to_string().into_bytes())
    }
    /// `cp -- FROM TO` of bytes the old script had written to FROM.
    fn copy(
        &mut self,
        _from: &std::path::Path,
        data: &[u8],
        to: &std::path::Path,
    ) -> Result<(), Vec<u8>> {
        std::fs::write(to, data).map_err(|e| e.to_string().into_bytes())
    }
}
pub struct NativeOperations;
impl Operations for NativeOperations {
    fn sync(&mut self, _: SyncPoint) -> std::io::Result<()> {
        Ok(())
    }
}

use std::cell::RefCell;
use std::sync::{Arc, Mutex};
thread_local! { static AUDIT: RefCell<Option<Arc<Mutex<Vec<AuditEvent>>>>> = const { RefCell::new(None) }; }
/// Typed opt-in observer; no CLI argument or environment variable enables it.
pub struct AuditScope {
    prior: Option<Arc<Mutex<Vec<AuditEvent>>>>,
}
impl AuditScope {
    pub fn enter(events: Arc<Mutex<Vec<AuditEvent>>>) -> Self {
        Self {
            prior: AUDIT.with(|slot| slot.replace(Some(events))),
        }
    }
}
impl Drop for AuditScope {
    fn drop(&mut self) {
        AUDIT.with(|slot| slot.replace(self.prior.take()));
    }
}
pub(crate) fn observe(event: AuditEvent) {
    AUDIT.with(|slot| {
        if let Some(log) = slot.borrow().as_ref() {
            log.lock().expect("audit lock poisoned").push(event);
        }
    });
}

/// Where a typed fault replaces the operation: the shell's `<FILE` open, or a
/// later read of the opened file by the tool.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaultStage {
    Open,
    Read,
}
type Fault = (&'static str, FaultStage, i32);
thread_local! { static FAULTS: RefCell<Vec<Fault>> = const { RefCell::new(Vec::new()) }; }
/// Typed-test substitution of an open that the old shell made with `<FILE`
/// (or of the tool's read after it). No CLI argument or environment variable
/// enables it.
pub struct FaultScope {
    prior: Vec<Fault>,
}
impl FaultScope {
    pub fn enter(point: &'static str, errno: i32) -> Self {
        Self::enter_at(point, FaultStage::Open, errno)
    }
    pub fn enter_at(point: &'static str, stage: FaultStage, errno: i32) -> Self {
        Self::enter_all(&[(point, stage, errno)])
    }
    pub fn enter_all(faults: &[(&'static str, FaultStage, i32)]) -> Self {
        Self {
            prior: FAULTS.with(|slot| slot.replace(faults.to_vec())),
        }
    }
}
impl Drop for FaultScope {
    fn drop(&mut self) {
        FAULTS.with(|slot| slot.replace(std::mem::take(&mut self.prior)));
    }
}
pub(crate) fn injected(point: &str) -> Option<std::io::Error> {
    injected_at(point, FaultStage::Open)
}
pub(crate) fn injected_at(point: &str, stage: FaultStage) -> Option<std::io::Error> {
    FAULTS.with(|slot| {
        slot.borrow()
            .iter()
            .find(|(p, s, _)| *p == point && *s == stage)
            .map(|(_, _, errno)| std::io::Error::from_raw_os_error(*errno))
    })
}
