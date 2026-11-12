//! 9 コマンドのうち `bootstrap`・`verify`・`host-settings`・`host-allowlist` と、内部 reader の固定期待の検査。
//! 固定期待(`native/ci/s2e_fixtures/command_cases.json`)は、旧 guard を `python3 -B <配布物の中の guard> <コマンド>`
//! で直接起動した(U5)結果で、`gen_cases.py` の area `commands` が作る。同じ物理入力(一時フォルダ)で、`bootstrap` →
//! (入力の変更)→ `verify` などの流れを Rust で動かし、終了コード・標準出力・標準エラー・出力フォルダの中身・
//! 控えの値・Git の起動回数・reader の値と比べる。dev・ino・時刻は `*`、一時フォルダは `{r}` に置き換える。
//!
//! 既定の監視先の `/etc/...` は、Mac では `/private/etc` へのリンクで、旧版も Rust も `bootstrap` で必ず失敗する
//! (親をリンクを辿らずに開く)。このため、固定期待の流れは Linux だけで流し、Mac は失敗の形だけを確かめる。
use super::dispatch::{NoUsers, ProcessHost, dispatch};
use super::error::PyException;
use super::observe_tests::{Dir, apply_ops, build_layout};
use super::output::run_main;
use super::pyjson::{PyCtx, load_state};
use super::pyops::{Cps, as_text};
use super::pypath::UserLookup;
use super::pystr::{decode_fs, to_string};
use super::pyvalue::PyValue;
use super::reader::{
    component_policy_digest, error_json, read_components, read_host_settings, typed_json,
};
use super::runtime::{Env, PyStream, StreamKind};
use super::specs::Host;
use super::testing::{fixture, with_big_stack};
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const GUARD_BYTES: &[u8] = include_bytes!(
    "../../../../../../plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py"
);
const GUARD_REL: &str = "plugin/skills/ship-task/scripts/environment-guard.py";

/// 環境変数・作業場所・Git の子へ渡す環境を、テストが決められる `Host`。
pub struct TestHost {
    env: HashMap<String, Cps>,
    cwd: Cps,
    process: Vec<(Vec<u8>, Vec<u8>)>,
}

impl TestHost {
    pub fn new(pairs: &[(String, String)], cwd: &Path) -> Self {
        Self {
            env: pairs
                .iter()
                .map(|(k, v)| (k.clone(), decode_fs(v.as_bytes())))
                .collect(),
            cwd: decode_fs(cwd.as_os_str().as_bytes()),
            process: pairs
                .iter()
                .map(|(k, v)| (k.as_bytes().to_vec(), v.as_bytes().to_vec()))
                .collect(),
        }
    }
}

impl Host for TestHost {
    fn env(&self, name: &str) -> Option<Cps> {
        self.env.get(name).cloned()
    }
    fn cwd(&self) -> Result<Cps, PyException> {
        Ok(self.cwd.clone())
    }
    fn users(&self) -> &dyn UserLookup {
        &NoUsers
    }
    fn process_env(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        self.process.clone()
    }
}

/// コマンドを `run_main` で動かす。戻り値は終了コード・標準出力・標準エラー。
pub fn run_cli(host: &TestHost, args: &[String]) -> (i32, Vec<u8>, Vec<u8>) {
    let args: Vec<Cps> = args.iter().map(|a| decode_fs(a.as_bytes())).collect();
    let env = Env {
        columns: None,
        terminal_columns: 80,
        unbuffered: false,
        max_str_digits: 4300,
    };
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let status = {
        let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 8192, false);
        let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 8192, false);
        run_main(
            &args,
            &env,
            &mut stdout,
            &mut stderr,
            &mut |parsed, stdout, _| dispatch(host, parsed, stdout),
        )
    };
    (status, out, err)
}

fn find_in_path(name: &str) -> String {
    std::env::var("PATH")
        .unwrap()
        .split(':')
        .map(|dir| Path::new(dir).join(name))
        .find(|path| path.is_file())
        .unwrap_or_else(|| panic!("{name} が PATH に無い"))
        .to_str()
        .unwrap()
        .to_owned()
}

fn set_mode(path: &Path, mode: u32) {
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
}

/// 配布物の guard を置く(旧版を起動する側と同じ位置と mode)。
fn place_guard(root: &Path) {
    let path = root.join(GUARD_REL);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    for each in [
        "plugin",
        "plugin/skills",
        "plugin/skills/ship-task",
        "plugin/skills/ship-task/scripts",
    ] {
        set_mode(&root.join(each), 0o755);
    }
    std::fs::write(&path, GUARD_BYTES).unwrap();
    set_mode(&path, 0o644);
}

fn sub(text: &str, root: &str) -> String {
    let mut out = text.replace("{r}", root);
    while let Some(start) = out.find("{state:").or_else(|| out.find("{sha:")) {
        let end = start + out[start..].find('}').unwrap();
        let token = out[start + 1..end].to_owned();
        let (kind, label) = token.split_once(':').unwrap();
        let path = Path::new(root)
            .join("out")
            .join(label)
            .join("environment.json");
        let value = if kind == "state" {
            path.to_str().unwrap().to_owned()
        } else {
            match std::fs::read(&path) {
                Ok(raw) => super::pyops::sha_hex(&raw),
                Err(_) => "0".repeat(64),
            }
        };
        out.replace_range(start..=end, &value);
    }
    out
}

fn mask_sha(text: &str) -> String {
    let marker = "\"sha256\": \"";
    let mut out = String::new();
    let mut rest = text;
    while let Some(at) = rest.find(marker) {
        let after = at + marker.len();
        let hex = &rest[after..];
        if hex.len() >= 65
            && hex[..64]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            && hex.as_bytes()[64] == b'"'
        {
            out.push_str(&rest[..after]);
            out.push('*');
            rest = &rest[after + 64..];
        } else {
            out.push_str(&rest[..after]);
            rest = &rest[after..];
        }
    }
    out.push_str(rest);
    out
}

// ---------------------------------------------------------------- 出力の正規化

fn to_json(value: &PyValue) -> Value {
    match value {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(int) => match int.to_i64() {
            Some(n) => json!(n),
            None => Value::String(int.to_decimal(0).unwrap()),
        },
        PyValue::Float(f) => json!(f),
        PyValue::Str(s) => Value::String(to_string(s.as_codepoints())),
        PyValue::Bytes(b) => json!(b),
        PyValue::List(items) => Value::Array(items.iter().map(to_json).collect()),
        PyValue::Dict(d) => Value::Object(
            d.iter()
                .map(|(k, v)| (to_string(as_text(k).unwrap()), to_json(v)))
                .collect(),
        ),
    }
}

fn unroot(value: Value, root: &str) -> Value {
    match value {
        Value::String(text) => Value::String(text.replace(root, "{r}")),
        Value::Array(items) => Value::Array(items.into_iter().map(|v| unroot(v, root)).collect()),
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(k, v)| (k.replace(root, "{r}"), unroot(v, root)))
                .collect(),
        ),
        other => other,
    }
}

/// 種類ごとの、実行ごとに変わる位置(`gen_cases.py` の `MASK_POSITIONS` と同じ)。
fn masked_positions(items: &[Value]) -> &'static [usize] {
    match items.first().and_then(Value::as_str) {
        Some("file-stat") => &[1, 2, 5, 6],
        Some("directory-stat") => &[1, 2],
        Some("directory-step") => &[2, 3],
        Some("link-step") => &[3, 4, 5],
        Some("installation-link") if items.len() == 5 => &[2, 3, 4],
        Some("installation-link") | Some("configuration-link") => &[2, 3, 4, 5],
        _ => &[],
    }
}

fn mask_entry(value: Value) -> Value {
    let Value::Array(items) = value else {
        return value;
    };
    let positions = masked_positions(&items);
    Value::Array(
        items
            .into_iter()
            .enumerate()
            .map(|(i, v)| {
                if positions.contains(&i) {
                    json!("*")
                } else {
                    v
                }
            })
            .collect(),
    )
}

const DROP_PREFIX: [&str; 4] = ["@route:", "@route-end:", "@absolute:", "@origin:"];

/// 本文に一時フォルダの絶対パス(`{r}`)を含むファイルの、SHA-256 の読み替え(`gen_cases.py` の `cmd_sha_map` と同じ)。
fn sha_map(layout: &[Value], root: &str) -> HashMap<String, String> {
    layout
        .iter()
        .filter(|item| item.get("kind").and_then(Value::as_str).unwrap_or("file") == "file")
        .filter_map(|item| item.get("body").and_then(Value::as_str))
        .filter(|body| body.contains("{r}"))
        .map(|body| {
            (
                super::pyops::sha_hex(body.replace("{r}", root).as_bytes()),
                super::pyops::sha_hex(body.as_bytes()),
            )
        })
        .collect()
}

/// 控えの値(`gen_cases.py` の `cmd_norm_state` と同じ規則)。
fn norm_state(raw: &[u8], root: &str, shamap: &HashMap<String, String>) -> Value {
    let loaded = load_state(raw, PyCtx::new(0)).unwrap();
    let dict = loaded.as_dict().unwrap();
    let mut out = Map::new();
    for (key, value) in dict.iter() {
        let key = to_string(as_text(key).unwrap());
        if key != "entries" {
            out.insert(key, unroot(to_json(value), root));
        }
    }
    let mut entries = Map::new();
    let mut order = Vec::new();
    for (key, entry) in dict.get_str("entries").unwrap().as_dict().unwrap().iter() {
        let key = to_string(as_text(key).unwrap());
        if DROP_PREFIX.iter().any(|prefix| key.starts_with(prefix)) {
            continue;
        }
        if key.contains("external:/etc/") && !key.contains("external:/etc/claude-code") {
            continue;
        }
        let name = key.replace(root, "{r}");
        let mut value = unroot(mask_entry(to_json(entry)), root);
        if let Value::Array(items) = &mut value {
            for item in items {
                if let Some(mapped) = item.as_str().and_then(|text| shamap.get(text)) {
                    *item = Value::String(mapped.clone());
                }
            }
        }
        entries.insert(name.clone(), value);
        order.push(Value::String(name));
    }
    out.insert("entries".to_owned(), Value::Object(entries));
    out.insert("entry_order".to_owned(), Value::Array(order));
    Value::Object(out)
}

fn listing(directory: &Path) -> Vec<Value> {
    fn walk(base: &Path, rel: &Path, out: &mut Vec<Value>) {
        let mut names: Vec<String> = std::fs::read_dir(base)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        let mut dirs = Vec::new();
        for name in &names {
            let path = base.join(name);
            let meta = std::fs::symlink_metadata(&path).unwrap();
            let mode = meta.permissions().mode() & 0o7777;
            let rel_name = rel.join(name).to_str().unwrap().to_owned();
            if meta.is_dir() {
                out.push(json!([rel_name, "d", mode]));
                dirs.push(name.clone());
            } else {
                let sha = if rel_name == "environment.json" {
                    "state".to_owned()
                } else {
                    super::pyops::sha_hex(&std::fs::read(&path).unwrap())
                };
                out.push(json!([rel_name, "f", mode, sha]));
            }
        }
        for name in dirs {
            walk(&base.join(&name), &rel.join(&name), out);
        }
    }
    let mut out = Vec::new();
    walk(directory, Path::new(""), &mut out);
    out
}

/// 型の印つきの JSON の `bytes` を、長さと SHA-256 だけにする(`gen_cases.py` の `cmd_typed` と同じ形)。
fn digest_bytes(value: Value) -> Value {
    match value {
        Value::Object(mut map) => {
            if map.get("t") == Some(&json!("bytes")) && map.contains_key("v") {
                let hex = map.remove("v").unwrap();
                let hex = hex.as_str().unwrap();
                let raw: Vec<u8> = (0..hex.len())
                    .step_by(2)
                    .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                    .collect();
                map.insert("len".to_owned(), json!(raw.len()));
                map.insert("sha256".to_owned(), json!(super::pyops::sha_hex(&raw)));
                return Value::Object(map);
            }
            Value::Object(map.into_iter().map(|(k, v)| (k, digest_bytes(v))).collect())
        }
        Value::Array(items) => Value::Array(items.into_iter().map(digest_bytes).collect()),
        other => other,
    }
}

// ---------------------------------------------------------------- 流れの実行

fn replace_body(items: &[Value], root: &str) -> Vec<Value> {
    items
        .iter()
        .map(|item| {
            let mut item = item.clone();
            if let Some(body) = item.get("body").and_then(Value::as_str) {
                let body = body.replace("{r}", root);
                item["body"] = Value::String(body);
            }
            item
        })
        .collect()
}

fn install_recorder(dir: &Path, template: &str) -> String {
    let bin = dir.join("bin");
    std::fs::create_dir(&bin).unwrap();
    let text = template
        .replace("@PYTHON@", &find_in_path("python3"))
        .replace("@OUT@", dir.to_str().unwrap())
        .replace("@GIT@", &find_in_path("git"));
    let path = bin.join("git");
    std::fs::write(&path, text).unwrap();
    set_mode(&path, 0o755);
    bin.to_str().unwrap().to_owned()
}

fn run_ops(root: &Path, ops: &[Value]) {
    // `bytes`(16 進の生バイトで書く操作)だけ、ここで行う。ほかは観測の検査と同じ操作。
    let mut rest: Vec<Value> = Vec::new();
    for op in ops {
        if op["op"] == "deepdirs" {
            if !rest.is_empty() {
                apply_ops(root, &std::mem::take(&mut rest));
            }
            let path = root.join(op["path"].as_str().unwrap());
            std::fs::create_dir_all(&path).unwrap();
            // `create_dir_all` の mode は umask を受けるので、そろえる(深い木は固定期待に modes が出ない用途だけ)。
        } else if op["op"] == "bytes" {
            if !rest.is_empty() {
                apply_ops(root, &std::mem::take(&mut rest));
            }
            let hex = op["hex"].as_str().unwrap();
            let raw: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            std::thread::sleep(std::time::Duration::from_millis(30));
            std::fs::write(root.join(op["path"].as_str().unwrap()), raw).unwrap();
        } else {
            rest.push(op.clone());
        }
    }
    if !rest.is_empty() {
        apply_ops(root, &rest);
    }
}

/// 2 つの値の最初の違い(失敗の説明のため)。
fn first_diff(got: &Value, want: &Value, at: &str) -> Option<String> {
    match (got, want) {
        (Value::Object(a), Value::Object(b)) => {
            for key in a.keys().chain(b.keys()) {
                match (a.get(key), b.get(key)) {
                    (Some(x), Some(y)) => {
                        if let Some(found) = first_diff(x, y, &format!("{at}/{key}")) {
                            return Some(found);
                        }
                    }
                    (x, y) => return Some(format!("{at}/{key}: 実際 {x:?} / 期待 {y:?}")),
                }
            }
            None
        }
        (Value::Array(a), Value::Array(b)) => {
            for (i, (x, y)) in a.iter().zip(b).enumerate() {
                if let Some(found) = first_diff(x, y, &format!("{at}[{i}]")) {
                    return Some(found);
                }
            }
            (a.len() != b.len()).then(|| format!("{at}: 長さ {} / {}", a.len(), b.len()))
        }
        _ => (got != want).then(|| format!("{at}: 実際 {got} / 期待 {want}")),
    }
}

/// 控えを作り直す(`gen_cases.py` の `cmd_rewrite_state` と同じ手順。旧テスト `test_legacy_state_verify_retains_previous_contract`)。
/// `drop_components`・`user_directories`・`recompute`(`snapshot` で `entries` を作り直す)・`add_entries`。書いた生バイトの
/// SHA-256 を返す。
fn rewrite_state(host: &TestHost, root: &Path, name: &str, change: &Value) -> String {
    use super::inventory::Blobs;
    use super::pyjson::dump_state;
    use super::pyops::lit;
    use super::pyvalue::PyDict;
    use super::snapshot::snapshot;
    let path = root.join("out").join(name).join("environment.json");
    let ctx = PyCtx::new(0);
    let loaded = load_state(&std::fs::read(&path).unwrap(), ctx).unwrap();
    let mut value = loaded.as_dict().unwrap().clone();
    let mut specs = value.get_str("specs").unwrap().as_dict().unwrap().clone();
    let root_text = root.to_str().unwrap();
    if change.get("drop_components") == Some(&json!(true)) {
        let mut kept = PyDict::new();
        for (key, item) in specs.iter() {
            if as_text(key).unwrap() != lit("components") {
                kept.insert(key.clone(), item.clone()).unwrap();
            }
        }
        specs = kept;
    }
    if let Some(paths) = change.get("user_directories").and_then(Value::as_array) {
        let items = paths
            .iter()
            .map(|item| PyValue::from(item.as_str().unwrap().replace("{r}", root_text).as_str()))
            .collect();
        specs.insert_str("user_directories", PyValue::List(items));
    }
    if change.get("recompute") == Some(&json!(true)) {
        let inventory = value.get_str("inventory").unwrap().clone();
        let held_root = as_text(value.get_str("root").unwrap()).unwrap().to_vec();
        let mut blobs = Blobs::new();
        let entries = snapshot(
            host,
            &held_root,
            &mut specs,
            Some(&inventory),
            &mut blobs,
            None,
            10,
            ctx,
        )
        .unwrap();
        value.insert_str("entries", PyValue::Dict(entries));
    }
    if let Some(added) = change.get("add_entries").and_then(Value::as_object) {
        let mut entries = value.get_str("entries").unwrap().as_dict().unwrap().clone();
        for (key, held) in added {
            let items = held
                .as_array()
                .unwrap()
                .iter()
                .map(|item| PyValue::from(item.as_str().unwrap()))
                .collect();
            entries
                .insert(PyValue::from(key.as_str()), PyValue::List(items))
                .unwrap();
        }
        value.insert_str("entries", PyValue::Dict(entries));
    }
    value.insert_str("specs", PyValue::Dict(specs));
    let raw = dump_state(&PyValue::Dict(value), ctx).unwrap();
    std::fs::write(&path, &raw).unwrap();
    super::pyops::sha_hex(&raw)
}

/// 固定期待の 1 つの流れを、Rust で動かして比べる。
fn run_case(case: &Value, recorder: &str) {
    let name = case["name"].as_str().unwrap();
    let dir = Dir::new("cmd");
    let root_path = dir.0.join("case");
    std::fs::create_dir(&root_path).unwrap();
    set_mode(&root_path, 0o775);
    let root = root_path.to_str().unwrap().to_owned();
    build_layout(
        &root_path,
        &replace_body(case["layout"].as_array().unwrap(), &root),
    );
    let shamap = sha_map(case["layout"].as_array().unwrap(), &root);
    place_guard(&root_path);
    std::fs::create_dir(root_path.join("out")).unwrap();
    set_mode(&root_path.join("out"), 0o755);
    let mut path = std::env::var("PATH").unwrap();
    let recording = case.get("git_recorder") == Some(&json!(true));
    if recording {
        path = format!("{}:{path}", install_recorder(&dir.0, recorder));
    }
    let mut pairs: Vec<(String, String)> = vec![
        ("PATH".to_owned(), path),
        ("HOME".to_owned(), format!("{root}/home")),
        ("GIT_CONFIG_NOSYSTEM".to_owned(), "1".to_owned()),
        ("LANG".to_owned(), "C.UTF-8".to_owned()),
    ];
    if let Some(extra) = case.get("env").and_then(Value::as_object) {
        for (key, value) in extra {
            let value = sub(value.as_str().unwrap(), &root);
            match pairs.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = value,
                None => pairs.push((key.clone(), value)),
            }
        }
    }
    let host = TestHost::new(&pairs, &root_path);
    let calls_file = dir.0.join("calls.jsonl");
    let mut shas: Vec<(String, String)> = Vec::new();
    let results = case["results"].as_array().unwrap();
    let runs = case["runs"].as_array().unwrap();
    assert_eq!(runs.len(), results.len(), "{name}");
    for (index, (run, want)) in runs.iter().zip(results).enumerate() {
        let label = format!("{name} #{index}");
        // 控えの作り直しが先、入力の変更は後(変更を控えに含めないため)。
        if let Some(rewrite) = run.get("rewrite") {
            let state_name = rewrite["name"].as_str().unwrap();
            let sha = rewrite_state(&host, &root_path, state_name, &rewrite["change"]);
            shas.retain(|(held, _)| held != state_name);
            shas.push((state_name.to_owned(), sha));
            // 作り直した控えの中身も、旧版の作り直しと同じか。控えは鍵を並べ替えて書くので、挿入順は
            // 比べられない。entry_order は並べ替えた順の一致だけを見る(走査の順序の差は、Git の起動回数と
            // 先に出る停止の文面で見つける)。
            if let Some(want_state) = want.get("rewritten") {
                let raw = std::fs::read(
                    root_path
                        .join("out")
                        .join(state_name)
                        .join("environment.json"),
                )
                .unwrap();
                let got = norm_state(&raw, &root, &shamap);
                assert!(
                    got == *want_state,
                    "{label}: 作り直した控えの値: {:?}",
                    first_diff(&got, want_state, "")
                );
            }
        }
        if let Some(ops) = run.get("ops_before").and_then(Value::as_array) {
            run_ops(&root_path, ops);
        }
        let _ = std::fs::remove_file(&calls_file);
        let args: Vec<String> = run["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| sub(a.as_str().unwrap(), &root))
            .collect();
        let (code, out, err) = run_cli(&host, &args);
        assert_eq!(
            i64::from(code),
            want["code"].as_i64().unwrap(),
            "{label}: 終了コード"
        );
        let stdout = String::from_utf8_lossy(&out).replace(&root, "{r}");
        assert_eq!(
            mask_sha(&stdout),
            want["stdout"].as_str().unwrap(),
            "{label}: 標準出力"
        );
        let stderr = String::from_utf8_lossy(&err).replace(&root, "{r}");
        assert_eq!(
            stderr,
            want["stderr"].as_str().unwrap(),
            "{label}: 標準エラー"
        );
        if let Some(calls) = want.get("git_calls") {
            let count = std::fs::read_to_string(&calls_file).map_or(0, |text| text.lines().count());
            assert_eq!(json!(count), *calls, "{label}: Git の起動回数");
        }
        let brief = case.get("brief") == Some(&json!(true));
        if let Some(position) = args.iter().position(|a| a == "--output").filter(|_| !brief) {
            let output = PathBuf::from(&args[position + 1]);
            if output.is_dir() {
                assert_eq!(
                    Value::Array(listing(&output)),
                    want["files"],
                    "{label}: 出力フォルダの中身"
                );
                // 出力フォルダ自身の mode(`mkdir(0o700)` は umask を受ける。022 でも 002 でも 0o700)。
                assert_eq!(
                    json!(
                        std::fs::symlink_metadata(&output)
                            .unwrap()
                            .permissions()
                            .mode()
                            & 0o7777
                    ),
                    want["root_mode"],
                    "{label}: 出力フォルダ自身の mode"
                );
                let state = output.join("environment.json");
                if state.exists() {
                    let raw = std::fs::read(&state).unwrap();
                    let got = norm_state(&raw, &root, &shamap);
                    assert!(
                        got == want["state"],
                        "{label}: 控えの値: {:?}",
                        first_diff(&got, &want["state"], "")
                    );
                    let sha = super::pyops::sha_hex(&raw);
                    let matches = stdout.contains(&format!("\"sha256\": \"{sha}\""));
                    assert_eq!(
                        json!(matches),
                        want["receipt_sha_matches"],
                        "{label}: 受領の sha256"
                    );
                    if matches {
                        let held = output.file_name().unwrap().to_str().unwrap().to_owned();
                        shas.retain(|(name, _)| *name != held);
                        shas.push((held, sha));
                    }
                } else {
                    assert!(want.get("state").is_none(), "{label}");
                }
            } else {
                assert!(want.get("files").is_none(), "{label}");
            }
        }
    }
    // 内部 reader 3 つ(最後の入力の状態で)。深い入れ子の設定は、型の印つきの JSON が `serde_json` の深さの上限
    // (128)を超えるので、流れの側で外してある(`no_readers`)。
    if case.get("no_readers") == Some(&json!(true)) || case.get("brief") == Some(&json!(true)) {
        return;
    }
    let readers = case["readers"].as_object().unwrap();
    assert_eq!(
        readers.keys().collect::<Vec<_>>(),
        {
            let mut names: Vec<&String> = shas.iter().map(|(n, _)| n).collect();
            names.sort();
            names.dedup();
            names
        },
        "{name}: reader の対象"
    );
    for (state_name, sha) in &shas {
        let state = decode_fs(
            root_path
                .join("out")
                .join(state_name)
                .join("environment.json")
                .as_os_str()
                .as_bytes(),
        );
        let sha_cps = decode_fs(sha.as_bytes());
        let ctx = PyCtx::new(2);
        let wants = &readers[state_name];
        let results: [(&str, Result<PyValue, PyException>); 3] = [
            (
                "host_settings",
                read_host_settings(&host, &state, &sha_cps, ctx).map(|mut result| {
                    mask_sources(&mut result, &root_path, state_name);
                    PyValue::Dict(result)
                }),
            ),
            (
                "components",
                read_components(&host, &state, &sha_cps, ctx).map(PyValue::List),
            ),
            (
                "digest",
                component_policy_digest(&host, &state, &sha_cps, ctx).map(|digest| {
                    assert!(
                        digest.len() == 64
                            && digest
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    );
                    PyValue::from("*")
                }),
            ),
        ];
        for (label, result) in results {
            let text = match result {
                Ok(value) => format!("{{\"ok\":{}}}", typed_json(&value)),
                Err(error) => error_json(&error),
            };
            let got: Value = serde_json::from_str(&text.replace(&root, "{r}")).unwrap();
            assert_eq!(
                digest_bytes(got),
                wants[label],
                "{name} reader {state_name} {label}"
            );
        }
    }
}

/// `permission_sources` の `source` は `sha256(dump([kind, path]))`。同じ式で確かめてから `*` にする
/// (一時フォルダの絶対パスから決まる値は、実行ごとに変わる)。
fn mask_sources(result: &mut super::pyvalue::PyDict, root: &Path, state_name: &str) {
    let loaded = load_state(
        &std::fs::read(root.join("out").join(state_name).join("environment.json")).unwrap(),
        PyCtx::new(0),
    )
    .unwrap();
    let desc: Vec<Cps> = loaded
        .as_dict()
        .unwrap()
        .get_str("host_settings")
        .unwrap()
        .as_list()
        .unwrap()
        .iter()
        .map(|item| {
            as_text(item.as_dict().unwrap().get_str("path").unwrap())
                .unwrap()
                .to_vec()
        })
        .collect();
    let Some(PyValue::List(items)) = result.get_str("permission_sources").cloned() else {
        panic!("permission_sources が無い");
    };
    let mut masked = Vec::new();
    for (item, path) in items.into_iter().zip(desc) {
        let mut item = item.as_dict().unwrap().clone();
        let kind = as_text(item.get_str("kind").unwrap()).unwrap().to_vec();
        let preimage = PyValue::List(vec![super::pyops::text(kind), super::pyops::text(path)]);
        let raw = super::pyjson::dump_state(&preimage, PyCtx::new(0)).unwrap();
        let want = super::pyops::sha_hex(&raw);
        assert_eq!(
            to_string(as_text(item.get_str("source").unwrap()).unwrap()),
            want
        );
        item.insert_str("source", PyValue::from("*"));
        masked.push(PyValue::Dict(item));
    }
    result.insert_str("permission_sources", PyValue::List(masked));
}

fn commands() -> Value {
    fixture!("command_cases.json")
}

fn entry_depths() -> Value {
    fixture!("entry_depth_cases.json")
}

fn run_group(load: fn() -> Value, keep: fn(&str) -> bool, at_least: usize) {
    with_big_stack(move || {
        let cases = load();
        let recorder = fixture!("git_cases.json")["recorder"]
            .as_str()
            .unwrap()
            .to_owned();
        let mut count = 0;
        for case in cases["cases"].as_array().unwrap() {
            if keep(case["name"].as_str().unwrap()) {
                run_case(case, &recorder);
                count += 1;
            }
        }
        assert!(count >= at_least, "{count}");
    });
}

/// 流れの名前から、検査(テスト)の組を決める。どれにも入らない流れがあれば、`the_fixture_has_the_expected_number_of_cases` が気づく。
fn group_of(name: &str) -> &'static str {
    if matches!(
        name,
        "basic" | "options" | "bootstrap-refusals" | "settings-link"
    ) {
        "flows"
    } else if name.starts_with("verify-") {
        "verify"
    } else if name.starts_with("legacy-")
        || name.starts_with("reader-")
        || name.starts_with("userdir-")
        || name == "options-readers"
    {
        "rewritten"
    } else if name.starts_with("host-") {
        "host"
    } else if name.starts_with("include-") || name.starts_with("git-") || name.starts_with("order-")
    {
        "include"
    } else {
        "other"
    }
}

#[cfg(target_os = "linux")]
#[test]
fn bootstrap_and_verify_flows_match_the_old_python_3_10() {
    run_group(commands, |name| group_of(name) == "flows", 4);
}

#[cfg(target_os = "linux")]
#[test]
fn verify_refusals_match_the_old_python_3_10() {
    run_group(commands, |name| group_of(name) == "verify", 28);
}

#[cfg(target_os = "linux")]
#[test]
fn host_settings_and_allowlist_match_the_old_python_3_10() {
    run_group(commands, |name| group_of(name) == "host", 30);
}

/// 控えを作り直した流れ(旧形式の控え・記述子の外の部品の鍵・`marketplace-manifest` の部品・`user_directories` の導入リンク)。
#[cfg(target_os = "linux")]
#[test]
fn rewritten_state_flows_match_the_old_python_3_10() {
    run_group(commands, |name| group_of(name) == "rewritten", 11);
}

#[cfg(target_os = "linux")]
#[test]
fn include_wording_stop_order_and_observation_order_match_the_old_python_3_10() {
    run_group(commands, |name| group_of(name) == "include", 13);
}

/// 各コマンドの入口の再帰の深さ(U5)。深いディレクトリだけの木で、旧版が通る最後の段数の前後を動かす。
/// 旧版は 990 段まで全部通り、991 段は `host-allowlist` だけ落ち、992 段は全部落ちる。
#[cfg(target_os = "linux")]
#[test]
fn entry_recursion_depths_match_the_old_python_3_10() {
    run_group(entry_depths, |_| true, 4);
}

/// 固定期待の全体の件数(流れと 1 回の実行)。減らしたら気づけるように固定する。
#[test]
fn the_fixture_has_the_expected_number_of_cases() {
    let cases = fixture!("command_cases.json");
    let list = cases["cases"].as_array().unwrap();
    let runs: usize = list
        .iter()
        .map(|c| c["results"].as_array().unwrap().len())
        .sum();
    assert_eq!((list.len(), runs), (EXPECTED_CASES, EXPECTED_RUNS));
    let strays: Vec<&str> = list
        .iter()
        .map(|c| c["name"].as_str().unwrap())
        .filter(|name| group_of(name) == "other")
        .collect();
    assert!(
        strays.is_empty(),
        "どの検査の組にも入らない流れ: {strays:?}"
    );
}

const EXPECTED_CASES: usize = 100;
const EXPECTED_RUNS: usize = 413;

/// Mac では、既定の監視先の `/etc` がリンクなので、旧版と同じく `bootstrap` が止まる(設計書の「比較と検査の設計」。
/// Mac 実機での確認は未実施。型名は OS の errno で変わるので、終了コードと標準出力が空であることだけを見る)。
#[cfg(target_os = "macos")]
#[test]
fn bootstrap_with_the_default_etc_paths_stops_on_mac() {
    let dir = Dir::new("cmd-mac");
    let root = dir.0.join("case");
    std::fs::create_dir(&root).unwrap();
    build_layout(
        &root,
        &[json!({"path": "plugin/README.md", "kind": "file", "body": "x"})],
    );
    place_guard(&root);
    let pairs = vec![
        ("PATH".to_owned(), std::env::var("PATH").unwrap()),
        ("HOME".to_owned(), format!("{}/home", root.display())),
        ("GIT_CONFIG_NOSYSTEM".to_owned(), "1".to_owned()),
    ];
    let host = TestHost::new(&pairs, &root);
    let args = vec![
        "bootstrap".to_owned(),
        "--root".to_owned(),
        format!("{}/plugin", root.display()),
        "--output".to_owned(),
        format!("{}/out", root.display()),
    ];
    let (code, out, err) = run_cli(&host, &args);
    assert_eq!(code, 20);
    assert!(out.is_empty());
    assert!(String::from_utf8_lossy(&err).starts_with("ERROR [environment-guard] "));
}

// ---------------------------------------------------------------- Rust だけの検査(U2 など)

// U2 の差: 旧版はこの場合、自分の `__file__` をコピーして成功する。固定期待は旧版の値ではなく設計の値(終了 20・出力なし)。
#[cfg(target_os = "linux")]
#[test]
fn bootstrap_without_a_guard_in_the_distribution_stops_before_creating_the_output() {
    let dir = Dir::new("cmd-noguard");
    let root = dir.0.join("case");
    std::fs::create_dir(&root).unwrap();
    set_mode(&root, 0o775);
    let root_text = root.to_str().unwrap().to_owned();
    build_layout(
        &root,
        &[
            json!({"path": "plugin/README.md", "kind": "file", "body": "x"}),
            json!({"path": "home/.claude/settings.json", "kind": "file", "body": "{}"}),
        ],
    );
    let pairs = vec![
        ("PATH".to_owned(), std::env::var("PATH").unwrap()),
        ("HOME".to_owned(), format!("{root_text}/home")),
        ("GIT_CONFIG_NOSYSTEM".to_owned(), "1".to_owned()),
    ];
    let host = TestHost::new(&pairs, &root);
    let args = vec![
        "bootstrap".to_owned(),
        "--root".to_owned(),
        format!("{root_text}/plugin"),
        "--output".to_owned(),
        format!("{root_text}/out"),
    ];
    let (code, out, err) = run_cli(&host, &args);
    assert_eq!(code, 20);
    assert!(out.is_empty());
    assert_eq!(
        String::from_utf8_lossy(&err),
        "ERROR [environment-guard] guard の配布元が配布物に無い\n"
    );
    // 出力フォルダもコピーも作らない(観測の直後に止まる)。
    assert!(std::fs::symlink_metadata(root.join("out")).is_err());
}

// ---------------------------------------------------------------- 細かい競合(`cfg(test)` のフック)

#[cfg(target_os = "linux")]
mod races {
    use super::super::fsio::hooks::{Point, with};
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    /// 配布物(guard つき)と利用者設定だけの最小の入力。
    fn small(label: &str) -> (Dir, PathBuf, TestHost) {
        let dir = Dir::new(label);
        let root = dir.0.join("case");
        std::fs::create_dir(&root).unwrap();
        set_mode(&root, 0o775);
        build_layout(
            &root,
            &[
                json!({"path": "plugin/README.md", "kind": "file", "body": "x"}),
                json!({"path": "home/.claude/settings.json", "kind": "file", "body": "{}"}),
            ],
        );
        place_guard(&root);
        let root_text = root.to_str().unwrap().to_owned();
        let pairs = vec![
            ("PATH".to_owned(), std::env::var("PATH").unwrap()),
            ("HOME".to_owned(), format!("{root_text}/home")),
            ("GIT_CONFIG_NOSYSTEM".to_owned(), "1".to_owned()),
            ("LANG".to_owned(), "C.UTF-8".to_owned()),
        ];
        let host = TestHost::new(&pairs, &root);
        (dir, root, host)
    }

    fn bootstrap_args(root: &Path) -> Vec<String> {
        vec![
            "bootstrap".to_owned(),
            "--root".to_owned(),
            format!("{}/plugin", root.display()),
            "--output".to_owned(),
            format!("{}/out", root.display()),
        ]
    }

    fn stderr_of(err: &[u8]) -> String {
        String::from_utf8_lossy(err).into_owned()
    }

    /// 位置 `name` の 1 回目に `action` を行うフックで、`bootstrap` を流す。
    fn bootstrap_with(
        name: &'static str,
        root: &Path,
        host: &TestHost,
        action: impl Fn() + 'static,
    ) -> (i32, Vec<u8>, Vec<u8>) {
        let fired = Rc::new(Cell::new(false));
        let seen = Rc::clone(&fired);
        let result = with(
            move |point| {
                if matches!(point, Point::Site(site) if *site == name) && !seen.get() {
                    seen.set(true);
                    std::thread::sleep(std::time::Duration::from_millis(30));
                    action();
                }
                None
            },
            || run_cli(host, &bootstrap_args(root)),
        );
        assert!(fired.get(), "{name} のフックが呼ばれた");
        result
    }

    #[test]
    fn an_input_changed_between_the_observation_and_the_check_is_refused() {
        let (_dir, root, host) = small("race-boot-input");
        let settings = root.join("home/.claude/settings.json");
        let (code, out, err) = bootstrap_with("bootstrap-before-verify", &root, &host, move || {
            std::fs::write(&settings, "{\"a\":1}").unwrap();
        });
        assert_eq!(code, 20);
        assert!(out.is_empty(), "受領は出さない");
        assert_eq!(
            stderr_of(&err),
            "ERROR [environment-guard] 配布物または利用者環境が変わった\n"
        );
        // 出力(控えまで)は作ってある(旧版と同じ。既存の出力は開かない)。
        assert!(root.join("out/environment.json").exists());
    }

    #[test]
    fn the_copy_changed_between_the_write_and_the_check_is_refused() {
        let (_dir, root, host) = small("race-boot-copy");
        let copy = root.join("out/plugin/README.md");
        let (code, out, err) = bootstrap_with("bootstrap-before-verify", &root, &host, move || {
            std::fs::write(&copy, "tampered").unwrap();
        });
        assert_eq!(code, 20);
        assert!(out.is_empty());
        assert_eq!(
            stderr_of(&err),
            "ERROR [environment-guard] 信頼コピーが変わった\n"
        );
    }

    #[test]
    fn the_state_changed_between_the_write_and_the_check_is_refused() {
        let (_dir, root, host) = small("race-boot-state");
        let state = root.join("out/environment.json");
        let (code, _out, err) =
            bootstrap_with("bootstrap-before-verify", &root, &host, move || {
                std::fs::write(&state, "{}").unwrap();
            });
        assert_eq!(code, 20);
        assert_eq!(
            stderr_of(&err),
            "ERROR [environment-guard] 環境の控えが保持値と一致しない\n"
        );
    }

    /// 出力の中の項目(フォルダ・ファイル)の集合(`out` からの相対パス)。
    fn created(out: &Path) -> std::collections::BTreeSet<String> {
        fn walk(base: &Path, rel: &str, set: &mut std::collections::BTreeSet<String>) {
            for entry in std::fs::read_dir(base).unwrap() {
                let entry = entry.unwrap();
                let name = entry.file_name().into_string().unwrap();
                let rel_name = if rel.is_empty() {
                    name.clone()
                } else {
                    format!("{rel}/{name}")
                };
                set.insert(rel_name.clone());
                if entry.file_type().unwrap().is_dir() {
                    walk(&entry.path(), &rel_name, set);
                }
            }
        }
        let mut set = std::collections::BTreeSet::new();
        if out.is_dir() {
            walk(out, "", &mut set);
        }
        set
    }

    #[test]
    fn the_output_is_created_in_the_order_of_the_entries_one_file_at_a_time() {
        // 設計書「観測の順序」9: 控えの `entries` の挿入順に 1 回だけ回り、フォルダはその項目の所で作る(フォルダとファイルは
        // 交互に並ぶ)。そのあとで guard のコピー、控えの順。ファイルを作るたびに、それまでに出力にある項目の集合を取る。
        let (_dir, root, host) = small("race-boot-order");
        build_layout(
            &root,
            &[
                json!({"path": "plugin/a/z.txt", "kind": "file", "body": "z"}),
                json!({"path": "plugin/a/b/y.txt", "kind": "file", "body": "y"}),
                json!({"path": "plugin/c.txt", "kind": "file", "body": "c"}),
                json!({"path": "plugin/m/n/o.txt", "kind": "file", "body": "o"}),
            ],
        );
        let out = root.join("out");
        let snapshots = Rc::new(std::cell::RefCell::new(Vec::new()));
        let seen = Rc::clone(&snapshots);
        let watch = out.clone();
        let (code, _stdout, _err) = with(
            move |point| {
                if matches!(point, Point::AfterCreate) {
                    seen.borrow_mut().push(created(&watch));
                }
                None
            },
            || run_cli(&host, &bootstrap_args(&root)),
        );
        assert_eq!(code, 0);
        let raw = std::fs::read(out.join("environment.json")).unwrap();
        let state = load_state(&raw, PyCtx::new(0)).unwrap();
        let keys: Vec<String> = state
            .as_dict()
            .unwrap()
            .get_str("entries")
            .unwrap()
            .as_dict()
            .unwrap()
            .keys()
            .map(|key| to_string(as_text(key).unwrap()))
            .filter(|key| key == "plugin" || key.starts_with("plugin/"))
            .collect();
        let kinds: Vec<bool> = keys.iter().map(|key| out.join(key).is_dir()).collect();
        let mut expected: Vec<std::collections::BTreeSet<String>> = Vec::new();
        let mut so_far = std::collections::BTreeSet::new();
        for (key, is_dir) in keys.iter().zip(&kinds) {
            so_far.insert(key.clone());
            if !is_dir {
                expected.push(so_far.clone());
            }
        }
        let mut with_guard = so_far.clone();
        with_guard.insert("environment-guard.py".to_owned());
        expected.push(with_guard.clone());
        with_guard.insert("environment.json".to_owned());
        expected.push(with_guard);
        // 最後(控え)の作成の直後は、`environment.json` を含む。
        assert_eq!(*snapshots.borrow(), expected);
        // 交互: 最初のフォルダの次に、そのフォルダの中のファイルより前に別のフォルダの項目が来る入力を含む。
        assert!(
            keys.iter().zip(&kinds).any(|(_, is_dir)| !is_dir)
                && kinds.iter().filter(|d| **d).count() >= 4
        );
    }

    #[test]
    fn the_guard_copy_keeps_the_bytes_of_the_same_observation() {
        // 観測の後で配布物の guard を書き換えても、控えるのは観測したバイト列(U2)。照合は配布物の変化で止まる。
        let (_dir, root, host) = small("race-boot-guard");
        let guard = root.join(GUARD_REL);
        let (code, out, err) =
            bootstrap_with("bootstrap-after-snapshot", &root, &host, move || {
                std::fs::write(&guard, "changed after the observation").unwrap();
            });
        assert_eq!(code, 20);
        assert!(out.is_empty());
        assert_eq!(
            stderr_of(&err),
            "ERROR [environment-guard] component の実体が開始時から変わった\n"
        );
        assert_eq!(
            std::fs::read(root.join("out/environment-guard.py")).unwrap(),
            GUARD_BYTES
        );
    }
}

#[test]
fn include_text_is_matched_by_bytes_ignoring_ascii_case() {
    use super::snapshot::mentions_include;
    assert!(mentions_include(b"[include]\npath = x\n"));
    assert!(mentions_include(b"[INCLUDE]"));
    assert!(mentions_include(b"[includeIf \"gitdir:/x/\"]"));
    assert!(mentions_include(b"# IncLuDe"));
    assert!(mentions_include(b"xxinclude"));
    assert!(!mentions_include(b""));
    assert!(!mentions_include(b"[user]\nname = a\n"));
    assert!(!mentions_include(b"includ"));
    assert!(!mentions_include(b"inc lude"));
    // ASCII だけを小文字にする(`bytes.lower`): 全角の `ｉｎｃｌｕｄｅ` や、U+0130(İ)は一致しない。
    assert!(!mentions_include("ｉｎｃｌｕｄｅ".as_bytes()));
    assert!(!mentions_include("\u{130}nclude".as_bytes()));
}

#[test]
fn typed_json_keeps_types_order_and_escapes() {
    use super::pyops::{dict, text_of};
    let value = PyValue::Dict(dict(vec![
        ("s", text_of("a\"b\\c\n\u{e9}\u{1f600}")),
        ("n", PyValue::None),
        ("b", PyValue::Bool(true)),
        ("i", PyValue::from(7)),
        ("f", PyValue::Float(1.5)),
        ("y", PyValue::Bytes(vec![0, 255])),
        ("l", PyValue::List(vec![PyValue::from(false)])),
    ]));
    let got: Value = serde_json::from_str(&typed_json(&value)).unwrap();
    assert_eq!(
        got,
        json!({"t": "dict", "v": [
            [{"t": "str", "v": "s"}, {"t": "str", "v": "a\"b\\c\n\u{e9}\u{1f600}"}],
            [{"t": "str", "v": "n"}, {"t": "none"}],
            [{"t": "str", "v": "b"}, {"t": "bool", "v": true}],
            [{"t": "str", "v": "i"}, {"t": "int", "v": "7"}],
            [{"t": "str", "v": "f"}, {"t": "float", "v": "1.5"}],
            [{"t": "str", "v": "y"}, {"t": "bytes", "v": "00ff"}],
            [{"t": "str", "v": "l"}, {"t": "list", "v": [{"t": "bool", "v": false}]}],
        ]})
    );
    // `json.dumps(ensure_ascii=True)` と同じ字面(非 ASCII は `\uXXXX`、BMP の外は代用対)。
    assert!(typed_json(&text_of("\u{e9}\u{1f600}")).contains("\"\\u00e9\\ud83d\\ude00\""));
}

#[test]
fn a_legacy_state_observes_the_distribution_root_as_a_path() {
    // 控えに `components` が無い旧形式は、配布物の根を `tree(root, 'plugin', …)` で読む。旧版の `snapshot` は最初に
    // `root = absolute(root)` を通すので、`bootstrap` でも `verify` でも `tree` が受け取るのは必ず `Path`。
    // 旧 Python 3.10.12 で、`snapshot` を深さ 988〜995 に置いて測ると、根を文字列でも `Path` でも結果は同じで、
    // 深さ 992 ではどちらも無印の `maximum recursion depth exceeded`(2b-1c の 1 巡目の直し 3)。
    use super::pyops::lit;
    use super::pyvalue::PyDict;
    use super::snapshot::snapshot;
    let mut specs = PyDict::new();
    for key in ["files", "configs", "directories"] {
        specs.insert_str(key, PyValue::List(vec![]));
    }
    let host = TestHost::new(&[("HOME".to_owned(), "/".to_owned())], Path::new("/"));
    let mut blobs = super::inventory::Blobs::new();
    let error = snapshot(
        &host,
        &lit("/nonexistent-s2e-root"),
        &mut specs,
        None,
        &mut blobs,
        None,
        992,
        PyCtx::new(0),
    )
    .unwrap_err();
    assert_eq!(
        to_string(error.message.as_codepoints()),
        "maximum recursion depth exceeded"
    );
}

#[test]
fn the_process_host_reads_the_user_database() {
    // U6: Linux の musl は `/etc/passwd` だけ、Mac は旧版と同じ仕組みで引く。`root` は両方にあり、無い名前は引けない。
    let users = ProcessHost.users();
    let root = users
        .home_of_name(b"root")
        .unwrap()
        .expect("root の利用者情報");
    assert!(root.starts_with(b"/"), "{root:?}");
    assert_eq!(users.home_of_name(b"s2e-no-such-user-zz").unwrap(), None);
    // 名前に NUL があれば、引けない(`pwd.getpwnam` が `ValueError` にする所は、呼出し側の `expanduser` が先に止める)。
    assert_eq!(users.home_of_name(b"ro\0ot").unwrap(), None);
}
