//! Test-only operation substitution. No production flag or environment activates it.
use super::*;
use dev_workflow_platform::diff_snapshot::{FaultStage, Operations, SyncPoint};
use serde_json::{Value, json};
use std::io::{BufRead, Read, Write};
use std::process::Stdio;

const MANIFEST: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../ci/diff_snapshot_fixtures/cases.json"
));
fn snapshot(root: &Path) -> Snapshot {
    // A run exports its throw-away index (1260) for the rest of that run; an
    // earlier in-process run must not lend it to this snapshot's Git children.
    process::export("GIT_INDEX_FILE", None);
    Snapshot {
        args: Args::default(),
        git: Git::new(root.into()),
        root: root.into(),
        temp: temp(None, 613).unwrap(),
        out: root.join("out.md"),
        patch: Some(root.join("out.patch")),
        ere: vec![],
        notes: vec![],
        rows: vec![],
        tamper: false,
        stop: false,
        digest: vec![],
        show_digest: false,
        mismatch: false,
        included: vec![],
        tracked: vec![],
        untracked: vec![],
        stage: vec![],
        sparse: vec![],
        filters: HashMap::new(),
        pre: HashSet::new(),
        include: HashSet::new(),
        sections: std::array::from_fn(|_| vec![]),
        counts: [0; 7],
        failed: 0,
        body_empty: vec![],
        head: vec![],
        ancestor: vec![],
        want_patch: true,
        patch_bytes: b"NEW_PATCH\n".to_vec(),
        signals: Signals::install().unwrap(),
    }
}
fn point_name(point: SyncPoint) -> &'static str {
    match point {
        SyncPoint::BeforePatch => "patch-before",
        SyncPoint::AfterPatch => "patch-after",
        SyncPoint::BeforeReport => "out-before",
        SyncPoint::AfterReport => "out-after",
        SyncPoint::BeforeCleanup => "cleanup-before",
        SyncPoint::ProfileInspected => "profile-inspected",
        SyncPoint::ProfileOpened => "profile-opened",
        SyncPoint::ProfileReading => "profile-reading",
        SyncPoint::ProfileRead => "profile-read",
    }
}
fn handshake(label: &str) {
    println!("S3D_READY:{label}");
    std::io::stdout().flush().unwrap();
    let mut byte = [0];
    loop {
        match std::io::stdin().read_exact(&mut byte) {
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            r => {
                r.unwrap();
                break;
            }
        }
    }
    assert_eq!(&byte, b"x");
}
fn wait_signal(guard: &Signals) {
    let start = std::time::Instant::now();
    while guard.received().is_none() {
        assert!(
            start.elapsed() < std::time::Duration::from_secs(5),
            "signal was not delivered"
        );
        std::thread::yield_now();
    }
}
struct PublicationOps {
    signals: Signals,
    target: String,
    action: String,
    points: Vec<String>,
    renames: Vec<(PathBuf, PathBuf)>,
}
impl Operations for PublicationOps {
    fn sync(&mut self, point: SyncPoint) -> std::io::Result<()> {
        let name = point_name(point);
        self.points.push(name.into());
        if name == self.target {
            handshake(name);
            wait_signal(&self.signals);
        }
        Ok(())
    }
    fn rename(&mut self, from: &Path, to: &Path) -> std::result::Result<(), Vec<u8>> {
        self.renames.push((from.into(), to.into()));
        // The old stub mv exits 1 without a diagnostic for an injected failure.
        if (self.action == "patch-failure" && to.extension().is_some_and(|s| s == "patch"))
            || (self.action == "out-failure" && to.extension().is_some_and(|s| s == "md"))
        {
            return Err(vec![]);
        }
        tools::move_path(from, to)
    }
    fn copy(&mut self, from: &Path, data: &[u8], to: &Path) -> std::result::Result<(), Vec<u8>> {
        tools::copy_bytes(from, data, to)
    }
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn optional_file(path: &Path) -> Option<String> {
    match std::fs::read(path) {
        Ok(b) => Some(hex(&b)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => panic!("file read: {e}"),
    }
}
fn publication_child(case: &Value, root: &Path) -> Value {
    if case["old_out"].as_bool().unwrap() {
        std::fs::write(root.join("out.md"), b"OLD_REPORT\n").unwrap();
    }
    let outside = root.join("outside");
    std::fs::write(&outside, b"OUTSIDE").unwrap();
    std::os::unix::fs::symlink(&outside, root.join("out.md.tmp.12345")).unwrap();
    let s = snapshot(root);
    let mut ops = PublicationOps {
        signals: s.signals.clone(),
        target: case["point"].as_str().unwrap_or("").into(),
        action: case["action"].as_str().unwrap_or("").into(),
        points: vec![],
        renames: vec![],
    };
    let result = publish::publish(&s, b"NEW_REPORT\n", &mut ops);
    let code = result.as_ref().map_or_else(|f| f.code, |_| 0);
    let stderr = result.err().map_or_else(Vec::new, |f| f.stderr);
    drop(s);
    assert_eq!(std::fs::read(&outside).unwrap(), b"OUTSIDE");
    let directories: Vec<String> = std::fs::read_dir(root)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with(".diff-snapshot."))
        .collect();
    for (from, to) in &ops.renames {
        let dir = from.parent().unwrap();
        assert_eq!(dir.parent(), Some(root));
        let name = dir.file_name().unwrap().to_str().unwrap();
        assert!(name.starts_with(".diff-snapshot."));
        assert_eq!(name.len(), 21);
        assert_eq!(to.parent(), Some(root));
    }
    let hits = ops.points.iter().filter(|p| **p == ops.target).count();
    json!({"case_id":case["id"],"code":code,"out_hex":optional_file(&root.join("out.md")),"patch_hex":optional_file(&root.join("out.patch")),"temporary_directories":directories,"points":ops.points,"sync_target_hits":hits,"renames":ops.renames.iter().map(|(a,b)|vec![a.to_string_lossy().into_owned(),b.to_string_lossy().into_owned()]).collect::<Vec<_>>(),"stderr_hex":hex(&stderr)})
}
#[test]
fn typed_child() {
    let Ok(case) = std::env::var("DEV_WORKFLOW_S3D_TYPED_CASE") else {
        return;
    };
    let case: Value = serde_json::from_str(&case).unwrap();
    let root = PathBuf::from(std::env::var_os("DEV_WORKFLOW_S3D_TYPED_ROOT").unwrap());
    let observation = match case["category"].as_str().unwrap() {
        "publish" => publication_child(&case, &root),
        "signal" => signal_child(&case),
        "display-stderr" => display_stderr_child(&case, &root),
        "profile-timeout" => profile_timeout_child(&case, &root),
        _ => panic!("unknown typed child"),
    };
    std::fs::write(
        root.join("observation.json"),
        serde_json::to_vec(&observation).unwrap(),
    )
    .unwrap();
    std::process::exit(observation["code"].as_i64().unwrap() as i32);
}
/// The whole command with a profile ref, the first profile Git call seeing its
/// 10 s limit at once (the same moment the comparison gives the old helper).
fn profile_timeout_child(case: &Value, root: &Path) -> Value {
    let repo = root.join("repo");
    std::fs::create_dir(&repo).unwrap();
    fixture_git(&repo, &["init", "--quiet"]);
    std::fs::create_dir(repo.join(".claude")).unwrap();
    std::fs::write(
        repo.join(".claude/project-profile.yml"),
        b"secret_paths:\n  - secret.key\n",
    )
    .unwrap();
    std::fs::write(repo.join("seed.txt"), b"seed\n").unwrap();
    fixture_git(&repo, &["add", "-A"]);
    fixture_git(&repo, &["commit", "--quiet", "-m", "profile"]);
    let base = String::from_utf8(fixture_git(&repo, &["rev-parse", "HEAD"])).unwrap();
    std::fs::write(repo.join("seed.txt"), b"profile timeout change\n").unwrap();
    git::PROFILE_DEADLINE_AT_ONCE.store(true, std::sync::atomic::Ordering::SeqCst);
    let args = [
        "--cwd".into(),
        repo.clone().into_os_string(),
        "--base".into(),
        base.trim().into(),
        "--out".into(),
        root.join("out.md").into_os_string(),
        "--exclude-glob".into(),
        ".env".into(),
        "--secret-profile-ref".into(),
        base.trim().into(),
    ];
    let output = execute(args.to_vec(), Signals::install().unwrap());
    git::PROFILE_DEADLINE_AT_ONCE.store(false, std::sync::atomic::Ordering::SeqCst);
    json!({"case_id":case["id"],"code":output.code.as_u8(),"stdout_hex":hex(&output.stdout),"stderr_hex":hex(&output.stderr),"published":root.join("out.md").exists()})
}
fn display_stderr_child(case: &Value, root: &Path) -> Value {
    let mut s = snapshot(root);
    let field = bytes_from_hex(case["row_hex"].as_str().unwrap());
    s.rows.push(Row {
        one: field.clone(),
        two: field.clone(),
        three: field.clone(),
        four: field,
        allowed: true,
    });
    output::stderr(&approval::rows(&s)).unwrap();
    json!({"case_id":case["id"],"code":0})
}
fn signal_child(case: &Value) -> Value {
    let guard = Signals::install().unwrap();
    let mut child_completed = false;
    if case["state"] == "child-wait" {
        let result = process::capture_observed(
            Command::new("sh").args(["-c", "read value; printf completed"]),
            b"release\n",
            None,
            |_| handshake("signal-ready"),
        )
        .unwrap();
        assert_eq!(result.code, 0);
        assert_eq!(result.stdout, b"completed");
        child_completed = true;
    } else {
        handshake("signal-ready");
    }
    if case["state"] != "ignored" {
        wait_signal(&guard);
    }
    let received = guard.received();
    json!({"case_id":case["id"],"code":if received.is_some(){20}else{0},"received":received,"child_completed":child_completed})
}
fn run_child(case: &Value) -> Value {
    let root = temp(None, 613).unwrap();
    let python = std::env::var_os("DEV_WORKFLOW_S3D_PYTHON").unwrap_or_else(|| "python3".into());
    let setup = "import os,signal,sys; [signal.signal(s,signal.SIG_DFL) for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)]; signal.signal(getattr(signal,'SIG'+sys.argv[1]),signal.SIG_IGN) if sys.argv[1] else None; os.execv(sys.argv[2],sys.argv[2:])";
    let ignored = if case["state"] == "ignored" {
        case["signal"].as_str().unwrap()
    } else {
        ""
    };
    let mut child = Command::new(python)
        .arg("-c")
        .arg(setup)
        .arg(ignored)
        .arg(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "diff_snapshot::typed_tests::typed_child",
            "--nocapture",
        ])
        .env("DEV_WORKFLOW_S3D_TYPED_CASE", case.to_string())
        .env("DEV_WORKFLOW_S3D_TYPED_ROOT", &root.0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdout = std::io::BufReader::new(child.stdout.take().unwrap());
    let target = case["point"]
        .as_str()
        .unwrap_or(if case["category"] == "signal" {
            "signal-ready"
        } else {
            ""
        });
    let mut notifications = 0;
    if !target.is_empty() {
        let expected = format!("S3D_READY:{target}");
        loop {
            let mut line = String::new();
            assert!(
                stdout.read_line(&mut line).unwrap() > 0,
                "child never reached {target}"
            );
            if line.trim_end() == expected {
                break;
            }
        }
        notifications += 1;
        let status = Command::new("kill")
            .arg(format!("-{}", case["signal"].as_str().unwrap()))
            .arg(child.id().to_string())
            .status()
            .unwrap();
        assert!(status.success());
        child.stdin.as_mut().unwrap().write_all(b"x").unwrap();
    }
    drop(child.stdin.take());
    let mut rest = String::new();
    stdout.read_to_string(&mut rest).unwrap();
    assert!(!rest.contains("S3D_READY:"), "duplicate synchronization");
    let output = child.wait_with_output().unwrap();
    let mut observed: Value = serde_json::from_slice(
        &std::fs::read(root.0.join("observation.json")).unwrap_or_else(|e| {
            panic!(
                "{}: {e}; {}",
                case["id"],
                String::from_utf8_lossy(&output.stderr)
            )
        }),
    )
    .unwrap();
    assert_eq!(
        output.status.code(),
        Some(observed["code"].as_i64().unwrap() as i32)
    );
    if !target.is_empty() {
        assert_eq!(notifications, 1);
    }
    observed["captured_stderr_hex"] = json!(hex(&output.stderr));
    observed
}
fn check_publication(case: &Value, observed: &Value) {
    let success = case["action"] == "success";
    let published =
        success || matches!(case["point"].as_str(), Some("out-after" | "cleanup-before"));
    assert_eq!(
        observed["code"],
        if success { 0 } else { 20 },
        "{}",
        case["id"]
    );
    assert_eq!(
        observed["patch_hex"],
        if published {
            json!(hex(b"NEW_PATCH\n"))
        } else {
            Value::Null
        }
    );
    assert_eq!(
        observed["out_hex"],
        if published {
            json!(hex(b"NEW_REPORT\n"))
        } else if case["old_out"] == true {
            json!(hex(b"OLD_REPORT\n"))
        } else {
            Value::Null
        }
    );
    assert_eq!(observed["temporary_directories"], json!([]));
    assert_eq!(
        observed["sync_target_hits"],
        if case["point"].is_string() { 1 } else { 0 }
    );
    let order = [
        "patch-before",
        "patch-after",
        "out-before",
        "out-after",
        "cleanup-before",
    ];
    let count = if let Some(point) = case["point"].as_str() {
        order.iter().position(|p| *p == point).unwrap() + 1
    } else if case["action"] == "patch-failure" {
        1
    } else if case["action"] == "out-failure" {
        3
    } else {
        5
    };
    assert_eq!(observed["points"], json!(&order[..count]));
}
fn display_expected(input: &[u8]) -> Vec<u8> {
    let text = std::str::from_utf8(input).unwrap();
    text.chars()
        .map(|c| if c.is_control() { '?' } else { c })
        .take(200)
        .collect::<String>()
        .into_bytes()
}
fn bytes_from_hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn complete_legacy(passed: &mut Vec<String>, observations: &[Value]) {
    for observation in observations {
        let id = observation["case_id"].as_str().unwrap();
        let lines: &[u32] = match id {
            "publish-old-success" => &[1251, 1262, 1263, 1274, 1275, 1276],
            "publish-old-out-failure" => &[1755, 1756, 1757, 1758],
            "publish-old-patch-failure" => &[1771, 1772, 1773, 1774, 1775],
            "publish-absent-out-failure" => &[1812, 1813, 1814, 1815],
            "publish-old-patch-after-TERM" => &[1832, 1833, 1835, 1839],
            "publish-absent-patch-after-TERM" => &[1837],
            "publish-old-out-before-TERM" => &[1858, 1859, 1861, 1865],
            "publish-absent-out-before-TERM" => &[1863],
            "publish-old-out-after-TERM" => &[1878, 1879, 1880, 1881],
            _ => &[],
        };
        if id == "publish-old-success" {
            assert_eq!(observation["renames"].as_array().unwrap().len(), 2);
        }
        for line in lines {
            passed.push(format!("legacy-typed-{line:04}"));
        }
    }
    let directory = temp(None, 613).unwrap();
    let mut s = snapshot(&directory.0);
    let controls = (1u8..=31).chain([127]).collect::<Vec<_>>();
    assert_eq!(bytes::sanitize(&controls), vec![b'?'; 32]);
    passed.push("legacy-typed-3878".into());
    let c1 = (0x80u8..=0x9f).flat_map(|c| [0xc2, c]).collect::<Vec<_>>();
    let mixed = ["日本語é€🙂".as_bytes(), &c1].concat();
    assert_eq!(
        bytes::sanitize(&mixed),
        ["日本語é€🙂".as_bytes(), &[b'?'; 32]].concat()
    );
    passed.push("legacy-typed-3877".into());
    let cut = ["あ".repeat(198).as_bytes(), &[0xc2, 0x80, 0xc2, 0x9f, b'z']].concat();
    assert_eq!(
        bytes::display_value(&cut),
        ["あ".repeat(198).as_bytes(), b"??"].concat()
    );
    passed.push("legacy-typed-3879".into());
    s.rows.push(Row {
        one: mixed.clone(),
        two: mixed.clone(),
        three: mixed.clone(),
        four: mixed.clone(),
        allowed: true,
    });
    let field = bytes::sanitize(&mixed);
    let expected = joined(&[&field, b"\t", &field, b"\t", &field, b"\t", &field, b"\n"]);
    assert_eq!(approval::rows(&s), expected);
    s.sections[9] = approval::rows(&s);
    assert_eq!(s.sections[9], expected);
    passed.push("legacy-typed-3881".into());
    let observation = run_child(
        &json!({"id":"display-stderr-columns","category":"display-stderr","row_hex":hex(&mixed)}),
    );
    assert_eq!(observation["captured_stderr_hex"], hex(&expected));
    passed.push("legacy-typed-3882".into());
}
fn fixture_git(root: &Path, args: &[&str]) -> Vec<u8> {
    let output = Command::new("git")
        .current_dir(root)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("LC_ALL", "C")
        .args([
            "-c",
            "user.name=S3D fixture",
            "-c",
            "user.email=s3d@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "fixture Git {:?}: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}
fn repository() -> Temp {
    let root = temp(None, 613).unwrap();
    fixture_git(&root.0, &["init", "--quiet"]);
    std::fs::write(root.0.join("public.txt"), b"before\n").unwrap();
    fixture_git(&root.0, &["add", "--", "public.txt"]);
    fixture_git(&root.0, &["commit", "--quiet", "-m", "fixture"]);
    root
}
fn repository_snapshot(root: &Path) -> Snapshot {
    let mut s = snapshot(root);
    s.args.base = b"HEAD".to_vec();
    s.args.patch_base = b"HEAD".to_vec();
    s.args.cwd = raw(root.as_os_str()).to_vec();
    s.args.out = raw(s.out.as_os_str()).to_vec();
    s.ere = br"(^|/)\.env($|/)".to_vec();
    s
}
fn legacy_mutants(passed: &mut Vec<String>) {
    use std::os::unix::fs::PermissionsExt;
    let root = repository();
    fixture_git(
        &root.0,
        &["config", "diff.s3d.textconv", "printf CONVERTED:"],
    );
    std::fs::write(root.0.join(".gitattributes"), b"public.txt diff=s3d\n").unwrap();
    std::fs::write(root.0.join("public.txt"), b"real change\n").unwrap();
    let mut s = repository_snapshot(&root.0);
    precheck::run(&mut s).unwrap();
    assert!(s.tamper);
    assert!(s.rows.iter().any(|r| r.one == b"diff.s3d.textconv"));
    // The compiled mutation removes only the textconv precheck branch result.
    s.rows.retain(|r| r.one != b"diff.s3d.textconv");
    assert!(s.rows.is_empty());
    s.tamper = false;
    passed.extend(["legacy-typed-0452".into(), "legacy-typed-0453".into()]);
    let out = s.out.clone();
    assert_eq!(finish(s).unwrap(), 0);
    passed.push("legacy-typed-0456".into());
    let report = std::fs::read(out).unwrap();
    assert!(
        report
            .windows(b"+real change".len())
            .any(|b| b == b"+real change")
    );
    assert!(
        !report
            .windows(b"CONVERTED:".len())
            .any(|b| b == b"CONVERTED:")
    );
    passed.extend(["legacy-typed-0458".into(), "legacy-typed-0459".into()]);
    let root = repository();
    let oid = String::from_utf8(trim_lf(fixture_git(&root.0, &["rev-parse", "HEAD"]))).unwrap();
    fixture_git(
        &root.0,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{oid},module"),
        ],
    );
    let module = root.0.join("module");
    std::fs::create_dir(&module).unwrap();
    std::fs::write(module.join("payload"), b"fixture contents\n").unwrap();
    let mut s = repository_snapshot(&root.0);
    precheck::run(&mut s).unwrap();
    assert!(
        s.rows
            .iter()
            .any(|r| r.two == "gitlink に .git 名エントリが無い非空ディレクトリ".as_bytes())
    );
    s.rows.clear();
    s.tamper = false;
    assert_eq!(finish(s).unwrap(), 0);
    passed.extend(["legacy-typed-0637".into(), "legacy-typed-0644".into()]);
    std::fs::set_permissions(&module, std::fs::Permissions::from_mode(0)).unwrap();
    let mut s = repository_snapshot(&root.0);
    s.args.precheck = true;
    let result = precheck::run(&mut s);
    std::fs::set_permissions(&module, std::fs::Permissions::from_mode(0o700)).unwrap();
    result.unwrap();
    assert!(s.tamper);
    let rows = approval::rows(&s);
    assert!(
        rows.windows("gitlink のディレクトリを列挙できない".len())
            .any(|b| b == "gitlink のディレクトリを列挙できない".as_bytes())
    );
    assert!(s.rows.iter().any(|r| !r.allowed));
    assert_eq!(finish(s).unwrap(), 22);
    passed.extend(["legacy-typed-0583".into(), "legacy-typed-0584".into()]);
}

struct ProfileOps {
    root: PathBuf,
    target: SyncPoint,
    action: &'static str,
    calls: usize,
    changes: usize,
    held_changed: Option<bool>,
}
impl Operations for ProfileOps {
    fn sync(&mut self, point: SyncPoint) -> std::io::Result<()> {
        if point != self.target {
            return Ok(());
        }
        self.calls += 1;
        if self.changes > 0 || (point == SyncPoint::ProfileReading && self.calls == 1) {
            return Ok(());
        }
        self.changes += 1;
        let path = self.root.join(".claude/project-profile.yml");
        match self.action {
            "parent-replace" => {
                std::fs::rename(self.root.join(".claude"), self.root.join("old-parent"))?;
                std::fs::create_dir(self.root.join(".claude"))?;
                std::fs::write(&path, b"REPLACEMENT")?;
            }
            "leaf-replace" | "leaf-replace-mtime" => {
                use std::os::unix::fs::MetadataExt;
                let held = std::fs::File::open(&path)?;
                let before = held.metadata()?;
                if self.action == "leaf-replace-mtime" {
                    held.set_modified(std::time::SystemTime::UNIX_EPOCH)?;
                }
                let replacement = self.root.join("replacement");
                std::fs::write(&replacement, b"REPLACEMENT")?;
                std::fs::rename(replacement, &path)?;
                let after = held.metadata()?;
                let fields = |m: &std::fs::Metadata| {
                    (
                        m.dev(),
                        m.ino(),
                        m.mode(),
                        m.len(),
                        m.mtime(),
                        m.mtime_nsec(),
                        m.ctime(),
                        m.ctime_nsec(),
                    )
                };
                self.held_changed = Some(fields(&before) != fields(&after));
            }
            "overwrite" => std::fs::write(&path, b"CHANGED")?,
            "mode" => {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))?;
            }
            "grow" => {
                let f = std::fs::OpenOptions::new().write(true).open(&path)?;
                f.set_len(1048577)?;
            }
            _ => panic!("unknown mutation"),
        }
        Ok(())
    }
}
fn profile_replacements(passed: &mut Vec<String>) -> Vec<Value> {
    let original = vec![b'a'; 131072];
    let mut observations = vec![];
    for point in [
        SyncPoint::ProfileInspected,
        SyncPoint::ProfileOpened,
        SyncPoint::ProfileReading,
        SyncPoint::ProfileRead,
    ] {
        for action in [
            "parent-replace",
            "leaf-replace",
            "leaf-replace-mtime",
            "overwrite",
            "mode",
            "grow",
        ] {
            let root = temp(None, 613).unwrap();
            std::fs::create_dir(root.0.join(".claude")).unwrap();
            std::fs::write(root.0.join(".claude/project-profile.yml"), &original).unwrap();
            let mut ops = ProfileOps {
                root: root.0.clone(),
                target: point,
                action,
                calls: 0,
                changes: 0,
                held_changed: None,
            };
            let result = sfs::current_profile(&root.0, &mut ops);
            assert_eq!(ops.changes, 1);
            let id = format!("{}-{action}", point_name(point));
            if action == "parent-replace"
                || (action == "leaf-replace"
                    && point != SyncPoint::ProfileInspected
                    && ops.held_changed == Some(false))
            {
                assert_eq!(result.as_ref().unwrap().as_ref().unwrap(), &original);
            } else {
                assert!(result.is_err(), "{id}: replacement was not detected");
            }
            let (value, error) = match result {
                Ok(Some(data)) => (
                    json!({"size":data.len(),"sha256":String::from_utf8(sha(&data)).unwrap()}),
                    Value::Null,
                ),
                Ok(None) => panic!("profile disappeared"),
                Err(error) => (Value::Null, json!(error.text(tools::strerror))),
            };
            observations.push(json!({"case_id":id,"point":point_name(point),"action":action,"changes":ops.changes,"held_metadata_changed":ops.held_changed,"value":value,"error":error}));
            passed.push(id);
        }
    }
    observations
}

fn audit_events(passed: &mut Vec<String>) -> Vec<Value> {
    use dev_workflow_platform::diff_snapshot::{AuditEvent, AuditScope, Operation};
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::sync::{Arc, Mutex};
    fn as_json(event: &AuditEvent) -> Value {
        json!({"phase":event.phase,"operation":format!("{:?}",event.operation),"path_hex":hex(raw(event.path.as_os_str())),"dev":event.dev,"ino":event.ino,"fd":event.fd,"bytes":event.bytes,"child_pid":event.child_pid,"arguments_hex":event.arguments.iter().map(|v|hex(v)).collect::<Vec<_>>()})
    }
    let mut observations = vec![];
    for modified in [false, true] {
        for readable in [false, true] {
            let root = repository();
            let secret = root.0.join(".env");
            std::fs::write(&secret, b"S3D_TYPED_SECRET_ORIGINAL\n").unwrap();
            fixture_git(&root.0, &["add", "--", ".env"]);
            fixture_git(&root.0, &["commit", "--quiet", "-m", "secret fixture"]);
            if modified {
                std::fs::write(&secret, b"S3D_TYPED_SECRET_MODIFIED\n").unwrap();
            }
            std::fs::write(root.0.join("public.txt"), b"after\n").unwrap();
            if !readable {
                std::fs::set_permissions(&secret, std::fs::Permissions::from_mode(0)).unwrap();
            }
            let metadata = std::fs::metadata(&secret).unwrap();
            let events = Arc::new(Mutex::new(vec![]));
            let scope = AuditScope::enter(Arc::clone(&events));
            let out = root.0.join("audit.md");
            let patch = root.0.join("audit.patch");
            let args = vec![
                "--cwd".into(),
                root.0.as_os_str().into(),
                "--base".into(),
                "HEAD".into(),
                "--out".into(),
                out.as_os_str().into(),
                "--patch-out".into(),
                patch.as_os_str().into(),
                "--exclude-glob".into(),
                ".env".into(),
            ];
            let result = execute(args, Signals::install().unwrap());
            drop(scope);
            std::fs::set_permissions(&secret, std::fs::Permissions::from_mode(0o600)).unwrap();
            let events = events.lock().unwrap().clone();
            let secret_event = |e: &AuditEvent| {
                matches!(
                    e.operation,
                    Operation::Open | Operation::Read | Operation::Hash
                ) && (e.path == secret
                    || (e.dev == Some(metadata.dev()) && e.ino == Some(metadata.ino())))
            };
            assert!(!events.iter().any(secret_event));
            assert!(events.iter().any(|e| e.operation == Operation::Spawn
                && e.path.file_name().is_some_and(|p| p == "git")));
            assert!(
                events
                    .iter()
                    .filter(|e| e.operation == Operation::Spawn)
                    .all(|e| e.child_pid.is_some() && e.dev.is_some() && e.ino.is_some())
            );
            for data in [&result.stdout, &result.stderr] {
                assert!(
                    !data
                        .windows(b"S3D_TYPED_SECRET".len())
                        .any(|w| w == b"S3D_TYPED_SECRET")
                );
            }
            for file in [&out, &patch] {
                if let Ok(data) = std::fs::read(file) {
                    assert!(
                        !data
                            .windows(b"S3D_TYPED_SECRET".len())
                            .any(|w| w == b"S3D_TYPED_SECRET")
                    );
                }
            }
            let id = format!(
                "audit-typed-{}-{}",
                if readable { "readable" } else { "denied" },
                if modified { "modified" } else { "unchanged" }
            );
            observations.push(json!({"case_id":id,"root_hex":hex(raw(root.0.as_os_str())),"secret":{"path_hex":hex(raw(secret.as_os_str())),"dev":metadata.dev(),"ino":metadata.ino()},"events":events.iter().map(as_json).collect::<Vec<_>>(),"code":result.code.as_u8()}));
            passed.push(id);
        }
    }
    let root = repository();
    let secret = root.0.join(".env");
    std::fs::write(&secret, b"S3D_TYPED_MUTANT\n").unwrap();
    let alias = root.0.join("alias");
    std::fs::hard_link(&secret, &alias).unwrap();
    let metadata = std::fs::metadata(&secret).unwrap();
    for operation in ["read", "hash", "extra-git"] {
        let events = Arc::new(Mutex::new(vec![]));
        let scope = AuditScope::enter(Arc::clone(&events));
        match operation {
            "read" => {
                let _ =
                    sfs::read(&alias, None, "after-classification-mutant", &mut NoAudit).unwrap();
            }
            "hash" => {
                let _ =
                    sfs::read_and_hash(&alias, "after-classification-mutant", &mut NoAudit, sha)
                        .unwrap();
            }
            "extra-git" => {
                let output = Git::new(root.0.clone())
                    .words(&["diff", "--name-only", "HEAD"])
                    .unwrap();
                assert_eq!(output.code, 0);
            }
            _ => unreachable!(),
        }
        drop(scope);
        let events = events.lock().unwrap();
        let detected = if operation == "extra-git" {
            events.iter().any(|e| {
                e.operation == Operation::Spawn && e.arguments.iter().any(|a| a == b"diff")
            })
        } else {
            events.iter().any(|e| {
                e.operation
                    == if operation == "hash" {
                        Operation::Hash
                    } else {
                        Operation::Read
                    }
                    && e.dev == Some(metadata.dev())
                    && e.ino == Some(metadata.ino())
                    && e.bytes > 0
            })
        };
        assert!(detected);
        let id = format!("audit-typed-reject-{operation}");
        observations.push(json!({"case_id":id,"secret":{"path_hex":hex(raw(secret.as_os_str())),"dev":metadata.dev(),"ino":metadata.ino()},"events":events.iter().map(as_json).collect::<Vec<_>>(),"detected":detected}));
        passed.push(id);
    }
    observations
}
#[test]
fn typed_contract() {
    if std::env::var_os("DEV_WORKFLOW_S3D_TYPED_CASE").is_some() {
        return;
    }
    let manifest: Value = serde_json::from_str(MANIFEST).unwrap();
    let mut passed = Vec::<String>::new();
    let mut publication_observations = vec![];
    let mut signal_observations = vec![];
    for case in manifest["vectors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| v["executor"] == "typed")
    {
        match case["category"].as_str().unwrap() {
            "display" => {
                let input = bytes_from_hex(case["input_hex"].as_str().unwrap());
                let expected = display_expected(&input);
                assert_eq!(bytes::display_value(&input), expected, "{}", case["id"]);
            }
            "publish" => {
                let observation = run_child(case);
                check_publication(case, &observation);
                publication_observations.push(observation);
            }
            "signal" => {
                let observation = run_child(case);
                assert_eq!(
                    observation["code"],
                    if case["state"] == "ignored" { 0 } else { 20 }
                );
                signal_observations.push(observation);
            }
            _ => panic!("unimplemented vector {}", case["id"]),
        }
        passed.push(case["id"].as_str().unwrap().into());
    }
    complete_legacy(&mut passed, &publication_observations);
    legacy_mutants(&mut passed);
    let profile_observations = profile_replacements(&mut passed);
    let audit_observations = audit_events(&mut passed);
    let mut tool_observations = tool_failures(&mut passed);
    tool_observations.extend(read_failures(&mut passed));
    let timeout_case = json!({"id":"profile-timeout-injected","category":"profile-timeout"});
    let timeout = run_child(&timeout_case);
    assert_eq!(timeout["code"], 20, "{timeout}");
    passed.push("profile-timeout-injected".into());
    let timeout_observations = vec![timeout];
    let report = json!({"schema":1,"source_sha256":dev_workflow_core::build_info::SOURCE_INPUT_SHA256,"manifest_sha256":String::from_utf8(sha(MANIFEST.as_bytes())).unwrap(),"passed":passed,"publication_observations":publication_observations,"profile_observations":profile_observations,"audit_observations":audit_observations,"signal_observations":signal_observations,"tool_observations":tool_observations,"timeout_observations":timeout_observations});
    if let Some(path) = std::env::var_os("DEV_WORKFLOW_S3D_TYPED_REPORT") {
        std::fs::write(path, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    }
}

// ── Nの方式: 起動しない道具の失敗を、型で差し替えた操作で起こす ──
const LAUNCH_SITES: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../ci/diff_snapshot_fixtures/launch_sites.json"
));
/// The table text for one diagnostic, with each `{name}` replaced.
fn site_text(line: u32, name: &str, fill: &[(&str, Vec<u8>)]) -> Vec<u8> {
    let table: Value = serde_json::from_str(LAUNCH_SITES).unwrap();
    let site = table["sites"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["lines"].as_array().unwrap().iter().any(|l| l == line))
        .unwrap_or_else(|| panic!("no launch site for line {line}"));
    let diagnostic = site["diagnostics"]
        .as_array()
        .unwrap()
        .iter()
        .find(|d| d["name"] == name)
        .unwrap_or_else(|| panic!("no diagnostic {name} for line {line}"));
    let mut text = diagnostic["template"].as_str().unwrap().as_bytes().to_vec();
    for (key, value) in fill {
        let marker = format!("{{{key}}}").into_bytes();
        let at = text
            .windows(marker.len())
            .position(|w| w == marker.as_slice())
            .unwrap_or_else(|| panic!("{key} is not in the template"));
        text.splice(at..at + marker.len(), value.iter().copied());
    }
    assert!(!text.contains(&b'{'), "unfilled template for line {line}");
    text
}
struct ToolOps {
    action: &'static str,
    copies: Vec<(PathBuf, PathBuf)>,
    renames: Vec<(PathBuf, PathBuf)>,
    root: PathBuf,
}
impl Operations for ToolOps {
    fn sync(&mut self, point: SyncPoint) -> std::io::Result<()> {
        if self.action == "mv-1194" && point == SyncPoint::BeforeReport {
            // A directory took the place of --out after its check; it already holds out.tmp.
            let _ = std::fs::remove_file(self.root.join("out.md"));
            std::fs::create_dir_all(self.root.join("out.md/out.tmp")).unwrap();
        }
        Ok(())
    }
    fn rename(&mut self, from: &Path, to: &Path) -> std::result::Result<(), Vec<u8>> {
        self.renames.push((from.into(), to.into()));
        if self.action == "mv-1192" && to.extension().is_some_and(|s| s == "patch") {
            return tools::move_path_with(from, to, |_, _| {
                Err(std::io::Error::from_raw_os_error(13))
            });
        }
        tools::move_path(from, to)
    }
    fn copy(&mut self, from: &Path, data: &[u8], to: &Path) -> std::result::Result<(), Vec<u8>> {
        use std::io::Write;
        self.copies.push((from.into(), to.into()));
        let name = to.file_name().unwrap().to_str().unwrap();
        match (self.action, name) {
            ("cp-1187", "out.tmp") => tools::copy_bytes_with(
                from,
                data,
                to,
                |_| Err::<std::fs::File, _>(std::io::Error::from_raw_os_error(13)),
                |_, _| Ok(()),
            ),
            ("cp-1191", "patch.tmp") => tools::copy_bytes_with(
                from,
                data,
                to,
                |path: &Path| std::fs::File::create(path),
                |_, _| Err(std::io::Error::from_raw_os_error(27)),
            ),
            _ => tools::copy_bytes_with(
                from,
                data,
                to,
                |path: &Path| std::fs::File::create(path),
                |file: &mut std::fs::File, data: &[u8]| file.write_all(data),
            ),
        }
    }
}
fn publication_failure(action: &'static str) -> (Failure, ToolOps, PathBuf, Temp) {
    let root = temp(None, 613).unwrap();
    std::fs::write(root.0.join("out.md"), b"OLD_REPORT\n").unwrap();
    let s = snapshot(&root.0);
    let mut ops = ToolOps {
        action,
        copies: vec![],
        renames: vec![],
        root: root.0.clone(),
    };
    let failure = publish::publish(&s, b"NEW_REPORT\n", &mut ops).unwrap_err();
    let tmpd = s.temp.0.clone();
    drop(s);
    let left: Vec<_> = std::fs::read_dir(&root.0)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .filter(|n| n.to_string_lossy().starts_with(".diff-snapshot."))
        .collect();
    assert!(left.is_empty(), "{action}: publication directories left");
    assert!(!tmpd.exists(), "{action}: TMPD left");
    (failure, ops, tmpd, root)
}
fn quoted(path: &Path) -> Vec<u8> {
    tools::quoteaf(raw(path.as_os_str()))
}
fn tool_failures(passed: &mut Vec<String>) -> Vec<Value> {
    let mut observations = vec![];
    let mut record = |id: &str, failure: &Failure, expected: &[u8], passed: &mut Vec<String>| {
        assert_eq!(failure.code, 20, "{id}");
        assert_eq!(
            String::from_utf8_lossy(&failure.stderr),
            String::from_utf8_lossy(expected),
            "{id}"
        );
        observations
            .push(json!({"case_id":id,"code":failure.code,"stderr_hex":hex(&failure.stderr)}));
        passed.push(id.into());
    };
    // 1187: cp cannot create PUB/out.tmp.
    let (failure, ops, _, root) = publication_failure("cp-1187");
    let expected = joined(&[
        &site_text(1187, "create", &[("dest", quoted(&ops.copies[0].1))]),
        &Failure::unexpected(1, 1187).stderr,
    ]);
    record("tool-n-cp-1187", &failure, &expected, passed);
    assert_eq!(
        std::fs::read(root.0.join("out.md")).unwrap(),
        b"OLD_REPORT\n"
    );
    assert!(!root.0.join("out.patch").exists());
    // 1191: cp fails while writing PUB2/patch.tmp (EFBIG).
    let (failure, ops, tmpd, root) = publication_failure("cp-1191");
    let (from, to) = &ops.copies[1];
    assert_eq!(from, &tmpd.join("patch.raw"));
    let expected = joined(&[
        &site_text(
            1191,
            "write",
            &[("source", quoted(from)), ("dest", quoted(to))],
        ),
        &Failure::unexpected(1, 1191).stderr,
    ]);
    record("tool-n-cp-1191", &failure, &expected, passed);
    assert_eq!(
        std::fs::read(root.0.join("out.md")).unwrap(),
        b"OLD_REPORT\n"
    );
    assert!(!root.0.join("out.patch").exists());
    // 1192: mv of the patch is refused.
    let (failure, ops, _, root) = publication_failure("mv-1192");
    let (from, to) = &ops.renames[0];
    let expected = joined(&[
        &site_text(
            1192,
            "denied",
            &[("source", quoted(from)), ("dest", quoted(to))],
        ),
        "ERROR [internal] パッチの公開に失敗した(何も公開していない)\n".as_bytes(),
    ]);
    record("tool-n-mv-1192", &failure, &expected, passed);
    assert_eq!(
        std::fs::read(root.0.join("out.md")).unwrap(),
        b"OLD_REPORT\n"
    );
    assert!(!root.0.join("out.patch").exists());
    // 1194: a directory replaced --out; mv cannot overwrite out.md/out.tmp.
    let (failure, _, _, root) = publication_failure("mv-1194");
    let target = root.0.join("out.md/out.tmp");
    let expected = joined(&[
        &site_text(1194, "directory", &[("dest", quoted(&target))]),
        "ERROR [internal] 出力の公開に失敗した\n".as_bytes(),
    ]);
    record("tool-n-mv-1194", &failure, &expected, passed);
    assert!(target.is_dir());
    assert!(
        !root.0.join("out.patch").exists(),
        "1195 removes the published patch"
    );
    // 247: rm -rf "$TMPD" in the EXIT trap after a normal exit.
    use std::os::unix::fs::PermissionsExt;
    let mut routes = vec![];
    for route in [Route::Normal, Route::Trapped, Route::Signal] {
        CLEANUP_STOP.with(|s| s.borrow_mut().take());
        CLEANUP_ACTIVE.with(|a| a.set(true));
        let t = temp(None, 613).unwrap();
        let blocked = t.0.join("b");
        std::fs::create_dir(&blocked).unwrap();
        std::fs::write(blocked.join("f"), b"x").unwrap();
        std::fs::set_permissions(&blocked, std::fs::Permissions::from_mode(0o555)).unwrap();
        let path = t.0.clone();
        drop(t);
        let result = match route {
            Route::Normal => after_cleanup(Ok(0), route),
            Route::Trapped => after_cleanup(Err(Failure::unexpected(1, 1000)), route),
            Route::Signal => after_cleanup(Err(Failure::silent(20)), route),
        };
        std::fs::set_permissions(&blocked, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::remove_dir_all(&path).unwrap();
        let diagnostic = site_text(247, "unremovable", &[("path", quoted(&blocked.join("f")))]);
        let failure = result.unwrap_err();
        let trap = Failure::unexpected(1, 247).stderr;
        match route {
            Route::Normal => {
                record(
                    "tool-n-rm-247",
                    &failure,
                    &joined(&[&diagnostic, &trap]),
                    passed,
                );
            }
            Route::Trapped => {
                assert_eq!(failure.code, 1);
                assert_eq!(
                    failure.stderr,
                    joined(&[&Failure::unexpected(1, 1000).stderr, &diagnostic])
                );
            }
            Route::Signal => {
                assert_eq!(failure.code, 1);
                assert_eq!(failure.stderr, joined(&[&diagnostic, &trap, &diagnostic]));
            }
        }
        routes.push(failure.code);
    }
    assert_eq!(routes, [20, 1, 1]);
    // 1347・1416・1472: the size read `wc -c <FILE` cannot open FILE.
    for (line, point, name) in [
        (1347u32, "tracked-size", "public.txt"),
        (1416, "outside-size", "outside.txt"),
        (1472, "untracked-size", "fresh.txt"),
    ] {
        let root = repository();
        std::fs::write(root.0.join("public.txt"), b"changed\n").unwrap();
        std::fs::write(root.0.join("outside.txt"), b"outside\n").unwrap();
        std::fs::write(root.0.join("fresh.txt"), b"fresh\n").unwrap();
        let mut s = repository_snapshot(&root.0);
        s.pre.insert(b"outside.txt".to_vec());
        precheck::run(&mut s).unwrap();
        let failure = {
            let _fault = dev_workflow_platform::diff_snapshot::FaultScope::enter(point, 13);
            finish(s).unwrap_err()
        };
        let expected = joined(&[
            &site_text(
                line,
                "redirect",
                &[
                    ("script", b"diff-snapshot.sh".to_vec()),
                    ("line", line.to_string().into_bytes()),
                    ("path", name.as_bytes().to_vec()),
                ],
            ),
            &Failure::substitution(1, line as usize).stderr,
        ]);
        record(
            &format!("tool-n-redirect-{line}"),
            &failure,
            &expected,
            passed,
        );
    }
    observations
}

// ── Nの方式: リダイレクトで読むファイルの失敗(開けない・読めない)を地点ごとに ──
const EACCES: i32 = 13;
const EISDIR: i32 = 21;
fn captured<T>(run: impl FnOnce() -> T) -> (T, Vec<u8>) {
    output::CAPTURE.with(|c| *c.borrow_mut() = Some(vec![]));
    let value = run();
    let written = output::CAPTURE.with(|c| c.borrow_mut().take()).unwrap();
    (value, written)
}
fn redirect_text(line: u32, path: &[u8]) -> Vec<u8> {
    site_text(
        line,
        "redirect",
        &[
            ("script", b"diff-snapshot.sh".to_vec()),
            ("line", line.to_string().into_bytes()),
            ("path", path.to_vec()),
        ],
    )
}
/// The diagnostic for a fault at a `sha256_file` site, then the ERR traps of
/// the substitution (279) and of the assignment.
fn sha_text(stage: FaultStage, path: &[u8], line: usize) -> Vec<u8> {
    let diagnostic = match stage {
        FaultStage::Open => redirect_text(279, path),
        FaultStage::Read => site_text(279, "read", &[]),
    };
    joined(&[
        &diagnostic,
        &Failure::unexpected(1, 279).stderr,
        &Failure::unexpected(20, line).stderr,
    ])
}
fn stage_name(stage: FaultStage) -> &'static str {
    match stage {
        FaultStage::Open => "open",
        FaultStage::Read => "read",
    }
}
fn read_failures(passed: &mut Vec<String>) -> Vec<Value> {
    use dev_workflow_platform::diff_snapshot::FaultScope;
    let mut observations = vec![];
    let mut record = |id: String, code: u8, stderr: &[u8], expected_code: u8, expected: &[u8]| {
        assert_eq!(code, expected_code, "{id}");
        assert_eq!(
            String::from_utf8_lossy(stderr),
            String::from_utf8_lossy(expected),
            "{id}"
        );
        observations.push(json!({"case_id":id,"code":code,"stderr_hex":hex(stderr)}));
        passed.push(id);
    };
    for stage in [FaultStage::Open, FaultStage::Read] {
        let errno = if stage == FaultStage::Open {
            EACCES
        } else {
            EISDIR
        };
        // 694: GOT_SHA="$(sha256_file "$PRE_LIST")".
        let root = repository();
        let list = root.0.join("pre.list");
        std::fs::write(&list, b"").unwrap();
        let out = root.0.join("out.md");
        let args: Vec<OsString> = vec![
            "--cwd".into(),
            root.0.clone().into_os_string(),
            "--base".into(),
            "HEAD".into(),
            "--out".into(),
            out.into_os_string(),
            "--exclude-glob".into(),
            ".env".into(),
            "--pre-untracked".into(),
            list.clone().into_os_string(),
            "--pre-untracked-sha256".into(),
            String::from_utf8(sha(b"")).unwrap().into(),
        ];
        let (result, written) = captured(|| {
            let _fault = FaultScope::enter_at("pre-list", stage, errno);
            execute(args, Signals::install().unwrap())
        });
        let stderr = joined(&[&written, &result.stderr]);
        let expected = sha_text(stage, raw(list.as_os_str()), 694);
        record(
            format!("tool-n-read-0694-{}", stage_name(stage)),
            result.code.as_u8(),
            &stderr,
            20,
            &expected,
        );
        // 807: ia_sha="$(sha256_file "$INFO_ATTR")".
        let root = repository();
        let info = root.0.join(".git/info/attributes");
        std::fs::create_dir_all(info.parent().unwrap()).unwrap();
        std::fs::write(&info, b"* -diff\n").unwrap();
        let mut s = repository_snapshot(&root.0);
        let failure = {
            let _fault = FaultScope::enter_at("info-attributes", stage, errno);
            precheck::run(&mut s).unwrap_err()
        };
        record(
            format!("tool-n-read-0807-{}", stage_name(stage)),
            failure.code,
            &failure.stderr,
            20,
            // 実体のパス: 旧 801 行は `git rev-parse --path-format=absolute --git-path` の出力を使い、git が
            // シンボリックリンクを実体へ解決する(0694 の一覧は渡したままのパスなので `raw(list)`)。
            &sha_text(stage, raw(info.canonicalize().unwrap().as_os_str()), 807),
        );
        // 1418: sha="$(sha256_file "$f")" of a file present before (outside).
        let root = repository();
        std::fs::write(root.0.join("outside.txt"), b"outside\n").unwrap();
        let mut s = repository_snapshot(&root.0);
        s.pre.insert(b"outside.txt".to_vec());
        precheck::run(&mut s).unwrap();
        let failure = {
            let _fault = FaultScope::enter_at("outside-hash", stage, errno);
            finish(s).unwrap_err()
        };
        record(
            format!("tool-n-read-1418-{}", stage_name(stage)),
            failure.code,
            &failure.stderr,
            20,
            &sha_text(stage, b"outside.txt", 1418),
        );
        // 1346・1474: `if head_has_nul FILE`: both pipelines (1314・1315) report,
        // nothing stops, and the file is not binary.
        for (line, point, name) in [
            (1346u32, "tracked-binary", "public.txt"),
            (1474, "untracked-binary", "fresh.txt"),
        ] {
            let root = repository();
            let nul = |file: &str| if file == name { &b"\0"[..] } else { b"" };
            std::fs::write(
                root.0.join("public.txt"),
                joined(&[b"changed", nul("public.txt"), b"\n"]),
            )
            .unwrap();
            std::fs::write(
                root.0.join("fresh.txt"),
                joined(&[b"fresh", nul("fresh.txt"), b"\n"]),
            )
            .unwrap();
            let mut s = repository_snapshot(&root.0);
            precheck::run(&mut s).unwrap();
            let (result, written) = captured(|| {
                let _fault = FaultScope::enter_at(point, stage, errno);
                finish(s)
            });
            let one = match stage {
                FaultStage::Open => joined(&[
                    &redirect_text(1314, name.as_bytes()),
                    &redirect_text(1315, name.as_bytes()),
                ]),
                FaultStage::Read => {
                    let text = site_text(1314, "read", &[]);
                    joined(&[&text, &text])
                }
            };
            let code = result.as_ref().map_or_else(|f| f.code, |c| *c);
            record(
                format!("tool-n-head-{line}-{}", stage_name(stage)),
                code,
                &written,
                0,
                &one,
            );
            // Not binary: the file's text is in the report (a NUL included).
            let report = std::fs::read(root.0.join("out.md")).unwrap();
            assert!(
                report.windows(name.len()).any(|w| w == name.as_bytes())
                    && !report
                        .windows("バイナリ".len())
                        .any(|w| w == "バイナリ".as_bytes()),
                "tool-n-head-{line}: the file was taken as binary: {}",
                String::from_utf8_lossy(&report)
            );
        }
    }
    // 1346 then 1347: a file that cannot be opened at all (`/proc/PID/mem` of
    // another process): the two head diagnostics, then the size read stops.
    let root = repository();
    std::fs::write(root.0.join("public.txt"), b"changed\n").unwrap();
    let mut s = repository_snapshot(&root.0);
    precheck::run(&mut s).unwrap();
    let (result, written) = captured(|| {
        let _fault = FaultScope::enter_all(&[
            ("tracked-binary", FaultStage::Open, EACCES),
            ("tracked-size", FaultStage::Open, EACCES),
        ]);
        finish(s)
    });
    let failure = result.unwrap_err();
    let expected = joined(&[
        &redirect_text(1314, b"public.txt"),
        &redirect_text(1315, b"public.txt"),
        &redirect_text(1347, b"public.txt"),
        &Failure::substitution(1, 1347).stderr,
    ]);
    record(
        "tool-n-head-size-1347".into(),
        failure.code,
        &joined(&[&written, &failure.stderr]),
        20,
        &expected,
    );
    // 978: `for p in …; do printf '%s\0' "$p" >>"$TMPD/cand.z"; done` cannot write:
    // Bash reports the `for` line (977 only truncates the file).
    let root = repository();
    fixture_git(&root.0, &["update-index", "--skip-worktree", "public.txt"]);
    fixture_git(&root.0, &["config", "core.sparseCheckout", "true"]);
    std::fs::remove_file(root.0.join("public.txt")).unwrap();
    std::fs::create_dir_all(root.0.join(".git/info")).unwrap();
    std::fs::write(root.0.join(".git/info/sparse-checkout"), b"/other\n").unwrap();
    let mut s = repository_snapshot(&root.0);
    let failure = {
        let _fault = FaultScope::enter("cand.z", 27);
        precheck::run(&mut s).unwrap_err()
    };
    let expected = joined(&[
        &site_text(
            978,
            "write",
            &[
                ("script", b"diff-snapshot.sh".to_vec()),
                ("line", b"978".to_vec()),
            ],
        ),
        &Failure::unexpected(1, 978).stderr,
    ]);
    record(
        "tool-n-printf-0978".into(),
        failure.code,
        &failure.stderr,
        20,
        &expected,
    );
    // 1411・1462: `lnk="$(readlink -- "$f" 2>/dev/null; printf x)"`: a failed
    // readlink leaves the value empty and nothing is reported.
    for (point, id) in [
        ("outside-readlink", "tool-n-readlink-1411"),
        ("untracked-readlink", "tool-n-readlink-1462"),
    ] {
        let mut reports = vec![];
        for fault in [false, true] {
            let root = repository();
            std::os::unix::fs::symlink("public.txt", root.0.join("link")).unwrap();
            let mut s = repository_snapshot(&root.0);
            if point == "outside-readlink" {
                s.pre.insert(b"link".to_vec());
            }
            s.args.max_bytes = b"3".to_vec();
            precheck::run(&mut s).unwrap();
            let (result, written) = captured(|| {
                let _fault = fault.then(|| FaultScope::enter(point, 2));
                finish(s)
            });
            assert!(result.is_ok() && written.is_empty(), "{id}");
            reports.push(std::fs::read(root.0.join("out.md")).unwrap());
        }
        let has = |report: &[u8], text: &[u8]| report.windows(text.len()).any(|w| w == text);
        if point == "outside-readlink" {
            // The row hashes the empty value (size 0) instead of the target.
            assert!(
                has(&reports[0], &sha(b"public.txt")) && !has(&reports[0], &sha(b"")),
                "{id}"
            );
            assert!(
                has(&reports[1], &sha(b"")) && !has(&reports[1], &sha(b"public.txt")),
                "{id}"
            );
        } else {
            // Size 0 stays under --max-untracked-bytes 3; the target (10) does not.
            let cap = "総量上限".as_bytes();
            assert!(has(&reports[0], cap) && !has(&reports[1], cap), "{id}");
        }
        observations.push(json!({"case_id":id,"code":0,"stderr_hex":""}));
        passed.push(id.into());
    }
    observations
}

/// `case … in "$TOP"|"$TOP"/*)` compares strings: with a TOP of `/`, the
/// pattern `//*` matches no resolved path (only `/` itself is inside).
#[test]
fn top_containment_follows_the_case_patterns() {
    for (path, top, expected) in [
        ("/", "/", true),
        ("/x", "/", false),
        ("/a", "/a", true),
        ("/a/b", "/a", true),
        ("/ab", "/a", false),
        ("/a/", "/a", true),
    ] {
        assert_eq!(
            inside(Path::new(path), Path::new(top)),
            expected,
            "{path} in {top}"
        );
    }
}
/// The fixed glibc 2.39 table, not Rust's `io::Error` text (musl's in the
/// static Linux build), for the stdout write error and the profile reasons.
#[cfg(target_os = "linux")]
#[test]
fn reasons_use_the_fixed_strerror_table() {
    let eio = std::io::Error::from_raw_os_error(5);
    let failure = output::write_failure(&eio, 567);
    assert!(
        failure
            .stderr
            .starts_with(b"diff-snapshot.sh: line 567: printf: write error: Input/output error\n")
    );
    let profile = sfs::ProfileError::Os(
        "profile の親を安全に開けない",
        std::io::Error::from_raw_os_error(5),
    );
    let failure = secret_profiles::current_profile_error(&profile);
    assert!(
        String::from_utf8_lossy(&failure.stderr)
            .contains("profile の親を安全に開けない: Input/output error")
    );
    let failure = sha_failure(
        b".git/info/attributes",
        sfs::ReadFailure::Open(std::io::Error::from_raw_os_error(5)),
        807,
    );
    assert!(
        failure
            .stderr
            .starts_with(b"diff-snapshot.sh: line 279: .git/info/attributes: Input/output error\n")
    );
}
