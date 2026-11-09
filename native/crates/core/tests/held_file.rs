#![forbid(unsafe_code)]

//! 製品 CLI と独立した C0b 比較用接続。通常起動は比較器の必須入口。
use dev_workflow_core::budget::{Budget, BudgetError, BudgetLimits};
use dev_workflow_core::fs::{ReadError, read_verified};
use dev_workflow_core::{CommandError, CommandOutput, ExitCode, execute};
use dev_workflow_platform::fs::{FsError, HeldDir, HeldFile, SnapshotMatch};
use serde_json::{Value, json};
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

#[derive(Debug)]
struct Failure {
    kind: &'static str,
    errno: Option<i32>,
}
impl From<BudgetError> for Failure {
    fn from(error: BudgetError) -> Self {
        Self {
            kind: match error {
                BudgetError::Deadline => "deadline",
                BudgetError::Items => "items",
                BudgetError::FileBytes => "file-bytes",
                BudgetError::TotalBytes => "total-bytes",
                BudgetError::Overflow => "overflow",
            },
            errno: None,
        }
    }
}
fn failure(error: FsError, stage: &str) -> Failure {
    let kind = match &error {
        FsError::Io(_) => "io",
        FsError::InvalidInput => "invalid-input",
        FsError::NotRegular => "not-regular",
        FsError::NotDirectory => "not-directory",
        FsError::AlreadyRead => "already-read",
        FsError::Changed if stage == "read" => "changed-read",
        FsError::Changed if stage == "entry" => "changed-entry",
        FsError::Changed => "changed-open",
    };
    let errno = match error {
        FsError::Io(error) => Some(error.raw_os_error().expect("OS errno")),
        _ => None,
    };
    Failure { kind, errno }
}
fn read_failure(error: ReadError) -> Failure {
    match error {
        ReadError::Fs(error) => failure(error, "read"),
        ReadError::Budget(error) => error.into(),
    }
}
fn number(v: &Value, key: &str) -> u64 {
    v[key].as_u64().expect(key)
}
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().expect(key)
}
fn unhex(value: &str) -> Vec<u8> {
    assert!(value.len().is_multiple_of(2));
    (0..value.len())
        .step_by(2)
        .map(|n| u8::from_str_radix(&value[n..n + 2], 16).unwrap())
        .collect()
}
fn hex(value: &[u8]) -> String {
    value.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn tick(budget: &mut Budget, size: u64) -> Result<(), Failure> {
    budget.check_time()?;
    budget.add_items(1)?;
    budget.check_file_bytes(size)?;
    budget.add_bytes(size)?;
    Ok(())
}
fn replace(path: &Path, kind: &str) {
    // 旧 inode を残して、同じ番号の再利用に依存しない。
    fs::rename(path, path.with_file_name("replaced-old")).unwrap();
    match kind {
        "replace-file" => fs::write(path, b"NEW").unwrap(),
        "replace-link" => symlink("replaced-old", path).unwrap(),
        // mkfifo は Python が fixture の準備で用意した FIFO を移す。
        "replace-fifo" => fs::rename(path.with_file_name("replacement-fifo"), path).unwrap(),
        _ => unreachable!(),
    }
}
fn mutate_opened(path: &Path, action: &str, file: &HeldFile) {
    let before = file.snapshot().unwrap();
    let modified = fs::metadata(path).unwrap().modified().unwrap();
    match action {
        "shrink" => fs::write(path, b"x").unwrap(),
        "grow" => fs::write(path, b"longer").unwrap(),
        "overwrite" | "mtime-restored" => fs::write(path, b"NEW").unwrap(),
        "chmod" => fs::set_permissions(path, fs::Permissions::from_mode(0o400)).unwrap(),
        "none" => return,
        _ => panic!("unknown post-open mutation: {action}"),
    }
    if action == "mtime-restored" {
        let until = Instant::now() + Duration::from_secs(2);
        loop {
            fs::File::open(path)
                .unwrap()
                .set_times(fs::FileTimes::new().set_modified(modified))
                .unwrap();
            let after = file.snapshot().unwrap();
            if (before.ctime_seconds, before.ctime_nanoseconds)
                != (after.ctime_seconds, after.ctime_nanoseconds)
            {
                break;
            }
            assert!(Instant::now() < until, "ctime did not change");
        }
    }
}
fn read_one(v: &Value, budget: &mut Budget) -> Result<Vec<u8>, Failure> {
    let cwd = std::env::current_dir().unwrap();
    let directory = cwd.join(text(v, "parent"));
    let parent = if text(v, "operation") == "profile" {
        let root = HeldDir::open_absolute(&cwd).map_err(|e| failure(e, "ancestor"))?;
        let before = root
            .metadata(OsStr::new(".claude"))
            .map_err(|e| failure(e, "stat"))?;
        if text(v, "before_open") == "parent-content" {
            fs::write(directory.join("added"), b"x").unwrap();
        }
        root.open_dir(OsStr::new(".claude"), &before, SnapshotMatch::Full)
            .map_err(|e| failure(e, "open"))?
    } else if text(v, "operation") == "ancestor" {
        let root = HeldDir::open_absolute(&cwd).map_err(|e| failure(e, "ancestor"))?;
        let before = root
            .metadata(OsStr::new("input"))
            .map_err(|e| failure(e, "ancestor"))?;
        if text(v, "before_open") == "ancestor-content" {
            fs::write(directory.join("added"), b"x").unwrap();
        } else {
            fs::rename(&directory, cwd.join("held-input")).unwrap();
            fs::create_dir(&directory).unwrap();
            fs::write(directory.join("file"), b"NEW").unwrap();
        }
        root.open_dir(OsStr::new("input"), &before, SnapshotMatch::DirectoryPath)
            .map_err(|e| failure(e, "ancestor"))?
    } else {
        HeldDir::open_absolute(&directory).map_err(|e| failure(e, "ancestor"))?
    };
    let raw = unhex(text(v, "name_hex"));
    let name = OsStr::from_bytes(&raw);
    let path = directory.join(name);
    let before = parent.metadata(name).map_err(|e| failure(e, "stat"))?;
    match text(v, "before_open") {
        action @ ("replace-file" | "replace-link" | "replace-fifo") => replace(&path, action),
        "parent-replace" => {
            fs::rename(&directory, cwd.join("held-claude")).unwrap();
            fs::create_dir(&directory).unwrap();
            fs::write(&path, b"NEW").unwrap();
        }
        "none" | "parent-content" | "ancestor-content" | "ancestor-replace" => (),
        other => panic!("unknown before-open mutation: {other}"),
    }
    budget.check_file_bytes(u64::try_from(before.size).unwrap())?;
    tick(budget, 0)?;
    let mut file = HeldFile::open_at(&parent, name, &before).map_err(|e| failure(e, "open"))?;
    mutate_opened(&path, text(v, "after_open"), &file);
    let bytes = read_verified(&mut file, budget).map_err(read_failure)?;
    if text(v, "operation") == "entry" {
        replace(&path, "replace-file");
        parent
            .verify_entry(name, &before, SnapshotMatch::Full)
            .map_err(|e| failure(e, "entry"))?;
    }
    // 親とファイルはこの関数で閉じ、解析側へは本文だけを返す。
    Ok(bytes)
}
fn descriptors() -> std::collections::BTreeSet<u32> {
    // fixture は単一スレッド。自分自身の列挙用 fd も毎回同じ位置で閉じる。
    fs::read_dir("/dev/fd")
        .unwrap()
        .map(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_str()
                .unwrap()
                .parse()
                .unwrap()
        })
        .collect()
}
fn fixture(case: &str, v: &Value) -> Value {
    let before_fds = descriptors();
    let deadline = if v["expired"].as_bool().unwrap() {
        Instant::now() - Duration::from_secs(1)
    } else {
        Instant::now() + Duration::from_secs(30)
    };
    let mut budget = Budget::new(
        BudgetLimits {
            items: number(v, "items"),
            total_bytes: number(v, "total"),
            file_bytes: number(v, "one"),
        },
        deadline,
    );
    budget.add_items(number(v, "prior_items")).unwrap();
    budget.add_bytes(number(v, "prior_bytes")).unwrap();
    let result = match text(v, "operation") {
        "tick" => tick(&mut budget, number(v, "size")).map(|()| vec![]),
        "remaining" => budget.remaining().map(|_| vec![]).map_err(Failure::from),
        _ => {
            let mut result = Ok(Vec::new());
            for _ in 0..number(v, "reads") {
                match read_one(v, &mut budget) {
                    Ok(bytes) => result.as_mut().unwrap().extend(bytes),
                    Err(error) => {
                        result = Err(error);
                        break;
                    }
                }
            }
            result
        }
    };
    assert_eq!(
        descriptors(),
        before_fds,
        "成功・拒否の後に保持 fd が残った"
    );
    match result {
        Ok(bytes) => json!({ "case": case, "outcome": "ok", "body_hex": hex(&bytes), "error": null,
            "usage": { "items": budget.items(), "bytes": budget.bytes() }, "deadline_unchanged": budget.deadline() == deadline }),
        Err(error) => {
            json!({ "case": case, "outcome": "error", "body_hex": null, "error": { "kind": error.kind, "errno": error.errno },
            "usage": { "items": budget.items(), "bytes": budget.bytes() }, "deadline_unchanged": budget.deadline() == deadline })
        }
    }
}
fn fixture_main(case: &str) -> ExitCode {
    execute(|io| {
        let mut input = Vec::new();
        let mut buffer = [0; 4096];
        loop {
            let count = io
                .read_input(&mut buffer)
                .map_err(|e| CommandError::runtime(e.to_string()))?;
            if count == 0 {
                break;
            }
            input.extend_from_slice(&buffer[..count]);
            if input.len() > 65_536 {
                return Err(CommandError::runtime("fixture input too large"));
            }
        }
        let plan: Value =
            serde_json::from_slice(&input).map_err(|e| CommandError::runtime(e.to_string()))?;
        let mut stdout = serde_json::to_vec(&fixture(case, &plan)).unwrap();
        stdout.push(b'\n');
        Ok(CommandOutput {
            stdout,
            stderr: vec![],
            code: ExitCode::SUCCESS,
        })
    })
}
struct CheckerChild(Child);
impl Drop for CheckerChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        self.0.wait().expect("比較器の子を回収できません");
    }
}
fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--c0b-fixture") {
        assert_eq!(args.len(), 3);
        return std::process::ExitCode::from(fixture_main(&args[2]).as_u8());
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let checker = root.join("native/ci/check_c0b.py");
    assert!(checker.is_file(), "C0b checker missing");
    let mut child = CheckerChild(
        Command::new("python3")
            .arg("-B")
            .arg(checker)
            .arg("--fixture")
            .arg(std::env::current_exe().unwrap())
            .current_dir(root)
            .spawn()
            .expect("C0b checker could not start"),
    );
    let deadline = Instant::now() + Duration::from_secs(180);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success(), "C0b checker failed: {status}");
            return std::process::ExitCode::SUCCESS;
        }
        if Instant::now() >= deadline {
            child.0.kill().unwrap();
            child.0.wait().unwrap();
            panic!("C0b checker timed out");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
