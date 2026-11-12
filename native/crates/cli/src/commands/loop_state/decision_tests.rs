//! r3 の 3 分類と、全コマンド共通の決定の Rust だけの検査(旧版と比べない)。
use super::{
    budget::{Budget, Limits},
    error::LegacyException as E,
    json::{PyValue as V, Text},
    observe::{self, Event},
    scan, state,
};
use dev_workflow_platform::{
    fs::HeldDir,
    loop_state::{errno, signal},
};
use std::{
    ffi::OsStr,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
fn base(label: &str) -> PathBuf {
    let path = std::fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!(
            "s2l-decision-{label}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    std::fs::create_dir_all(&path).unwrap();
    path
}
fn budget() -> Budget {
    Budget::new(Limits::default()).unwrap()
}
fn raw_name(raw: &[u8]) -> &OsStr {
    OsStr::from_bytes(raw)
}
/// UTF-8 でない名前のディレクトリを作る。Mac の APFS は EILSEQ で拒むので、そのときだけ
/// 飛ばしたことを S2L_OS_BOUNDARY の行で出して false を返す(黙って合格にしない)。
/// ほかの OS とほかの失敗は止める(Linux の検査は変えない)。
fn create_raw_directory(test: &str, path: &Path, control: &str) -> bool {
    match std::fs::create_dir(path) {
        Ok(()) => true,
        Err(error) if cfg!(target_os = "macos") && error.raw_os_error() == Some(errno::EILSEQ) => {
            println!(
                "S2L_OS_BOUNDARY name={test} os=macos input=non_utf8_name errno={} action=skipped ascii_control={control}",
                errno::EILSEQ
            );
            false
        }
        Err(error) => panic!("S2L_RAW_NAME_CREATE: {}: {error}", path.display()),
    }
}
fn descriptors() -> usize {
    std::fs::read_dir(if cfg!(target_os = "linux") {
        "/proc/self/fd"
    } else {
        "/dev/fd"
    })
    .unwrap()
    .count()
}

#[test]
fn r3_enumeration_failure_keeps_surrogate_name() {
    for (name, shown) in [(&b"d-\xff"[..], "d-\\udcff"), (&b"d-a"[..], "d-a")] {
        let root = base("names");
        let directory = root.join(raw_name(name));
        // ASCII の対照(d-a)は全 OS で流す。UTF-8 でない名前だけ、ファイル系が拒めば飛ばす。
        if name.is_ascii() {
            std::fs::create_dir(&directory).unwrap();
        } else if !create_raw_directory(
            "r3_enumeration_failure_keeps_surrogate_name",
            &directory,
            "d-a",
        ) {
            std::fs::remove_dir_all(root).unwrap();
            continue;
        }
        std::fs::write(directory.join("f"), b"body").unwrap();
        let _guard = observe::install(|event| match event {
            Event::Names { label }
                if label
                    .as_codepoints()
                    .starts_with(&"ディレクトリ d-".chars().map(u32::from).collect::<Vec<_>>()) =>
            {
                Err(E::os(std::io::Error::from_raw_os_error(24), "inject"))
            }
            _ => Ok(()),
        });
        let error = scan::tree(&root, &[], &mut budget(), None).unwrap_err();
        assert_eq!(
            error.stop_reason(),
            Some(format!("ディレクトリ {shown} を列挙できない: Too many open files").as_str()),
            "R3_ENUMERATION_SURROGATE"
        );
        drop(_guard);
        std::fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn r3_directory_change_during_read_keeps_surrogate_name() {
    // ASCII の対照(d-a)は全 OS で流す。UTF-8 でない名前だけ、ファイル系が拒めば飛ばす。
    for (name, shown) in [(&b"d-\xff"[..], "d-\\udcff"), (&b"d-a"[..], "d-a")] {
        let root = base("changed");
        let directory = root.join(raw_name(name));
        if name.is_ascii() {
            std::fs::create_dir(&directory).unwrap();
        } else if !create_raw_directory(
            "r3_directory_change_during_read_keeps_surrogate_name",
            &directory,
            "d-a",
        ) {
            std::fs::remove_dir_all(root).unwrap();
            continue;
        }
        std::fs::write(directory.join("f"), b"body").unwrap();
        let target = directory.clone();
        let _guard = observe::install(move |event| {
            if let Event::Open { name, .. } = event
                && name == "f"
            {
                // 時刻の刻みに依らず必ず変わる mode を変える(旧 same() も mode を比べる)。
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(&target).unwrap().permissions().mode() & 0o777;
                std::fs::set_permissions(&target, std::fs::Permissions::from_mode(mode ^ 0o010))
                    .unwrap();
            }
            Ok(())
        });
        let error = scan::tree(&root, &[], &mut budget(), None).unwrap_err();
        assert_eq!(
            error.stop_reason(),
            Some(format!("ディレクトリが読取中に変わった: {shown}").as_str()),
            "R3_CHANGED_SURROGATE"
        );
        drop(_guard);
        std::fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn r3_held_directory_listing_duplicates_and_rewinds() {
    // fd の数は、ほかの検査の並行の open に左右されないよう、単独の子で数える。
    if !isolated("r3_held_directory_listing_duplicates_and_rewinds", false) {
        return;
    }
    let root = base("listing");
    let directory = root.join("mode0400");
    std::fs::create_dir(&directory).unwrap();
    for name in ["c", "a", "b"] {
        std::fs::write(directory.join(name), b"x").unwrap();
    }
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o400)).unwrap();
    let held = HeldDir::open_absolute(&directory).unwrap();
    let before = descriptors();
    let first = scan::names(&held, &mut budget(), "listing").unwrap();
    let second = scan::names(&held, &mut budget(), "listing").unwrap();
    let after = descriptors();
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    // 検索の権限(x)が無くても、保持した fd の複製から列挙できる(旧 os.scandir(fd))。
    assert_eq!(
        first,
        ["a", "b", "c"].map(std::ffi::OsString::from),
        "R3_HELD_LISTING"
    );
    // 終わりで巻き戻すので、同じ保持 fd を再び列挙できる。複製した fd は閉じる。
    assert_eq!(first, second, "R3_HELD_REWIND");
    assert_eq!(before, after, "R3_HELD_DUPLICATE_RELEASED");
    drop(held);
    std::fs::remove_dir_all(root).unwrap();
}

fn isolated(name: &str, ignore_interrupt: bool) -> bool {
    isolated_with(name, ignore_interrupt, &[])
}
fn isolated_with(name: &str, ignore_interrupt: bool, envs: &[(&str, &str)]) -> bool {
    if std::env::var("S2L_DECISION_TEST").ok().as_deref() == Some(name) {
        return true;
    }
    let exe = std::env::current_exe().unwrap();
    let test = format!("loop_state::decision_tests::{name}");
    let status = if ignore_interrupt {
        // 対話でないシェルの `trap '' INT` と同じく、無視(SIG_IGN)を受け継いで起動する。
        Command::new("/bin/sh")
            .args(["-c", "trap '' INT; exec \"$0\" --exact \"$1\" --nocapture"])
            .arg(&exe)
            .arg(&test)
            .env("S2L_DECISION_TEST", name)
            .envs(envs.iter().copied())
            .status()
            .unwrap()
    } else {
        Command::new(exe)
            .args(["--exact", &test, "--nocapture"])
            .env("S2L_DECISION_TEST", name)
            .envs(envs.iter().copied())
            .status()
            .unwrap()
    };
    assert!(status.success(), "S2L_DECISION_ISOLATED: {name}");
    false
}

#[test]
fn interrupt_during_state_write_keeps_temporary() {
    if !isolated("interrupt_during_state_write_keeps_temporary", false) {
        return;
    }
    assert_eq!(
        signal::startup(),
        signal::Interrupt::Handled,
        "SIGINT_HANDLED_AT_START"
    );
    let root = base("interrupt");
    let out = root.join("out");
    signal::raise_interrupt();
    // 複数のスレッドがあると、送った信号の処理は kill の戻りより後になりうる。
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while !signal::interrupted() && std::time::Instant::now() < deadline {
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    assert!(signal::interrupted(), "SIGINT_FLAG");
    let value = V::object([
        ("schema", V::from(7i128)),
        ("pad", V::from(Text::from("x"))),
    ]);
    let error = state::write(&out, &value).unwrap_err();
    assert!(
        super::interrupt::is_interrupt(&error),
        "SIGINT_KEYBOARD_INTERRUPT"
    );
    assert!(
        super::error::render(&error).ends_with("\nKeyboardInterrupt\n"),
        "SIGINT_LAST_LINE"
    );
    // except Exception に当たらないので、一時ファイルは残り、出力先は作らない。
    let temporary = root.join(format!(".out.tmp.{}", std::process::id()));
    assert!(
        temporary.is_file() && !out.exists(),
        "SIGINT_TEMPORARY_REMAINS"
    );
    // 区切り(予算の確認)でも同じく止まる。
    assert!(
        super::interrupt::is_interrupt(&budget().tick(0).unwrap_err()),
        "SIGINT_CHECKPOINT"
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn sigint_ignored_at_start_stays_ignored() {
    if !isolated("sigint_ignored_at_start_stays_ignored", true) {
        return;
    }
    assert_eq!(
        signal::startup(),
        signal::Interrupt::Ignored,
        "SIGINT_IGNORED_AT_START"
    );
    signal::raise_interrupt();
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert!(!signal::interrupted(), "SIGINT_IGNORED_STAYS");
    budget().tick(0).unwrap();
    let root = base("ignored");
    state::write(&root.join("out"), &V::object([("schema", V::from(7i128))])).unwrap();
    assert!(
        Path::new(&root.join("out")).is_file(),
        "SIGINT_IGNORED_NORMAL_SAVE"
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn state_write_buffer_follows_block_size() {
    let root = base("blksize");
    let held = HeldDir::open_absolute(&root).unwrap();
    let raw =
        dev_workflow_platform::loop_state::StateWriter::create(&held, OsStr::new("x")).unwrap();
    use std::os::unix::fs::MetadataExt;
    let expected = std::fs::metadata(root.join("x")).unwrap().blksize() as usize;
    let file = super::legacy_file::LegacyTextFile::new(raw);
    assert_eq!(
        file.buffer_size(),
        if expected > 1 { expected } else { 8192 },
        "WRITE_BLKSIZE"
    );
    drop(file);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn mkdtemp_candidates_and_names_follow_tempfile() {
    if !isolated_with(
        "mkdtemp_candidates_and_names_follow_tempfile",
        false,
        &[("TMPDIR", "/s2l-first"), ("TEMP", ""), ("TMP", "relative")],
    ) {
        return;
    }
    let list = super::legacy_tempfile::candidate_list();
    let cwd = std::env::current_dir().unwrap().into_os_string();
    assert_eq!(
        list,
        ["/s2l-first", "relative", "/tmp", "/var/tmp", "/usr/tmp"]
            .map(std::ffi::OsString::from)
            .into_iter()
            .chain([cwd])
            .collect::<Vec<_>>(),
        "MKDTEMP_CANDIDATES"
    );
    let name = super::legacy_tempfile::sample_name();
    assert!(
        name.len() == 8
            && name
                .bytes()
                .all(|c| b"abcdefghijklmnopqrstuvwxyz0123456789_".contains(&c)),
        "MKDTEMP_NAME"
    );
}

#[test]
fn onbranch_cleanup_follows_os_walk_bottom_up() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    // ディレクトリへのリンクは directories に入り(辿る判定)、降りない(辿らない判定)。
    // os.rmdir は ENOTDIR で止まり、置き場とリンクが残る。
    let root = base("cleanup-link");
    let parent = root.join("loop-state-branch-test");
    let temp = parent.join("git");
    std::fs::create_dir_all(temp.join("sub")).unwrap();
    std::fs::write(temp.join("sub/g"), b"g").unwrap();
    std::fs::write(temp.join("f"), b"f").unwrap();
    symlink("sub", temp.join("link")).unwrap();
    symlink("f", temp.join("file-link")).unwrap();
    let error = super::preflight::cleanup_temporary(&temp, &parent).unwrap_err();
    let rendered = super::error::render(&error);
    let expected = format!(
        "NotADirectoryError: [Errno 20] Not a directory: '{}'\n",
        temp.join("link").display()
    );
    assert!(
        rendered.ends_with(&expected),
        "CLEANUP_LINK_ERROR: {rendered}"
    );
    assert!(
        rendered.contains("line 1054, in preflight_condition\n    for name in directories: os.rmdir(os.path.join(base, name))\n"),
        "CLEANUP_LINK_LINE: {rendered}"
    );
    assert!(
        temp.join("link").symlink_metadata().is_ok() && parent.is_dir(),
        "CLEANUP_LINK_REMAINS"
    );
    assert!(
        !temp.join("f").exists() && temp.join("file-link").symlink_metadata().is_err(),
        "CLEANUP_FILES_FIRST"
    );
    std::fs::remove_dir_all(&root).unwrap();

    // 列挙できない段は出さない(onerror=None)。中身が残るので、親の段の rmdir が失敗する。
    let root = base("cleanup-unlisted");
    let parent = root.join("loop-state-branch-test");
    let temp = parent.join("git");
    std::fs::create_dir_all(temp.join("closed")).unwrap();
    std::fs::write(temp.join("closed/inside"), b"x").unwrap();
    std::fs::set_permissions(temp.join("closed"), std::fs::Permissions::from_mode(0o000)).unwrap();
    let error = super::preflight::cleanup_temporary(&temp, &parent).unwrap_err();
    std::fs::set_permissions(temp.join("closed"), std::fs::Permissions::from_mode(0o700)).unwrap();
    let rendered = super::error::render(&error);
    // errno の番号と文字列は OS の値から作る(ENOTEMPTY は Linux 39・Mac 66)。
    let reason = std::io::Error::from_raw_os_error(errno::ENOTEMPTY).to_string();
    let reason = reason.split(" (os error ").next().unwrap();
    assert!(
        rendered.ends_with(&format!(
            "OSError: [Errno {}] {reason}: '{}'\n",
            errno::ENOTEMPTY,
            temp.join("closed").display()
        )),
        "CLEANUP_UNLISTED: {rendered}"
    );
    assert!(
        temp.join("closed/inside").is_file(),
        "CLEANUP_UNLISTED_REMAINS"
    );
    std::fs::remove_dir_all(&root).unwrap();

    // 通常の木はすべて消え、置き場も消える。
    let root = base("cleanup-normal");
    let parent = root.join("loop-state-branch-test");
    let temp = parent.join("git");
    std::fs::create_dir_all(temp.join("refs/heads")).unwrap();
    std::fs::write(temp.join("refs/heads/main"), b"0\n").unwrap();
    std::fs::write(temp.join("HEAD"), b"ref: refs/heads/main\n").unwrap();
    super::preflight::cleanup_temporary(&temp, &parent).unwrap();
    assert!(!parent.exists(), "CLEANUP_NORMAL");
    // temp が無い(init の前の失敗)なら、親だけを消す。
    std::fs::create_dir(&parent).unwrap();
    super::preflight::cleanup_temporary(&temp, &parent).unwrap();
    assert!(!parent.exists(), "CLEANUP_PARENT_ONLY");
    std::fs::remove_dir_all(&root).unwrap();
}

#[test]
fn entry_readlink_failure_is_stop_with_codepoints() {
    let root = base("readlink");
    std::fs::create_dir(root.join("admin")).unwrap();
    std::os::unix::fs::symlink("target", root.join("admin/index")).unwrap();
    let _guard = observe::install(|event| match event {
        Event::ReadLink { .. } => Err(E::os(std::io::Error::from_raw_os_error(2), "inject")),
        _ => Ok(()),
    });
    let error = scan::entry(&root.join("admin/index"), "index", &mut budget()).unwrap_err();
    assert_eq!(
        error.stop_reason(),
        Some("symlink を読めない: index: No such file or directory"),
        "READLINK_STOP"
    );
    // 木の走査の readlink は捕捉しない(ファイル名つきの OSError)。
    let error = scan::tree(&root.join("admin"), &[], &mut budget(), None).unwrap_err();
    let rendered = super::error::render(&error);
    assert!(error.stop_reason().is_none(), "READLINK_WALK_NOT_STOP");
    assert!(
        rendered.ends_with("FileNotFoundError: [Errno 2] No such file or directory: 'index'\n")
            && rendered.contains("line 489, in walk\n"),
        "READLINK_WALK_OSERROR: {rendered}"
    );
    drop(_guard);
    std::fs::remove_dir_all(root).unwrap();
}
