//! 実操作の境界を対象単体検査へ記録する。製品ビルドでは記録しない。
use super::{budget::Budget, error::Result};
use dev_workflow_platform::fs::{FileSnapshot, HeldDir, HeldFile};
use std::ffi::{OsStr, OsString};
#[cfg(test)]
#[derive(Clone, Debug)]
pub enum Event {
    Budget {
        phase: &'static str,
        address: usize,
        count: u128,
        bytes: u128,
        deadline: (std::time::Instant, u64),
    },
    Open {
        parent: FileSnapshot,
        name: OsString,
        before: FileSnapshot,
    },
    Read {
        file: FileSnapshot,
        count: usize,
    },
    Spawn {
        top: OsString,
        arguments: Vec<OsString>,
        index: bool,
    },
    Spawned {
        pid: u32,
        index: bool,
    },
    ProfileParse,
    Returned {
        pid: u32,
        index: bool,
        alive: bool,
        stdout_closed: bool,
    },
    #[cfg(target_os = "linux")]
    OsAccess {
        path: std::path::PathBuf,
        mask: u32,
    },
    Directory {
        path: std::path::PathBuf,
        before: FileSnapshot,
    },
    /// os.readlink(name, dir_fd=parent) の直前。単体の検査で失敗を注入する。
    ReadLink {
        #[allow(dead_code)]
        name: OsString,
    },
    /// 保持した fd からの列挙(os.scandir(fd))の直前。単体の検査で失敗を注入する。
    Names {
        label: super::json::Text,
    },
}
#[cfg(test)]
type Callback = std::rc::Rc<std::cell::RefCell<dyn FnMut(&Event) -> Result<()>>>;
#[cfg(test)]
thread_local! {static HOOK:std::cell::RefCell<Option<Callback>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
pub struct Guard;
#[cfg(test)]
impl Drop for Guard {
    fn drop(&mut self) {
        HOOK.with(|h| {
            h.borrow_mut().take();
        });
    }
}
#[cfg(test)]
pub fn install(callback: impl FnMut(&Event) -> Result<()> + 'static) -> Guard {
    HOOK.with(|h| {
        assert!(h.borrow().is_none());
        *h.borrow_mut() = Some(std::rc::Rc::new(std::cell::RefCell::new(callback)));
    });
    Guard
}
#[cfg(test)]
pub fn event(event: Event) -> Result<()> {
    let callback = HOOK.with(|h| h.borrow().clone());
    if let Some(callback) = callback {
        callback.borrow_mut()(&event)
    } else {
        Ok(())
    }
}
pub fn budget(phase: &'static str, budget: &Budget) -> Result<()> {
    #[cfg(test)]
    event(Event::Budget {
        phase,
        address: budget as *const Budget as usize,
        count: budget.count,
        bytes: budget.bytes,
        deadline: budget.deadline_identity(),
    })?;
    let _ = (phase, budget);
    Ok(())
}
pub fn open(
    parent: &HeldDir,
    name: &OsStr,
    before: &FileSnapshot,
) -> std::result::Result<HeldFile, dev_workflow_platform::fs::FsError> {
    #[cfg(test)]
    event(Event::Open {
        parent: parent.snapshot()?,
        name: name.to_owned(),
        before: before.clone(),
    })
    .map_err(|e| match e {
        super::error::LegacyException::Os { error, .. } => {
            dev_workflow_platform::fs::FsError::Io(error)
        }
        _ => dev_workflow_platform::fs::FsError::Changed,
    })?;
    HeldFile::open_at(parent, name, before)
}
pub fn open_tree(
    parent: &HeldDir,
    name: &OsStr,
    before: &FileSnapshot,
    check: impl FnOnce() -> Result<()>,
) -> Result<HeldFile> {
    #[cfg(test)]
    event(Event::Open {
        parent: parent.snapshot()?,
        name: name.to_owned(),
        before: *before,
    })?;
    dev_workflow_platform::loop_state::open_tree_file(parent, name, before, check)
}
pub fn read(file: &FileSnapshot, count: usize) -> Result<()> {
    #[cfg(test)]
    event(Event::Read {
        file: file.clone(),
        count,
    })?;
    let _ = (file, count);
    Ok(())
}
pub fn spawn(top: &OsStr, args: &[OsString], index: bool) -> Result<()> {
    #[cfg(test)]
    event(Event::Spawn {
        top: top.to_owned(),
        arguments: args.to_vec(),
        index,
    })?;
    let _ = (top, args, index);
    Ok(())
}
pub fn spawned(pid: u32, index: bool) -> Result<()> {
    #[cfg(test)]
    event(Event::Spawned { pid, index })?;
    let _ = (pid, index);
    Ok(())
}
pub fn profile_parse() -> Result<()> {
    #[cfg(test)]
    event(Event::ProfileParse)?;
    Ok(())
}

/// 保持した親からの readlink。単体の検査では失敗を注入できる。
pub fn read_link(
    parent: &HeldDir,
    name: &OsStr,
) -> std::result::Result<OsString, dev_workflow_platform::fs::FsError> {
    #[cfg(test)]
    event(Event::ReadLink {
        name: name.to_owned(),
    })
    .map_err(|e| match e {
        super::error::LegacyException::Os { error, .. } => {
            dev_workflow_platform::fs::FsError::Io(error)
        }
        _ => dev_workflow_platform::fs::FsError::Changed,
    })?;
    dev_workflow_platform::loop_state::read_link(parent, name)
}
pub fn names(
    label: &super::json::Text,
) -> std::result::Result<(), dev_workflow_platform::fs::FsError> {
    #[cfg(test)]
    event(Event::Names {
        label: label.clone(),
    })
    .map_err(|e| match e {
        super::error::LegacyException::Os { error, .. } => {
            dev_workflow_platform::fs::FsError::Io(error)
        }
        _ => dev_workflow_platform::fs::FsError::Changed,
    })?;
    let _ = label;
    Ok(())
}
pub fn directory(
    path: &std::path::Path,
    before: &FileSnapshot,
) -> std::result::Result<(), dev_workflow_platform::fs::FsError> {
    #[cfg(test)]
    event(Event::Directory {
        path: path.to_owned(),
        before: before.clone(),
    })
    .map_err(|_| dev_workflow_platform::fs::FsError::Changed)?;
    let _ = (path, before);
    Ok(())
}

#[cfg(test)]
pub fn returned(
    child: &mut dev_workflow_platform::loop_state::LoopChild,
    index: bool,
) -> Result<()> {
    let alive = child.try_wait()?.is_none();
    let result = event(Event::Returned {
        pid: child.id(),
        index,
        alive,
        stdout_closed: child.stdout_is_closed(),
    });
    // 変異で回収を壊した時も検査自身が所有する直接子を片付ける。
    // 記録した alive は cleanup 前の実際の返却境界であり、成功へ読み替えない。
    if alive {
        child.kill_wait()?;
    }
    result
}
