#![forbid(unsafe_code)]
//! R4D-2: 診断の厳密な JSON・base64・確認用の値・応答の符号化・上限付きの読み取りを、旧版(Python)と同じ入力・
//! 同じ予算の条件・同じ筋書きで比べる。製品への登録は行わない。登録しない内部部品を `#[path]` で読み込む。
//!
//! 流れ: 作業用のフォルダ → 比較器の `--generate`(入力と筋書きの一覧・旧版の結果)→ 同じ入力と筋書きを Rust で
//! 動かし、結果を JSON に書く(R の筋書きの木は、比較器が決めた同じパスに作り直す。R10 は自分の実行物を子として
//! 起動し、子の中で作業場所を移る)→ `--compare` → 差し込みの回(R の筋書きの 1 つの量に 1 を足す・J3 の 1 点の
//! 深さに 1 を足す)→ `--compare --expect-mismatch`。
#[allow(dead_code)]
#[path = "../src/commands/diagnostic_git/mod.rs"]
mod diagnostic_git;

use dev_workflow_core::loop_state::CodepointString;
use dev_workflow_platform::diagnostic::{self as os, Stat};
use diagnostic_git::budget::{Budget, BudgetOp, Failure, Kind};
use diagnostic_git::json::{
    self, DecodeError, Dict, EncodeError, Int, ResponseError, Value as JValue, fingerprint,
};
use diagnostic_git::reader::{Faults, Reader, ReaderError};
use serde_json::{Map, Value, json};
use std::cell::Cell;
use std::ffi::OsStr;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant, UNIX_EPOCH};

/// 1 回の比較のテストの中の、全部の子(比較器・R10 の子)の起動の合計の上限時間(最初の起動から数える)。
const TOTAL_LIMIT: Duration = Duration::from_secs(900);
const R10_SPEC: &str = "R4D2_R10_SPEC";
const R10_OUT: &str = "R4D2_R10_OUT";
const R10_CHILD: &str = "r10_child_runs_scenarios_in_its_own_working_directory";
const DEEP_COUNT: usize = 25;

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

fn hexv(value: &Value) -> Vec<u8> {
    unhex(value.as_str().expect("16 進の字面"))
}

fn path_of(bytes: &[u8]) -> PathBuf {
    PathBuf::from(OsStr::from_bytes(bytes))
}

fn join(root: &[u8], rel: &[u8]) -> PathBuf {
    let mut path = root.to_vec();
    path.push(b'/');
    path.extend_from_slice(rel);
    path_of(&path)
}

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        remove_tree(self.0.as_os_str().as_bytes());
    }
}

/// 直接の子。落とすときは止めて回収する。
struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap()
}

/// 子を起動し、終わるまで待つ。上限時間を過ぎたら止めて回収し、失敗にする。
/// 標準エラーは受け取り、`echo` が真ならそのまま標準エラーへ書き出す(CI のログに残す)。
fn wait_child(
    mut command: Command,
    started: Instant,
    echo: bool,
    what: &str,
) -> (bool, String, String) {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = ChildGuard(
        command
            .spawn()
            .unwrap_or_else(|e| panic!("{what} を起動できません: {e}")),
    );
    let mut out = child.0.stdout.take().unwrap();
    let mut err = child.0.stderr.take().unwrap();
    let out_reader = std::thread::spawn(move || {
        let mut collected = Vec::new();
        let _ = out.read_to_end(&mut collected);
        collected
    });
    let err_reader = std::thread::spawn(move || {
        let mut collected = Vec::new();
        let mut chunk = [0_u8; 8192];
        while let Ok(count) = err.read(&mut chunk) {
            if count == 0 {
                break;
            }
            if echo {
                let _ = std::io::stderr().write_all(&chunk[..count]);
            }
            collected.extend_from_slice(&chunk[..count]);
        }
        collected
    });
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            let stdout = String::from_utf8_lossy(&out_reader.join().unwrap()).into_owned();
            let stderr = String::from_utf8_lossy(&err_reader.join().unwrap()).into_owned();
            return (status.success(), stdout, stderr);
        }
        if started.elapsed() >= TOTAL_LIMIT {
            let _ = child.0.kill();
            let _ = child.0.wait();
            panic!(
                "R4D-2 の比較が上限時間 {} 秒に当たりました({what})",
                TOTAL_LIMIT.as_secs()
            );
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// 比較器を起動する。外の環境変数が比較器の動きを変えないよう、自己検査を飛ばす印と小さい組の印を外す。
fn run_checker(args: &[&OsStr], started: Instant) -> String {
    let mut command = Command::new("python3");
    command
        .arg("-B")
        .arg(repo_root().join("native/ci/check_diagnostic_runtime.py"))
        .args(args)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("R4D2_NO_SELFTEST")
        .env_remove("R4D2_ALLOW_MINI");
    let (ok, _, stderr) = wait_child(command, started, true, "R4D-2 の比較器");
    assert!(ok, "R4D-2 の比較器が失敗しました");
    stderr
}

// ---------------------------------------------------------------- 値

fn codepoints(value: &Value) -> CodepointString {
    let list: Vec<u32> = value
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_u64().unwrap() as u32)
        .collect();
    CodepointString::try_from_codepoints(list).unwrap()
}

fn cps_json(text: &CodepointString) -> Value {
    Value::Array(text.as_codepoints().iter().map(|&c| json!(c)).collect())
}

fn untyped(data: &Value) -> JValue {
    let v = &data["v"];
    match data["t"].as_str().unwrap() {
        "null" => JValue::Null,
        "bool" => JValue::Bool(v.as_bool().unwrap()),
        "int" => JValue::Int(Int::parse(v.as_str().unwrap()).expect("正規の整数")),
        "float" => JValue::Float(f64::from_bits(
            u64::from_str_radix(v.as_str().unwrap(), 16).unwrap(),
        )),
        "str" => JValue::Str(codepoints(v)),
        "list" => JValue::List(v.as_array().unwrap().iter().map(untyped).collect()),
        "dict" => {
            let mut dict = Dict::new();
            for pair in v.as_array().unwrap() {
                dict.insert(codepoints(&pair[0]), untyped(&pair[1]));
            }
            JValue::Dict(dict)
        }
        other => panic!("未知の型: {other}"),
    }
}

/// decode の結果を、前から順に並べた印の列にする(再帰を使わない)。
fn tokens(value: &JValue) -> Value {
    enum Item<'a> {
        Value(&'a JValue),
        Key(&'a CodepointString),
    }
    let mut out = Vec::new();
    let mut stack = vec![Item::Value(value)];
    while let Some(item) = stack.pop() {
        match item {
            Item::Key(key) => {
                out.push(json!("K"));
                out.push(cps_json(key));
            }
            Item::Value(JValue::Null) => out.push(json!("N")),
            Item::Value(JValue::Bool(true)) => out.push(json!("T")),
            Item::Value(JValue::Bool(false)) => out.push(json!("F")),
            Item::Value(JValue::Int(int)) => {
                out.push(json!("I"));
                out.push(json!(int.as_str()));
            }
            Item::Value(JValue::Float(float)) => {
                out.push(json!("R"));
                out.push(json!(format!("{:016x}", float.to_bits())));
            }
            Item::Value(JValue::Str(text)) => {
                out.push(json!("S"));
                out.push(cps_json(text));
            }
            Item::Value(JValue::List(items)) => {
                out.push(json!("L"));
                out.push(json!(items.len()));
                for child in items.iter().rev() {
                    stack.push(Item::Value(child));
                }
            }
            Item::Value(JValue::Dict(dict)) => {
                out.push(json!("D"));
                out.push(json!(dict.len()));
                let pairs: Vec<_> = dict.iter().collect();
                for (key, child) in pairs.into_iter().rev() {
                    stack.push(Item::Value(child));
                    stack.push(Item::Key(key));
                }
            }
        }
    }
    Value::Array(out)
}

fn decode_result(result: Result<JValue, DecodeError>) -> Value {
    match result {
        Ok(value) => json!(["ok", tokens(&value)]),
        Err(DecodeError::Schema) => json!(["err", "schema"]),
        Err(DecodeError::Recursion) => json!(["err", "recursion"]),
    }
}

fn encode_result(result: Result<Vec<u8>, EncodeError>) -> Value {
    match result {
        Ok(bytes) => json!(["ok", hex(&bytes)]),
        Err(EncodeError::Value) => json!(["err", "value"]),
    }
}

fn failure_json(failure: Failure) -> Value {
    json!(["err", "failure", failure.reason, failure.code])
}

// ---------------------------------------------------------------- 予算

fn limits() -> [u64; 9] {
    Kind::ALL.map(Kind::default_limit)
}

/// 比較器の条件 ['normal'] ['expired'] ['stop'] ['n', N] ['q', 量, L] と同じ予算(操作の列を記録する)。
fn make_budget(cond: &Value) -> Budget {
    let list = cond.as_array().unwrap();
    let constant = || -> Box<dyn FnMut() -> f64> { Box::new(|| 0.0) };
    let mut budget = match list[0].as_str().unwrap() {
        "normal" => Budget::for_test(300.0, constant(), limits()),
        "stop" => {
            let budget = Budget::for_test(300.0, constant(), limits());
            budget.request_stop();
            budget
        }
        "expired" => {
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
        "n" => {
            let calls = Cell::new(0_u64);
            Budget::for_test(
                list[1].as_u64().unwrap() as f64,
                Box::new(move || {
                    calls.set(calls.get() + 1);
                    (calls.get() - 1) as f64
                }),
                limits(),
            )
        }
        "q" => {
            let mut table = limits();
            let kind = Kind::from_name(list[1].as_str().unwrap()).unwrap();
            table[kind as usize] = list[2].as_u64().expect("0 以上の上限");
            Budget::for_test(300.0, constant(), table)
        }
        other => panic!("未知の条件: {other}"),
    };
    budget.set_test_log(true);
    budget
}

fn log_text(budget: &mut Budget) -> String {
    budget
        .take_test_log()
        .into_iter()
        .map(|op| match op {
            BudgetOp::Check => "c".to_owned(),
            BudgetOp::Charge(kind, count) => format!("{}={count}", kind.name()),
        })
        .collect::<Vec<_>>()
        .join(";")
}

// ---------------------------------------------------------------- J の組

fn run_j(group: &str, item: &Value, depth_bias: usize) -> Value {
    match group {
        "j1-decode-corpus" | "j2-decode-random" | "j3-decode-depth" => {
            let depth = item["depth"].as_u64().unwrap() as usize + depth_bias;
            decode_result(json::decode(&hexv(&item["raw"]), depth))
        }
        "j4-encode" => {
            let value = untyped(&item["value"]);
            json!([
                encode_result(json::encoded(&value)),
                encode_result(json::encoded_unicode(&value))
            ])
        }
        "j5-response" => {
            let value = untyped(&item["value"]);
            let mut budget = make_budget(&item["cond"]);
            let result = match json::response_bytes(&value, &mut budget) {
                Ok(bytes) => json!(["ok", hex(&bytes)]),
                Err(ResponseError::Budget(failure)) => failure_json(failure),
                Err(ResponseError::Value) => json!(["err", "value"]),
            };
            json!({"result": result, "budget": log_text(&mut budget)})
        }
        "j6-base64" => match item["op"].as_str().unwrap() {
            "unb64" => match json::unb64(&untyped(&item["value"])) {
                Ok(raw) => json!(["ok", hex(&raw)]),
                Err(failure) => failure_json(failure),
            },
            _ => {
                let text = json::b64(&hexv(&item["raw"]));
                json!(["ok", cps_json(&CodepointString::from(text.as_str()))])
            }
        },
        "j7-keys" => {
            let names: String = codepoints(&item["names"])
                .as_codepoints()
                .iter()
                .map(|&c| char::from_u32(c).unwrap())
                .collect();
            match json::keys(&untyped(&item["value"]), &names) {
                Ok(()) => json!(["ok"]),
                Err(failure) => failure_json(failure),
            }
        }
        "j8-metadata" => {
            let mut budget = make_budget(&json!(["normal"]));
            let mut reader = Reader::new();
            let info = reader
                .lstat(&mut budget, &hexv(&item["path"]), false)
                .unwrap()
                .unwrap();
            let print = fingerprint(&info);
            json!([
                "ok",
                [
                    print.dev.to_string(),
                    print.ino.to_string(),
                    print.mode.to_string(),
                    print.size.to_string(),
                    print.mtime_ns.to_string(),
                    print.ctime_ns.to_string()
                ],
                hex(&json::encoded(&json::metadata(&info)).unwrap())
            ])
        }
        other => panic!("未知の組: {other}"),
    }
}

// ---------------------------------------------------------------- R の筋書き

fn chmod(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

/// 比較器の `content_of` と同じ中身(16 進の字面か、i 番目のバイトが (i * 7 + seed) % 256 の列)。
fn content_of(spec: &Value) -> Vec<u8> {
    if let Some(text) = spec.as_str() {
        return unhex(text);
    }
    let size = spec["size"].as_u64().unwrap() as usize;
    let seed = spec["seed"].as_u64().unwrap() as usize;
    (0..size)
        .map(|i| ((i % (256 * 7)) * 7 + seed) as u8)
        .collect()
}

fn build_tree(root: &[u8], tree: &Value) {
    let root_path = path_of(root);
    fs::create_dir(&root_path).unwrap();
    chmod(&root_path, 0o755);
    for entry in tree.as_array().unwrap() {
        let entry = entry.as_array().unwrap();
        let path = join(root, &hexv(&entry[1]));
        match entry[0].as_str().unwrap() {
            "dir" => {
                fs::create_dir(&path).unwrap();
                chmod(&path, entry[2].as_u64().unwrap() as u32);
            }
            "file" => {
                fs::write(&path, content_of(&entry[2])).unwrap();
                chmod(&path, entry[3].as_u64().unwrap() as u32);
            }
            "link" => std::os::unix::fs::symlink(path_of(&hexv(&entry[2])), &path).unwrap(),
            "fifo" => {
                let status = Command::new("mkfifo").arg(&path).status().unwrap();
                assert!(status.success(), "mkfifo が失敗しました");
                chmod(&path, entry[2].as_u64().unwrap() as u32);
            }
            "files" => {
                for index in 0..entry[2].as_u64().unwrap() {
                    fs::write(path.join(format!("{index:05}")), b"").unwrap();
                }
            }
            "chmod" => chmod(&path, entry[2].as_u64().unwrap() as u32),
            other => panic!("木の項目が不明: {other}"),
        }
    }
}

/// 権限を戻してから消す(リンクは辿らない)。
fn remove_tree(root: &[u8]) {
    fn open_up(path: &Path) {
        let Ok(entries) = fs::read_dir(path) else {
            return;
        };
        for entry in entries.flatten() {
            let child = entry.path();
            if let Ok(info) = fs::symlink_metadata(&child)
                && info.is_dir()
            {
                let _ = fs::set_permissions(&child, fs::Permissions::from_mode(0o700));
                open_up(&child);
            }
        }
    }
    let path = path_of(root);
    if fs::symlink_metadata(&path).is_err() {
        return;
    }
    let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o700));
    open_up(&path);
    fs::remove_dir_all(&path).unwrap();
}

fn mutate(root: &[u8], op: &[Value]) {
    let path = join(root, &hexv(&op[1]));
    match op[0].as_str().unwrap() {
        "m-append" => {
            let mut handle = fs::OpenOptions::new().append(true).open(&path).unwrap();
            handle.write_all(&hexv(&op[2])).unwrap();
        }
        "m-replace" => {
            let mut temporary = path.clone().into_os_string();
            temporary.push(".new");
            fs::write(&temporary, hexv(&op[2])).unwrap();
            chmod(Path::new(&temporary), op[3].as_u64().unwrap() as u32);
            fs::rename(&temporary, &path).unwrap();
        }
        "m-file" => fs::write(&path, hexv(&op[2])).unwrap(),
        "m-chmod" => chmod(&path, op[2].as_u64().unwrap() as u32),
        "m-mtime" => {
            let at = UNIX_EPOCH + Duration::from_nanos(op[2].as_u64().unwrap());
            let handle = fs::OpenOptions::new().write(true).open(&path).unwrap();
            handle
                .set_times(fs::FileTimes::new().set_accessed(at).set_modified(at))
                .unwrap();
        }
        "m-mkdir" => fs::create_dir(&path).unwrap(),
        "m-rmdir" => fs::remove_dir(&path).unwrap(),
        "m-unlink" => fs::remove_file(&path).unwrap(),
        "m-rename" => fs::rename(&path, join(root, &hexv(&op[2]))).unwrap(),
        "m-symlink" => std::os::unix::fs::symlink(path_of(&hexv(&op[2])), &path).unwrap(),
        other => panic!("変更の操作が不明: {other}"),
    }
}

/// 旧版の `os.lstat(path)`(Reader の外。予算を使わない)。
fn outside_lstat(path: &[u8]) -> Stat {
    let info = fs::symlink_metadata(path_of(path)).unwrap();
    Stat {
        dev: info.dev() as i64,
        ino: info.ino(),
        mode: info.mode(),
        size: info.size() as i64,
        uid: info.uid(),
        mtime_ns: i128::from(info.mtime()) * 1_000_000_000 + i128::from(info.mtime_nsec()),
        ctime_ns: i128::from(info.ctime()) * 1_000_000_000 + i128::from(info.ctime_nsec()),
    }
}

fn summary(info: Option<Stat>) -> Value {
    match info {
        None => Value::Null,
        Some(info) => json!([
            info.format(),
            info.mode,
            info.size,
            info.uid == os::geteuid()
        ]),
    }
}

fn reader_error(error: ReaderError) -> Value {
    match error {
        ReaderError::Failure(failure) => failure_json(failure),
        ReaderError::Os(number) => json!(["err", "os", os::errno_name(number)]),
        ReaderError::Nul => json!(["err", "nul"]),
    }
}

fn kind_of(value: &Value) -> Option<Kind> {
    value.as_str().map(|name| Kind::from_name(name).unwrap())
}

fn reader_op(reader: &mut Reader, budget: &mut Budget, op: &[Value]) -> Result<Value, ReaderError> {
    let path = op.get(1).and_then(Value::as_str).map(unhex);
    let path = path.as_deref().unwrap_or_default();
    Ok(match op[0].as_str().unwrap() {
        "owner" => {
            let info = (op[2] != "none").then(|| outside_lstat(path));
            reader.owner(budget, path, info.as_ref(), op[3].as_u64().unwrap() as u8)?;
            Value::Null
        }
        "directory" => {
            reader.directory(budget, path)?.close()?;
            json!("fd")
        }
        "lstat" => summary(reader.lstat(budget, path, op[2].as_bool().unwrap())?),
        "read" => {
            let data = reader.read(
                budget,
                path,
                op[2].as_u64().unwrap(),
                kind_of(&op[3]),
                op[4].as_bool().unwrap(),
            )?;
            data.map_or(Value::Null, |data| json!(hex(&data)))
        }
        "names" => {
            let names = reader.names(
                budget,
                path,
                kind_of(&op[2]).unwrap(),
                op[3].as_bool().unwrap(),
            )?;
            Value::Array(names.iter().map(|name| json!(hex(name))).collect())
        }
        "verify" => {
            reader.verify(budget)?;
            Value::Null
        }
        "add-missing" => {
            reader.add_missing(path);
            Value::Null
        }
        "put-file" => {
            let source = hexv(&op[2]);
            let record = (
                fingerprint(&outside_lstat(&source)),
                op[3].as_str().unwrap().to_owned(),
            );
            reader.put_file_record(path, record);
            Value::Null
        }
        "get-file" => reader
            .file_record(path)
            .map_or(Value::Null, |held| json!(held.1)),
        other => panic!("操作が不明: {other}"),
    })
}

fn records(reader: &Reader) -> Value {
    let mut missing: Vec<String> = reader.missing().map(hex).collect();
    missing.sort();
    json!({
        "files": reader.files().iter().map(|(p, held)| json!([hex(p), held.1])).collect::<Vec<_>>(),
        "dirs": reader.dirs().iter().map(|(p, held)| json!([hex(p), held.2])).collect::<Vec<_>>(),
        "missing": missing,
        "sets": reader.sets().iter().map(|(p, names)| {
            json!([hex(p), names.iter().map(|n| hex(n)).collect::<Vec<_>>()])
        }).collect::<Vec<_>>(),
        "owners": reader.owners().iter().map(|(p, held)| {
            json!([hex(p), held.1, held.0 == os::geteuid()])
        }).collect::<Vec<_>>(),
    })
}

fn errno_of(name: &Value) -> i32 {
    match name.as_str().unwrap() {
        "EIO" => os::EIO,
        "EACCES" => os::EACCES,
        "ENOENT" => os::ENOENT,
        other => panic!("未知の errno の名前: {other}"),
    }
}

/// R8 の差し込みを、比較器の `LegacyFaults` と同じ意味で継ぎ目に足す。
fn faults_of(root: &[u8], spec: &Value, cwd: Option<Vec<u8>>) -> Faults {
    let mut faults = Faults::default();
    faults.cwd = cwd;
    if spec.is_null() {
        return faults;
    }
    let pair = |key: &str| {
        spec.get(key)
            .filter(|v| !v.is_null())
            .map(|v| (v[0].as_u64().unwrap(), errno_of(&v[1])))
    };
    faults.read_error = pair("read_error");
    faults.open_error = pair("open_error");
    faults.close_error = pair("close_error");
    faults.read_empty_at = spec.get("read_empty_at").and_then(Value::as_u64);
    if let Some(value) = spec.get("names_error").filter(|v| !v.is_null()) {
        faults.names_error = Some((
            value[0].as_u64().unwrap(),
            value[1].as_u64().unwrap(),
            errno_of(&value[2]),
        ));
    }
    if let Some(value) = spec.get("cwd_error").filter(|v| !v.is_null()) {
        faults.cwd_error = Some(errno_of(value));
    }
    if let Some(value) = spec.get("append_after_first_read").filter(|v| !v.is_null()) {
        let target = join(root, &hexv(&value[0]));
        let extra = hexv(&value[1]);
        faults.after_first_read = Some(Box::new(move || {
            let mut handle = fs::OpenOptions::new().append(true).open(&target).unwrap();
            handle.write_all(&extra).unwrap();
        }));
    }
    if let Some(value) = spec.get("replace_before_open").filter(|v| !v.is_null()) {
        let source = join(root, &hexv(&value[0]));
        let target = join(root, &hexv(&value[1]));
        faults.before_file_open = Some(Box::new(move |_name: &[u8]| {
            fs::rename(&source, &target).unwrap();
        }));
    }
    faults
}

/// 操作を順に流す(Reader の操作が失敗したら止める)。記録と予算の操作の列を返す。
fn run_ops(root: &[u8], item: &Value, reader: &mut Reader, budget: &mut Budget) -> Value {
    let mut results = Vec::new();
    for op in item["ops"].as_array().unwrap() {
        let op = op.as_array().unwrap();
        if op[0].as_str().unwrap().starts_with("m-") {
            mutate(root, op);
            results.push(json!(["ok", null]));
            continue;
        }
        match reader_op(reader, budget, op) {
            Ok(value) => results.push(json!(["ok", value])),
            Err(error) => {
                results.push(reader_error(error));
                break;
            }
        }
    }
    json!({"ops": results, "records": records(reader), "budget": log_text(budget)})
}

fn run_scenario(item: &Value, bias: Option<Kind>) -> Value {
    let root = hexv(&item["root"]);
    remove_tree(&root);
    build_tree(&root, &item["tree"]);
    let mut budget = make_budget(&item["cond"]);
    if let Some(kind) = bias {
        budget.set_test_bias(kind, 1);
    }
    let mut reader = Reader::new();
    let cwd = item["cwd"].as_str().map(unhex);
    reader.set_faults(faults_of(&root, &item["faults"], cwd));
    let result = run_ops(&root, item, &mut reader, &mut budget);
    drop(reader);
    remove_tree(&root);
    result
}

/// R10: 子の中で、作業場所を 1 段ずつ相対パスで移ってから、相対パスの筋書きを流す。
fn run_cwd_scenario(item: &Value) -> Value {
    let mode = item["child"].as_str().unwrap();
    let root = hexv(&item["root"]);
    remove_tree(&root);
    fs::DirBuilder::new()
        .mode(0o755)
        .create(path_of(&root))
        .unwrap();
    let original = std::env::current_dir().unwrap();
    std::env::set_current_dir(path_of(&root)).unwrap();
    let level = "d".repeat(200);
    if mode == "deep" {
        for _ in 0..DEEP_COUNT {
            fs::DirBuilder::new().mode(0o755).create(&level).unwrap();
            std::env::set_current_dir(&level).unwrap();
        }
    }
    if mode == "deleted" {
        fs::DirBuilder::new().mode(0o755).create("gone").unwrap();
        std::env::set_current_dir("gone").unwrap();
        fs::remove_dir(join(&root, b"gone")).unwrap();
    } else {
        let mut handle = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o644)
            .open("f")
            .unwrap();
        handle.write_all(b"abc").unwrap();
    }
    let mut budget = make_budget(&item["cond"]);
    let mut reader = Reader::new();
    let result = run_ops(&root, item, &mut reader, &mut budget);
    if mode != "deleted" {
        fs::remove_file("f").unwrap();
    }
    if mode == "deep" {
        for _ in 0..DEEP_COUNT {
            std::env::set_current_dir("..").unwrap();
            fs::remove_dir(&level).unwrap();
        }
    }
    std::env::set_current_dir(&original).unwrap();
    remove_tree(&root);
    result
}

/// R10 の補助のテスト。環境変数が無ければ何もしない。子は作業場所を変えてよい(親は変えない)。
#[test]
#[ignore = "R4D-2 の比較が子として起動する補助(R10)。単独では何もしない"]
fn r10_child_runs_scenarios_in_its_own_working_directory() {
    let Some(spec) = std::env::var_os(R10_SPEC) else {
        return;
    };
    let out = PathBuf::from(std::env::var_os(R10_OUT).unwrap());
    let items: Vec<Value> = serde_json::from_slice(&fs::read(spec).unwrap()).unwrap();
    let mut results = Map::new();
    for item in &items {
        results.insert(
            item["id"].as_str().unwrap().to_owned(),
            run_cwd_scenario(item),
        );
    }
    fs::write(out, serde_json::to_vec(&Value::Object(results)).unwrap()).unwrap();
}

fn run_r10(items: &[Value], work: &Path, started: Instant) -> Map<String, Value> {
    let spec = work.join("r10-spec.json");
    let out = work.join("r10-out.json");
    fs::write(&spec, serde_json::to_vec(items).unwrap()).unwrap();
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--ignored", "--exact", R10_CHILD, "--test-threads=1"])
        .env(R10_SPEC, &spec)
        .env(R10_OUT, &out)
        .current_dir(work);
    // 子の標準出力(libtest の `test result:` の行)は受け止め、失敗したときだけ標準エラーへ出す。
    let (ok, stdout, stderr) = wait_child(command, started, false, "R10 の子");
    if !ok {
        eprintln!("{stdout}{stderr}");
        panic!("R10 の子が失敗しました");
    }
    let results: Value = serde_json::from_slice(&fs::read(&out).unwrap()).unwrap();
    fs::remove_file(&spec).unwrap();
    fs::remove_file(&out).unwrap();
    results.as_object().unwrap().clone()
}

fn write_json(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

#[test]
#[ignore = "R4D-2 の比較。CI は r4d2 のジョブ、手元は build.py r4d2 か -- --ignored で流す"]
fn rust_matches_the_legacy_runtime_parts_on_every_input() {
    let started = Instant::now();
    let scratch =
        Scratch(std::env::temp_dir().join(format!("r4d2-compare-{}", std::process::id())));
    remove_tree(scratch.0.as_os_str().as_bytes());
    fs::create_dir(&scratch.0).unwrap();
    let work = scratch.0.canonicalize().unwrap();
    let generated = work.join("gen");
    fs::create_dir(&generated).unwrap();
    run_checker(&[OsStr::new("--generate"), generated.as_os_str()], started);
    let cases: Value =
        serde_json::from_slice(&fs::read(generated.join("cases.json")).unwrap()).unwrap();
    assert_eq!(cases["scope"], "full");

    let rust_started = Instant::now();
    let mut results = Map::new();
    let mut r10 = Vec::new();
    for (group, items) in cases["groups"].as_object().unwrap() {
        for item in items.as_array().unwrap() {
            let id = item["id"].as_str().unwrap().to_owned();
            let result = if group.starts_with('j') {
                run_j(group, item, 0)
            } else if item["child"].is_string() {
                r10.push(item.clone());
                continue;
            } else {
                run_scenario(item, None)
            };
            results.insert(id, result);
        }
    }
    results.extend(run_r10(&r10, &work, started));
    let rust_path = work.join("rust.json");
    write_json(&rust_path, &json!({ "results": results }));

    // 差し込みの回: R の筋書きの 1 つの量に 1 を足し、J3 の 1 点の深さに 1 を足す。
    let bias = &cases["bias"];
    let find = |id: &Value| {
        cases["groups"]
            .as_object()
            .unwrap()
            .iter()
            .flat_map(|(group, items)| {
                items
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(move |item| (group, item))
            })
            .find(|(_, item)| item["id"] == *id)
            .unwrap()
    };
    let mut biased = Map::new();
    let (group, item) = find(&bias["j3"]);
    biased.insert(
        bias["j3"].as_str().unwrap().to_owned(),
        run_j(group, item, 1),
    );
    let (_, item) = find(&bias["r"]);
    let kind = Kind::from_name(bias["kind"].as_str().unwrap()).unwrap();
    biased.insert(
        bias["r"].as_str().unwrap().to_owned(),
        run_scenario(item, Some(kind)),
    );
    let biased_path = work.join("rust-bias.json");
    write_json(&biased_path, &json!({ "results": biased }));
    let _ = writeln!(
        std::io::stderr(),
        "R4D-2 Rust の実行: {:.1} 秒",
        rust_started.elapsed().as_secs_f64()
    );

    let legacy = generated.join("legacy.json");
    let log = run_checker(
        &[
            OsStr::new("--compare"),
            legacy.as_os_str(),
            rust_path.as_os_str(),
        ],
        started,
    );
    // 通常の比較は、比較器の自己検査を流していなければならない(黙って飛ばした合格にしない)。
    let passed = log.lines().any(|line| {
        line.strip_prefix("R4D-2 比較器の自己検査: ")
            .and_then(|rest| rest.strip_suffix(" 件合格"))
            .is_some_and(|count| count.parse::<u64>().is_ok_and(|n| n >= 1))
    });
    assert!(
        passed,
        "比較器が自己検査を流していない(「N 件合格」の行が無い)"
    );
    assert!(!log.contains("飛ばした"), "比較器が自己検査を飛ばした");
    for line in ["R4D-2 の比較: 入力 ", "R4D-2 件数: ", "R4D-2 資源の上限: "] {
        assert!(log.contains(line), "要約の行が無い: {line}");
    }
    run_checker(
        &[
            OsStr::new("--compare"),
            legacy.as_os_str(),
            biased_path.as_os_str(),
            OsStr::new("--expect-mismatch"),
        ],
        started,
    );
}
