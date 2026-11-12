//! 旧テスト(`scripts/test_restore_taskmd.py`)のうち、内部を差し替える 46 件に対応する検査と、
//! 書き出しの flush の順と回数・EINTR の検査。`cargo test -p dev-workflow-cli --example r4r_contract`
//! だけで流れる。旧テストの名前と同じ名前にし、対応表(`native/ci/r4r_test_map.json`)で結ぶ。
//!
//! - 差し込み点(`Point`)は `cfg(test)` のときだけ働く。外した経路は製品と同じコードを通る。
//! - 信号の記録はプロセス全体の状態なので、全件を `serial` で 1 件ずつ流す。
//! - 成功のコピーが要る件は Linux だけで流す(Mac の成功には数えない)。
// Linux だけの件を外した Mac では、その件だけが使う部品が未使用になる。
#![cfg_attr(not(target_os = "linux"), allow(dead_code, unused_imports))]
use super::child::ChildProcess;
use super::operation::{Call, Error, Point, Restore, Temp};
use super::response::{ARGUMENT_RESPONSE, Output, RawWrite, unraisable_lines};
use super::{Emitted, emit, exit_close, exit_flush};
use dev_workflow_platform::restore_taskmd::{self as os, Stat};
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

// ---- 差し込み点 ----

/// 差し込み点の動作。
pub(super) enum Action {
    Continue,
    /// `Failure(reason)` を起こす。
    Fail(&'static str),
    /// OS の操作を失敗させる(番号)。`StatAt` では不在(`stat_at` が None)を表す。
    Errno(i32),
    /// 想定外の例外(panic)。
    Panic,
}

pub(super) type Hook = Box<dyn FnMut(&mut Restore, &Point) -> Action>;

impl Restore {
    pub(super) fn at(&mut self, point: Point) -> Result<(), Error> {
        let Some(mut hook) = self.hook.take() else {
            return Ok(());
        };
        let action = hook(self, &point);
        self.hook = Some(hook);
        match action {
            Action::Continue | Action::Errno(_) => Ok(()),
            Action::Fail(reason) => Err(Error::Failure(reason)),
            Action::Panic => panic!("PRIVATE-CONTENT"),
        }
    }

    pub(super) fn inject(&mut self, call: Call) -> Option<io::Error> {
        let mut hook = self.hook.take()?;
        let action = hook(self, &Point::Os(call));
        self.hook = Some(hook);
        match action {
            Action::Errno(number) => Some(io::Error::from_raw_os_error(number)),
            Action::Panic => panic!("PRIVATE-CONTENT"),
            _ => None,
        }
    }

    pub(super) fn absent(&mut self, name: &[u8]) -> bool {
        let Some(mut hook) = self.hook.take() else {
            return false;
        };
        let action = hook(self, &Point::StatAt(name.to_vec()));
        self.hook = Some(hook);
        matches!(action, Action::Errno(n) if n == os::errno::ENOENT)
    }

    pub(super) fn adjust_stat(&mut self, path: &[u8], info: &mut Stat) {
        if SHIFT_ALIAS_DEVICE.with(|shift| *shift.borrow())
            && (path.starts_with(b"/proc/self/fd/") || path.starts_with(b"/dev/fd/"))
        {
            info.dev = info.dev.wrapping_add(1);
        }
    }

    /// 旧テストの `op.deadline = 0` / `op.deadline = now + secs`。
    fn set_deadline(&mut self, after: Duration) {
        self.budget =
            dev_workflow_core::budget::Budget::new(self.budget.limits(), Instant::now() + after);
    }
}

type EmitHook = Box<dyn FnMut(EmitPoint) -> Option<io::Error>>;

thread_local! {
    static SHIFT_ALIAS_DEVICE: RefCell<bool> = const { RefCell::new(false) };
    static EMIT_HOOK: RefCell<Option<EmitHook>> = const { RefCell::new(None) };
}

/// 書き出しの 1 段目の差し込み点。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum EmitPoint {
    /// mask を設定する直前(Some を返すと設定の失敗)。
    Block,
    /// mask を設定した直後。
    Blocked,
    /// 応答を確定した直後(応答を作る前)。
    Response,
    /// 明示の flush の直前。
    Flush,
    /// mask を戻す直前(Some を返すと戻しの失敗)。
    Unblock,
}

pub(super) fn emit_point(point: EmitPoint) -> Option<io::Error> {
    EMIT_HOOK.with(|hook| hook.borrow_mut().as_mut().and_then(|hook| hook(point)))
}

fn set_emit_hook(hook: Option<EmitHook>) {
    EMIT_HOOK.with(|slot| *slot.borrow_mut() = hook);
}

/// 旧テストの `op.on_signal(number)` の直接の呼び出し。mask と関係なく「記録したことにする」。
/// 本物の記録と同じく、最初の 1 つだけを残す。
static PRETENDED: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);

fn pretend_signal(number: i32) {
    if os::recorded_signal().is_none() {
        let _ = PRETENDED.compare_exchange(
            0,
            number,
            std::sync::atomic::Ordering::SeqCst,
            std::sync::atomic::Ordering::SeqCst,
        );
    }
}

pub(super) fn pretended_signal() -> Option<i32> {
    match PRETENDED.load(std::sync::atomic::Ordering::SeqCst) {
        0 => None,
        number => Some(number),
    }
}

pub(super) fn reset_pretended() {
    PRETENDED.store(0, std::sync::atomic::Ordering::SeqCst);
}

// ---- 共通の準備 ----

static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn hook(f: impl FnMut(&mut Restore, &Point) -> Action + 'static) -> Option<Hook> {
    Some(Box::new(f))
}

fn bytes(path: &Path) -> Vec<u8> {
    path.as_os_str().as_bytes().to_vec()
}

fn path(raw: &[u8]) -> PathBuf {
    PathBuf::from(OsStr::from_bytes(raw))
}

fn digest(path: &Path) -> String {
    let data = std::fs::read(path).unwrap();
    Sha256::digest(&data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn mode_of(path: &Path) -> u32 {
    std::fs::symlink_metadata(path)
        .unwrap()
        .permissions()
        .mode()
        & 0o7777
}

/// PATH の `cp` の実体(Linux)。Mac は GNU を名乗る代用品。
fn system_cp(root: &Path) -> PathBuf {
    if cfg!(target_os = "linux") {
        let path = std::env::var_os("PATH").unwrap_or_default();
        for dir in std::env::split_paths(&path) {
            let candidate = dir.join("cp");
            if candidate.is_file() {
                return candidate.canonicalize().unwrap();
            }
        }
        panic!("cp が無い");
    }
    script(
        root,
        "gnu-cp",
        "if [ \"$1\" = --version ]; then echo 'cp (GNU coreutils) fixture'; exit 0; fi\nexit 1\n",
    )
}

fn script(root: &Path, name: &str, body: &str) -> PathBuf {
    let path = root.join(name);
    std::fs::write(&path, format!("#!/bin/sh\n{body}")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    path
}

static COUNTER: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

struct Fixture {
    root: PathBuf,
    source: PathBuf,
    destination: PathBuf,
    before: (Vec<u8>, u32),
    cp: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let number = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!("r4r-unit-{}-{number}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let source = root.join("taskmd-body");
        let destination = root.join("current");
        std::fs::write(&source, b"baseline\x00\xff\n").unwrap();
        std::fs::set_permissions(&source, std::fs::Permissions::from_mode(0o640)).unwrap();
        std::fs::write(&destination, b"current\x00contents\n").unwrap();
        std::fs::set_permissions(&destination, std::fs::Permissions::from_mode(0o600)).unwrap();
        let cp = system_cp(&root);
        Self {
            before: (b"current\x00contents\n".to_vec(), 0o600),
            root,
            source,
            destination,
            cp,
        }
    }

    fn argv(&self, cp: Option<&Path>) -> Vec<OsString> {
        let mode = format!("{:o}", mode_of(&self.source));
        [
            OsString::from("restore"),
            "--source".into(),
            self.source.clone().into(),
            "--destination".into(),
            self.destination.clone().into(),
            "--expected-mode".into(),
            mode.into(),
            "--expected-sha256".into(),
            digest(&self.source).into(),
            "--cp".into(),
            cp.unwrap_or(&self.cp).into(),
        ]
        .to_vec()
    }

    fn operation(&self) -> Restore {
        Restore::new(super::parse_request(&self.argv(None)).unwrap())
    }

    fn operation_with_cp(&self, cp: &Path) -> Restore {
        Restore::new(super::parse_request(&self.argv(Some(cp))).unwrap())
    }

    fn reset_destination(&self) {
        std::fs::write(&self.destination, &self.before.0).unwrap();
        std::fs::set_permissions(
            &self.destination,
            std::fs::Permissions::from_mode(self.before.1),
        )
        .unwrap();
    }

    fn unchanged(&self) {
        assert_eq!(
            (
                std::fs::read(&self.destination).unwrap(),
                mode_of(&self.destination)
            ),
            self.before
        );
    }

    fn restored(&self) {
        assert_eq!(
            std::fs::read(&self.destination).unwrap(),
            std::fs::read(&self.source).unwrap()
        );
        assert_eq!(mode_of(&self.destination), mode_of(&self.source));
    }

    fn temp_dirs(&self, parent: &Path) -> Vec<PathBuf> {
        std::fs::read_dir(parent)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|p| {
                p.file_name()
                    .unwrap()
                    .as_bytes()
                    .starts_with(b".restore-taskmd-")
            })
            .collect()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fn writable(path: &Path) {
            if let Ok(meta) = std::fs::symlink_metadata(path)
                && meta.is_dir()
            {
                let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
                if let Ok(entries) = std::fs::read_dir(path) {
                    for entry in entries.flatten() {
                        writable(&entry.path());
                    }
                }
            }
        }
        writable(&self.root);
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

/// C 風引用を独立に戻す(UTF-8 として読まない)。
fn unquote(raw: &[u8]) -> Vec<u8> {
    if raw.first() != Some(&b'"') {
        return raw.to_vec();
    }
    let inner = &raw[1..raw.len() - 1];
    let mut out = Vec::new();
    let mut i = 0;
    while i < inner.len() {
        if inner[i] != b'\\' {
            out.push(inner[i]);
            i += 1;
            continue;
        }
        i += 1;
        let escaped = match inner[i] {
            b'a' => Some(7),
            b'b' => Some(8),
            b't' => Some(9),
            b'n' => Some(10),
            b'v' => Some(11),
            b'f' => Some(12),
            b'r' => Some(13),
            b'"' => Some(34),
            b'\\' => Some(92),
            _ => None,
        };
        match escaped {
            Some(c) => {
                out.push(c);
                i += 1;
            }
            None => {
                let text = std::str::from_utf8(&inner[i..i + 3]).unwrap();
                out.push(u8::from_str_radix(text, 8).unwrap());
                i += 3;
            }
        }
    }
    out
}

/// 旧テストの `fields`。応答の形を確かめて値の表を返す。
fn fields(rc: u8, output: &[u8]) -> BTreeMap<String, Vec<u8>> {
    assert!(output.len() <= 4 * 1024 * 1024);
    assert!(output.ends_with(b"\n"), "{output:?}");
    let lines: Vec<&[u8]> = output[..output.len() - 1].split(|c| *c == b'\n').collect();
    let mut keys = Vec::new();
    let mut values = BTreeMap::new();
    for line in lines {
        let index = line.iter().position(|c| *c == b'=').expect("=");
        let key = String::from_utf8(line[..index].to_vec()).unwrap();
        keys.push(key.clone());
        assert!(values.insert(key, line[index + 1..].to_vec()).is_none());
    }
    let mut expected = vec!["RESTORED", "TOUCHED", "TEMP"];
    assert!([&b"none"[..], b"copied", b"replaced", b"unknown"].contains(&&values["TOUCHED"][..]));
    assert!([&b"none"[..], b"removed", b"retained", b"unknown"].contains(&&values["TEMP"][..]));
    if rc == 0 {
        assert_eq!(values["RESTORED"], b"yes");
        assert!([&b"copied"[..], b"replaced"].contains(&&values["TOUCHED"][..]));
        assert_eq!(values["TEMP"], b"removed");
    } else {
        assert!(rc == 20 || rc == 33, "{rc}");
        assert_eq!(values["RESTORED"], b"no");
        expected.push("REASON");
        if rc == 20 {
            assert_eq!(values["REASON"], b"interrupted");
        }
    }
    if values["TEMP"] == b"retained" {
        expected.push("TEMP_PATH");
        let temp = path(&unquote(&values["TEMP_PATH"]));
        assert!(temp.is_dir(), "{temp:?}");
        assert!(
            temp.file_name()
                .unwrap()
                .as_bytes()
                .starts_with(b".restore-taskmd-")
        );
    }
    assert_eq!(keys, expected, "{}", String::from_utf8_lossy(output));
    values
}

fn assert_stopped_before_publication(f: &Fixture, rc: u8, output: &[u8], reason: &[u8]) {
    let values = fields(rc, output);
    assert_eq!(rc, 33);
    assert_eq!(values["REASON"], reason);
    assert_eq!(values["TOUCHED"], b"none");
    assert_eq!(values["TEMP"], b"removed");
    f.unchanged();
    assert!(f.temp_dirs(&f.root).is_empty());
}

fn source_raw(op: &Restore) -> i64 {
    op.source_fd.map_or(-1, |fd| op.raw(fd) as i64)
}

// ---- 内部を差し替える 46 件 ----

#[test]
fn test_prepare_failure_keeps_original() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("prepare_directory", _) => Action::Fail("prepare-failed"),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_eq!(fields(rc, &output)["REASON"], b"prepare-failed");
    f.unchanged();
}

#[cfg(target_os = "linux")]
#[test]
fn test_copy_failure_preserves_existing_and_absent() {
    let _guard = serial();
    for exists in [true, false] {
        let f = Fixture::new();
        if !exists {
            std::fs::remove_file(&f.destination).unwrap();
        }
        let mut op = f.operation();
        op.hook = hook(|op, point| match point {
            Point::Enter("copy_file", raw) if *raw == source_raw(op) => Action::Fail("copy-failed"),
            _ => Action::Continue,
        });
        let (rc, output) = op.run();
        let values = fields(rc, &output);
        assert_eq!(rc, 33);
        assert_eq!(values["TOUCHED"], b"none");
        assert_eq!(values["TEMP"], b"removed");
        assert_eq!(values["REASON"], b"copy-failed");
        if exists {
            f.unchanged();
        } else {
            assert!(!f.destination.exists());
        }
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_replace_failure_preserves_original() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Os(Call::Rename(name)) if name == b"prepared" => Action::Errno(os::errno::EACCES),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"replace-failed");
    assert_eq!(values["TOUCHED"], b"none");
    f.unchanged();
}

#[cfg(target_os = "linux")]
#[test]
fn test_post_publish_verification_failure_is_not_rolled_back() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("verify_published", _) => Action::Fail("verify-failed"),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_eq!(rc, 33);
    assert_eq!(fields(rc, &output)["TOUCHED"], b"replaced");
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_signals_at_every_boundary_report_actual_change_once() {
    let _guard = serial();
    let before = [
        "start",
        "before-prepare",
        "probe",
        "before-copy",
        "copy",
        "before-publish",
        "publish-ready",
        "replace",
    ];
    let after = [
        "after-publish",
        "after-verify",
        "before-cleanup",
        "before-response",
    ];
    let f = Fixture::new();
    // copy の印は、子の cp が動いている間だけ通る。copy の段だけ、合図のファイルができるまで
    // 本物の cp に替わらない偽の cp を使う(60 秒で諦める)。
    let gate = f.root.join("copy-gate");
    let held = script(
        &f.root,
        "held-cp",
        &format!(
            "if [ \"$*\" != --version ]; then i=0; while [ ! -e '{gate}' ]; do i=$((i+1)); [ $i -ge 6000 ] && exit 70; sleep 0.01; done; fi\nexec '{cp}' \"$@\"\n",
            gate = gate.display(),
            cp = f.cp.display()
        ),
    );
    for stage in before.iter().chain(after.iter()).copied() {
        for number in [os::SIGTERM, os::SIGINT, os::SIGHUP] {
            f.reset_destination();
            let _ = std::fs::remove_file(&gate);
            let mut op = if stage == "copy" {
                f.operation_with_cp(&held)
            } else {
                f.operation()
            };
            let reached = Rc::new(RefCell::new(false));
            let seen = reached.clone();
            let gate = gate.clone();
            op.hook = hook(move |_, point| {
                if let Point::Checkpoint(current) = point
                    && *current == stage
                    && !*seen.borrow()
                {
                    *seen.borrow_mut() = true;
                    // 旧テストの op.on_signal(number) の直接の呼び出し(mask の中でも記録する)。
                    pretend_signal(number);
                    if stage == "copy" {
                        std::fs::write(&gate, b"").unwrap();
                    }
                }
                Action::Continue
            });
            let (rc, output) = op.run();
            assert!(*reached.borrow(), "{stage}");
            let values = fields(rc, &output);
            assert_eq!(rc, 20, "{stage} {number}");
            let after_publish = after.contains(&stage);
            assert_eq!(
                values["TOUCHED"],
                if after_publish {
                    &b"replaced"[..]
                } else {
                    b"none"
                }
            );
            if after_publish {
                f.restored();
            } else {
                f.unchanged();
            }
        }
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_signal_during_real_replace_keeps_replaced_state() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| {
        if let Point::After("replace", _) = point {
            // 公開の mask の中なので保留され、戻した時に記録される。
            os::signal_current_thread(os::SIGTERM).unwrap();
            assert_eq!(os::recorded_signal(), None);
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    assert_eq!(rc, 20);
    assert_eq!(fields(rc, &output)["TOUCHED"], b"replaced");
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_deadline_before_and_after_publication() {
    let _guard = serial();
    let f = Fixture::new();
    for (stage, touched) in [
        ("hash-before", &b"none"[..]),
        ("before-publish", b"none"),
        ("after-publish", b"replaced"),
    ] {
        f.reset_destination();
        let mut op = f.operation();
        op.hook = hook(move |op, point| {
            if let Point::Checkpoint(current) = point
                && *current == stage
            {
                op.set_deadline(Duration::ZERO);
            }
            Action::Continue
        });
        let (rc, output) = op.run();
        let values = fields(rc, &output);
        assert_eq!(values["REASON"], b"budget-exceeded");
        assert_eq!(values["TOUCHED"], touched);
        if touched == b"none" {
            f.unchanged();
        } else {
            f.restored();
        }
    }
}

#[test]
fn test_source_short_read_growth_and_same_size_mutation() {
    let _guard = serial();
    for variant in ["short", "grow", "same"] {
        let f = Fixture::new();
        let original = vec![b'A'; 131073];
        std::fs::write(&f.source, &original).unwrap();
        let mut op = f.operation();
        let source = f.source.clone();
        let modified = Rc::new(RefCell::new(false));
        op.hook = hook(move |_, point| {
            if let Point::Checkpoint("hash-before") = point
                && !*modified.borrow()
            {
                *modified.borrow_mut() = true;
                let data = match variant {
                    "short" => b"X".to_vec(),
                    "grow" => [original.clone(), b"Y".to_vec()].concat(),
                    _ => vec![b'B'; original.len()],
                };
                std::fs::write(&source, data).unwrap();
            }
            Action::Continue
        });
        let (rc, output) = op.run();
        assert_eq!(fields(rc, &output)["REASON"], b"taskmd-body", "{variant}");
        f.unchanged();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_hash_reads_are_chunked_and_three_passes_bounded() {
    let _guard = serial();
    let f = Fixture::new();
    std::fs::write(&f.source, vec![b'A'; 140000]).unwrap();
    let mut op = f.operation();
    let reads = Rc::new(RefCell::new(Vec::new()));
    let seen = reads.clone();
    op.hook = hook(move |op, point| {
        if let Point::Os(Call::Read(fd, size)) = point {
            let held: Vec<i64> = [op.source_fd, op.prepared_fd]
                .iter()
                .flatten()
                .map(|id| op.raw(*id) as i64)
                .collect();
            if held.contains(&(*fd as i64)) {
                seen.borrow_mut().push(*size);
            }
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    fields(rc, &output);
    assert_eq!(rc, 0);
    let reads = reads.borrow();
    assert!(!reads.is_empty());
    assert!(reads.iter().max().copied().unwrap() <= 65536);
    assert_eq!(reads.iter().sum::<usize>(), 3 * (140000 + 1));
    f.restored();
}

#[test]
fn test_missing_static_api_is_no_temp_and_no_change() {
    // Rust では静的な機能の欠けが起きないので、機能の確かめの段の失敗で同じ結果を確かめる。
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("check_features", _) => Action::Fail("runtime-unavailable"),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"runtime-unavailable");
    assert_eq!(values["TEMP"], b"none");
    f.unchanged();
}

#[test]
fn test_missing_fd_aliases_fails_without_publication() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.limits.fd_roots = vec![b"/missing-fd-alias".to_vec()];
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"fd-copy-unavailable");
    assert_eq!(values["TEMP"], b"removed");
    f.unchanged();
}

#[test]
fn test_fd_alias_device_mismatch_is_fd_copy_unavailable() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    SHIFT_ALIAS_DEVICE.with(|shift| *shift.borrow_mut() = true);
    let (rc, output) = op.run();
    SHIFT_ALIAS_DEVICE.with(|shift| *shift.borrow_mut() = false);
    assert_stopped_before_publication(&f, rc, &output, b"fd-copy-unavailable");
}

#[cfg(target_os = "linux")]
#[test]
fn test_cp_launch_failure_in_probe_is_runtime_unavailable() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Os(Call::Spawn(false)) => Action::Errno(os::errno::EACCES),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_stopped_before_publication(&f, rc, &output, b"runtime-unavailable");
}

#[test]
fn test_probe_setup_failure_is_runtime_unavailable() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Os(Call::Mkdir(name)) if name == b"probe-directory" => {
            Action::Errno(os::errno::EACCES)
        }
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_stopped_before_publication(&f, rc, &output, b"runtime-unavailable");
}

#[cfg(target_os = "linux")]
#[test]
fn test_probe_rename_failure_is_runtime_unavailable() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Os(Call::Rename(name)) if name == b"probe-destination" => {
            Action::Errno(os::errno::EACCES)
        }
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_stopped_before_publication(&f, rc, &output, b"runtime-unavailable");
}

#[cfg(target_os = "linux")]
#[test]
fn test_probe_cleanup_unverified_entry_is_cleanup_failed() {
    // 起動時の試しの間だけ、`stat_at` が probe-renamed を不在と答える(旧テストの差し替えと同じ範囲)。
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    let cleaning = Rc::new(RefCell::new(false));
    op.hook = hook(move |_, point| match point {
        Point::Checkpoint("before-cleanup") => {
            *cleaning.borrow_mut() = true;
            Action::Continue
        }
        Point::StatAt(name) if name == b"probe-renamed" && !*cleaning.borrow() => {
            Action::Errno(os::errno::ENOENT)
        }
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    assert_stopped_before_publication(&f, rc, &output, b"cleanup-failed");
}

#[cfg(target_os = "linux")]
#[test]
fn test_second_fd_alias_candidate_can_succeed() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.limits.fd_roots = vec![b"/missing-fd-alias".to_vec(), b"/proc/self/fd".to_vec()];
    let (rc, output) = op.run();
    fields(rc, &output);
    assert_eq!(rc, 0);
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_cleanup_failure_reports_non_utf8_exact_retained_path() {
    let _guard = serial();
    let mut f = Fixture::new();
    let raw_parent = [bytes(&f.root), b"/nonutf-\xff\n\"\\".to_vec()].concat();
    std::fs::create_dir(path(&raw_parent)).unwrap();
    f.destination = path(&[raw_parent.clone(), b"/file-\xfe".to_vec()].concat());
    std::fs::write(&f.destination, &f.before.0).unwrap();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("publish", _) => Action::Fail("replace-failed"),
        Point::Remove(name) if name == b"prepared" => Action::Fail("cleanup-failed"),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["TEMP"], b"retained");
    assert_eq!(values["TOUCHED"], b"none");
    let temp = unquote(&values["TEMP_PATH"]);
    assert_eq!(temp, op.temp_path);
    assert_eq!(
        path(&temp).parent().unwrap().as_os_str().as_bytes(),
        &raw_parent[..]
    );
    assert_eq!(
        output
            .split(|c| *c == b'\n')
            .filter(|l| !l.is_empty())
            .count(),
        5
    );
    assert_eq!(std::fs::read(&f.destination).unwrap(), f.before.0);
    assert_eq!(mode_of(&path(&temp)), 0o700);
}

#[cfg(target_os = "linux")]
#[test]
fn test_cleanup_after_publish_failure_is_not_success() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("cleanup", _) => Action::Fail("cleanup-failed"),
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(rc, 33);
    assert_eq!(values["REASON"], b"cleanup-failed");
    assert_eq!(values["TOUCHED"], b"replaced");
    assert_eq!(values["TEMP"], b"retained");
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_prepared_name_swap_is_detected_without_deleting_replacement() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|op, point| {
        if let Point::Checkpoint("before-publish") = point {
            let temp = path(&op.temp_path);
            std::fs::rename(temp.join("prepared"), temp.join("saved")).unwrap();
            std::fs::write(temp.join("prepared"), b"foreign-sentinel").unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(rc, 33);
    assert_eq!(values["TOUCHED"], b"none");
    f.unchanged();
    assert_eq!(
        std::fs::read(path(&op.temp_path).join("prepared")).unwrap(),
        b"foreign-sentinel"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn test_target_created_after_absence_check_is_not_overwritten() {
    let _guard = serial();
    let f = Fixture::new();
    std::fs::remove_file(&f.destination).unwrap();
    let mut op = f.operation();
    let destination = f.destination.clone();
    op.hook = hook(move |_, point| {
        if let Point::Checkpoint("before-publish") = point {
            std::fs::write(&destination, b"new-sentinel").unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    assert_eq!(fields(rc, &output)["TOUCHED"], b"none");
    assert_eq!(std::fs::read(&f.destination).unwrap(), b"new-sentinel");
}

#[cfg(target_os = "linux")]
#[test]
fn test_parent_swap_does_not_write_to_symlink_target() {
    let _guard = serial();
    let mut f = Fixture::new();
    let parent = f.root.join("parent");
    std::fs::create_dir(&parent).unwrap();
    f.destination = parent.join("file");
    std::fs::write(&f.destination, b"old").unwrap();
    let other = f.root.join("other");
    std::fs::create_dir(&other).unwrap();
    std::fs::write(other.join("file"), b"sentinel").unwrap();
    let mut op = f.operation();
    let root = f.root.clone();
    let (moved, target) = (parent.clone(), other.clone());
    op.hook = hook(move |_, point| {
        if let Point::Checkpoint("before-publish") = point {
            std::fs::rename(&moved, root.join("held-parent")).unwrap();
            std::os::unix::fs::symlink(&target, &moved).unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(rc, 33);
    assert_eq!(values["TOUCHED"], b"none");
    assert_eq!(std::fs::read(other.join("file")).unwrap(), b"sentinel");
    assert_eq!(
        std::fs::read(f.root.join("held-parent/file")).unwrap(),
        b"old"
    );
}

#[test]
fn test_cp_version_timeout_stops_and_reaps_direct_child() {
    let _guard = serial();
    let f = Fixture::new();
    let cp = script(
        &f.root,
        "slow-cp",
        "trap '' TERM\necho 'cp (GNU coreutils) fixture'\nexec sleep 30\n",
    );
    let mut op = f.operation_with_cp(&cp);
    op.limits.stop = Duration::from_millis(50);
    let started = Instant::now();
    let (rc, output) = op.run();
    assert_eq!(fields(rc, &output)["REASON"], b"runtime-unavailable");
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(op.child.is_none());
    f.unchanged();
}

#[cfg(target_os = "linux")]
#[test]
fn test_copy_deadline_term_ignore_kill_and_reap() {
    let _guard = serial();
    let f = Fixture::new();
    let marker = f.root.join("child-pid");
    let cp = script(
        &f.root,
        "hold-cp",
        &format!(
            "if [ \"$*\" = --version ]; then echo 'cp (GNU coreutils) fixture'; exit 0; fi\ncase $(readlink \"$4\") in */taskmd-body) trap '' TERM; echo $$ > '{marker}'; exec sleep 30;; esac\nexec '{cp}' \"$@\"\n",
            marker = marker.display(),
            cp = f.cp.display()
        ),
    );
    let mut op = f.operation_with_cp(&cp);
    op.limits.stop = Duration::from_millis(50);
    op.hook = hook(|op, point| {
        if let Point::Enter("copy_file", raw) = point
            && *raw == source_raw(op)
        {
            op.set_deadline(Duration::from_millis(250));
        }
        Action::Continue
    });
    let started = Instant::now();
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"budget-exceeded");
    assert_eq!(values["TEMP"], b"removed");
    assert!(started.elapsed() < Duration::from_secs(5));
    let pid: i32 = std::fs::read_to_string(&marker)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let error = os::wait_nohang(pid).unwrap_err();
    assert_eq!(error.raw_os_error(), Some(os::errno::ECHILD));
    f.unchanged();
}

/// 終わらない偽の子。
struct NeverExited(Rc<RefCell<Vec<&'static str>>>);

impl ChildProcess for NeverExited {
    fn poll(&mut self) -> Option<i32> {
        None
    }
    fn wait(&mut self) -> io::Result<i32> {
        panic!("unreaped child must not be treated as exited");
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.0.borrow_mut().push("TERM");
        Ok(())
    }
    fn kill(&mut self) -> io::Result<()> {
        self.0.borrow_mut().push("KILL");
        Ok(())
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_unreaped_child_uses_single_stop_budget_and_retains_temp() {
    let _guard = serial();
    let f = Fixture::new();
    let calls = Rc::new(RefCell::new(Vec::new()));
    let mut op = f.operation();
    op.limits.stop = Duration::from_millis(20);
    let record = calls.clone();
    op.hook = hook(move |op, point| match point {
        Point::Enter("copy_file", raw) if *raw == source_raw(op) => {
            op.child = Some(Box::new(NeverExited(record.clone())));
            op.stop_child();
            Action::Fail("child-unreaped")
        }
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(*calls.borrow(), vec!["TERM", "KILL"]);
    assert_eq!(values["REASON"], b"child-unreaped");
    assert_eq!(values["TEMP"], b"retained");
    assert_eq!(values["TOUCHED"], b"none");
    assert!(path(&op.temp_path).join("prepared").exists());
    f.unchanged();
}

/// 呼び出しを記録する偽の子(旧テストの `Recorder`)。
struct Recorder {
    calls: Rc<RefCell<Vec<&'static str>>>,
    polls: Vec<Option<i32>>,
    terminate_error: bool,
}

impl ChildProcess for Recorder {
    fn poll(&mut self) -> Option<i32> {
        self.calls.borrow_mut().push("poll");
        if self.polls.len() > 1 {
            self.polls.remove(0)
        } else {
            self.polls[0]
        }
    }
    fn wait(&mut self) -> io::Result<i32> {
        self.calls.borrow_mut().push("wait");
        Ok(0)
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.calls.borrow_mut().push("terminate");
        if self.terminate_error {
            return Err(io::Error::from_raw_os_error(os::errno::ESRCH));
        }
        Ok(())
    }
    fn kill(&mut self) -> io::Result<()> {
        self.calls.borrow_mut().push("kill");
        Ok(())
    }
}

/// 旧テストの `stop_child_trace`。stop が None なら既定の 5 秒。
fn stop_child_trace(
    polls: &[Option<i32>],
    stop: Option<Duration>,
    terminate_error: bool,
    child_unreaped: bool,
    with_child: bool,
) -> (Vec<&'static str>, bool, bool, bool) {
    let f = Fixture::new();
    let calls = Rc::new(RefCell::new(Vec::new()));
    let mut op = f.operation();
    if with_child {
        op.child = Some(Box::new(Recorder {
            calls: calls.clone(),
            polls: polls.to_vec(),
            terminate_error,
        }));
    }
    op.child_unreaped = child_unreaped;
    if let Some(stop) = stop {
        op.limits.stop = stop;
    }
    let result = op.stop_child();
    let recorded = calls.borrow().clone();
    (recorded, result, op.child.is_none(), op.child_unreaped)
}

const ZERO: Option<Duration> = Some(Duration::ZERO);

#[test]
fn test_stop_child_without_child_returns_true() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None], ZERO, false, false, false),
        (vec![], true, true, false)
    );
}

#[test]
fn test_stop_child_unreaped_flag_returns_false_without_calls() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[Some(0)], ZERO, false, true, true),
        (vec![], false, false, true)
    );
}

#[test]
fn test_stop_child_already_exited_waits_once() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[Some(0)], ZERO, false, false, true),
        (vec!["poll", "wait"], true, true, false)
    );
}

#[test]
fn test_stop_child_exits_after_terminate_without_kill() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None, Some(0)], ZERO, false, false, true),
        (vec!["poll", "terminate", "poll", "wait"], true, true, false)
    );
}

#[test]
fn test_stop_child_exits_after_kill_reaped_by_final_check() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None, None, Some(0)], ZERO, false, false, true),
        (
            vec!["poll", "terminate", "poll", "kill", "poll", "wait"],
            true,
            true,
            false
        )
    );
}

#[test]
fn test_stop_child_never_exits_marks_unreaped() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None], ZERO, false, false, true),
        (
            vec!["poll", "terminate", "poll", "kill", "poll"],
            false,
            false,
            true
        )
    );
}

#[test]
fn test_stop_child_terminate_oserror_proceeds_to_kill() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None, None, Some(0)], ZERO, true, false, true),
        (
            vec!["poll", "terminate", "poll", "kill", "poll", "wait"],
            true,
            true,
            false
        )
    );
}

#[test]
fn test_stop_child_reaps_during_wait_loop() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None, None, Some(0)], None, false, false, true),
        (
            vec!["poll", "terminate", "poll", "poll", "wait"],
            true,
            true,
            false
        )
    );
}

#[test]
fn test_stop_child_terminate_oserror_proceeds_to_wait_loop() {
    let _guard = serial();
    assert_eq!(
        stop_child_trace(&[None, None, Some(0)], None, true, false, true),
        (
            vec!["poll", "terminate", "poll", "poll", "wait"],
            true,
            true,
            false
        )
    );
}

#[cfg(target_os = "linux")]
#[test]
fn test_parent_swap_forces_unknown_temp_without_stale_path() {
    let _guard = serial();
    let mut f = Fixture::new();
    let parent = f.root.join("parent");
    std::fs::create_dir(&parent).unwrap();
    f.destination = parent.join("file");
    std::fs::write(&f.destination, b"old").unwrap();
    let mut op = f.operation();
    let (moved, root) = (parent.clone(), f.root.clone());
    op.hook = hook(move |_, point| {
        if let Point::Checkpoint("before-cleanup") = point {
            std::fs::rename(&moved, root.join("moved")).unwrap();
            std::fs::create_dir(&moved).unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(rc, 33);
    assert_eq!(values["TEMP"], b"unknown");
    assert!(!values.contains_key("TEMP_PATH"));
    assert!(!parent.join("file").exists());
    assert_eq!(
        std::fs::read(f.root.join("moved/file")).unwrap(),
        std::fs::read(&f.source).unwrap()
    );
}

#[cfg(target_os = "linux")]
#[test]
fn test_cleanup_deadline_retains_temp() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.limits.cleanup = Duration::ZERO;
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(rc, 33);
    assert_eq!(values["REASON"], b"cleanup-failed");
    assert_eq!(values["TEMP"], b"retained");
    f.restored();
}

#[test]
fn test_response_size_checked_before_temp_creation() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.limits.max_response = 32;
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"internal-argument");
    assert_eq!(values["TEMP"], b"none");
    f.unchanged();
}

#[cfg(target_os = "linux")]
#[test]
fn test_corrupt_prepared_bytes_are_never_published() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|op, point| {
        if let Point::After("copy_file", raw) = point
            && *raw == source_raw(op)
        {
            let prepared = op.bfd(op.prepared_fd.unwrap()).unwrap();
            os::seek_start(prepared).unwrap();
            os::write(prepared, b"X").unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"verify-failed");
    assert_eq!(values["TOUCHED"], b"none");
    f.unchanged();
}

#[cfg(target_os = "linux")]
#[test]
fn test_published_name_swap_reports_changed_and_preserves_foreign_inode() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    let (destination, root) = (f.destination.clone(), f.root.clone());
    op.hook = hook(move |_, point| {
        if let Point::Checkpoint("after-publish") = point {
            std::fs::rename(&destination, root.join("published")).unwrap();
            std::fs::write(&destination, b"foreign").unwrap();
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"verify-failed");
    assert_eq!(values["TOUCHED"], b"replaced");
    assert_eq!(std::fs::read(&f.destination).unwrap(), b"foreign");
    assert_eq!(
        std::fs::read(f.root.join("published")).unwrap(),
        std::fs::read(&f.source).unwrap()
    );
}

#[test]
fn test_unexpected_exception_emits_no_traceback_or_content() {
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    op.hook = hook(|_, point| match point {
        Point::Enter("read_source", _) => Action::Panic,
        _ => Action::Continue,
    });
    let (rc, output) = op.run();
    fields(rc, &output);
    assert_eq!(rc, 33);
    let text = String::from_utf8_lossy(&output);
    assert!(!text.contains("PRIVATE-CONTENT"));
    assert!(!text.contains("Traceback"));
    assert!(!text.contains("panicked"));
}

/// 書き込みを記録する出力先。`on_write` は 1 回の書き込みごとに呼ぶ。
struct Sink {
    data: Rc<RefCell<Vec<u8>>>,
    writes: Rc<RefCell<usize>>,
    plan: Vec<Step>,
    on_write: Option<Box<dyn FnMut()>>,
}

#[derive(Clone, Copy, Debug)]
enum Step {
    Ok,
    Fail(i32),
    Partial(usize),
    Signal(i32),
    Record(i32),
}

impl Sink {
    fn new(plan: Vec<Step>) -> Self {
        Self {
            data: Rc::new(RefCell::new(Vec::new())),
            writes: Rc::new(RefCell::new(0)),
            plan,
            on_write: None,
        }
    }
}

impl RawWrite for Sink {
    fn write_once(&mut self, data: &[u8]) -> io::Result<usize> {
        let step = {
            let mut writes = self.writes.borrow_mut();
            let step = self.plan.get(*writes).copied().unwrap_or(Step::Ok);
            *writes += 1;
            step
        };
        if let Some(on_write) = self.on_write.as_mut() {
            on_write();
        }
        match step {
            Step::Ok => {
                self.data.borrow_mut().extend_from_slice(data);
                Ok(data.len())
            }
            Step::Fail(number) => Err(io::Error::from_raw_os_error(number)),
            Step::Partial(count) => {
                let count = count.min(data.len());
                self.data.borrow_mut().extend_from_slice(&data[..count]);
                Ok(count)
            }
            Step::Signal(number) => {
                os::signal_current_thread(number).unwrap();
                self.data.borrow_mut().extend_from_slice(data);
                Ok(data.len())
            }
            Step::Record(number) => {
                pretend_signal(number);
                self.data.borrow_mut().extend_from_slice(data);
                Ok(data.len())
            }
        }
    }
}

#[cfg(target_os = "linux")]
fn completed(f: &Fixture) -> (Restore, u8, Vec<u8>) {
    let mut op = f.operation();
    let (rc, output) = op.run();
    assert_eq!(rc, 0, "{}", String::from_utf8_lossy(&output));
    (op, rc, output)
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_response_captured_signal_before_output_is_failure() {
    let _guard = serial();
    let f = Fixture::new();
    let (mut op, rc, result) = completed(&f);
    pretend_signal(os::SIGTERM);
    let sink = Sink::new(vec![]);
    let data = sink.data.clone();
    let mut out = Output::new(sink, 4096);
    let (code, _) = emit(&mut out, Some(&mut op), rc, &result);
    let values = fields(code, &data.borrow());
    assert_eq!(code, 20);
    assert_eq!(values["TOUCHED"], b"replaced");
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_response_commit_masks_signal_until_single_result_written() {
    let _guard = serial();
    let f = Fixture::new();
    let (mut op, rc, result) = completed(&f);
    let sink = Sink::new(vec![Step::Signal(os::SIGTERM)]);
    let (data, writes) = (sink.data.clone(), sink.writes.clone());
    let mut out = Output::new(sink, 4096);
    let (code, state) = emit(&mut out, Some(&mut op), rc, &result);
    assert_eq!((code, state), (0, Emitted::Written));
    fields(code, &data.borrow());
    assert_eq!(*writes.borrow(), 1);
    assert_eq!(os::recorded_signal(), Some(os::SIGTERM));
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_output_failure_never_returns_success() {
    let _guard = serial();
    let f = Fixture::new();
    let (mut op, rc, result) = completed(&f);
    let sink = Sink::new(vec![Step::Fail(os::errno::EPIPE)]);
    let writes = sink.writes.clone();
    let mut out = Output::new(sink, 4096);
    let (code, _) = emit(&mut out, Some(&mut op), rc, &result);
    assert_eq!(code, 33);
    assert_eq!(*writes.borrow(), 1);
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_mask_setup_failure_has_no_valid_response() {
    let _guard = serial();
    for number in [os::errno::EIO, os::errno::EBADF, os::errno::EACCES] {
        let f = Fixture::new();
        let (mut op, rc, result) = completed(&f);
        let sink = Sink::new(vec![]);
        let data = sink.data.clone();
        let mut out = Output::new(sink, 4096);
        set_emit_hook(Some(Box::new(move |point| {
            (point == EmitPoint::Block).then(|| io::Error::from_raw_os_error(number))
        })));
        let (code, state) = emit(&mut out, Some(&mut op), rc, &result);
        set_emit_hook(None);
        assert_ne!(code, 0);
        assert_eq!(state, Emitted::MaskFailed);
        assert!(data.borrow().is_empty());
        f.restored();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_mask_restoration_failure_preserves_one_final_result() {
    let _guard = serial();
    for number in [os::errno::EIO, os::errno::EBADF, os::errno::EACCES] {
        let f = Fixture::new();
        let (mut op, rc, result) = completed(&f);
        let original = os::block_recorded_signals().unwrap();
        os::restore_signal_mask(&original).unwrap();
        let sink = Sink::new(vec![]);
        let data = sink.data.clone();
        let mut out = Output::new(sink, 4096);
        set_emit_hook(Some(Box::new(move |point| {
            (point == EmitPoint::Unblock).then(|| io::Error::from_raw_os_error(number))
        })));
        let (code, _) = emit(&mut out, Some(&mut op), rc, &result);
        set_emit_hook(None);
        os::restore_signal_mask(&original).unwrap();
        assert_eq!(code, 0);
        fields(code, &data.borrow());
        f.restored();
    }
}

#[cfg(target_os = "linux")]
#[test]
fn test_final_signal_mask_boundaries_generate_write_flush_and_unmask() {
    let _guard = serial();
    for boundary in [
        "before-mask",
        "after-mask",
        "response",
        "write",
        "flush",
        "unmask",
    ] {
        for number in [os::SIGTERM, os::SIGINT, os::SIGHUP] {
            let f = Fixture::new();
            let (mut op, rc, result) = completed(&f);
            let mut sink = Sink::new(vec![]);
            if boundary == "write" {
                sink.plan = vec![Step::Record(number)];
            }
            let (data, writes) = (sink.data.clone(), sink.writes.clone());
            let mut out = Output::new(sink, 4096);
            set_emit_hook(Some(Box::new(move |point| {
                let hit = matches!(
                    (boundary, point),
                    ("before-mask", EmitPoint::Block)
                        | ("after-mask", EmitPoint::Blocked)
                        | ("response", EmitPoint::Response)
                        | ("flush", EmitPoint::Flush)
                        | ("unmask", EmitPoint::Unblock)
                );
                if hit {
                    if boundary == "after-mask" {
                        // 旧テストは mask を設定した後に os.kill で本物の信号を送る(保留される)。
                        os::signal_current_thread(number).unwrap();
                    } else {
                        // ほかの境界は op.on_signal(number) の直接の呼び出し。
                        pretend_signal(number);
                    }
                }
                None
            })));
            let (code, _) = emit(&mut out, Some(&mut op), rc, &result);
            set_emit_hook(None);
            assert_eq!(
                code,
                if boundary == "before-mask" { 20 } else { 0 },
                "{boundary}"
            );
            fields(code, &data.borrow());
            assert_eq!(*writes.borrow(), 1);
            assert_eq!(super::operation::recorded_signal(), Some(number));
            f.restored();
        }
    }
}

// ---- 46 件のほか: 書き出しの flush の順と回数 ----

/// 入口と同じ順で書き出しを終える。(終了値, stderr へ出した 2 行の回数, 書けたバイト)。
fn finish(plan: Vec<Step>, size: usize, result: &[u8]) -> (u8, usize, Vec<u8>) {
    let sink = Sink::new(plan);
    let data = sink.data.clone();
    let mut out = Output::new(sink, size);
    let (mut code, _) = emit(&mut out, None, 33, result);
    let mut lines = 0;
    let (_first, second) = exit_flush(&mut out);
    if let Err(error) = second {
        let [first_line, second_line] = unraisable_lines(&error);
        assert!(first_line.starts_with(b"Exception ignored in: "));
        assert!(second_line.starts_with(b"OSError: [Errno "));
        lines += 1;
        code = 120;
    }
    let _ = exit_close(&mut out, false);
    let written = data.borrow().clone();
    (code, lines, written)
}

#[test]
fn test_exit_flush_order_and_counts() {
    let _guard = serial();
    let eio = Step::Fail(os::errno::EIO);
    // 明示の flush から数えて EIO が 1〜5 回。
    for (count, code, lines, written) in [
        (1, 33, 0, true),
        (2, 33, 0, true),
        (3, 120, 1, true),
        (4, 120, 1, true),
        (5, 120, 1, false),
    ] {
        let (got_code, got_lines, data) = finish(vec![eio; count], 4096, ARGUMENT_RESPONSE);
        assert_eq!((got_code, got_lines), (code, lines), "{count}");
        assert_eq!(
            data,
            if written { ARGUMENT_RESPONSE } else { b"" },
            "{count}"
        );
    }
    // B=4096・L=4116 で最初の直接の書き込みが 1000 バイトだけ書け、続く 2 回が EIO の後に成功する。
    let long = vec![b'L'; 4116];
    let (code, lines, data) = finish(vec![Step::Partial(1000), eio, eio], 4096, &long);
    assert_eq!((code, lines, data), (33, 0, long.clone()));
    // L が B より長く、直接の書き込みが失敗すると 33 で何も出さない。
    let (code, lines, data) = finish(vec![Step::Fail(os::errno::ENOSPC)], 4096, &long);
    assert_eq!((code, lines, data), (33, 0, Vec::new()));
}

#[test]
fn test_exit_term_during_close_flush_ends_by_term_after_two_lines() {
    // (4) の途中に TERM が届くと、(3) で既定へ戻った後なので TERM で終わる。この検査の実行物を子として
    // 起動し、子の中で書き出しを終える。
    const CHILD: &str = "R4R_UNIT_CHILD";
    if std::env::var(CHILD).is_ok() {
        let _ = os::install_recording_handlers();
        let eio = Step::Fail(os::errno::EIO);
        let sink = Sink::new(vec![eio, eio, eio, Step::Signal(os::SIGTERM)]);
        let mut out = Output::new(sink, 4096);
        let _ = emit(&mut out, None, 33, ARGUMENT_RESPONSE);
        let (_first, second) = exit_flush(&mut out);
        if let Err(error) = second {
            super::write_stderr_lines(&unraisable_lines(&error));
        }
        let _ = exit_close(&mut out, true);
        std::process::exit(0);
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "restore_taskmd::tests::test_exit_term_during_close_flush_ends_by_term_after_two_lines",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD, "1")
        .output()
        .unwrap();
    use std::os::unix::process::ExitStatusExt;
    assert_eq!(output.status.signal(), Some(os::SIGTERM));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains(
        "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno"
    ));
}

// ---- 46 件のほか: EINTR ----

#[cfg(target_os = "linux")]
#[test]
fn test_eintr_on_non_retrying_cleanup_stat_stops_once() {
    // 後片付けの最初の属性の取得(os.stat。再試行しない)へ EINTR を 1 回入れる。旧 helper と同じく
    // 1 回で止まり、cleanup-failed・TEMP=unknown・空の一時フォルダが残る。
    let _guard = serial();
    let f = Fixture::new();
    let mut op = f.operation();
    let state = Rc::new(RefCell::new((false, 0usize)));
    let seen = state.clone();
    op.hook = hook(move |_, point| {
        let mut state = seen.borrow_mut();
        match point {
            Point::Checkpoint("before-cleanup") => state.0 = true,
            Point::Os(Call::StatAt(_)) if state.0 => {
                state.1 += 1;
                if state.1 == 1 {
                    return Action::Errno(os::errno::EINTR);
                }
            }
            _ => {}
        }
        Action::Continue
    });
    let (rc, output) = op.run();
    let values = fields(rc, &output);
    assert_eq!(values["REASON"], b"cleanup-failed");
    assert_eq!(values["TOUCHED"], b"replaced");
    assert_eq!(values["TEMP"], b"unknown");
    assert_eq!(state.borrow().1, 1);
    let temps = f.temp_dirs(&f.root);
    assert_eq!(temps.len(), 1);
    assert_eq!(std::fs::read_dir(&temps[0]).unwrap().count(), 0);
    assert_eq!(op.temp, Temp::Unknown);
    f.restored();
}

#[cfg(target_os = "linux")]
#[test]
fn test_eintr_on_retrying_calls_adds_one_attempt_and_continues() {
    // 再試行する関数(os.open・os.fstat・os.read)は、EINTR の後に同じ呼び出しをもう一度行って続く。
    let _guard = serial();
    for target in ["open", "fstat", "read"] {
        let f = Fixture::new();
        let mut op = f.operation();
        let counts = Rc::new(RefCell::new((0usize, false)));
        let seen = counts.clone();
        op.hook = hook(move |op, point| {
            let hit = match point {
                Point::Os(Call::OpenRead(name)) => target == "open" && name == b"taskmd-body",
                Point::Os(Call::Fstat(fd)) => target == "fstat" && *fd as i64 == source_raw(op),
                Point::Os(Call::Read(fd, _)) => target == "read" && *fd as i64 == source_raw(op),
                _ => false,
            };
            if hit {
                let mut counts = seen.borrow_mut();
                counts.0 += 1;
                if !counts.1 {
                    counts.1 = true;
                    return Action::Errno(os::errno::EINTR);
                }
            }
            Action::Continue
        });
        let (rc, output) = op.run();
        fields(rc, &output);
        assert_eq!(rc, 0, "{target}");
        f.restored();
        let with_eintr = counts.borrow().0;
        // 同じ配置で EINTR なしの回数を数え、1 回だけ多いことを確かめる。
        let g = Fixture::new();
        let mut plain = g.operation();
        let plain_counts = Rc::new(RefCell::new(0usize));
        let seen = plain_counts.clone();
        plain.hook = hook(move |op, point| {
            let hit = match point {
                Point::Os(Call::OpenRead(name)) => target == "open" && name == b"taskmd-body",
                Point::Os(Call::Fstat(fd)) => target == "fstat" && *fd as i64 == source_raw(op),
                Point::Os(Call::Read(fd, _)) => target == "read" && *fd as i64 == source_raw(op),
                _ => false,
            };
            if hit {
                *seen.borrow_mut() += 1;
            }
            Action::Continue
        });
        let (rc, _) = plain.run();
        assert_eq!(rc, 0);
        assert_eq!(with_eintr, *plain_counts.borrow() + 1, "{target}");
    }
}

#[test]
fn test_argument_error_response_is_fixed() {
    let _guard = serial();
    let sink = Sink::new(vec![]);
    let data = sink.data.clone();
    let mut out = Output::new(sink, 4096);
    let (code, state) = emit(&mut out, None, 33, ARGUMENT_RESPONSE);
    assert_eq!((code, state), (33, Emitted::Written));
    assert_eq!(
        &data.borrow()[..],
        b"RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=internal-argument\n"
    );
}

// ---- 46 件のほか: 乱数の切り替え ----

#[cfg(target_os = "linux")]
#[test]
fn test_getrandom_enosys_and_eperm_fall_back_to_dev_urandom() {
    // CPython の pyurandom と同じく、getrandom が ENOSYS・EPERM なら /dev/urandom を読み、以後も
    // getrandom を呼ばない。
    let _guard = serial();
    for number in [os::errno::ENOSYS, os::errno::EPERM] {
        super::operation::reset_getrandom();
        let calls = Rc::new(RefCell::new((0usize, 0usize)));
        for _ in 0..2 {
            let f = Fixture::new();
            let mut op = f.operation();
            let seen = calls.clone();
            op.hook = hook(move |_, point| {
                match point {
                    Point::Os(Call::Random) => {
                        seen.borrow_mut().0 += 1;
                        return Action::Errno(number);
                    }
                    Point::Os(Call::Urandom) => seen.borrow_mut().1 += 1,
                    _ => {}
                }
                Action::Continue
            });
            let (rc, output) = op.run();
            fields(rc, &output);
            assert_eq!(rc, 0);
            f.restored();
        }
        assert_eq!(*calls.borrow(), (1, 2), "{number}");
    }
    super::operation::reset_getrandom();
}
