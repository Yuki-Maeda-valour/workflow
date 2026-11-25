//! 注入つきのシナリオ(`fault_scenarios.json`)を、実の OS 部品・実 Git・注入の `Injected` と代役の依存部品で流し、
//! 旧版の実出力から作った固定期待に一致することを確かめる。比較器の実比較(旧版と example の実プロセス)の前段。
use super::flow_tests::{StubCandidates, StubPrecheck, Tmp};
use super::inject::{Injected, InjectedPrecheck};
use super::program::{Context, run};
use super::real::RealOps;
use super::runtime::{Env, Sink};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::process::Command;

const SCENARIOS: &str =
    include_str!("../../../../../ci/adopt_candidate_fixtures/fault_scenarios.json");
const BODY: &str = "> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n";

struct NoEnv;
impl Env for NoEnv {
    fn get(&self, _name: &str) -> Option<Vec<u8>> {
        None
    }
}

#[derive(Default)]
struct Capture(Vec<u8>);
impl Sink for Capture {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, i32> {
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
}

fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

/// Python の `_snapshot` と同じ形(`.git`・root 自身は除く。inode は元の候補と同じなら `origin`、ほかは同じ実体どうしが同じ番号)。
fn snapshot(root: &Path, origin: Option<u64>) -> Value {
    use std::os::unix::fs::MetadataExt;
    fn visit(base: &Path, dir: &Path, out: &mut Vec<(String, std::path::PathBuf)>) {
        let mut entries: Vec<_> = std::fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        entries.sort();
        for path in entries {
            let relative = path
                .strip_prefix(base)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            if relative == ".git" {
                continue;
            }
            let meta = std::fs::symlink_metadata(&path).unwrap();
            out.push((relative, path.clone()));
            if meta.is_dir() && !meta.file_type().is_symlink() {
                visit(base, &path, out);
            }
        }
    }
    let mut paths = Vec::new();
    visit(root, root, &mut paths);
    paths.sort();
    let mut out = serde_json::Map::new();
    let mut labels: Vec<u64> = Vec::new();
    for (relative, path) in paths {
        let meta = std::fs::symlink_metadata(&path).unwrap();
        // 権限は `S_IMODE` の全体(symlink は OS で値が違うので記録しない)。
        let mode = meta.mode() & 0o7777;
        let mut entry = if meta.file_type().is_symlink() {
            use std::os::unix::ffi::OsStrExt;
            json!({"kind": "symlink", "sha256": sha(std::fs::read_link(&path).unwrap().as_os_str().as_bytes())})
        } else if meta.is_dir() {
            json!({"kind": "directory", "sha256": Value::Null, "mode": mode})
        } else if meta.file_type().is_file() {
            let label = if Some(meta.ino()) == origin {
                "origin".to_owned()
            } else {
                let at = labels
                    .iter()
                    .position(|ino| *ino == meta.ino())
                    .unwrap_or_else(|| {
                        labels.push(meta.ino());
                        labels.len() - 1
                    });
                format!("other-{}", at + 1)
            };
            json!({"kind": "file", "sha256": std::fs::read(&path).map_or_else(|_| "unreadable".to_owned(), |bytes| sha(&bytes)), "mode": mode, "inode": label, "nlink": meta.nlink()})
        } else {
            json!({"kind": "fifo", "sha256": Value::Null, "mode": mode})
        };
        let _ = &mut entry;
        out.insert(relative, entry);
    }
    Value::Object(out)
}

/// 作る実体の権限が、機械の umask で変わらないようにする(旧版の固定期待は umask 022 で作った)。
/// 検査の実行ファイルの中でだけ、最初に 1 回設定する(並列の検査の間で戻す・戻さないの競り合いを作らない)。
fn fix_umask() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        dev_workflow_platform::adopt_candidate::set_umask(0o022);
    });
}

fn run_scenario(scenario: &Value) -> Value {
    fix_umask();
    let tmp = Tmp::new("inject");
    let root = tmp.root().to_path_buf();
    std::fs::write(root.join("候補_例.md"), BODY).unwrap();
    let prep = &scenario["prep"];
    for (name, mode) in prep["modes"].as_object().into_iter().flatten() {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            root.join(name),
            std::fs::Permissions::from_mode(mode.as_u64().unwrap() as u32),
        )
        .unwrap();
    }
    if prep["git"].as_bool().unwrap_or(false) {
        tmp.git(&["init", "-q"]);
        tmp.git(&["add", "--", "候補_例.md"]);
        tmp.git(&["commit", "-q", "-m", "base"]);
    }
    for (name, text) in prep["files"].as_object().into_iter().flatten() {
        std::fs::write(root.join(name), text.as_str().unwrap()).unwrap();
    }
    for name in prep["dirs"].as_array().into_iter().flatten() {
        std::fs::create_dir(root.join(name.as_str().unwrap())).unwrap();
    }
    for (name, target) in prep["symlinks"].as_object().into_iter().flatten() {
        std::os::unix::fs::symlink(target.as_str().unwrap(), root.join(name)).unwrap();
    }
    let mut digest = scenario["digest"]
        .as_str()
        .map_or_else(|| sha(BODY.as_bytes()), str::to_owned);
    let source_path = root.join("候補_例.md");
    if let Some(kind) = prep["source_as"].as_str() {
        std::fs::remove_file(&source_path).unwrap();
        match kind {
            "directory" => std::fs::create_dir(&source_path).unwrap(),
            "unreadable" => {
                use std::os::unix::fs::PermissionsExt;
                std::fs::write(&source_path, BODY).unwrap();
                std::fs::set_permissions(&source_path, std::fs::Permissions::from_mode(0)).unwrap();
            }
            "fifo" => assert!(
                Command::new("mkfifo")
                    .arg(&source_path)
                    .status()
                    .unwrap()
                    .success()
            ),
            "empty" => {
                std::fs::write(&source_path, b"").unwrap();
                digest = sha(b"");
            }
            "absent" => {}
            other => {
                let size: usize = other
                    .strip_prefix("size:")
                    .expect("source_as")
                    .parse()
                    .unwrap();
                let head = "## 指摘\n";
                let body = format!("{head}{}", "x".repeat(size - head.len()));
                std::fs::write(&source_path, &body).unwrap();
                digest = sha(body.as_bytes());
            }
        }
    }
    let origin = {
        use std::os::unix::fs::MetadataExt;
        std::fs::symlink_metadata(&source_path)
            .ok()
            .map(|meta| meta.ino())
    };
    // 読取りの途中で書き換える注入は、属性(mtime)の変化で気づく。時計の刻みや待ちに頼らず、準備の後に候補の時刻を過去へ戻す。
    if scenario["rules"]
        .as_array()
        .is_some_and(|rules| rules.iter().any(|rule| rule["phase"] == "during"))
        && source_path.is_file()
    {
        std::fs::File::options()
            .write(true)
            .open(&source_path)
            .unwrap()
            .set_modified(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000_000))
            .unwrap();
    }
    let mut spec = json!({"rules": scenario["rules"], "body": BODY});
    spec["log"] = Value::Null;
    let injected = Injected::new(RealOps, &spec, None);
    let precheck = InjectedPrecheck {
        inner: StubPrecheck::passing(),
        injected: &injected,
    };
    let context = Context {
        ops: &injected,
        candidates: &StubCandidates,
        precheck: &precheck,
        env: &NoEnv,
        directory: [false; 3],
        blksize: [4096; 3],
        thread: 0,
        terminal_columns: 80,
        stdout_terminal: false,
    };
    let mut args: Vec<std::ffi::OsString> = vec![
        "--root".into(),
        root.clone().into_os_string(),
        "--expect-sha256".into(),
        digest.into(),
    ];
    args.extend(
        scenario["args"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|a| a.as_str().unwrap().into()),
    );
    args.push(root.join("候補_例.md").into_os_string());
    let (mut out, mut err) = (Capture::default(), Capture::default());
    let code = run(&context, &args, &mut out, &mut err);
    let shown = root.to_string_lossy().into_owned();
    let text = |bytes: &[u8]| String::from_utf8_lossy(bytes).replace(&shown, "<ROOT>");
    let mut record = json!({"code": code, "stdout": text(&out.0), "stderr": text(&err.0), "tree": snapshot(&root, origin)});
    if root.join(".git").exists() {
        let listing = Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(["ls-files", "--stage", "-z"])
            .output()
            .unwrap()
            .stdout;
        record["index"] = Value::String(sha(&listing));
    }
    record
}

#[test]
fn every_injected_scenario_matches_the_old_behaviour_with_real_parts() {
    let document: Value = serde_json::from_str(SCENARIOS).unwrap();
    let mut ran = 0;
    let mut skipped_other_os = 0;
    let mut skipped_dependency = 0;
    for scenario in document["scenarios"].as_array().unwrap() {
        // 競り合いの窓を pipe で同期する kill と、環境を消す子の経路は、比較器が実プロセスで行う。
        if matches!(
            scenario["kind"].as_str(),
            Some("kill" | "interrupt" | "bypass")
        ) {
            continue;
        }
        // 実プロセスの環境・制限(rlimit・環境変数・時間)が要るものと、この OS の対象外は、比較器だけが流す。
        if scenario.get("warnings_from_dependency").is_some() {
            skipped_dependency += 1;
            continue;
        }
        if scenario.get("rlimit_fsize").is_some()
            || scenario.get("env").is_some()
            || scenario.get("timeout").is_some()
        {
            continue;
        }
        let here = if cfg!(target_os = "linux") {
            "linux"
        } else {
            "darwin"
        };
        if scenario["os"]
            .as_array()
            .is_some_and(|list| !list.iter().any(|name| name == here))
        {
            skipped_other_os += 1;
            continue;
        }
        let record = run_scenario(scenario);
        assert_eq!(record, scenario["expect"], "{}", scenario["id"]);
        ran += 1;
    }
    // この OS の対象外は、実行せず、成功に数えない。
    assert!(
        ran + skipped_other_os > 60 && ran > 55,
        "{ran} {skipped_other_os} {skipped_dependency}"
    );
}
