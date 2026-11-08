#![forbid(unsafe_code)]

use dev_workflow_core::build_info;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_dev-workflow"))
}

#[derive(Debug, PartialEq, Eq)]
enum Contents {
    Directory,
    File(Vec<u8>),
    Symlink(PathBuf),
}

#[derive(Debug, PartialEq, Eq)]
struct Entry {
    contents: Contents,
    #[cfg(unix)]
    mode: u32,
    #[cfg(not(unix))]
    readonly: bool,
}

fn snapshot(root: &Path) -> BTreeMap<PathBuf, Entry> {
    fn visit(root: &Path, path: &Path, entries: &mut BTreeMap<PathBuf, Entry>) {
        let metadata = fs::symlink_metadata(path).unwrap();
        let contents = if metadata.is_symlink() {
            Contents::Symlink(fs::read_link(path).unwrap())
        } else if metadata.is_dir() {
            for child in fs::read_dir(path).unwrap() {
                visit(root, &child.unwrap().path(), entries);
            }
            Contents::Directory
        } else {
            assert!(metadata.is_file(), "予期しないファイルの種類: {path:?}");
            Contents::File(fs::read(path).unwrap())
        };
        #[cfg(unix)]
        let mode = {
            use std::os::unix::fs::PermissionsExt;
            metadata.permissions().mode()
        };
        entries.insert(
            path.strip_prefix(root).unwrap().to_path_buf(),
            Entry {
                contents,
                #[cfg(unix)]
                mode,
                #[cfg(not(unix))]
                readonly: metadata.permissions().readonly(),
            },
        );
    }

    let mut entries = BTreeMap::new();
    visit(root, root, &mut entries);
    entries
}

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "dev-workflow-probe-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let fixture = Self(root);
        for directory in ["work/.claude", "work/nested/empty", "home", "tmp", "bin"] {
            fs::create_dir_all(fixture.0.join(directory)).unwrap();
        }
        fs::write(fixture.0.join("work/.git"), b"not a git repository\n").unwrap();
        fs::write(
            fixture.0.join("work/.claude/project-profile.yml"),
            b"invalid: [\n",
        )
        .unwrap();
        fs::write(
            fixture.0.join("work/nested/data"),
            b"existing contents\0\xff\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::{PermissionsExt, symlink};
            fs::set_permissions(
                fixture.0.join("work/nested/data"),
                fs::Permissions::from_mode(0o400),
            )
            .unwrap();
            symlink("nested/data", fixture.0.join("work/link")).unwrap();
            symlink("missing", fixture.0.join("work/dangling")).unwrap();
            // 外部の道具を呼んだら、出力とフォルダの比較が失敗する。
            for name in ["git", "uname", "sha256sum", "shasum", "openssl", "curl"] {
                let path = fixture.0.join("bin").join(name);
                fs::write(
                    &path,
                    b"#!/bin/sh\nprintf invoked > \"$HOME/external-command\"\nexit 99\n",
                )
                .unwrap();
                fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        fixture
    }

    fn run(&self, args: &[&str]) -> Output {
        let args = args.iter().map(OsString::from).collect::<Vec<_>>();
        self.run_os(&args)
    }

    fn run_os(&self, args: &[OsString]) -> Output {
        let mut command = Command::new(binary());
        command.args(args);
        self.execute(command)
    }

    fn execute(&self, mut command: Command) -> Output {
        let before = snapshot(&self.0);
        command
            .current_dir(self.0.join("work"))
            .env_clear()
            .env("PATH", self.0.join("bin"))
            .env("HOME", self.0.join("home"))
            .env("TMPDIR", self.0.join("tmp"))
            .env("GIT_DIR", self.0.join("missing-git"))
            .env("DEV_WORKFLOW_SOURCE_INPUT_SHA256", "not-the-build-hash")
            .env("DEV_WORKFLOW_RUSTC", "not-the-build-compiler")
            .stdin(Stdio::null());
        // コピーした実行ファイルを起動するテストでは、並列のほかのテストが fork した子が、
        // 書き込みの fd を exec までの間だけ持つことがある。そのときの ETXTBSY だけを、少しの間やり直す。
        let mut attempts = 0;
        let output = loop {
            match command.output() {
                Err(error)
                    if error.kind() == std::io::ErrorKind::ExecutableFileBusy && attempts < 50 =>
                {
                    attempts += 1;
                    std::thread::sleep(std::time::Duration::from_millis(20));
                }
                result => break result.unwrap(),
            }
        };
        assert_eq!(snapshot(&self.0), before, "実行でファイルが変わりました");
        output
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn success(output: &Output) -> &[u8] {
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert!(output.stdout.ends_with(b"\n"), "{output:?}");
    &output.stdout
}

fn failure(output: &Output, code: i32) -> &str {
    assert_eq!(output.status.code(), Some(code), "{output:?}");
    assert!(output.stdout.is_empty(), "{output:?}");
    let reason = std::str::from_utf8(&output.stderr).unwrap();
    assert!(reason.starts_with("dev-workflow: "), "{output:?}");
    assert!(reason.ends_with('\n'), "{output:?}");
    assert!(
        !reason
            .trim_start_matches("dev-workflow: ")
            .trim()
            .is_empty()
    );
    reason
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[test]
fn version_has_the_native_version_and_source_hash_on_one_line() {
    let output = Fixture::new().run(&["--version"]);
    assert_eq!(
        success(&output),
        format!(
            "dev-workflow {} (source_input_sha256: {})\n",
            build_info::NATIVE_VERSION,
            build_info::SOURCE_INPUT_SHA256
        )
        .as_bytes()
    );
    assert!(is_sha256(build_info::SOURCE_INPUT_SHA256));
}

#[test]
fn help_lists_all_four_invocations() {
    let output = Fixture::new().run(&["--help"]);
    assert_eq!(
        success(&output),
        "使い方:\n  dev-workflow --version\n  dev-workflow --help\n  dev-workflow probe --json\n  dev-workflow probe --self-sha256\n  dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]\n".as_bytes()
    );
}

#[test]
fn json_contains_exactly_the_promised_build_and_running_platform_fields() {
    let output = Fixture::new().run(&["probe", "--json"]);
    let bytes = success(&output);
    assert_eq!(bytes.iter().filter(|&&byte| byte == b'\n').count(), 1);
    let value: Value = serde_json::from_slice(bytes).unwrap();
    assert_eq!(
        value,
        json!({
            "schema_version": 1,
            "ok": true,
            "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH,
            "native_version": build_info::NATIVE_VERSION,
            "source_input_sha256": build_info::SOURCE_INPUT_SHA256,
            "rustc": build_info::RUSTC,
        })
    );
    assert!(is_sha256(value["source_input_sha256"].as_str().unwrap()));
    assert!(
        value["rustc"]
            .as_str()
            .unwrap()
            .starts_with("rustc 1.99.1 ")
    );
}

#[test]
fn self_sha256_matches_the_entire_executable() {
    let output = Fixture::new().run(&["probe", "--self-sha256"]);
    let bytes = success(&output);
    let hash = std::str::from_utf8(bytes)
        .unwrap()
        .strip_suffix('\n')
        .unwrap();
    assert!(is_sha256(hash));
    assert_eq!(
        hash,
        format!("{:x}", Sha256::digest(fs::read(binary()).unwrap()))
    );
}

#[test]
fn all_four_invocations_work_without_project_files_or_external_tools() {
    let fixture = Fixture::new();
    for directory in ["work", "bin"] {
        fs::remove_dir_all(fixture.0.join(directory)).unwrap();
        fs::create_dir(fixture.0.join(directory)).unwrap();
    }
    for args in [
        &["--version"][..],
        &["--help"],
        &["probe", "--json"],
        &["probe", "--self-sha256"],
    ] {
        success(&fixture.run(args));
    }
}

#[test]
fn absent_unknown_and_malformed_commands_are_usage_errors() {
    let fixture = Fixture::new();
    for args in [
        &[][..],
        &[""],
        &["unknown"],
        &["unknown", "--help"],
        &["unknown", "--version"],
        &["--json"],
        &["--self-sha256"],
        &["--"],
        &["-h"],
        &["-V"],
        &["--help=true"],
        &["--version=true"],
        &["Probe", "--json"],
        &["probe\n", "--json"],
    ] {
        failure(&fixture.run(args), 2);
    }
}

#[test]
fn global_options_reject_every_extra_argument() {
    let fixture = Fixture::new();
    for option in ["--version", "--help"] {
        for extra in [
            "--version",
            "--help",
            "probe",
            "--json",
            "--self-sha256",
            "",
            "--",
            "unknown",
        ] {
            failure(&fixture.run(&[option, extra]), 2);
            failure(&fixture.run(&[extra, option]), 2);
        }
        failure(&fixture.run(&[option, "probe", "--json"]), 2);
    }
}

#[test]
fn probe_rejects_missing_conflicting_repeated_unknown_and_positional_arguments() {
    let fixture = Fixture::new();
    failure(&fixture.run(&["probe"]), 2);
    for option in ["--json", "--self-sha256"] {
        for extra in [
            "--json",
            "--self-sha256",
            "--help",
            "--version",
            "--unknown",
            "file",
            "",
            "--",
        ] {
            failure(&fixture.run(&["probe", option, extra]), 2);
            failure(&fixture.run(&["probe", extra, option]), 2);
        }
    }
    for argument in [
        "--help",
        "--version",
        "--unknown",
        "file",
        "",
        "--",
        "--json=true",
        "--self-sha256=true",
        "--json\n",
        "--self-sha256 ",
        "−−json",
        "日本語",
    ] {
        failure(&fixture.run(&["probe", argument]), 2);
    }
}

#[cfg(unix)]
#[test]
fn non_utf8_arguments_are_usage_errors_without_panicking() {
    use std::os::unix::ffi::OsStringExt;

    let fixture = Fixture::new();
    for bytes in [
        vec![0xff],
        b"--json\xff".to_vec(),
        b"--self-sha256\xff".to_vec(),
    ] {
        let invalid = OsString::from_vec(bytes);
        for args in [
            vec![invalid.clone()],
            vec!["--version".into(), invalid.clone()],
            vec!["--help".into(), invalid.clone()],
            vec!["probe".into(), invalid.clone()],
            vec!["probe".into(), "--json".into(), invalid.clone()],
            vec!["probe".into(), invalid.clone(), "--json".into()],
            vec!["probe".into(), "--self-sha256".into(), invalid.clone()],
            vec!["probe".into(), invalid, "--self-sha256".into()],
        ] {
            failure(&fixture.run_os(&args), 2);
        }
    }
}

#[cfg(unix)]
#[test]
fn self_sha256_uses_current_exe_with_non_utf8_symlink_and_false_argv_zero() {
    use std::os::unix::{ffi::OsStringExt, fs::symlink, process::CommandExt};

    let fixture = Fixture::new();
    let mut link = fixture
        .0
        .join(OsString::from_vec(b"executable-\xff".to_vec()));
    if let Err(error) = symlink(binary(), &link) {
        // macOS の EILSEQ(Illegal byte sequence)の値。
        const MACOS_EILSEQ: i32 = 92;
        // macOS の APFS は、UTF-8 でない名前を EILSEQ で拒む。そこでは UTF-8 の名前のリンクで確かめる
        // (argv[0] を偽る確かめは残る)。ほかの失敗はそのまま落とす。
        assert!(
            cfg!(target_os = "macos") && error.raw_os_error() == Some(MACOS_EILSEQ),
            "リンクを作れない: {error}"
        );
        link = fixture.0.join("executable-link");
        symlink(binary(), &link).unwrap();
    }
    let decoy = fixture.0.join("work/argv-zero");
    fs::write(&decoy, b"this is not the executable").unwrap();
    let mut command = Command::new(link);
    command.arg0(decoy).args(["probe", "--self-sha256"]);
    assert_eq!(
        success(&fixture.execute(command)),
        format!("{:x}\n", Sha256::digest(fs::read(binary()).unwrap())).as_bytes()
    );
}

#[cfg(target_os = "linux")]
#[test]
fn unreadable_current_executable_is_a_runtime_error() {
    use std::os::fd::AsRawFd;

    let fixture = Fixture::new();
    let path = fixture.0.join("removed-executable");
    fs::copy(binary(), &path).unwrap();
    let file = fs::File::open(&path).unwrap();
    fs::remove_file(path).unwrap();
    // 親の開いたファイルから起動する。current_exe のパスは既に削除済み。
    let mut command = Command::new(format!(
        "/proc/{}/fd/{}",
        std::process::id(),
        file.as_raw_fd()
    ));
    command.args(["probe", "--self-sha256"]);
    let output = fixture.execute(command);
    assert!(failure(&output, 1).contains("自己の実行ファイル"));
}
