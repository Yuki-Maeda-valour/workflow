//! Linux の実 open/access 監視。テストが所有する木だけを監視する。
use std::{
    collections::HashMap,
    ffi::CString,
    io::Read,
    os::{fd::FromRawFd, unix::ffi::OsStrExt},
    path::{Path, PathBuf},
};
pub struct Watch {
    file: std::fs::File,
    paths: HashMap<i32, PathBuf>,
}
unsafe extern "C" {
    fn inotify_init1(flags: i32) -> i32;
    fn inotify_add_watch(fd: i32, path: *const std::ffi::c_char, mask: u32) -> i32;
}
impl Watch {
    pub fn new(root: &Path) -> Self {
        fn paths(root: &Path, out: &mut Vec<PathBuf>) {
            out.push(root.into());
            if std::fs::symlink_metadata(root).unwrap().is_dir() {
                for entry in std::fs::read_dir(root).unwrap() {
                    paths(&entry.unwrap().path(), out)
                }
            }
        }
        let mut targets = vec![];
        paths(root, &mut targets);
        let fd = unsafe { inotify_init1(0x80800) };
        assert!(fd >= 0, "OS_WATCH_INIT");
        let mut paths = HashMap::new();
        for path in targets {
            let raw = CString::new(path.as_os_str().as_bytes()).unwrap();
            let wd = unsafe { inotify_add_watch(fd, raw.as_ptr(), 0x2000021) };
            assert!(wd >= 0, "OS_WATCH_ADD: {}", path.display());
            paths.insert(wd, path);
        }
        Self {
            file: unsafe { std::fs::File::from_raw_fd(fd) },
            paths,
        }
    }
    pub fn finish(mut self) -> Vec<(PathBuf, u32)> {
        let mut events = vec![];
        let mut raw = [0u8; 65536];
        loop {
            let count = match self.file.read(&mut raw) {
                Ok(0) => break,
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("OS_WATCH_READ: {e}"),
            };
            let mut at = 0;
            while at < count {
                assert!(count - at >= 16);
                let wd = i32::from_ne_bytes(raw[at..at + 4].try_into().unwrap());
                let mask = u32::from_ne_bytes(raw[at + 4..at + 8].try_into().unwrap());
                let length = u32::from_ne_bytes(raw[at + 12..at + 16].try_into().unwrap()) as usize;
                assert!(16 + length <= count - at);
                if let Some(path) = self.paths.get(&wd) {
                    if mask & 0x21 != 0 {
                        let name = &raw[at + 16..at + 16 + length];
                        let end = name.iter().position(|b| *b == 0).unwrap_or(name.len());
                        let path = if end == 0 {
                            path.clone()
                        } else {
                            path.join(std::ffi::OsStr::from_bytes(&name[..end]))
                        };
                        events.push((path, mask));
                    }
                } else {
                    assert_eq!(mask & 0x4000, 0, "OS_WATCH_OVERFLOW");
                }
                at += 16 + length;
            }
        }
        events
    }
}
