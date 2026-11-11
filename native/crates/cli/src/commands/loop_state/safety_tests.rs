//! S2L 自身の open/read/Git 入口を観測する。C0 の部品検査とは別に数える。
use super::{
    budget::{Budget, Limits},
    config,
    error::{LegacyException as E, Result},
    json::{PyValue as V, Text},
    observe::{self, Event},
    preflight, process, runtime, scan, state,
};
use std::{
    cell::RefCell,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    rc::Rc,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    base: PathBuf,
    root: PathBuf,
    common: PathBuf,
    global: PathBuf,
}
impl Fixture {
    fn at(base: PathBuf) -> Self {
        let root = base.join("repo");
        Self {
            common: root.join(".git"),
            global: base.join("global"),
            root,
            base,
        }
    }
    fn new() -> Self {
        let f = Self::at(
            std::fs::canonicalize(std::env::temp_dir())
                .unwrap()
                .join(format!(
                    "s2l-safety-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                )),
        );
        std::fs::create_dir_all(&f.root).unwrap();
        std::fs::write(&f.global, b"").unwrap();
        f.git(&["-c", "init.defaultBranch=main", "init", "-q"]);
        std::fs::write(f.root.join("ordinary"), b"ordinary body").unwrap();
        f.git(&["add", "ordinary"]);
        f.git(&[
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ]);
        f
    }
    fn git(&self, args: &[&str]) {
        let result = Command::new("git")
            .arg("-C")
            .arg(&self.root)
            .args(process::SAFE_GIT)
            .args(args)
            .env("GIT_CONFIG_GLOBAL", &self.global)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "fixture Git: {}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
    fn baseline(&self) -> (config::Trusted, config::Candidates) {
        let candidates =
            config::candidates(&self.root, &self.common, &self.common, &mut budget()).unwrap();
        let values = candidates.list().unwrap();
        let trusted = values
            .iter()
            .filter(|v| v.get("state").is("present"))
            .map(|v| {
                (
                    v.get("path").string().unwrap().clone(),
                    v.get("entry").clone(),
                )
            })
            .collect();
        let candidates = values
            .iter()
            .map(|v| {
                (
                    (
                        runtime::utf8(v.get("kind").string().unwrap()),
                        v.get("path").string().unwrap().clone(),
                    ),
                    v.clone(),
                )
            })
            .collect();
        (trusted, candidates)
    }
    fn context(&self, trust: &mut config::Trusted, candidates: &config::Candidates) -> Result<()> {
        preflight::context(
            &self.root,
            Some(trust),
            Some(candidates),
            &mut budget(),
            Some(&config::path_text(&self.common)),
            &self.common,
            None,
            false,
            None,
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.base);
    }
}
fn budget() -> Budget {
    Budget::new(Limits {
        items: 100000i128.into(),
        total: 10000000i128.into(),
        one: 1000000i128.into(),
        seconds: 10i128.into(),
    })
    .unwrap()
}
fn isolated(name: &str, body: fn(&Fixture)) {
    if std::env::var("S2L_SAFETY_CHILD").ok().as_deref() == Some(name) {
        body(&Fixture::at(
            std::env::var_os("S2L_SAFETY_FIXTURE").unwrap().into(),
        ));
    } else {
        let f = Fixture::new();
        let mut command = Command::new(std::env::current_exe().unwrap());
        if name.starts_with("yaml_recursion_") || name == "m02_refs_share_budget_and_keep_dangling"
        {
            let resolved=Command::new("python3").args(["-B","-c","import pathlib,yaml;assert yaml.__version__=='6.0.3';print(pathlib.Path(yaml.__file__).resolve().parent.parent)"]).output().unwrap();
            assert!(
                resolved.status.success(),
                "S2L_PYYAML_BASELINE: {}",
                String::from_utf8_lossy(&resolved.stderr)
            );
            command.env(
                "S2L_PYYAML_ROOT",
                String::from_utf8(resolved.stdout).unwrap().trim(),
            );
        }

        command
            .args([
                "--exact",
                &format!("loop_state::safety_tests::{name}"),
                "--nocapture",
            ])
            .env("S2L_SAFETY_CHILD", name)
            .env("S2L_SAFETY_FIXTURE", &f.base)
            .env("GIT_CONFIG_GLOBAL", &f.global)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("HOME", &f.base)
            .env("XDG_CONFIG_HOME", f.base.join("xdg"));
        if name.starts_with("i05_") || name.starts_with("i06_") {
            let bin = f.base.join("bin");
            std::fs::create_dir(&bin).unwrap();
            let git = bin.join("git");
            std::fs::write(&git, b"#!/bin/sh\nexit 71\n").unwrap();
            std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o700)).unwrap();
            command
                .env("PATH", bin)
                .env("GIT_CONFIG_NOSYSTEM", "0")
                .env_remove("GIT_CONFIG_SYSTEM");
            if name.starts_with("i06_") {
                command.env("GIT_CONFIG_SYSTEM", f.base.join("system"));
            }
        }
        if name == "m02_refs_share_budget_and_keep_dangling" {
            // libtest's default 2 MiB worker stack is smaller than the real
            // CLI main stack. Virtual Python limits, not Rust stack exhaustion,
            // must decide the 1000-level refs boundary in this component test.
            command.env("RUST_MIN_STACK", "16777216");
        }
        let out = command.output().unwrap();
        assert!(
            out.status.success(),
            "SAFETY_CHILD {name}: {}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        print!("{}", String::from_utf8_lossy(&out.stdout));
    }
}

fn traced<T>(body: impl FnOnce() -> T) -> (T, Vec<Event>) {
    let events = Rc::new(RefCell::new(Vec::new()));
    let sink = events.clone();
    let guard = observe::install(move |e| {
        sink.borrow_mut().push(e.clone());
        Ok(())
    });
    #[cfg(target_os = "linux")]
    let watch = super::os_watch_tests::Watch::new(Path::new(
        &std::env::var_os("S2L_SAFETY_FIXTURE").unwrap(),
    ));
    let result = body();
    drop(guard);
    let mut events = Rc::try_unwrap(events).unwrap().into_inner();
    #[cfg(target_os = "linux")]
    events.extend(watch.finish());
    (result, events)
}
fn opened(events: &[Event], name: &str) -> usize {
    let attempts = events
        .iter()
        .filter(|e| matches!(e,Event::Open{name:n,..}if n==name))
        .count();
    #[cfg(target_os = "linux")]
    let attempts = attempts.max(
        events
            .iter()
            .filter_map(|e| match e {
                Event::OsAccess { path, mask }
                    if path.file_name() == Some(std::ffi::OsStr::new(name)) && mask & 0x21 != 0 =>
                {
                    Some(path)
                }
                _ => None,
            })
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
    );
    attempts
}
fn no_git(events: &[Event]) {
    assert!(
        !events.iter().any(|e| matches!(e, Event::Spawn { .. })),
        "SAFETY_NO_GIT"
    );
}
fn row<'a>(rows: &'a [V], path: &str) -> &'a V {
    rows.iter().find(|v| v.get("path").is(path)).unwrap()
}
#[test]
fn i01_secret_globs_never_open_body() {
    isolated("i01_secret_globs_never_open_body", |f| {
        for name in ["private.txt", "PRIVATE.txt", "ordinary"] {
            std::fs::write(f.root.join(name), b"owned sentinel").unwrap()
        }
        let (rows, events) = traced(|| {
            scan::tree(&f.root, &[Text::from("private.txt")], &mut budget(), None).unwrap()
        });
        for name in ["private.txt", "PRIVATE.txt"] {
            assert!(row(&rows, name).get("secret").truth(), "I01_SECRET");
            assert_eq!(opened(&events, name), 0, "I01_NO_OPEN");
            assert!(matches!(row(&rows, name).get("sha256"), V::Null));
        }
        assert_eq!(opened(&events, "ordinary"), 1, "I01_CONTROL_OPEN");
        assert!(
            events
                .iter()
                .any(|e| matches!(e,Event::Read{count,..}if *count>0)),
            "I01_CONTROL_READ"
        );
    });
}
#[test]
fn i02_double_star_and_secret_children_metadata() {
    isolated("i02_double_star_and_secret_children_metadata", |f| {
        std::fs::create_dir(f.root.join("secrets")).unwrap();
        std::fs::write(f.root.join("key.txt"), b"one").unwrap();
        std::fs::write(f.root.join("secrets/child"), b"a").unwrap();
        let patterns = [Text::from("**/key.txt"), Text::from("secrets/")];
        let (first, events) =
            traced(|| scan::tree(&f.root, &patterns, &mut budget(), None).unwrap());
        assert!(
            row(&first, "key.txt").get("secret").truth(),
            "I02_ZERO_LEVEL"
        );
        assert!(
            row(&first, "secrets/child").get("secret").truth(),
            "I02_CHILD_ENUMERATION"
        );
        assert_eq!(
            opened(&events, "key.txt") + opened(&events, "child"),
            0,
            "I02_NO_OPEN"
        );
        std::fs::write(f.root.join("secrets/child"), b"longer").unwrap();
        let second = scan::tree(&f.root, &patterns, &mut budget(), None).unwrap();
        assert_ne!(
            row(&first, "secrets/child"),
            row(&second, "secrets/child"),
            "I02_CHILD_METADATA"
        );
    });
}
#[test]
fn i03_git_and_empty_names_share_consumption() {
    isolated("i03_git_and_empty_names_share_consumption", |f| {
        let mut full = budget();
        let (raw, events) = traced(|| {
            process::git(
                f.root.as_os_str(),
                &process::strings(&["rev-parse", "HEAD"]),
                &mut full,
            )
            .unwrap()
        });
        assert_eq!(raw.len(), 41);
        assert!(
            events
                .iter()
                .any(|e| matches!(e,Event::Spawn{top,index:false,..}if top==f.root.as_os_str())),
            "I03_REAL_GIT"
        );
        // 出力予算を超えた時点で確実に生存する直接子を使う。
        let bin = f.base.join("output-blocking-bin");
        std::fs::create_dir(&bin).unwrap();
        let python = Command::new("python3")
            .args(["-c", "import sys;print(sys.executable)"])
            .output()
            .unwrap();
        let python = String::from_utf8(python.stdout).unwrap();
        let ready = f.base.join("output-ready");
        let script = format!(
            "#!{}\nimport os,signal\nopen({:?},'w').write(str(os.getpid()))\nos.write(1,b'x'*8192)\nsignal.pause()\n",
            python.trim(),
            ready.to_str().unwrap()
        );
        let git = bin.join("git");
        std::fs::write(&git, script).unwrap();
        std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o700)).unwrap();
        let events = Rc::new(RefCell::new(vec![]));
        let sink = events.clone();
        let guard = observe::install(move |event| {
            sink.borrow_mut().push(event.clone());
            if let Event::Spawned { pid, index: false } = event {
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                while std::fs::read_to_string(&ready).ok().as_deref() != Some(&pid.to_string()) {
                    assert!(std::time::Instant::now() < deadline, "I03_READY_TIMEOUT");
                    std::thread::yield_now();
                }
            }
            Ok(())
        });
        let mut small = budget();
        small.limits.total = 1i128.into();
        small.read(1).unwrap();
        let err = process::run(
            f.root.as_os_str(),
            &[],
            &[],
            &mut small,
            false,
            &[("PATH".into(), bin.into_os_string())],
        )
        .unwrap_err();
        assert_eq!(
            err.stop_reason(),
            Some("状態の観察の総読取量が上限を超えた")
        );
        assert_eq!(small.bytes, 1, "I03_CHUNK_UNCHARGED");
        drop(guard);
        let returned = events
            .borrow()
            .iter()
            .filter_map(|event| match event {
                Event::Returned {
                    alive,
                    stdout_closed,
                    ..
                } => Some((*alive, *stdout_closed)),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert_eq!(returned, vec![(false, true)], "I03_REAPED_AND_CLOSED");
        let names_root = f.base.join("names-only");
        std::fs::create_dir(&names_root).unwrap();
        for n in 0..12 {
            std::fs::create_dir(names_root.join(format!("empty-{n}"))).unwrap()
        }
        let held = state::directory(&names_root).unwrap();
        let mut exact = budget();
        exact.limits.items = 12i128.into();
        assert_eq!(
            scan::names(&held, &mut exact, "empty names").unwrap().len(),
            12,
            "I03_NAMES_CONTROL"
        );
        assert_eq!(exact.count, 12, "I03_NAMES_EXACT_CHARGE");
        let mut short = budget();
        short.limits.items = 11i128.into();
        assert!(
            scan::names(&held, &mut short, "empty names").is_err(),
            "I03_NAMES_BEFORE_DESCENT"
        );
        for n in 0..12 {
            std::fs::create_dir(f.root.join(format!("empty-{n}"))).unwrap()
        }
        let mut small = budget();
        small.limits.items = 5i128.into();
        assert!(
            scan::tree(&f.root, &[], &mut small, None).is_err(),
            "I03_NAMES_CHARGED"
        );
        scan::tree(&f.root, &[], &mut budget(), None).unwrap();
    });
}
#[test]
fn i04_secret_symlink_ignores_target_and_text() {
    isolated("i04_secret_symlink_ignores_target_and_text", |f| {
        let target = f.base.join("sentinel");
        std::fs::write(&target, b"one").unwrap();
        symlink(&target, f.root.join("private.link")).unwrap();
        let patterns = [Text::from("private.link")];
        let (first, events) =
            traced(|| scan::tree(&f.root, &patterns, &mut budget(), None).unwrap());
        assert_eq!(
            opened(&events, "sentinel") + opened(&events, "private.link"),
            0,
            "I04_NO_TARGET_OPEN"
        );
        assert!(
            matches!(row(&first, "private.link").get("target"), V::Null),
            "I04_NO_LINK_TEXT"
        );
        std::fs::write(&target, b"changed target").unwrap();
        assert_eq!(
            row(&first, "private.link"),
            row(
                &scan::tree(&f.root, &patterns, &mut budget(), None).unwrap(),
                "private.link"
            ),
            "I04_TARGET_INDEPENDENT"
        );
    });
}
#[test]
fn i07_read_oserror_is_not_promoted_to_stop() {
    isolated("i07_read_oserror_is_not_promoted_to_stop", |f| {
        let paths = (0..3)
            .map(|n| {
                let p = f.base.join(format!("state-{n}"));
                std::fs::write(&p, b"raw state").unwrap();
                p
            })
            .collect::<Vec<_>>();
        let runtime = runtime::LegacyRuntime::from_environment().unwrap();
        for phase in ["open", "read"] {
            for failed in 0..3 {
                let failed_name = paths[failed].file_name().unwrap().to_owned();
                let meta = std::fs::metadata(&paths[failed]).unwrap();
                let identity = (meta.dev() as i128, meta.ino() as i128);
                let guard = observe::install(move |e| {
                    let fail = match e {
                        Event::Open {
                            parent,
                            name,
                            before,
                        } => {
                            assert!(
                                parent.is_directory() && before.is_regular(),
                                "I07_REAL_OPEN"
                            );
                            phase == "open" && name == &failed_name
                        }
                        Event::Read { file, count } => {
                            phase == "read" && *count > 0 && (file.dev, file.ino) == identity
                        }
                        _ => false,
                    };
                    if fail {
                        Err(E::from(std::io::Error::from_raw_os_error(
                            if phase == "open" { 13 } else { 5 },
                        )))
                    } else {
                        Ok(())
                    }
                });
                let argv = std::iter::once(std::ffi::OsString::from("state-digests"))
                    .chain(paths.iter().flat_map(|p| {
                        [
                            std::ffi::OsString::from("--state"),
                            p.as_os_str().to_owned(),
                        ]
                    }))
                    .collect::<Vec<_>>();
                let arguments =
                    super::args::parse(&argv, 4300).unwrap_or_else(|_| panic!("I07_ARGUMENTS"));
                let mut out = vec![];
                let error = super::run(&arguments, &runtime, &mut out, &mut None).unwrap_err();
                assert_eq!(
                    error.stop_reason(),
                    Some("状態を読めない"),
                    "I07_FIXED_DIAGNOSTIC"
                );
                let digest = state::hash(b"raw state");
                let expected = (0..3)
                    .map(|n| {
                        if n == failed {
                            "-\n".into()
                        } else {
                            format!("{digest}\n")
                        }
                    })
                    .collect::<String>();
                assert_eq!(
                    out,
                    expected.as_bytes(),
                    "I07_CONTINUE_IN_INPUT_ORDER: {phase}/{failed}"
                );
                drop(guard);
            }
        }
        for path in paths {
            assert_eq!(
                state::digest(&path, &budget().limits).unwrap(),
                state::hash(b"raw state"),
                "I07_CONTROL"
            );
        }
    });
}
#[test]
fn i08_growth_and_lstat_open_replacement_rejected() {
    isolated("i08_growth_and_lstat_open_replacement_rejected", |f| {
        let path = f.base.join("state");
        std::fs::write(&path, b"initial").unwrap();
        let change = path.clone();
        let mut changed = false;
        let guard = observe::install(move |e| {
            if matches!(e,Event::Read{count,..}if *count>0) && !changed {
                use std::io::Write;
                std::fs::OpenOptions::new()
                    .append(true)
                    .open(&change)
                    .unwrap()
                    .write_all(b"growth")
                    .unwrap();
                changed = true;
            }
            Ok(())
        });
        assert!(
            state::read_bytes(&path, &budget().limits).is_err(),
            "I08_GROWTH"
        );
        drop(guard);
        state::read_bytes(&path, &budget().limits).unwrap();
        let change = path.clone();
        let target = f.base.join("other");
        std::fs::write(&target, b"sentinel").unwrap();
        let guard = observe::install(move |e| {
            if matches!(e,Event::Open{name,..}if name=="state") {
                std::fs::remove_file(&change).unwrap();
                symlink(&target, &change).unwrap();
            }
            Ok(())
        });
        assert!(
            state::read_bytes(&path, &budget().limits).is_err(),
            "I08_REPLACEMENT"
        );
        drop(guard);
    });
}
#[test]
fn m13_state_total_bound_checked_before_open_and_growth() {
    isolated(
        "m13_state_total_bound_checked_before_open_and_growth",
        |f| {
            let path = f.base.join("state");
            std::fs::write(&path, b"12345678").unwrap();
            let mut limits = budget().limits;
            limits.total = 7i128.into();
            let (result, events) = traced(|| state::read_bytes(&path, &limits));
            assert_eq!(
                result.unwrap_err().stop_reason(),
                Some("状態の読取量が上限を超えた")
            );
            assert_eq!(opened(&events, "state"), 0, "M13_BEFORE_OPEN");
            limits.total = 8i128.into();
            limits.one = 1i128.into();
            let (result, events) = traced(|| state::read_bytes(&path, &limits));
            assert_eq!(result.unwrap(), b"12345678", "M13_STATE_ONE_MAX_TOTAL");
            assert_eq!(opened(&events, "state"), 1, "M13_CONTROL_OPEN");
        },
    );
}
#[test]
fn m03_global_link_switch_rejected_before_git() {
    isolated("m03_global_link_switch_rejected_before_git", |f| {
        std::fs::remove_file(&f.global).unwrap();
        let old = f.base.join("old-global");
        std::fs::write(&old, b"").unwrap();
        symlink(&old, &f.global).unwrap();
        let (mut trust, candidates) = f.baseline();
        let target = f.base.join("new-global");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        std::fs::remove_file(&f.global).unwrap();
        symlink(&target, &f.global).unwrap();
        let (result, events) = traced(|| f.context(&mut trust, &candidates));
        assert!(result.is_err(), "M03_REJECT");
        assert_eq!(opened(&events, "new-global"), 0, "M03_NO_TARGET_OPEN");
        no_git(&events);
    });
}
#[test]
fn m04_changed_include_rejected_before_any_parser() {
    isolated("m04_changed_include_rejected_before_any_parser", |f| {
        let (mut trust, candidates) = f.baseline();
        let target = f.base.join("unknown");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(f.common.join("config"))
            .unwrap()
            .write_all(format!("\n[include]\npath={}\n", target.display()).as_bytes())
            .unwrap();
        let (result, events) = traced(|| f.context(&mut trust, &candidates));
        assert!(
            result.err().is_some_and(
                |e| e.stop_reason() == Some("保持済み config がイテレーションの途中で変わった")
            ),
            "M04_REJECT"
        );
        assert_eq!(opened(&events, "unknown"), 0, "M04_NO_TARGET_OPEN");
        no_git(&events);
    });
}
#[test]
fn m07_changed_git_pointer_never_opens_new_admin() {
    isolated("m07_changed_git_pointer_never_opens_new_admin", |f| {
        let root = f.base.join("linked");
        std::fs::create_dir(&root).unwrap();
        let admin = f.base.join("admin");
        std::fs::create_dir(&admin).unwrap();
        std::fs::write(root.join(".git"), format!("gitdir: {}\n", admin.display())).unwrap();
        let expected = f.common.clone();
        let (result, events) = traced(|| {
            preflight::context(
                &root,
                Some(&mut vec![]),
                Some(&vec![]),
                &mut budget(),
                Some(&config::path_text(&expected)),
                &f.common,
                None,
                false,
                None,
            )
        });
        assert_eq!(
            result.unwrap_err().stop_reason(),
            Some("worktree の git-dir が基準から変わった")
        );
        assert_eq!(opened(&events, "HEAD"), 0, "M07_NO_HEAD");
        no_directory(&events, &admin);
        no_git(&events);
    });
}
#[test]
fn m08_changed_commondir_rejected_before_git() {
    isolated("m08_changed_commondir_rejected_before_git", |f| {
        let target = f.base.join("new-common");
        std::fs::create_dir(&target).unwrap();
        std::fs::write(
            f.common.join("commondir"),
            format!("{}\n", target.display()),
        )
        .unwrap();
        let (result, events) =
            traced(|| preflight::common_dir(&f.root, &f.common, &f.common, &mut budget()));
        assert_eq!(
            result.unwrap_err().stop_reason(),
            Some("worktree の commondir が基準から変わった")
        );
        no_git(&events);
        no_directory(&events, &target);
        assert_eq!(opened(&events, "config"), 0, "M08_NO_NEW_CONFIG");
    });
}
#[test]
fn m09_new_config_layer_rejected_before_link_target() {
    isolated("m09_new_config_layer_rejected_before_link_target", |f| {
        let (mut trust, candidates) = f.baseline();
        f.git(&["config", "extensions.worktreeConfig", "true"]);
        let target = f.base.join("layer-secret");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        symlink(&target, f.common.join("config.worktree")).unwrap();
        let (result, events) = traced(|| f.context(&mut trust, &candidates));
        assert!(result.is_err(), "M09_REJECT");
        assert_eq!(opened(&events, "layer-secret"), 0, "M09_NO_TARGET_OPEN");
        no_git(&events);
    });
}
#[test]
fn m10_absent_global_appearance_rejected_before_git() {
    isolated("m10_absent_global_appearance_rejected_before_git", |f| {
        std::fs::remove_file(&f.global).unwrap();
        let (mut trust, candidates) = f.baseline();
        let target = f.base.join("global-secret");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        symlink(&target, &f.global).unwrap();
        let (result, events) = traced(|| f.context(&mut trust, &candidates));
        assert!(
            result.err().is_some_and(
                |e| e.stop_reason() == Some("config 候補がイテレーションの途中で変わった")
            ),
            "M10_REJECT"
        );
        assert_eq!(opened(&events, "global-secret"), 0, "M10_NO_TARGET_OPEN");
        no_git(&events);
    });
}
#[test]
fn m11_empty_global_include_change_rejected() {
    isolated("m11_empty_global_include_change_rejected", |f| {
        let (mut trust, candidates) = f.baseline();
        let target = f.base.join("new-include");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        std::fs::write(&f.global, format!("[include]\npath={}\n", target.display())).unwrap();
        let (result, events) = traced(|| f.context(&mut trust, &candidates));
        assert!(result.is_err(), "M11_REJECT");
        assert_eq!(opened(&events, "new-include"), 0, "M11_NO_TARGET_OPEN");
        no_git(&events);
    });
}
#[test]
fn l01_profile_parsing_starts_after_held_files_close() {
    isolated("l01_profile_parsing_starts_after_held_files_close", |f| {
        std::fs::create_dir(f.root.join(".claude")).unwrap();
        let profile = f.root.join(".claude/project-profile.yml");
        std::fs::write(&profile, b"secret_paths: [private]\n").unwrap();
        let identities = [&f.root, &f.root.join(".claude"), &profile].map(|p| {
            let m = std::fs::metadata(p).unwrap();
            (m.dev(), m.ino())
        });
        let reached = Rc::new(RefCell::new(0));
        let count = reached.clone();
        let guard = observe::install(move |e| {
            if matches!(e, Event::ProfileParse) {
                *count.borrow_mut() += 1;
                #[cfg(target_os = "linux")]
                let directory = "/proc/self/fd";
                #[cfg(target_os = "macos")]
                let directory = "/dev/fd";
                for entry in std::fs::read_dir(directory).unwrap() {
                    if let Ok(meta) = std::fs::metadata(entry.unwrap().path()) {
                        assert!(
                            !identities.contains(&(meta.dev(), meta.ino())),
                            "L01_PROFILE_FD_NOT_CLOSED"
                        );
                    }
                }
            }
            Ok(())
        });
        scan::profile(&f.root, &mut budget(), 4300).unwrap();
        assert_eq!(*reached.borrow(), 1, "L01_PARSER_REACHED");
        std::fs::write(&profile, b"secret_paths: [unterminated\n").unwrap();
        assert!(scan::profile(&f.root, &mut budget(), 4300).is_err());
        assert_eq!(*reached.borrow(), 2, "L01_REJECTION_PARSER_REACHED");
        drop(guard);
    });
}
#[test]
fn l05_run_expired_after_spawn_reaps_direct_child() {
    isolated("l05_run_expired_after_spawn_reaps_direct_child", |f| {
        let bin = f.base.join("blocking-bin");
        std::fs::create_dir(&bin).unwrap();
        let python = Command::new("python3")
            .args(["-c", "import sys;print(sys.executable)"])
            .output()
            .unwrap();
        let python = String::from_utf8(python.stdout).unwrap();
        let ready = f.base.join("ready");
        let script = format!(
            "#!{}\nimport os,signal\nopen({:?},'w').write(str(os.getpid()))\nsignal.pause()\n",
            python.trim(),
            ready.to_str().unwrap()
        );
        let git = bin.join("git");
        std::fs::write(&git, script).unwrap();
        std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o700)).unwrap();
        let events = Rc::new(RefCell::new(vec![]));
        let sink = events.clone();
        let ready_hook = ready.clone();
        let guard = observe::install(move |event| {
            sink.borrow_mut().push(event.clone());
            if let Event::Spawned { pid, index: false } = event {
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                while std::fs::read_to_string(&ready_hook).ok().as_deref() != Some(&pid.to_string())
                {
                    assert!(std::time::Instant::now() < deadline, "L05_READY_TIMEOUT");
                    std::thread::yield_now();
                }
                assert_eq!(
                    std::fs::read_to_string(&ready_hook).unwrap(),
                    pid.to_string(),
                    "L05_LIVE_DIRECT_PID"
                );
            }
            Ok(())
        });
        let mut b = budget();
        b.expire();
        let error = process::run(
            f.root.as_os_str(),
            &[],
            &[],
            &mut b,
            false,
            &[("PATH".into(), bin.into_os_string())],
        )
        .unwrap_err();
        assert_eq!(
            error.stop_reason(),
            Some("git の状態読取が時間内に終わらない"),
            "L05_REASON"
        );
        drop(guard);
        let events = events.borrow();
        let returned = events
            .iter()
            .filter_map(|e| {
                if let Event::Returned {
                    pid,
                    index,
                    alive,
                    stdout_closed,
                } = e
                {
                    Some((*pid, *index, *alive, *stdout_closed))
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        assert_eq!(returned.len(), 1, "L05_RETURN_BOUNDARY");
        for (pid, index, alive, closed) in returned {
            assert!(!index && !alive && closed, "L05_REAPED_AND_CLOSED");
            assert!(
                !Command::new("kill")
                    .args(["-0", &pid.to_string()])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .unwrap()
                    .success(),
                "L05_DIRECT_CHILD_ABSENT"
            );
        }
    });
}
#[test]
fn l06_index_expired_before_spawn() {
    isolated("l06_index_expired_before_spawn", |f| {
        let mut b = budget();
        let (result, control) = traced(|| process::index(f.root.as_os_str(), &b));
        assert!(result.unwrap(), "L06_CONTROL_CLEAN");
        assert!(
            control
                .iter()
                .any(|e| matches!(e, Event::Spawned { index: true, .. })),
            "L06_CONTROL_SPAWN"
        );
        b.expire();
        let (result, events) = traced(|| process::index(f.root.as_os_str(), &b));
        assert_eq!(
            result.unwrap_err().stop_reason(),
            Some("状態の観察が 60 秒の上限を超えた")
        );
        no_git(&events);
    });
}
#[test]
fn i09_ancestor_sibling_allowed_inode_change_rejected() {
    isolated("i09_ancestor_sibling_allowed_inode_change_rejected", |f| {
        let ancestor = f.base.join("ancestor");
        let nested = ancestor.join("nested");
        std::fs::create_dir_all(&nested).unwrap();
        let target = ancestor.clone();
        let sibling = ancestor.join("sibling");
        let guard = observe::install(move |e| {
            if matches!(e,Event::Directory{path,..}if path==&target) {
                std::fs::write(&sibling, b"sibling").unwrap();
                std::fs::File::open(&target)
                    .unwrap()
                    .set_times(std::fs::FileTimes::new().set_modified(std::time::UNIX_EPOCH))
                    .unwrap();
            }
            Ok(())
        });
        state::directory(&nested).unwrap_or_else(|_| panic!("I09_SIBLING_ALLOWED"));
        drop(guard);
        let target = nested.clone();
        let moved = ancestor.join("old");
        let guard = observe::install(move |e| {
            if let Event::Directory { path, before } = e {
                if path == &target {
                    assert!(before.is_directory(), "I09_REAL_DIRECTORY");
                    std::fs::rename(&target, &moved).unwrap();
                    std::fs::create_dir(&target).unwrap();
                }
            }
            Ok(())
        });
        assert_eq!(
            state::directory(&nested).unwrap_err().stop_reason(),
            Some("親ディレクトリが観察中に差し替わった"),
            "I09_REPLACED"
        );
        drop(guard);
    });
}
#[test]
fn i05_unavailable_default_system_path_requires_explicit_setting() {
    isolated(
        "i05_unavailable_default_system_path_requires_explicit_setting",
        |f| {
            let (result, events) =
                traced(|| config::candidates_paths(&f.common, &f.common, &mut budget(), None));
            assert!(
                result
                    .unwrap_err()
                    .stop_reason()
                    .unwrap()
                    .contains("GIT_CONFIG_SYSTEM"),
                "I05_REQUIRED_EXPLICIT"
            );
            assert!(events.iter().any(|e|matches!(e,Event::Spawn{arguments,..}if arguments==&process::strings(&["var","GIT_CONFIG_SYSTEM"]))),"I05_SPECIFIC_QUERY");
        },
    );
}
#[test]
fn i06_explicit_system_path_never_queries_git() {
    isolated("i06_explicit_system_path_never_queries_git", |f| {
        let (result, events) =
            traced(|| config::candidates_paths(&f.common, &f.common, &mut budget(), None));
        assert!(
            result
                .unwrap()
                .contains(&("system".into(), config::path_text(&f.base.join("system")))),
            "I06_EXPLICIT_PATH"
        );
        no_git(&events);
    });
}
#[test]
fn m01_new_owned_candidate_symlink_never_opened() {
    isolated("m01_new_owned_candidate_symlink_never_opened", |f| {
        let (_, expected) = f.baseline();
        let admin = f.base.join("new-admin");
        std::fs::create_dir(&admin).unwrap();
        let target = f.base.join("owned-secret");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        let control = preflight::candidate_preflight(
            &f.root,
            &f.common,
            &admin,
            &expected,
            &mut budget(),
            true,
            None,
        )
        .unwrap();
        assert!(control.is_empty(), "M01_ABSENT_CONTROL");
        symlink(&target, admin.join("config.worktree")).unwrap();
        let (result, events) = traced(|| {
            preflight::candidate_preflight(
                &f.root,
                &f.common,
                &admin,
                &expected,
                &mut budget(),
                true,
                None,
            )
        });
        assert!(
            result
                .err()
                .is_some_and(|e| e.stop_reason().unwrap().contains("config 候補")),
            "M01_REJECT"
        );
        assert_eq!(opened(&events, "owned-secret"), 0, "M01_NO_OPEN");
        no_git(&events);
        let (_, control) = traced(|| scan::entry(&target, "target", &mut budget()).unwrap());
        assert_eq!(opened(&control, "owned-secret"), 1, "M01_MONITOR_CONTROL");
    });
}
#[test]
fn m02_refs_share_budget_and_keep_dangling() {
    isolated("m02_refs_share_budget_and_keep_dangling", |f| {
        f.git(&["symbolic-ref", "refs/heads/dangling", "refs/heads/absent"]);
        let mut small = budget();
        small.limits.items = 1i128.into();
        assert!(
            super::snapshot::refs(&f.root, &mut small).is_err(),
            "M02_SMALL_LIMIT"
        );
        let mut b = budget();
        b.tick(0).unwrap();
        let id = &b as *const Budget as usize;
        let (rows, events) = traced(|| super::snapshot::refs(&f.root, &mut b).unwrap());
        assert!(
            rows.iter().any(|r| r.get("name").is("refs/heads/dangling")),
            "M02_DANGLING"
        );
        // Real S2L loose-ref recursion must release every held ancestor.
        // Fixture setup uses Python dir_fd operations, never an overlong
        // absolute pathname (Darwin PATH_MAX is smaller than this tree).
        struct Deep {
            root: PathBuf,
            held: PathBuf,
        }
        impl Deep {
            fn action(&self, action: &str, depth: usize, leaf: bool) {
                let script = r#"import pathlib,sys
sys.path.insert(0,sys.argv[1])
from loop_state_deep_fixture import DeepTree
root,held,action,depth,leaf=sys.argv[2:];depth=int(depth);leaf=leaf=='true'
if action=='create':DeepTree(pathlib.Path(root),995)
else:
 t=DeepTree.__new__(DeepTree);t.root=pathlib.Path(root);t.depth=995
 if action=='cut':
  t.detach(depth,held)
  if leaf:t.leaf(depth,b'ref: refs/heads/absent\n')
 elif action=='restore':
  if leaf:t.leaf(depth,None)
  t.restore(depth,held)
 else:t.close()
"#;
                let ci = Path::new(env!("CARGO_MANIFEST_DIR"))
                    .ancestors()
                    .nth(2)
                    .unwrap()
                    .join("ci");
                let result = Command::new("python3")
                    .args(["-B", "-c", script])
                    .arg(ci)
                    .arg(&self.root)
                    .arg(&self.held)
                    .arg(action)
                    .arg(depth.to_string())
                    .arg(leaf.to_string())
                    .output()
                    .unwrap();
                assert!(
                    result.status.success(),
                    "M02_DEEP_FIXTURE: {}",
                    String::from_utf8_lossy(&result.stderr)
                );
            }
        }
        impl Drop for Deep {
            fn drop(&mut self) {
                self.action("close", 0, false);
            }
        }
        let deep = Deep {
            root: f.common.join("refs"),
            held: f.base.join("held-tail"),
        };
        deep.action("create", 0, false);
        let descriptors = || {
            std::fs::read_dir(if cfg!(target_os = "linux") {
                "/proc/self/fd"
            } else {
                "/dev/fd"
            })
            .unwrap()
            .count()
        };
        for (depth, leaf, success) in [(992, false, true), (993, false, false), (992, true, false)]
        {
            deep.action("cut", depth, leaf);
            let before = descriptors();
            let result = super::snapshot::refs(&f.root, &mut budget());
            let after = descriptors();
            // Same module/main/snapshot/refs context. Darwin's actual fd limit
            // may refuse before recursion; never count that as boundary success.
            let script = r#"import importlib.util,json,linecache,os,sys,traceback
sys.path.insert(0,sys.argv[1])
import yaml
assert yaml.__version__=='6.0.3'
s=importlib.util.spec_from_file_location('s2l_old_refs',sys.argv[2]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def snapshot():return m.refs(sys.argv[3],m.Budget(100000,10000000,1000000,10))
def main():return snapshot()
initial_fds=len(os.listdir('/proc/self/fd'if sys.platform.startswith('linux')else'/dev/fd'))
try:main()
except BaseException as e:value={'kind':type(e).__name__,'reason':str(e),'frames':[(t.name,t.lineno,linecache.getline(t.filename,t.lineno).strip())for t in traceback.extract_tb(e.__traceback__)if t.filename==sys.argv[2]]}
else:value={'kind':'ok'}
value['initial_fds']=initial_fds;print(json.dumps(value))
"#;
            let old = Path::new(env!("CARGO_MANIFEST_DIR"))
                .ancestors()
                .nth(3)
                .unwrap()
                .join("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py");
            let observed = Command::new("python3")
                .args(["-B", "-c", script])
                .arg(std::env::var_os("S2L_PYYAML_ROOT").unwrap())
                .arg(old)
                .arg(&f.root)
                .output()
                .unwrap();
            deep.action("restore", depth, leaf);
            assert!(
                observed.status.success(),
                "M02_OLD_REFS_OBSERVATION: {}",
                String::from_utf8_lossy(&observed.stderr)
            );
            assert_eq!(before, after, "M02_RECURSION_FDS_CLOSED");
            let old: serde_json::Value = serde_json::from_slice(&observed.stdout).unwrap();
            // 旧処理系の fd の使い方(起動時の数)は比較の前提の外(2026-11-12 00:42 の決定)。
            // 数は記録だけにし、旧新の一致を求めない。
            println!(
                "M02_FD_BASELINES: rust={} old={} outcome={}",
                before, old["initial_fds"], old["kind"]
            );
            if old["kind"] == "ok" {
                assert!(result.is_ok(), "M02_OLD_NEW_ACCEPT");
            } else {
                let error = result.as_ref().unwrap_err();
                let rendered = super::error::render(error);
                assert!(
                    rendered.ends_with(&format!(
                        "{}: {}\n",
                        old["kind"].as_str().unwrap(),
                        old["reason"].as_str().unwrap()
                    )),
                    "M02_OLD_NEW_REFUSAL"
                );
                fn frames(error: &E, out: &mut Vec<serde_json::Value>) {
                    if let E::Context { error, .. } = error {
                        frames(error, out);
                    }
                    if let E::Traced {
                        error,
                        frames: rows,
                    } = error
                    {
                        for frame in rows.iter().rev() {
                            out.push(serde_json::json!([
                                frame.function,
                                frame.line,
                                frame.source
                            ]));
                        }
                        frames(error, out);
                    }
                }
                let mut actual = Vec::new();
                frames(error, &mut actual);
                assert_eq!(
                    serde_json::json!(actual),
                    old["frames"],
                    "M02_OLD_NEW_REFUSAL_FRAMES"
                );
            }
            if old["kind"] != "ok" && old["kind"] != "RecursionError" {
                println!("M02_OS_REFUSAL_BOUNDARY_NOT_REACHED: {}", old["kind"]);
            } else if cfg!(target_os = "linux") {
                assert_eq!(result.is_ok(), success, "M02_REFS_RECURSION_BOUNDARY");
                if !success {
                    assert!(
                        super::error::render(&result.unwrap_err()).contains("RecursionError:"),
                        "M02_REFS_RECURSION_KIND"
                    );
                }
            }
        }
        for e in events {
            if let Event::Budget { address, count, .. } = e {
                assert_eq!(address, id, "M02_BUDGET_ID");
                assert!(count >= 1, "M02_CONSUMPTION");
            }
        }
    });
}
#[test]
fn m12_link_fixed_during_run_and_refresh_reads_new_baseline() {
    isolated(
        "m12_link_fixed_during_run_and_refresh_reads_new_baseline",
        |f| {
            let first = f.base.join("first.inc");
            let second = f.base.join("second.inc");
            std::fs::write(&first, b"[demo]\nvalue=first\n").unwrap();
            std::fs::write(&second, b"[demo]\nvalue=second\n").unwrap();
            let link = f.base.join("config-link");
            symlink(&first, &link).unwrap();
            let saved = config::safe(&f.root, &link, &mut budget(), None, false, false)
                .unwrap()
                .0;
            let trust = vec![(config::path_text(&link), saved.clone())];
            std::fs::remove_file(&link).unwrap();
            symlink(&second, &link).unwrap();
            let (result, events) =
                traced(|| config::safe(&f.root, &link, &mut budget(), Some(&trust), true, false));
            assert!(result.is_err(), "M12_LINK_FIXED");
            assert_eq!(opened(&events, "second.inc"), 0, "M12_NO_NEW_TARGET");
            let (fresh, events) = traced(|| {
                config::safe(&f.root, &link, &mut budget(), None, false, false)
                    .unwrap()
                    .0
            });
            assert_ne!(fresh, saved, "M12_FRESH_NEW_BASELINE");
            assert_eq!(opened(&events, "second.inc"), 1, "M12_FRESH_REAL_READ");
            for is_link in [false, true] {
                let (mut trust, candidates) = f.baseline();
                let unknown = f
                    .base
                    .join(if is_link { "new-link" } else { "new-regular" });
                if is_link {
                    symlink(&second, &unknown).unwrap()
                } else {
                    std::fs::write(&unknown, b"[demo]\nvalue=unknown\n").unwrap()
                }
                std::fs::write(
                    &f.global,
                    format!("[include]\npath={}\n", unknown.display()),
                )
                .unwrap();
                let (result, events) = traced(|| f.context(&mut trust, &candidates));
                assert!(result.is_err(), "M12_CHANGED_ROOT");
                assert_eq!(
                    opened(&events, unknown.file_name().unwrap().to_str().unwrap()),
                    0,
                    "M12_NO_UNKNOWN_BODY"
                );
                no_git(&events);
                std::fs::write(&f.global, b"").unwrap();
            }
        },
    );
}
struct ContextOperations {
    source: PathBuf,
    target: PathBuf,
    condition_calls: Rc<RefCell<Vec<PathBuf>>>,
}
impl preflight::Operations for ContextOperations {
    fn file(
        &mut self,
        top: &Path,
        path: &Text,
        b: &mut Budget,
        trusted: &config::Trusted,
        parse: bool,
    ) -> Result<(V, config::Rows)> {
        let path = &PathBuf::from(runtime::os(path)?);
        let (item, _) = config::safe(top, path, b, Some(trusted), true, false)?;
        let rows = if parse && path == &self.source {
            vec![(
                Text::from("includeif.onbranch:human.path"),
                Some(Text::from(self.target.to_str().unwrap())),
            )]
        } else {
            vec![]
        };
        Ok((item, rows))
    }
    fn include(
        &mut self,
        _: &Path,
        value: Option<&Text>,
        _: &Text,
        b: &mut Budget,
    ) -> Result<Text> {
        b.tick(0)?;
        Ok(value.unwrap().clone())
    }
    fn condition(
        &mut self,
        top: &Path,
        expected: &Path,
        common: &Path,
        _: &Text,
        _: &Path,
        _: &config::Rows,
        b: &mut Budget,
        _: Option<&Text>,
    ) -> Result<Option<bool>> {
        self.condition_calls.borrow_mut().push(top.into());
        Ok(Some(
            preflight::branch(top, expected, common, b)?
                .is_some_and(|branch| branch == Text::from("refs/heads/human")),
        ))
    }
}
#[test]
fn m05_each_worktree_uses_its_own_branch_context() {
    isolated("m05_each_worktree_uses_its_own_branch_context", |f| {
        let linked = f.base.join("human");
        f.git(&[
            "worktree",
            "add",
            "-qb",
            "human",
            linked.to_str().unwrap(),
            "HEAD",
        ]);
        let admin = preflight::git_dir(&linked, &mut budget()).unwrap();
        let (mut trust, candidates) = f.baseline();
        let linked_candidates = config::candidates(&linked, &f.common, &admin, &mut budget())
            .unwrap()
            .list()
            .unwrap()
            .iter()
            .map(|v| {
                (
                    (
                        runtime::utf8(v.get("kind").string().unwrap()),
                        v.get("path").string().unwrap().clone(),
                    ),
                    v.clone(),
                )
            })
            .collect::<config::Candidates>();
        let target = f.base.join("unknown-context");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        let calls = Rc::new(RefCell::new(vec![]));
        let mut operations = ContextOperations {
            source: f.common.join("config"),
            target: target.clone(),
            condition_calls: calls.clone(),
        };
        preflight::context_with_operations(
            &f.root,
            Some(&mut trust),
            Some(&candidates),
            &mut budget(),
            Some(&config::path_text(&f.common)),
            &f.common,
            None,
            false,
            None,
            &mut operations,
        )
        .unwrap();
        let (result, events) = traced(|| {
            preflight::context_with_operations(
                &linked,
                Some(&mut trust),
                Some(&linked_candidates),
                &mut budget(),
                Some(&config::path_text(&admin)),
                &f.common,
                None,
                false,
                None,
                &mut operations,
            )
        });
        assert_eq!(
            result.unwrap_err().stop_reason(),
            Some("config の新しい origin は基準が無いため開けない"),
            "M05_HUMAN_CONTEXT_REJECT"
        );
        assert_eq!(
            *calls.borrow(),
            vec![f.root.clone(), linked],
            "M05_CONTEXTS_INDEPENDENT"
        );
        assert_eq!(opened(&events, "unknown-context"), 0, "M05_NO_OPEN");
        no_git(&events);
    });
}
#[test]
fn m06_symbolic_head_chain_uses_final_branch() {
    isolated("m06_symbolic_head_chain_uses_final_branch", |f| {
        let (mut trust, candidates) = f.baseline();
        f.git(&["symbolic-ref", "HEAD", "refs/heads/alias"]);
        f.git(&["symbolic-ref", "refs/heads/alias", "refs/heads/human"]);
        let target = f.base.join("chain-secret");
        std::fs::write(&target, b"[secret]\nvalue=sentinel\n").unwrap();
        assert_eq!(
            preflight::branch(&f.root, &f.common, &f.common, &mut budget()).unwrap(),
            Some(Text::from("refs/heads/human")),
            "M06_FINAL_BRANCH"
        );
        let mut operations = ContextOperations {
            source: f.common.join("config"),
            target,
            condition_calls: Rc::new(RefCell::new(vec![])),
        };
        let (result, events) = traced(|| {
            preflight::context_with_operations(
                &f.root,
                Some(&mut trust),
                Some(&candidates),
                &mut budget(),
                Some(&config::path_text(&f.common)),
                &f.common,
                None,
                false,
                None,
                &mut operations,
            )
        });
        assert_eq!(
            result.unwrap_err().stop_reason(),
            Some("config の新しい origin は基準が無いため開けない"),
            "M06_REJECT"
        );
        assert_eq!(opened(&events, "chain-secret"), 0, "M06_NO_OPEN");
        no_git(&events);
    });
}

fn no_directory(events: &[Event], target: &Path) {
    assert!(
        !events.iter().any(|event| match event {
            Event::Directory { path, .. } => path == target,
            #[cfg(target_os = "linux")]
            Event::OsAccess { path, mask } => path == target && mask & 0x21 != 0,
            _ => false,
        }),
        "SAFETY_NO_DIRECTORY: {}",
        target.display()
    );
}

#[test]
fn yaml_recursion_temporary_second_compose_and_merge_frames() {
    isolated(
        "yaml_recursion_temporary_second_compose_and_merge_frames",
        |f| {
            std::fs::create_dir(f.root.join(".claude")).unwrap();
            let profile = f.root.join(".claude/project-profile.yml");
            // 旧の実frame5/6と専用APIの開始文脈5/6を同じ入力で照合する。
            // 既定CLIのflow系列は深さが2ずつ増えるため、1加算の欠落が隠れ得る。
            let text = format!("unused: {}0{}\n", "[".repeat(490), "]".repeat(490));
            std::fs::write(&profile, &text).unwrap();
            let script = r#"import importlib.util,sys
sys.path.insert(0,__import__('os').environ['S2L_PYYAML_ROOT']);import yaml,pathlib,os
assert yaml.__version__=='6.0.3' and pathlib.Path(yaml.__file__).resolve().parent.parent==pathlib.Path(os.environ['S2L_PYYAML_ROOT'])
s=importlib.util.spec_from_file_location('old_s2l',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def a(): return b()
def b(): return c()
def c(): return d()
def d(): return m.profile_patterns(sys.argv[2],m.Budget(100000,10000000,1000000,60))
try: a()
except m.Stop: sys.exit(20)
"#;
            let old =
                Command::new("python3")
                    .args(["-B", "-c", script])
                    .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join(
                        "../../../plugins/dev-workflow/skills/ship-task/scripts/loop-state.py",
                    ))
                    .arg(&f.root)
                    .output()
                    .unwrap();
            assert_eq!(
                old.status.code(),
                Some(20),
                "YAML_TEMPORARY_OLD_ODD_CONTEXT: {}",
                String::from_utf8_lossy(&old.stderr)
            );
            use dev_workflow_core::loop_state::{
                LegacyIntLimit, legacy_recursion::LegacyRecursionContext, profile_patterns_for_loop,
            };
            let adjacent=script.replace("def c(): return d()","def c(): return m.profile_patterns(sys.argv[2],m.Budget(100000,10000000,1000000,60))");
            let old =
                Command::new("python3")
                    .args(["-B", "-c", &adjacent])
                    .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join(
                        "../../../plugins/dev-workflow/skills/ship-task/scripts/loop-state.py",
                    ))
                    .arg(&f.root)
                    .output()
                    .unwrap();
            assert_eq!(
                old.status.code(),
                Some(0),
                "YAML_TEMPORARY_OLD_ADJACENT: {}",
                String::from_utf8_lossy(&old.stderr)
            );
            assert!(
                profile_patterns_for_loop(
                    text.as_bytes(),
                    LegacyIntLimit::from_python_value(4300).unwrap(),
                    LegacyRecursionContext {
                        entry_depth: 5,
                        ..LegacyRecursionContext::cpython310_pyyaml603()
                    }
                )
                .is_ok(),
                "YAML_TEMPORARY_ADJACENT_CONTEXT"
            );
            let context = LegacyRecursionContext {
                entry_depth: 6,
                ..LegacyRecursionContext::cpython310_pyyaml603()
            };
            assert!(
                profile_patterns_for_loop(
                    text.as_bytes(),
                    LegacyIntLimit::from_python_value(4300).unwrap(),
                    context
                )
                .is_err(),
                "YAML_TEMPORARY_AND_SECOND_COMPOSE: odd context"
            );
            for n in [489, 490, 491] {
                for text in [
                    format!("unused: {}0{}\n", "[".repeat(n), "]".repeat(n)),
                    format!("unused: {}0{}\n", "[\n".repeat(n), "\n]".repeat(n)),
                    format!("unused: {}0{}\n", "[0,".repeat(n), "]".repeat(n)),
                ] {
                    std::fs::write(&profile, text).unwrap();
                    assert_eq!(
                        scan::profile(&f.root, &mut budget(), 4300).is_ok(),
                        n <= 490,
                        "YAML_TEMPORARY_AND_SECOND_COMPOSE: {n}"
                    );
                }
            }
            for n in [987, 988, 1100] {
                let mut text = String::from("a0: &a0 {other: ok}\n");
                for i in 1..=n {
                    text.push_str(&format!("a{i}: &a{i} {{<<: *a{}}}\n", i - 1));
                }
                text.push_str(&format!("<<: *a{n}\n"));
                std::fs::write(&profile, text).unwrap();
                assert_eq!(
                    scan::profile(&f.root, &mut budget(), 4300).is_ok(),
                    n == 987,
                    "YAML_MERGE_FRAMES: {n}"
                );
            }
        },
    );
}
