#![forbid(unsafe_code)]
//! R4D-1: 診断の reftable 読み取りを、旧版(Python)と同じ入力・同じ予算の条件で比べる。
//! 製品への登録は行わない。登録しない内部部品を `#[path]` で読み込み、比較器を起動する。
//!
//! 流れ: 比較器の `--generate` が入力と旧版の結果を作る → この検査が同じ条件で Rust の `read_stack` と
//! 予算の操作を動かす → 比較器の `--compare` が全件を比べる。差し込みを入れた Rust の結果では、
//! 「量」の条件の入力だけを `--compare --expect-mismatch` で流し、比較が働くことを確かめる。
#[allow(dead_code)]
#[path = "../src/commands/diagnostic_git/mod.rs"]
mod diagnostic_git;

use diagnostic_git::budget::{Budget, Kind};
use diagnostic_git::reftable::{ReadError, Stack, T7_TABLE, read_stack};
use serde_json::{Value, json};
use std::cell::Cell;
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::rc::Rc;
use std::time::{Duration, Instant};

/// 比較の検査 1 回の中の、すべての起動(`--generate`・`--compare`・`--compare --expect-mismatch`)の上限時間。
const TOTAL_LIMIT: Duration = Duration::from_secs(900);

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(text: &str) -> Vec<u8> {
    assert!(text.len().is_multiple_of(2));
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// 直接の子の比較器。落とすときは止めて回収する(git は比較器が上限時間を付けて回収する)。
struct CheckerChild(Child);
impl Drop for CheckerChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// 比較器を起動し、標準エラーを受け取る。受け取った行は、そのまま標準エラーへ書き出す(CI のログに残す)。
/// 外の環境変数が比較器の動きを変えないよう、自己検査を飛ばす印と小さい組の印を外して起動する。
fn run_checker(args: &[&OsStr], started: Instant) -> String {
    use std::io::{Read, Write};
    use std::process::Stdio;
    let root = repo_root();
    let mut child = CheckerChild(
        Command::new("python3")
            .arg("-B")
            .arg(root.join("native/ci/check_diagnostic_reftable.py"))
            .args(args)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env_remove("R4D1_NO_SELFTEST")
            .env_remove("R4D1_ALLOW_MINI")
            .stderr(Stdio::piped())
            .spawn()
            .expect("R4D-1 の比較器を起動できません"),
    );
    let mut pipe = child.0.stderr.take().unwrap();
    let reader = std::thread::spawn(move || {
        let mut collected = Vec::new();
        let mut chunk = [0_u8; 8192];
        while let Ok(count) = pipe.read(&mut chunk) {
            if count == 0 {
                break;
            }
            let _ = std::io::stderr().write_all(&chunk[..count]);
            collected.extend_from_slice(&chunk[..count]);
        }
        collected
    });
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            let text = String::from_utf8_lossy(&reader.join().unwrap()).into_owned();
            assert!(status.success(), "R4D-1 の比較器が失敗しました: {status}");
            return text;
        }
        if started.elapsed() >= TOTAL_LIMIT {
            let _ = child.0.kill();
            let _ = child.0.wait();
            panic!(
                "R4D-1 の比較が上限時間 {} 秒に当たりました",
                TOTAL_LIMIT.as_secs()
            );
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap()
}

/// 予算の条件。旧版の比較器と同じ名前と意味。
enum Cond {
    Normal,
    Expired,
    Stop,
    Nth(u64),
    Quantity(u64),
}

fn parse_cond(value: &Value) -> Cond {
    let list = value.as_array().unwrap();
    match list[0].as_str().unwrap() {
        "normal" => Cond::Normal,
        "expired" => Cond::Expired,
        "stop" => Cond::Stop,
        "n" => Cond::Nth(list[1].as_u64().unwrap()),
        "q" => Cond::Quantity(list[1].as_u64().unwrap()),
        other => panic!("未知の条件: {other}"),
    }
}

fn limits() -> [u64; 9] {
    Kind::ALL.map(Kind::default_limit)
}

fn make_budget(cond: &Cond) -> Budget {
    let constant: Box<dyn FnMut() -> f64> = Box::new(|| 0.0);
    match cond {
        Cond::Normal => Budget::for_test(300.0, constant, limits()),
        Cond::Stop => {
            let budget = Budget::for_test(300.0, constant, limits());
            budget.request_stop();
            budget
        }
        Cond::Expired => {
            let calls = Cell::new(0_u32);
            Budget::for_test(
                300.0,
                Box::new(move || {
                    calls.set(calls.get() + 1);
                    if calls.get() == 1 { 0.0 } else { 1000.0 }
                }),
                limits(),
            )
        }
        Cond::Nth(n) => {
            let calls = Cell::new(0_u64);
            Budget::for_test(
                *n as f64,
                Box::new(move || {
                    calls.set(calls.get() + 1);
                    (calls.get() - 1) as f64
                }),
                limits(),
            )
        }
        Cond::Quantity(limit) => {
            let mut table = limits();
            table[Kind::ManagementOutput as usize] = *limit;
            Budget::for_test(300.0, constant, table)
        }
    }
}

fn encode(result: Result<Stack, ReadError>) -> Value {
    match result {
        Ok(stack) => {
            let rows: Vec<Value> = stack
                .iter()
                .map(|entry| {
                    json!([
                        hex(&entry.name),
                        entry.oid,
                        entry.target.as_ref().map(|target| hex(target))
                    ])
                })
                .collect();
            json!(["ok", rows])
        }
        Err(ReadError::Reftable(failure)) => {
            json!(["err", "reftable", failure.reason, failure.code])
        }
        Err(ReadError::Budget(failure)) => json!(["err", "budget", failure.reason, failure.code]),
        Err(ReadError::Other(name)) => json!(["err", "other", name, 0]),
    }
}

fn run_one(item: &Value, cond: &Cond, bias: bool) -> Value {
    let path = unhex(item["path"].as_str().unwrap());
    let format = item["hash"].as_str().unwrap();
    let mut budget = make_budget(cond);
    if bias {
        budget.set_test_charge_bias(1);
    }
    encode(read_stack(OsStr::from_bytes(&path), format, &mut budget))
}

fn run_inputs(manifest: &Value, quantity_only: bool, bias: bool) -> Value {
    let mut results = serde_json::Map::new();
    for item in manifest["inputs"].as_array().unwrap() {
        let mut by_condition = serde_json::Map::new();
        for (index, cond) in item["conds"].as_array().unwrap().iter().enumerate() {
            let parsed = parse_cond(cond);
            if quantity_only && !matches!(parsed, Cond::Quantity(_)) {
                continue;
            }
            by_condition.insert(index.to_string(), run_one(item, &parsed, bias));
        }
        if !by_condition.is_empty() {
            results.insert(
                item["id"].as_str().unwrap().to_owned(),
                Value::Object(by_condition),
            );
        }
    }
    Value::Object(results)
}

/// 予算だけの操作(G5)。旧版の比較器の `run_budget_ops` と同じ操作の名前と意味。
fn run_budget_case(case: &Value) -> Value {
    let now = Rc::new(Cell::new(case["start"].as_f64().unwrap()));
    let clock = {
        let now = Rc::clone(&now);
        Box::new(move || now.get())
    };
    let mut budget = Budget::for_test(case["seconds"].as_f64().unwrap(), clock, limits());
    let mut results = Vec::new();
    for op in case["ops"].as_array().unwrap() {
        let op = op.as_array().unwrap();
        let outcome = match op[0].as_str().unwrap() {
            "charge" => {
                let kind = Kind::from_name(op[1].as_str().unwrap()).expect("量の名前");
                budget
                    .charge(kind, op[2].as_u64().unwrap())
                    .map(|()| json!(["ok"]))
            }
            "check" => budget.check().map(|()| json!(["ok"])),
            "remaining" => Ok(json!(["ok", budget.remaining()])),
            "set" => {
                now.set(op[1].as_f64().unwrap());
                Ok(json!(["ok"]))
            }
            "stop" => {
                budget.request_stop();
                Ok(json!(["ok"]))
            }
            "unstop" => {
                budget.clear_stop();
                Ok(json!(["ok"]))
            }
            other => panic!("未知の操作: {other}"),
        };
        results.push(match outcome {
            Ok(value) => value,
            Err(failure) => json!(["fail", failure.reason, failure.code]),
        });
    }
    Value::Array(results)
}

fn write_json(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

#[test]
fn rust_matches_the_legacy_reftable_reader_on_every_input() {
    let started = Instant::now();
    let scratch =
        Scratch(std::env::temp_dir().join(format!("r4d1-compare-{}", std::process::id())));
    let _ = fs::remove_dir_all(&scratch.0);
    fs::create_dir(&scratch.0).unwrap();
    let work = scratch.0.canonicalize().unwrap();
    let generated = work.join("gen");
    fs::create_dir(&generated).unwrap();
    // Rust の検査は作業場所を変えない。比較器は --relative-base へ移って旧版を動かす。
    let base = std::env::current_dir().unwrap().canonicalize().unwrap();
    run_checker(
        &[
            OsStr::new("--generate"),
            generated.as_os_str(),
            OsStr::new("--relative-base"),
            base.as_os_str(),
        ],
        started,
    );
    let manifest_path = generated.join("manifest.json");
    let manifest: Value = serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    assert_eq!(manifest["scope"], "full");

    // T7 のバイト列は、比較器が書いたものと、reftable.rs の定数が同じでなければならない。
    assert_eq!(
        manifest["t7_hex"].as_str().unwrap(),
        hex(T7_TABLE),
        "T7_TABLE が比較器の T7 と違う"
    );

    // 比較器が一時フォルダに書いた T7 のファイルも、この定数と同じでなければならない。
    assert_eq!(
        fs::read(generated.join("in/g2-t7/table-0.ref")).unwrap(),
        T7_TABLE,
        "比較器が書いた T7 のファイルが T7_TABLE と違う"
    );

    let rust_started = Instant::now();
    let mut results = json!({
        "results": run_inputs(&manifest, false, false),
        "t7_hex": hex(T7_TABLE),
    });
    let budget_cases: BTreeMap<String, Value> = manifest["budget_cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| {
            (
                case["id"].as_str().unwrap().to_owned(),
                run_budget_case(case),
            )
        })
        .collect();
    results["budget"] = json!(budget_cases);
    let rust_path = work.join("rust.json");
    write_json(&rust_path, &results);
    // 差し込みを入れた結果: 「量」の条件の入力だけを、management_output に足す量を 1 多くして動かす。
    let biased = json!({ "results": run_inputs(&manifest, true, true) });
    let biased_path = work.join("rust-bias.json");
    write_json(&biased_path, &biased);
    // 合格したテストの出力は libtest が隠すので、直接 stderr へ書く(CI のログに残す)。
    let _ = writeln!(
        std::io::stderr(),
        "R4D-1 Rust の実行: {:.1} 秒",
        rust_started.elapsed().as_secs_f64()
    );

    let compare_log = run_checker(
        &[
            OsStr::new("--compare"),
            manifest_path.as_os_str(),
            rust_path.as_os_str(),
        ],
        started,
    );
    // 通常の比較は、比較器の自己検査を流していなければならない(黙って飛ばした合格にしない)。
    let passed_lines = compare_log
        .lines()
        .filter(|line| {
            line.strip_prefix("R4D-1 比較器の自己検査: ")
                .and_then(|rest| rest.strip_suffix(" 件合格"))
                .is_some_and(|count| {
                    !count.is_empty()
                        && count.bytes().all(|b| b.is_ascii_digit())
                        && count.parse::<u64>().is_ok_and(|n| n >= 1)
                })
        })
        .count();
    assert!(
        passed_lines >= 1,
        "比較器が自己検査を流していない(「N 件合格」の行が無い)"
    );
    assert!(
        !compare_log.contains("飛ばした"),
        "比較器が自己検査を飛ばした"
    );
    run_checker(
        &[
            OsStr::new("--compare"),
            manifest_path.as_os_str(),
            biased_path.as_os_str(),
            OsStr::new("--expect-mismatch"),
        ],
        started,
    );
}
