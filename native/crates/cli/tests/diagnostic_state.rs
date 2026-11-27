#![forbid(unsafe_code)]
//! R4D-3: 保護記録の形式の検査・実名と REV の検査・参照の名前と中身と解決を、旧版(Python)と同じ入力・同じ予算の条件・
//! 同じ差し替えの部品で比べる。製品への登録は行わない。登録しない内部部品を `#[path]` で読み込み、比較器を起動する。
//!
//! 流れ: 比較器の `--generate` が入力と旧版の結果を作る → この検査が同じ条件で Rust の関数を動かす →
//! 比較器の `--compare` が全件を比べる(比較器の自己検査も、この中で流れる)。
//! M の組の 1 件の理由を変えた Rust の結果を `--compare --expect-mismatch` へ渡し、比較が働くことを確かめる。
#[allow(dead_code)]
#[path = "../src/commands/diagnostic_git/mod.rs"]
mod diagnostic_git;

use dev_workflow_core::loop_state::CodepointString;
use diagnostic_git::budget::{Budget, Failure, Kind};
use diagnostic_git::paths;
use diagnostic_git::private::{
    self, GitArg, GitQuery, Head, OidSource, RefMap, RefRecord, RefSource, RefValue, Resolution,
};
use diagnostic_git::state::{self, Backend, Context, Error, HashFormat, Records};
use serde_json::{Value, json};
use std::cell::Cell;
use std::ffi::OsStr;
use std::fs;
use std::io::Write;
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

fn bytes_of(value: &Value) -> Vec<u8> {
    unhex(value.as_str().expect("16 進の文字列"))
}

/// この回に作ったフォルダだけを持つ。作った物だけを消す(前からあるパスには触れない)。
struct Scratch(PathBuf);
impl Scratch {
    /// 一時フォルダの下に新しいフォルダを作る。名前が既にあれば(EEXIST)、接尾辞の番号を変えて作り直す。
    fn create() -> Self {
        let base = std::env::temp_dir();
        for attempt in 0_u32..1000 {
            let path = base.join(format!("r4d3-compare-{}-{attempt}", std::process::id()));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!(
                    "比較の一時フォルダを作れません({}): {error}",
                    path.display()
                ),
            }
        }
        panic!("比較の一時フォルダの名前が 1000 回続けて既にありました");
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// 直接の子の比較器。落とすときは止めて回収する(本物の take と git は、比較器が上限時間を付けて回収する)。
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
    use std::io::Read;
    use std::process::Stdio;
    let root = repo_root();
    let mut child = CheckerChild(
        Command::new("python3")
            .arg("-B")
            .arg(root.join("native/ci/check_diagnostic_state.py"))
            .args(args)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env_remove("R4D3_NO_SELFTEST")
            .env_remove("R4D3_ALLOW_MINI")
            .stderr(Stdio::piped())
            .spawn()
            .expect("R4D-3 の比較器を起動できません"),
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
            assert!(status.success(), "R4D-3 の比較器が失敗しました: {status}");
            return text;
        }
        if started.elapsed() >= TOTAL_LIMIT {
            let _ = child.0.kill();
            let _ = child.0.wait();
            panic!(
                "R4D-3 の比較が上限時間 {} 秒に当たりました",
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
    Stop,
    Nth(u64),
}

fn parse_cond(value: &Value) -> Cond {
    let list = value.as_array().unwrap();
    match list[0].as_str().unwrap() {
        "normal" => Cond::Normal,
        "stop" => Cond::Stop,
        "n" => Cond::Nth(list[1].as_u64().unwrap()),
        other => panic!("未知の条件: {other}"),
    }
}

/// 条件の予算と、時計を呼んだ回数。確かめの回数は、回数から作るときの 1 回を引いた数。
fn make_budget(cond: &Cond) -> (Budget, Rc<Cell<u64>>) {
    let calls = Rc::new(Cell::new(0_u64));
    let counter = Rc::clone(&calls);
    let limits = Kind::ALL.map(Kind::default_limit);
    match cond {
        Cond::Normal | Cond::Stop => {
            let budget = Budget::for_test(
                300.0,
                Box::new(move || {
                    counter.set(counter.get() + 1);
                    0.0
                }),
                limits,
            );
            if matches!(cond, Cond::Stop) {
                budget.request_stop();
            }
            (budget, calls)
        }
        Cond::Nth(n) => (
            Budget::for_test(
                *n as f64,
                Box::new(move || {
                    counter.set(counter.get() + 1);
                    (counter.get() - 1) as f64
                }),
                limits,
            ),
            calls,
        ),
    }
}

fn error_value(error: Error) -> Value {
    match error {
        Error::Failure(failure) => json!(["failure", failure.reason, failure.code]),
        Error::Value => json!(["value-error"]),
        Error::Unicode => json!(["unicode-error"]),
        Error::Os(errno) => json!(["os-error", errno]),
    }
}

fn outcome<T>(result: Result<T, Error>, ok: impl FnOnce(T) -> Value) -> Value {
    match result {
        Ok(value) => json!(["ok", ok(value)]),
        Err(error) => error_value(error),
    }
}

fn failure<T>(result: Result<T, Failure>) -> Result<T, Error> {
    result.map_err(Error::from)
}

/// 差し替えの部品が出す失敗の理由は、比較器の cases.json の文字列から作る(検査の中だけで使う)。
fn leak(text: &str) -> &'static str {
    Box::leak(text.to_owned().into_boxed_str())
}

fn spec_error(spec: &Value) -> Error {
    let list = spec.as_array().unwrap();
    match list[0].as_str().unwrap() {
        "failure" => Error::Failure(Failure::new(
            leak(list[1].as_str().unwrap()),
            list[2].as_u64().unwrap() as u8,
        )),
        "os" => Error::Os(list[1].as_i64().unwrap() as i32),
        other => panic!("未知の失敗の指定: {other}"),
    }
}

fn optional_text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned)
}

fn optional_bytes(value: &Value) -> Option<Vec<u8>> {
    value.as_str().map(unhex)
}

fn records_from(items: &Value) -> Vec<RefRecord> {
    items
        .as_array()
        .unwrap()
        .iter()
        .map(|item| RefRecord {
            name: bytes_of(&item[0]),
            oid: optional_text(&item[1]),
            target: optional_bytes(&item[2]),
            resolved: item[3] == "resolved",
        })
        .collect()
}

fn head_value(value: &Value) -> Option<RefValue> {
    value.as_array().map(|pair| RefValue {
        oid: optional_text(&pair[0]),
        target: optional_bytes(&pair[1]),
    })
}

/// 比較器の差し替えの部品と同じ値を返し、同じ物を同じ順で記録する。予算の確かめは呼ばない。
struct Stub {
    events: Vec<Value>,
    responses: Vec<Value>,
    refs: Value,
    head: Value,
}

impl Stub {
    fn new(input: &Value) -> Self {
        Self {
            events: Vec::new(),
            responses: input["responses"].as_array().cloned().unwrap_or_default(),
            refs: input["refs"].clone(),
            head: input["head"].clone(),
        }
    }
}

impl GitQuery for Stub {
    fn run(
        &mut self,
        _budget: &mut Budget,
        args: &[GitArg],
        input: Option<&[u8]>,
        limit: Option<u64>,
    ) -> Result<(i32, Vec<u8>), Error> {
        let mut raw = Vec::new();
        for arg in args {
            raw.push(hex(&arg.to_bytes()?));
        }
        // 旧版の 2 か所(:111-112・:164-165)と同じく、出力の種類は git_output。
        self.events.push(json!([
            "process",
            raw,
            hex(input.unwrap_or(&[])),
            "git_output",
            limit
        ]));
        let response = if self.responses.is_empty() {
            json!(["rc", 0, ""])
        } else {
            self.responses.remove(0)
        };
        if response[0] == "os" {
            return Err(Error::Os(response[1].as_i64().unwrap() as i32));
        }
        Ok((response[1].as_i64().unwrap() as i32, bytes_of(&response[2])))
    }
}

impl RefSource for Stub {
    fn refs(&mut self, _budget: &mut Budget) -> Result<Resolution, Error> {
        self.events.push(json!(["refs"]));
        if !self.refs["fail"].is_null() {
            return Err(spec_error(&self.refs["fail"]));
        }
        Ok(Resolution {
            records: records_from(&self.refs["records"]),
            head: head_value(&self.refs["head"]),
        })
    }
}

impl OidSource for Stub {
    fn head(&mut self, _budget: &mut Budget) -> Result<Head, Error> {
        self.events.push(json!(["head"]));
        if !self.head["fail"].is_null() {
            return Err(spec_error(&self.head["fail"]));
        }
        Ok(Head {
            target: optional_bytes(&self.head["target"]),
            oid: optional_text(&self.head["oid"]),
        })
    }
}

fn ref_map(items: &Value) -> RefMap {
    let mut map = RefMap::new();
    for item in items.as_array().unwrap() {
        map.insert(
            bytes_of(&item[0]),
            optional_text(&item[1]),
            optional_bytes(&item[2]),
        );
    }
    map
}

fn map_value(map: &RefMap) -> Value {
    Value::Array(
        map.iter()
            .map(|(name, value)| json!([hex(name), value.oid, value.target.as_deref().map(hex)]))
            .collect(),
    )
}

fn head_out(head: Head) -> Value {
    json!([head.target.as_deref().map(hex), head.oid])
}

fn codepoints(value: &Value) -> CodepointString {
    let points = value
        .as_array()
        .unwrap()
        .iter()
        .map(|point| point.as_u64().unwrap() as u32)
        .collect();
    CodepointString::try_from_codepoints(points).unwrap()
}

/// 渡された番号(10 進の文字列)。0 以上で u64 に収まるときだけ Some(旧版は範囲の外を context-binding にする)。
fn context_id(value: &Value) -> Option<u64> {
    let text = value.as_str().unwrap();
    if text.starts_with('-') {
        None
    } else {
        text.parse().ok()
    }
}

fn records_value(records: Records) -> Value {
    let contexts: Vec<Value> = records
        .contexts
        .iter()
        .map(|c| {
            json!([
                c.id,
                c.parent,
                c.depth,
                c.active,
                hex(&c.wt),
                hex(&c.gd),
                hex(&c.common),
                c.format.name(),
                c.backend.name(),
                c.held
            ])
        })
        .collect();
    json!({
        "cwd": records.cwd.as_deref().map(hex),
        "toplevel": records.toplevel.as_deref().map(hex),
        "contexts": contexts,
    })
}

fn origins_value(origins: state::Origins) -> Value {
    let groups: Vec<Value> = origins
        .groups
        .iter()
        .map(|(name, rows)| {
            let rows: Vec<Value> = rows
                .iter()
                .map(|row| Value::Array(row.iter().map(|field| json!(hex(field))).collect()))
                .collect();
            json!([hex(name), rows])
        })
        .collect();
    json!({"groups": groups, "admin": [hex(&origins.admin.0), hex(&origins.admin.1)]})
}

/// 関数を 1 つの予算で動かした結果(旧版の比較器の LEGACY と同じ名前)。
fn run_function(name: &str, input: &Value, budget: &mut Budget, stub: &mut Stub) -> Value {
    match name {
        "cfield" => outcome(failure(state::cfield(&bytes_of(&input["raw"]))), |v| {
            json!(hex(&v))
        }),
        "lines" => {
            let raw = bytes_of(&input["raw"]);
            outcome(failure(state::lines(&raw)), |rows| {
                Value::Array(rows.iter().map(|row| json!(hex(row))).collect())
            })
        }
        "integer" => outcome(
            state::integer(
                &bytes_of(&input["raw"]),
                input["min"].as_i64().unwrap(),
                input["max"].as_i64().unwrap(),
            ),
            |v| json!(v),
        ),
        "check_manifest" => outcome(
            state::check_manifest(&bytes_of(&input["raw"]), budget),
            |()| Value::Null,
        ),
        "parse_records" => outcome(
            state::parse_records(
                &bytes_of(&input["meta"]),
                &bytes_of(&input["contexts"]),
                &bytes_of(&input["cwd"]),
                context_id(&input["context_id"]),
            ),
            records_value,
        ),
        "parse_origins" => outcome(
            state::parse_origins(&bytes_of(&input["raw"])),
            origins_value,
        ),
        "ancestors" => {
            let contexts = input["parents"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
                .map(|(id, parent)| Context {
                    id,
                    parent: parent.as_i64().unwrap(),
                    depth: 0,
                    active: true,
                    wt: b"/".to_vec(),
                    gd: b"-".to_vec(),
                    common: b"-".to_vec(),
                    format: HashFormat::Sha1,
                    backend: Backend::Files,
                    held: None,
                })
                .collect();
            let records = Records {
                cwd: None,
                toplevel: None,
                contexts,
            };
            json!([
                "ok",
                records.ancestors(input["n"].as_u64().unwrap() as usize)
            ])
        }
        "literal" => outcome(
            failure(paths::literal(
                &bytes_of(&input["raw"]),
                input["under"].as_bool().unwrap(),
            )),
            |()| Value::Null,
        ),
        "revision" => {
            let value = input["value"].get("text").map(codepoints);
            outcome(paths::revision(value.as_ref()), |()| Value::Null)
        }
        "valid_ref" => json!(["ok", private::valid_ref(&bytes_of(&input["name"]))]),
        "file_refs" => {
            let width = input["width"].as_u64().unwrap() as usize;
            let mut map = RefMap::new();
            let result = if input["kind"] == "packed" {
                failure(private::parse_packed_refs(
                    &bytes_of(&input["raw"]),
                    width,
                    budget,
                    &mut map,
                ))
            } else {
                // 旧版の :188 を写す(項目ごとに予算を 1 回確かめてから、頭の 4097 バイトを読む)。
                failure(budget.check()).and_then(|()| {
                    let (oid, target) =
                        failure(private::parse_loose_ref(&bytes_of(&input["head"]), width))?;
                    map.insert(bytes_of(&input["name"]), oid, target);
                    Ok(())
                })
            };
            outcome(result, |()| map_value(&map))
        }
        "resolve_refs" => outcome(
            private::resolve_refs(
                &ref_map(&input["common"]),
                &ref_map(&input["local"]),
                input["same_dir"].as_bool().unwrap(),
                budget,
                stub,
            ),
            |resolution| {
                let records: Vec<Value> = resolution
                    .records
                    .iter()
                    .map(|record| {
                        json!([
                            hex(&record.name),
                            record.oid,
                            record.target.as_deref().map(hex),
                            if record.resolved {
                                "resolved"
                            } else {
                                "unresolved"
                            }
                        ])
                    })
                    .collect();
                let head = resolution
                    .head
                    .map(|head| json!([head.oid, head.target.as_deref().map(hex)]));
                json!({"records": records, "head": head})
            },
        ),
        "head" => {
            let width = input["width"].as_u64().unwrap() as usize;
            let result = if input["backend"] == "files" {
                private::head_from_file(&bytes_of(&input["head"]), width, budget, stub)
            } else {
                // 旧版の :198 を写す(先に参照の解決を 1 回呼んでから HEAD を読む)。
                stub.refs(budget)
                    .and_then(|resolution| failure(private::head_from_reftable(&resolution)))
            };
            outcome(result, head_out)
        }
        "oid" => outcome(
            private::oid(
                &codepoints(&input["value"]),
                input["unborn"].as_bool().unwrap(),
                input["width"].as_u64().unwrap() as usize,
                budget,
                stub,
            ),
            |oid| json!(oid),
        ),
        "take" => {
            let manifest = run_one(
                "check_manifest",
                &json!({"raw": input["manifest"]}),
                &Cond::Normal,
            );
            let records = run_one(
                "parse_records",
                &json!({
                    "meta": input["meta"],
                    "contexts": input["contexts"],
                    "cwd": input["cwd"],
                    "context_id": input["context_id"],
                }),
                &Cond::Normal,
            );
            let origins = run_one(
                "parse_origins",
                &json!({"raw": input["origins"]}),
                &Cond::Normal,
            );
            json!(["ok", {"manifest": manifest, "records": records, "origins": origins}])
        }
        other => panic!("未知の関数: {other}"),
    }
}

/// 1 つの条件で動かした結果・予算の確かめの回数・差し替えの部品の呼出し。
fn run_one(name: &str, input: &Value, cond: &Cond) -> Value {
    let (mut budget, calls) = make_budget(cond);
    let mut stub = Stub::new(input);
    let result = run_function(name, input, &mut budget, &mut stub);
    json!({"result": result, "checks": calls.get() - 1, "calls": stub.events})
}

fn run_cases(cases: &Value) -> Value {
    let mut results = serde_json::Map::new();
    for case in cases["cases"].as_array().unwrap() {
        let name = case["fn"].as_str().unwrap();
        let outputs: Vec<Value> = case["conds"]
            .as_array()
            .unwrap()
            .iter()
            .map(|cond| run_one(name, &case["input"], &parse_cond(cond)))
            .collect();
        results.insert(
            case["id"].as_str().unwrap().to_owned(),
            Value::Array(outputs),
        );
    }
    json!({ "results": Value::Object(results) })
}

/// M の組の、失敗で終わる最初の 1 件の理由を変える(比較が働くことを確かめるための差し込み)。
fn inject_mismatch(cases: &Value, results: &mut Value) {
    for case in cases["cases"].as_array().unwrap() {
        if case["group"] != "M" {
            continue;
        }
        let outputs = &mut results["results"][case["id"].as_str().unwrap()];
        let result = &mut outputs[0]["result"];
        if result[0] == "failure" {
            let reason = format!("{}-r4d3-mismatch", result[1].as_str().unwrap());
            result[1] = json!(reason);
            return;
        }
    }
    panic!("M の組に、失敗で終わる入力が無い");
}

fn write_json(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

fn count_line(log: &str, prefix: &str) -> usize {
    log.lines()
        .filter(|line| {
            line.strip_prefix(prefix)
                .and_then(|rest| rest.strip_suffix(" 件合格"))
                .is_some_and(|count| {
                    !count.is_empty()
                        && count.bytes().all(|b| b.is_ascii_digit())
                        && count.parse::<u64>().is_ok_and(|n| n >= 1)
                })
        })
        .count()
}

#[test]
#[ignore = "R4D-3 の比較は build.py r4d3 か --ignored で流す"]
fn rust_matches_the_legacy_state_and_ref_checks_on_every_input() {
    let started = Instant::now();
    let scratch = Scratch::create();
    let work = scratch.0.canonicalize().unwrap();
    let generated = work.join("gen");
    fs::create_dir(&generated).unwrap();
    run_checker(&[OsStr::new("--generate"), generated.as_os_str()], started);
    let cases_path = generated.join("cases.json");
    let cases: Value = serde_json::from_slice(&fs::read(&cases_path).unwrap()).unwrap();
    assert_eq!(cases["scope"], "full");

    let rust_started = Instant::now();
    let mut results = run_cases(&cases);
    let rust_path = work.join("rust.json");
    write_json(&rust_path, &results);
    // 合格したテストの出力は libtest が隠すので、直接 stderr へ書く(CI のログに残す)。
    let _ = writeln!(
        std::io::stderr(),
        "R4D-3 Rust の実行: {:.1} 秒",
        rust_started.elapsed().as_secs_f64()
    );

    let compare_log = run_checker(
        &[
            OsStr::new("--compare"),
            cases_path.as_os_str(),
            rust_path.as_os_str(),
        ],
        started,
    );
    // 通常の比較は、比較器の自己検査を流していなければならない(黙って飛ばした合格にしない)。
    assert!(
        count_line(&compare_log, "R4D-3 比較器の自己検査: ") >= 1,
        "比較器が自己検査を流していない(「N 件合格」の行が無い)"
    );
    assert!(
        !compare_log.contains("飛ばした"),
        "比較器が自己検査を飛ばした"
    );
    inject_mismatch(&cases, &mut results);
    let biased_path = work.join("rust-mismatch.json");
    write_json(&biased_path, &results);
    run_checker(
        &[
            OsStr::new("--compare"),
            cases_path.as_os_str(),
            biased_path.as_os_str(),
            OsStr::new("--expect-mismatch"),
        ],
        started,
    );
}
