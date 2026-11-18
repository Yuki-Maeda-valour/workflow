use super::*;
use std::{
    cell::RefCell,
    os::fd::FromRawFd,
    rc::Rc,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
};
#[derive(Clone, Debug)]
pub enum MemoryEvent {
    #[cfg(target_os = "linux")]
    Create(String),
    #[cfg(target_os = "linux")]
    Opened(String, i32),
    #[cfg(target_os = "linux")]
    Write(String),
    #[cfg(target_os = "linux")]
    Seek(String),
    Close(String),
}
type Hook = Rc<RefCell<dyn FnMut(MemoryEvent) -> io::Result<()>>>;
thread_local! {static MEMORY:RefCell<Option<Hook>>=const{RefCell::new(None)};static SETUP:RefCell<Setup>=const{RefCell::new(Setup{skip_range:false,skip_directory:false,limit:None})};}
pub fn memory_event(event: MemoryEvent) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    let MemoryEvent::Close(ref name) = event;
    #[cfg(target_os = "macos")]
    let _ = name.len();
    let hook = MEMORY.with(|h| h.borrow().clone());
    if let Some(hook) = hook {
        hook.borrow_mut()(event)
    } else {
        Ok(())
    }
}
#[derive(Clone, Copy, Default)]
pub struct Setup {
    pub skip_range: bool,
    pub skip_directory: bool,
    pub limit: Option<u64>,
}
pub fn setup() -> Setup {
    SETUP.with(|s| *s.borrow())
}
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
        MEMORY.with(|h| h.borrow_mut().take());
        SETUP.with(|s| *s.borrow_mut() = Setup::default());
    }
}
#[cfg(target_os = "linux")]
fn hook(callback: impl FnMut(MemoryEvent) -> io::Result<()> + 'static) -> Reset {
    MEMORY.with(|h| {
        assert!(h.borrow().is_none());
        *h.borrow_mut() = Some(Rc::new(RefCell::new(callback)));
    });
    Reset
}
static HELPER: OnceLock<std::path::PathBuf> = OnceLock::new();
fn helper() -> &'static std::path::Path {
    HELPER.get_or_init(||{let base=std::fs::canonicalize(std::env::temp_dir()).unwrap().join(format!("s2l-platform-helper-{}",std::process::id()));std::fs::create_dir_all(&base).unwrap();let source=base.join("helper.c");let binary=base.join("helper");std::fs::write(&source,br#"#include <unistd.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
int main(int argc,char**argv){
 printf("stdio %d %d %d\n",fcntl(0,F_GETFL)&O_ACCMODE,fcntl(1,F_GETFL)&O_ACCMODE,fcntl(2,F_GETFL)&O_ACCMODE);
 printf("signals %d %d\n",signal(SIGPIPE,SIG_DFL)==SIG_DFL,signal(SIGXFSZ,SIG_DFL)==SIG_DFL);
 for(int fd=3;fd<1024;fd++){if(fcntl(fd,F_GETFD)>=0)printf("fd %d\n",fd);}
 for(int i=1;i<argc;i++){int fd=open(argv[i],O_RDONLY);if(fd<0)return 31;char b[256];ssize_t n=read(fd,b,sizeof b);close(fd);if(n<0)return 32;printf("data ");fwrite(b,1,n,stdout);putchar('\n');}
 return 0;}
"#).unwrap();let out=Command::new("cc").arg(&source).args(["-O0","-o"]).arg(&binary).output().unwrap();assert!(out.status.success(),"S2L_HELPER_COMPILE: {}",String::from_utf8_lossy(&out.stderr));binary}).as_path()
}
fn child_output(mut child: LoopChild) -> String {
    let mut raw = vec![];
    let mut buffer = [0; 4096];
    loop {
        assert!(
            child.poll(Duration::from_secs(5)).unwrap(),
            "CHILD_OUTPUT_TIMEOUT"
        );
        let n = child.read(&mut buffer).unwrap();
        if n == 0 {
            break;
        }
        raw.extend_from_slice(&buffer[..n]);
    }
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(code) = child.try_wait().unwrap() {
            assert_eq!(code, 0, "CHILD_STATUS");
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::yield_now();
    }
    String::from_utf8(raw).unwrap()
}
fn noncloexec(minimum: i32) -> OwnedFd {
    let source = std::fs::File::open("/dev/null").unwrap();
    let fd = unsafe { c_fcntl(source.as_raw_fd(), 0, minimum) };
    assert!(fd >= minimum);
    unsafe { OwnedFd::from_raw_fd(fd) }
}
fn flags(fd: i32) -> i32 {
    unsafe { c_fcntl(fd, 1, 0) }
}
fn inherited() -> (OwnedFd, OwnedFd) {
    (noncloexec(80), noncloexec(90))
}

fn isolated(name: &str, body: impl FnOnce()) {
    if std::env::var("S2L_PLATFORM_CHILD").ok().as_deref() == Some(name) {
        struct Cleanup;
        impl Drop for Cleanup {
            fn drop(&mut self) {
                if let Some(path) = HELPER.get() {
                    let _ = std::fs::remove_dir_all(path.parent().unwrap());
                }
            }
        }
        let _cleanup = Cleanup;
        body();
    } else {
        let result = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("loop_state::tests::{name}"),
                "--nocapture",
            ])
            .env("S2L_PLATFORM_CHILD", name)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "PLATFORM_ISOLATED {name}: {}{}",
            String::from_utf8_lossy(&result.stdout),
            String::from_utf8_lossy(&result.stderr)
        );
    }
}
macro_rules! isolated_test {
    ($name:ident,$body:block) => {
        #[test]
        fn $name() {
            isolated(stringify!($name), || $body)
        }
    };
}
isolated_test!(
    ordinary_child_restores_stdio_and_signals_and_closes_unrelated,
    {
        let unrelated = noncloexec(40);
        let raw = child_output(LoopChild::spawn(Command::new(helper()), &[], &[]).unwrap());
        assert!(raw.contains("stdio 2 1 2\n"), "STDIO_RDWR: {raw}");
        assert!(
            raw.contains("signals 1 1\n"),
            "CHILD_RESTORE_SIGNALS: {raw}"
        );
        assert!(
            !raw.lines().any(|l| l.starts_with("fd ")),
            "UNRELATED_FD: {raw}"
        );
        assert_eq!(flags(unrelated.as_raw_fd()), 0, "PARENT_FD_UNCHANGED");
    }
);
isolated_test!(child_directory_fallback_closes_inheritable_fds, {
    let _reset = Reset;
    SETUP.with(|s| s.borrow_mut().skip_range = true);
    let unrelated = noncloexec(70);
    let raw = child_output(LoopChild::spawn(Command::new(helper()), &[], &[]).unwrap());
    assert!(
        !raw.contains(&format!("fd {}\n", unrelated.as_raw_fd())),
        "DIRECTORY_FALLBACK: {raw}"
    );
    assert_eq!(flags(unrelated.as_raw_fd()), 0);
});
isolated_test!(
    finite_fallback_covers_upper_endpoint_and_intervals_before_kept_fds,
    {
        let _reset = Reset;
        SETUP.with(|s| {
            *s.borrow_mut() = Setup {
                skip_range: true,
                skip_directory: true,
                limit: Some(64),
            }
        });
        let exact = noncloexec(64);
        let middle = noncloexec(70);
        let (one, two) = inherited();
        let first = MemoryFile {
            fd: one,
            _inputs: vec![],
            name: "finite-one".into(),
        };
        let second = MemoryFile {
            fd: two,
            _inputs: vec![],
            name: "finite-two".into(),
        };
        let kept = [
            InheritedInput::Memory(&first),
            InheritedInput::Memory(&second),
        ];
        let raw = child_output(LoopChild::spawn(Command::new(helper()), &[], &kept).unwrap());
        assert!(
            !raw.contains(&format!("fd {}\n", exact.as_raw_fd())),
            "FINITE_ENDPOINT: {raw}"
        );
        assert!(
            !raw.contains(&format!("fd {}\n", middle.as_raw_fd())),
            "FINITE_KEEP_INTERVAL: {raw}"
        );
        for fd in [first.fd.as_raw_fd(), second.fd.as_raw_fd()] {
            assert!(raw.contains(&format!("fd {fd}\n")), "FINITE_KEEP: {raw}");
            assert_eq!(flags(fd), 0, "PARENT_FLAGS");
        }
    }
);
isolated_test!(concurrent_noncloexec_opens_do_not_leak_into_child, {
    let running = Arc::new(AtomicBool::new(true));
    let flag = running.clone();
    let thread = std::thread::spawn(move || {
        while flag.load(Ordering::Relaxed) {
            let file = noncloexec(50);
            std::thread::yield_now();
            drop(file);
        }
    });
    for fallback in [false, true] {
        SETUP.with(|s| s.borrow_mut().skip_range = fallback);
        for _ in 0..20 {
            let raw = child_output(LoopChild::spawn(Command::new(helper()), &[], &[]).unwrap());
            assert!(
                !raw.lines().any(|l| l.starts_with("fd ")),
                "CONCURRENT_FD_LEAK: {raw}"
            );
        }
    }
    SETUP.with(|s| *s.borrow_mut() = Setup::default());
    running.store(false, Ordering::Relaxed);
    thread.join().unwrap();
});
isolated_test!(
    spawn_failure_and_unlisted_input_preserve_parent_flags_and_position,
    {
        let file = noncloexec(80);
        let memory = MemoryFile {
            fd: file,
            _inputs: vec![],
            name: "failure".into(),
        };
        let before = flags(memory.fd.as_raw_fd());
        let mut missing = Command::new("/s2l-test-owned-does-not-exist");
        missing.env_clear();
        assert!(LoopChild::spawn(missing, &[], &[InheritedInput::Memory(&memory)]).is_err());
        assert_eq!(
            flags(memory.fd.as_raw_fd()),
            before,
            "SPAWN_FAILURE_PARENT_FLAGS"
        );
        assert!(
            LoopChild::spawn(
                Command::new(helper()),
                &[LoopArg::InputPath(InheritedInput::Memory(&memory))],
                &[]
            )
            .is_err(),
            "UNLISTED_INPUT_MUST_NOT_SPAWN"
        );
    }
);
#[cfg(target_os = "linux")]
isolated_test!(
    marker_probe_memory_write_rewind_inheritance_and_reverse_close,
    {
        let events = Rc::new(RefCell::new(vec![]));
        let sink = events.clone();
        let _reset = hook(move |e| {
            sink.borrow_mut().push(e);
            Ok(())
        });
        let marker = MemoryFile::create("marker", &[MemoryPart::Raw(b"marker-body")]).unwrap();
        let marker_input = InheritedInput::Memory(&marker);
        let probe = MemoryFile::create(
            "probe",
            &[
                MemoryPart::Raw(b"include="),
                MemoryPart::InputPath(marker_input),
            ],
        )
        .unwrap();
        let inputs = [marker_input, InheritedInput::Memory(&probe)];
        let marker_offset = fs::seek(&marker.fd, fs::SeekFrom::Current(0)).unwrap();
        let raw = child_output(
            LoopChild::spawn(
                Command::new(helper()),
                &[
                    LoopArg::InputPath(marker_input),
                    LoopArg::InputPath(inputs[1]),
                ],
                &inputs,
            )
            .unwrap(),
        );
        assert!(raw.contains("data marker-body\n"), "MEMORY_REWIND: {raw}");
        assert!(
            raw.contains(&format!(
                "data include=/proc/self/fd/{}\n",
                marker.fd.as_raw_fd()
            )),
            "MEMORY_REFERENCE: {raw}"
        );
        assert_eq!(
            fs::seek(&marker.fd, fs::SeekFrom::Current(0)).unwrap(),
            marker_offset,
            "PARENT_OFFSET"
        );
        let fd1 = marker.fd.as_raw_fd();
        let fd2 = probe.fd.as_raw_fd();
        drop(probe);
        assert!(flags(fd2) < 0, "PROBE_CLOSED");
        drop(marker);
        assert!(flags(fd1) < 0, "MARKER_CLOSED");
        let events = events.borrow();
        let labels = events
            .iter()
            .map(|event| match event {
                MemoryEvent::Create(n) => format!("create:{n}"),
                MemoryEvent::Opened(n, _) => format!("open:{n}"),
                MemoryEvent::Write(n) => format!("write:{n}"),
                MemoryEvent::Seek(n) => format!("seek:{n}"),
                MemoryEvent::Close(n) => format!("close:{n}"),
            })
            .collect::<Vec<_>>();
        assert!(
            labels.iter().position(|s| s == "seek:marker")
                < labels.iter().position(|s| s == "create:probe"),
            "MEMORY_CREATION_ORDER"
        );
        assert_eq!(
            &labels[labels.len() - 2..],
            ["close:probe", "close:marker"],
            "MEMORY_REVERSE_CLOSE"
        );
    }
);
#[cfg(target_os = "linux")]
isolated_test!(
    memory_failures_close_created_fds_and_distinguish_create_write_seek,
    {
        for fail in ["create-second", "write", "seek"] {
            let opened = Rc::new(RefCell::new(vec![]));
            let sink = opened.clone();
            let _reset = hook(move |event| {
                if let MemoryEvent::Opened(_, fd) = &event {
                    sink.borrow_mut().push(*fd)
                }
                let failure = match &event {
                    MemoryEvent::Create(n) => fail == "create-second" && n == "second",
                    MemoryEvent::Write(_) => fail == "write",
                    MemoryEvent::Seek(_) => fail == "seek",
                    _ => false,
                };
                if failure {
                    Err(io::Error::from_raw_os_error(5))
                } else {
                    Ok(())
                }
            });
            let result = MemoryFile::create("first", &[MemoryPart::Raw(b"body")]);
            if fail == "create-second" {
                let first = result.unwrap();
                assert!(matches!(
                    MemoryFile::create(
                        "second",
                        &[MemoryPart::InputPath(InheritedInput::Memory(&first))]
                    ),
                    Err(MemoryError::Create(_))
                ));
                drop(first)
            } else {
                assert!(
                    matches!(result, Err(MemoryError::Write(_))),
                    "MEMORY_ERROR_CLASS"
                );
            }
            for &fd in opened.borrow().iter() {
                assert!(flags(fd) < 0, "MEMORY_FAILURE_FD_CLOSED: {fail}");
            }
        }
    }
);
#[cfg(target_os = "macos")]
isolated_test!(
    mac_memory_creation_is_unsupported_and_never_uses_temporary_files,
    {
        assert!(
            matches!(MemoryFile::create("mac-rejection",&[MemoryPart::Raw(b"body")]),Err(MemoryError::Create(error))if error.kind()==io::ErrorKind::Unsupported),
            "MAC_MEMORY_REJECTION"
        );
    }
);
isolated_test!(held_file_inheritance_preserves_parent_position_and_flags, {
    let path = helper().parent().unwrap().join("held-input");
    std::fs::write(&path, b"held-body").unwrap();
    let parent = HeldDir::open_absolute(path.parent().unwrap()).unwrap();
    let before = parent.metadata(OsStr::new("held-input")).unwrap();
    let mut file = HeldFile::open_at(&parent, OsStr::new("held-input"), &before).unwrap();
    {
        let mut session = file.begin_read().unwrap();
        assert_eq!(session.read_chunk(&mut [0u8; 1]).unwrap(), 1);
    }
    let fd = file.borrowed_fd().as_raw_fd();
    let position = fs::seek(file.borrowed_fd(), fs::SeekFrom::Current(0)).unwrap();
    let initial_flags = flags(fd);
    let input = InheritedInput::Held(&file);
    #[cfg(target_os = "linux")]
    let arguments = [LoopArg::InputPath(input)];
    #[cfg(target_os = "macos")]
    let arguments = [];
    let raw = child_output(LoopChild::spawn(Command::new(helper()), &arguments, &[input]).unwrap());
    assert!(raw.contains(&format!("fd {fd}\n")), "HELD_FD_INHERITED");
    #[cfg(target_os = "linux")]
    assert!(raw.contains("data held-body\n"), "HELD_INPUT_PATH_READ");
    assert_eq!(flags(fd), initial_flags, "HELD_PARENT_FLAGS");
    assert_eq!(
        fs::seek(file.borrowed_fd(), fs::SeekFrom::Current(0)).unwrap(),
        position,
        "HELD_PARENT_POSITION"
    );
    let mut missing = Command::new("/s2l-own-missing-command");
    missing.env_clear();
    assert!(LoopChild::spawn(missing, &[], &[input]).is_err());
    assert_eq!(flags(fd), initial_flags);
    assert_eq!(
        fs::seek(file.borrowed_fd(), fs::SeekFrom::Current(0)).unwrap(),
        position
    );
});
isolated_test!(quiet_child_uses_one_rdwr_null_description, {
    let report = helper().parent().unwrap().join("quiet.json");
    let script = "import os,fcntl,json,sys\na=[fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE for fd in range(3)]\nf=fcntl.fcntl(0,fcntl.F_GETFL);fcntl.fcntl(0,fcntl.F_SETFL,f|os.O_NONBLOCK)\nb=[bool(fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_NONBLOCK) for fd in range(3)]\nopen(sys.argv[1],'w').write(json.dumps([a,b]))\n";
    let mut command = Command::new("python3");
    command.args(["-B", "-c", script]).arg(&report);
    let mut child = LoopChild::spawn_quiet(command, &[]).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(code) = child.try_wait().unwrap() {
            assert_eq!(code, 0);
            break;
        }
        assert!(std::time::Instant::now() < deadline, "QUIET_WAIT_TIMEOUT");
        std::thread::yield_now();
    }
    assert_eq!(
        std::fs::read_to_string(report).unwrap(),
        "[[2, 2, 2], [true, true, true]]",
        "QUIET_SHARED_RDWR_NULL"
    );
});

isolated_test!(
    tree_open_comparison_failure_closes_owned_fds_and_keeps_c0_api,
    {
        use std::collections::BTreeSet;
        fn inventory() -> BTreeSet<i32> {
            let path = if cfg!(target_os = "linux") {
                "/proc/self/fd"
            } else {
                "/dev/fd"
            };
            let names = std::fs::read_dir(path)
                .unwrap()
                .map(|p| {
                    p.unwrap()
                        .file_name()
                        .to_string_lossy()
                        .parse::<i32>()
                        .unwrap()
                })
                .collect::<BTreeSet<_>>();
            names.into_iter().filter(|fd| flags(*fd) >= 0).collect()
        }
        let base = helper().parent().unwrap().join("tree-open-check");
        std::fs::create_dir(&base).unwrap();
        std::fs::create_dir(base.join("directory")).unwrap();
        std::fs::write(base.join("file"), b"body").unwrap();
        let parent = HeldDir::open_absolute(&base).unwrap();
        for directory in [true, false] {
            let name = OsStr::new(if directory { "directory" } else { "file" });
            let before = parent.metadata(name).unwrap();
            let initial = inventory();
            let mut opened = BTreeSet::new();
            let mut check = || -> Result<(), FsError> {
                opened = inventory().difference(&initial).copied().collect();
                assert_eq!(opened.len(), 1, "TREE_CHECK_RUNS_AFTER_OPEN");
                Err(FsError::Changed)
            };
            if directory {
                assert!(matches!(
                    open_tree_directory(&parent, name, &before, &mut check),
                    Err(FsError::Changed)
                ));
            } else {
                assert!(matches!(
                    open_tree_file(&parent, name, &before, &mut check),
                    Err(FsError::Changed)
                ));
            }
            assert_eq!(inventory(), initial, "TREE_CHECK_CLOSES_ON_ERROR");
            assert!(opened.into_iter().all(|fd| flags(fd) < 0));
            // The original C0 methods still accept precisely the same snapshots.
            if directory {
                let held = parent
                    .open_dir(name, &before, crate::fs::SnapshotMatch::Full)
                    .unwrap();
                assert_eq!(held.snapshot().unwrap(), before);
            } else {
                let held = HeldFile::open_at(&parent, name, &before).unwrap();
                assert_eq!(held.snapshot().unwrap(), before);
            }
            assert_eq!(inventory(), initial, "C0_API_OWNERSHIP_UNCHANGED");
        }
    }
);
