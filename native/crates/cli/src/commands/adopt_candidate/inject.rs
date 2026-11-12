//! 検査専用の注入(`R4C_FAULT_SPEC`)。旧版の比較用 harness が旧テストと同じ点へ差し込む操作を、
//! Rust 側は `Ops`・`Precheck` の境界で同じ点へ差し込む。製品の入口には接続しない(example と単体テストだけ)。
//! 仕様(JSON)の書き方は `native/ci/adopt_candidate_fixtures/fault_scenarios.json` と `check_adopt_candidate.py` を参照。
use super::diagnostics::{os_error, strerror, timeout_message};
use super::flow::{Child, LinkFailure, Ops, Precheck, Read};
use super::paths::PyPath;
use dev_workflow_platform::adopt_candidate as os;
use serde_json::Value;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Command;

const SOURCE: &str = "候補_例.md";
const DEST: &str = "進行中_例.md";

fn resolve(name: &str) -> &str {
    match name {
        "source" => SOURCE,
        "dest" => DEST,
        other => other,
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Before,
    After,
    During,
}

struct Rule {
    point: String,
    key: Option<String>,
    nth: usize,
    phase: Phase,
    actions: Vec<Value>,
    raise: Option<i32>,
    result: Option<(i32, Vec<u8>, Vec<u8>)>,
    timeout: bool,
    value: Value,
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&text[at..at + 2], 16).unwrap())
        .collect()
}

fn parse_rules(spec: &Value) -> Vec<Rule> {
    spec["rules"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(|rule| Rule {
            point: rule["point"].as_str().unwrap().to_owned(),
            key: rule["name"]
                .as_str()
                .or_else(|| rule["match"].as_str())
                .map(|name| resolve(name).to_owned()),
            nth: rule["nth"].as_u64().unwrap_or(1) as usize,
            phase: match rule["phase"].as_str() {
                Some("after") => Phase::After,
                Some("during") => Phase::During,
                _ => Phase::Before,
            },
            actions: rule["do"].as_array().cloned().unwrap_or_default(),
            raise: rule["raise"].as_i64().map(|value| value as i32),
            result: rule
                .get("result")
                .filter(|value| !value.is_null())
                .map(|result| {
                    (
                        result["code"].as_i64().unwrap_or(0) as i32,
                        hex(result["stdout_hex"].as_str().unwrap_or("")),
                        hex(result["stderr_hex"].as_str().unwrap_or("")),
                    )
                }),
            timeout: rule["timeout"].as_bool().unwrap_or(false),
            value: rule.clone(),
        })
        .collect()
}

/// 注入の本体。`Ops` を包み、決めた点で作業場所を書き換える・失敗を差し込む。
pub(crate) struct Injected<O: Ops> {
    inner: O,
    rules: Vec<Rule>,
    counters: RefCell<HashMap<(String, String), usize>>,
    root: RefCell<Option<PathBuf>>,
    marker_calls: Cell<usize>,
    notify: Option<(i32, i32)>,
    body: String,
    log: Option<PathBuf>,
}

impl<O: Ops> Injected<O> {
    /// `spec_dir` は仕様のファイルがあるフォルダ。`log` はその直下の 1 成分だけ許す(それ以外は拒否)。
    pub(crate) fn new(inner: O, spec: &Value, spec_dir: Option<&std::path::Path>) -> Self {
        let log = spec["log"].as_str().map(PathBuf::from);
        if let Some(path) = &log {
            let inside = spec_dir.is_some_and(|dir| {
                path.parent()
                    .zip(dir.canonicalize().ok())
                    .is_some_and(|(parent, dir)| parent.canonicalize().ok() == Some(dir))
            });
            assert!(
                inside && path.file_name().is_some(),
                "log は仕様と同じフォルダの直下だけ"
            );
        }
        let pipes = |name: &str| {
            std::env::var(name)
                .ok()
                .and_then(|value| value.parse::<i32>().ok())
        };
        Self {
            inner,
            rules: parse_rules(spec),
            counters: RefCell::new(HashMap::new()),
            root: RefCell::new(None),
            marker_calls: Cell::new(0),
            notify: pipes("R4C_FAULT_NOTIFY").zip(pipes("R4C_FAULT_WAIT")),
            body: spec["body"].as_str().unwrap_or_default().to_owned(),
            log,
        }
    }

    /// この呼び出しが `nth` 回目に当たる規則を返す(`point` と `key` ごとに数える)。
    fn hits(&self, point: &str, key: &str, phase_filter: Option<Phase>) -> Vec<&Rule> {
        let count = {
            let mut counters = self.counters.borrow_mut();
            let entry = counters
                .entry((point.to_owned(), key.to_owned()))
                .or_insert(0);
            if phase_filter.is_none() || phase_filter == Some(Phase::Before) {
                *entry += 1;
            }
            *entry
        };
        self.rules
            .iter()
            .filter(|rule| {
                rule.point == point
                    && rule
                        .key
                        .as_deref()
                        .is_none_or(|wanted| key.contains(wanted))
                    && rule.nth == count
            })
            .collect()
    }

    fn perform(&self, actions: &[Value]) {
        let root = self
            .root
            .borrow()
            .clone()
            .expect("root を開く前に注入できない");
        for action in actions {
            // 注入の対象は root の直下の 1 成分だけ。絶対パス・`..`・区切りは拒否する。
            let path = |field: &str| {
                let name = action[field].as_str().unwrap();
                assert!(
                    !name.is_empty()
                        && name != "."
                        && name != ".."
                        && !name.contains('/')
                        && !name.contains('\0'),
                    "注入の path は root の直下の 1 成分だけ: {name:?}"
                );
                root.join(name)
            };
            let text = |field: &str| match action[field].as_str().unwrap() {
                "@BODY" => self.body.clone(),
                other => other.to_owned(),
            };
            match action["op"].as_str().unwrap() {
                "write" => std::fs::write(path("path"), text("text")).unwrap(),
                "unlink" => std::fs::remove_file(path("path")).unwrap(),
                "mkdir" => std::fs::create_dir(path("path")).unwrap(),
                "symlink" => {
                    std::os::unix::fs::symlink(action["target"].as_str().unwrap(), path("path"))
                        .unwrap()
                }
                "replace_inode" => {
                    let target = path("path");
                    let bytes = std::fs::read(&target).unwrap();
                    let spare = root.join(".inode-swap");
                    std::fs::write(&spare, bytes).unwrap();
                    std::fs::rename(&spare, &target).unwrap();
                }
                "attacker_replace" => {
                    let attacker = root.join("attacker-replacement");
                    std::fs::write(&attacker, text("text")).unwrap();
                    std::fs::rename(&attacker, path("path")).unwrap();
                }
                "git" => {
                    let args: Vec<&str> = action["args"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|a| a.as_str().unwrap())
                        .collect();
                    let status = Command::new("git")
                        .arg("-C")
                        .arg(&root)
                        .args([
                            "-c",
                            "user.name=test",
                            "-c",
                            "user.email=test@example.invalid",
                        ])
                        .args(&args)
                        .env("GIT_CONFIG_NOSYSTEM", "1")
                        .env("GIT_CONFIG_GLOBAL", "/dev/null")
                        .status()
                        .unwrap();
                    assert!(status.success(), "注入の git が失敗した: {args:?}");
                }
                "pause" => {
                    let (notify, wait) = self
                        .notify
                        .expect("R4C_FAULT_NOTIFY と R4C_FAULT_WAIT が要る");
                    os::fault_injection_notify_and_wait(notify, wait);
                }
                other => panic!("未知の操作: {other}"),
            }
        }
    }

    fn apply(&self, point: &str, key: &str, phase: Phase) {
        for rule in self
            .hits(point, key, Some(phase))
            .into_iter()
            .filter(|rule| rule.phase == phase)
        {
            self.perform(&rule.actions);
        }
    }

    fn os_text(errno: i32, name: Option<&[u8]>) -> Vec<u8> {
        os_error(errno, &strerror(errno), name)
    }
}

impl<O: Ops> Ops for Injected<O> {
    type Dir = O::Dir;

    fn open_dir(&self, path: &PyPath) -> Result<Self::Dir, Vec<u8>> {
        let mut root = self.root.borrow_mut();
        if root.is_none() {
            use std::os::unix::ffi::OsStrExt;
            *root = Some(PathBuf::from(std::ffi::OsStr::from_bytes(&path.to_bytes())));
        }
        drop(root);
        self.inner.open_dir(path)
    }

    fn read_candidate(&self, dir: &Self::Dir, name: &[u8]) -> Result<Read, Vec<u8>> {
        let key = String::from_utf8_lossy(name).into_owned();
        let during = {
            // 呼び出しの回数は Before の規則の判定で 1 回だけ数える。
            let hit = self.hits("read", &key, Some(Phase::Before));
            if let Some(rule) = hit.iter().find(|rule| rule.raise.is_some()) {
                return Err(Self::os_text(rule.raise.unwrap(), Some(name)));
            }
            for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
                self.perform(&rule.actions);
            }
            hit.iter()
                .filter(|rule| rule.phase == Phase::During)
                .flat_map(|rule| rule.actions.clone())
                .collect::<Vec<_>>()
        };
        let result = if during.is_empty() {
            self.inner.read_candidate(dir, name)
        } else {
            // 実際に読む途中(最初の空でない塊の後)で、作業場所を書き換える。
            let done = Cell::new(false);
            self.inner.read_candidate_during(dir, name, &mut || {
                if !done.replace(true) {
                    self.perform(&during);
                }
            })
        };
        self.apply("read", &key, Phase::After);
        result
    }

    fn read_candidate_during(
        &self,
        dir: &Self::Dir,
        name: &[u8],
        hook: &mut dyn FnMut(),
    ) -> Result<Read, Vec<u8>> {
        self.inner.read_candidate_during(dir, name, hook)
    }

    fn stat(&self, dir: &Self::Dir, name: &[u8]) -> Result<Option<os::Entry>, Vec<u8>> {
        let key = String::from_utf8_lossy(name).into_owned();
        let hit = self.hits("stat", &key, Some(Phase::Before));
        if let Some(rule) = hit.iter().find(|rule| rule.raise.is_some()) {
            return Err(Self::os_text(rule.raise.unwrap(), Some(name)));
        }
        for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
            self.perform(&rule.actions);
        }
        self.inner.stat(dir, name)
    }

    fn lexists(&self, path: &[u8]) -> bool {
        self.inner.lexists(path)
    }

    fn rename_noreplace(
        &self,
        dir: &Self::Dir,
        source: &[u8],
        destination: &[u8],
    ) -> Result<bool, Vec<u8>> {
        let key = String::from_utf8_lossy(source).into_owned();
        let hit = self.hits("rename", &key, Some(Phase::Before));
        for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
            self.perform(&rule.actions);
        }
        if let Some(rule) = hit
            .iter()
            .find(|rule| rule.value.get("missing").is_some() || rule.raise.is_some())
        {
            if rule.value.get("missing").is_some() {
                return Ok(false);
            }
            // ENOSYS・EINVAL は link/unlink へ、他は旧版の文で停止する(platform の読み方をそのまま使う)。
            return os::classify_rename_errno(rule.raise.unwrap())
                .map_err(|failure| os_error(failure.errno, "上書きしない改名に失敗", None));
        }
        let result = self.inner.rename_noreplace(dir, source, destination);
        self.apply("rename", &key, Phase::After);
        result
    }

    fn link(&self, dir: &Self::Dir, source: &[u8], destination: &[u8]) -> Result<(), LinkFailure> {
        let key = String::from_utf8_lossy(destination).into_owned();
        let hit = self.hits("link", &key, Some(Phase::Before));
        for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
            self.perform(&rule.actions);
        }
        if let Some(errno) = hit.iter().find_map(|rule| rule.raise) {
            return Err(if errno == 17 {
                LinkFailure::Exists
            } else {
                LinkFailure::Other(super::diagnostics::os_error_pair(
                    errno,
                    source,
                    destination,
                ))
            });
        }
        let result = self.inner.link(dir, source, destination);
        if result.is_ok() {
            self.apply("link", &key, Phase::After);
        }
        result
    }

    fn unlink(&self, dir: &Self::Dir, name: &[u8]) -> Result<(), Vec<u8>> {
        let key = String::from_utf8_lossy(name).into_owned();
        let hit = self.hits("unlink", &key, Some(Phase::Before));
        for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
            self.perform(&rule.actions);
        }
        // 窓(pause)の間に SIGINT が届いたら、旧版と同じく、その場で例外にする(unlink しない)。
        if self.inner.interrupted() {
            return Err(b"KeyboardInterrupt".to_vec());
        }
        if let Some(errno) = hit.iter().find_map(|rule| rule.raise) {
            return Err(Self::os_text(errno, Some(name)));
        }
        let result = self.inner.unlink(dir, name);
        self.apply("unlink", &key, Phase::After);
        result
    }

    fn run(
        &self,
        dir: &Self::Dir,
        argv: &[Vec<u8>],
        env: &[(Vec<u8>, Vec<u8>)],
        timeout_seconds: u32,
    ) -> Result<Child, Vec<u8>> {
        if let Some(log) = &self.log {
            use std::io::Write;
            let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
            let git_env: std::collections::BTreeMap<String, String> = env
                .iter()
                .filter(|(key, _)| key.starts_with(b"GIT_"))
                .map(|(key, value)| {
                    (
                        String::from_utf8_lossy(key).into_owned(),
                        String::from_utf8_lossy(value).into_owned(),
                    )
                })
                .collect();
            let entry = serde_json::json!({"argv": argv.iter().map(|a| hex(a)).collect::<Vec<_>>(), "git_env": git_env});
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(log)
                .unwrap();
            writeln!(file, "{entry}").unwrap();
        }
        // 同じ引数を含む呼び出しを、`match` の語ごとに数える。
        let mut hits = Vec::new();
        for token in self
            .rules
            .iter()
            .filter(|rule| rule.point == "run")
            .filter_map(|rule| rule.key.clone())
            .collect::<std::collections::BTreeSet<_>>()
        {
            if argv.iter().any(|arg| arg.as_slice() == token.as_bytes()) {
                hits.extend(
                    self.hits("run", &token, Some(Phase::Before))
                        .into_iter()
                        .filter(|rule| rule.key.as_deref() == Some(token.as_str())),
                );
            }
        }
        for rule in hits.iter().filter(|rule| rule.phase == Phase::Before) {
            self.perform(&rule.actions);
        }
        if hits.iter().any(|rule| rule.timeout) {
            return Err(timeout_message(argv, timeout_seconds));
        }
        if let Some(errno) = hits
            .iter()
            .find_map(|rule| rule.value["spawn_errno"].as_i64())
        {
            return Err(Self::os_text(errno as i32, Some(&argv[0])));
        }
        if let Some((code, stdout, stderr)) = hits.iter().find_map(|rule| rule.result.clone()) {
            return Ok(Child {
                code,
                stdout,
                stderr,
            });
        }
        let result = self.inner.run(dir, argv, env, timeout_seconds);
        if result.as_ref().is_ok_and(|child| child.code == 0) {
            for rule in hits.iter().filter(|rule| rule.phase == Phase::After) {
                self.perform(&rule.actions);
            }
        }
        result
    }

    fn protect(&self, enter: bool) -> bool {
        self.inner.protect(enter)
    }

    fn interrupted(&self) -> bool {
        self.inner.interrupted()
    }

    fn interrupt_exit(&self) {
        self.inner.interrupt_exit()
    }

    fn environment(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        self.inner.environment()
    }

    fn loop_iteration(&self) -> Option<Vec<u8>> {
        // 最初の呼び出しは引数の確認(旧 `main()` の検査)に当たる。`after_calls` は採用の中の呼び出しを数える。
        let calls = self.marker_calls.get();
        self.marker_calls.set(calls + 1);
        let late = self.rules.iter().any(|rule| {
            rule.point == "marker"
                && calls > rule.value["after_calls"].as_u64().unwrap_or(1) as usize
        });
        if late {
            return Some(b"late-marker".to_vec());
        }
        self.inner.loop_iteration()
    }
}

/// 事前検査の注入(`point: "precheck"`)。実部品の前に作業場所を書き換える・結果を差し替える。
pub(crate) struct InjectedPrecheck<'a, O: Ops, P> {
    pub(crate) inner: P,
    pub(crate) injected: &'a Injected<O>,
}

impl<O: Ops, P: Precheck<O::Dir>> Precheck<O::Dir> for InjectedPrecheck<'_, O, P> {
    fn run(
        &self,
        dir: &O::Dir,
        root: &[u8],
        accept: Option<&[u8]>,
        env: &[(Vec<u8>, Vec<u8>)],
    ) -> Result<Child, Vec<u8>> {
        let hit = self.injected.hits("precheck", "", None);
        for rule in hit.iter().filter(|rule| rule.phase == Phase::Before) {
            self.injected.perform(&rule.actions);
        }
        // 事前検査の時間切れの注入は含めない(表示に出る argv は実部品の起動のしかたで決まる。#472 の接続後に足す)。
        if let Some((code, stdout, stderr)) = hit.iter().find_map(|rule| rule.result.clone()) {
            return Ok(Child {
                code,
                stdout,
                stderr,
            });
        }
        self.inner.run(dir, root, accept, env)
    }
}
