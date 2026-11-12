//! 木の走査(`fsio::tree`)・部品の木(`component::component_tree`)・記録(`record::record`)の固定期待の検査。
//! 固定期待(`native/ci/s2e_fixtures/observe_cases.json`)は、旧 Python 3.10 の関数を同じ配置の一時フォルダで
//! 動かした結果で、`gen_cases.py` の area `observe` が作る。dev・ino・時刻は `*` に置き換え、一時フォルダの
//! 絶対パスは `{r}` に置き換えてある(`obs_plain` と同じ規則を、ここの `plain_entries` が持つ)。
use super::budget::EnvBudget;
use super::component::component_tree;
use super::dispatch::ProcessHost;
use super::entry;
use super::error::PyException;
use super::fsio::{Obs, TreeOptions, tree};
use super::inventory::Blobs;
use super::pyops::{Cps, as_text, text};
use super::pystr::{cps, decode_fs, to_string};
use super::pyvalue::{PyDict, PyValue};
use super::record::{RecordOptions, record};
use super::testing::fixture;
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::time::Duration;

const MAX_FILE: u64 = 8 * 1024 * 1024;

/// テストの一時フォルダ。作った正確なパスだけを、終わりに消す。
pub struct Dir(pub PathBuf);

impl Dir {
    pub fn new(label: &str) -> Self {
        // 深い木の走査は、親の fd を開いたまま子を開く(約 1000 本)。Mac の既定の上限(256)では足りないので上げる。
        dev_workflow_platform::environment_guard::raise_open_file_limit(4096)
            .expect("fd の上限を上げられる");
        let base = std::env::var_os("S2E_TEST_TMP")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        // 親をリンクを辿らずに開くので、実パスで使う(Mac の `/var` はリンク)。
        let base = std::fs::canonicalize(base).unwrap();
        for count in 0u32.. {
            let path = base.join(format!("s2e-obs-{}-{label}-{count}", std::process::id()));
            match std::fs::create_dir(&path) {
                Ok(()) => {
                    // 旧版の固定期待を作った側は `mkdtemp`(0o700)。umask で mode が変わらないよう、そろえる。
                    set_mode(&path, 0o700);
                    return Self(path);
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("{}: {error}", path.display()),
            }
        }
        unreachable!()
    }

    pub fn cps(&self, rel: &str) -> Cps {
        decode_fs(self.0.join(rel).as_os_str().as_bytes())
    }
}

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn str_of<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key]
        .as_str()
        .unwrap_or_else(|| panic!("{key} が無い"))
}

fn mode_of(item: &Value, default: u32) -> u32 {
    item.get("mode").map_or(default, |mode| {
        u32::try_from(mode.as_u64().unwrap()).unwrap()
    })
}

fn set_mode(path: &Path, mode: u32) {
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
}

/// mode を指定しない新しいファイル・FIFO の mode。umask で変わらないよう明示する(固定期待は umask 002 の機械で作った値)。
const NEW_MODE: u32 = 0o664;

/// 配置(layout)を作る。暗黙の親ディレクトリは 0o755。
pub fn build_layout(root: &Path, items: &[Value]) {
    let mut dirs: std::collections::BTreeMap<PathBuf, u32> = std::collections::BTreeMap::new();
    let sub = |text: &str| text.replace("{r}", root.to_str().unwrap());
    for item in items {
        let path = root.join(str_of(item, "path"));
        let parent = path.parent().unwrap();
        let mut missing: Vec<&Path> = Vec::new();
        let mut walk = parent;
        while !walk.is_dir() {
            missing.push(walk);
            walk = walk.parent().unwrap();
        }
        std::fs::create_dir_all(parent).unwrap();
        for each in missing {
            dirs.entry(each.to_path_buf()).or_insert(0o755);
        }
        match str_of(item, "kind") {
            "dir" => {
                std::fs::create_dir_all(&path).unwrap();
                dirs.insert(path, mode_of(item, 0o755));
            }
            "file" => {
                let data = match item.get("body") {
                    Some(body) => body.as_str().unwrap().as_bytes().to_vec(),
                    None => vec![b'x'; usize::try_from(item["size"].as_u64().unwrap()).unwrap()],
                };
                std::fs::write(&path, data).unwrap();
                set_mode(&path, mode_of(item, 0o644));
            }
            "link" => symlink(sub(str_of(item, "target")), &path).unwrap(),
            "hardlink" => std::fs::hard_link(root.join(str_of(item, "target")), &path).unwrap(),
            "fifo" => make_fifo(&path),
            other => panic!("配置の種類が不明: {other}"),
        }
    }
    // 浅い親から順に mode を決める。
    for (path, mode) in dirs {
        set_mode(&path, mode);
    }
}

/// FIFO を作る(`mkfifo`)。標準ライブラリに無いので、コマンドを使う。
pub fn make_fifo(path: &Path) {
    let status = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .expect("mkfifo を起動できない");
    assert!(status.success(), "mkfifo が失敗した");
    // umask で変わらないよう、mode を決める(固定期待を作った側と同じ 0o664)。
    set_mode(path, NEW_MODE);
}

/// 変更の操作。内容を変えても時刻が同じにならないよう、カーネルの時計が進むのを待つ。
pub fn apply_ops(root: &Path, ops: &[Value]) {
    std::thread::sleep(Duration::from_millis(30));
    let sub = |text: &str| text.replace("{r}", root.to_str().unwrap());
    for op in ops {
        let path = op.get("path").map(|p| root.join(p.as_str().unwrap()));
        match str_of(op, "op") {
            "write" => {
                let path = path.unwrap();
                let created = path.symlink_metadata().is_err();
                std::fs::write(&path, str_of(op, "body").as_bytes()).unwrap();
                if op.get("mode").is_some() {
                    set_mode(&path, mode_of(op, 0o644));
                } else if created {
                    set_mode(&path, NEW_MODE);
                }
            }
            "rm" => std::fs::remove_file(path.unwrap()).unwrap(),
            "rmtree" => std::fs::remove_dir_all(path.unwrap()).unwrap(),
            "mkdir" => {
                let path = path.unwrap();
                std::fs::create_dir_all(&path).unwrap();
                set_mode(&path, mode_of(op, 0o755));
            }
            "link" => {
                let path = path.unwrap();
                if path.symlink_metadata().is_ok() {
                    std::fs::remove_file(&path).unwrap();
                }
                symlink(sub(str_of(op, "target")), &path).unwrap();
            }
            "rename" => std::fs::rename(path.unwrap(), root.join(str_of(op, "to"))).unwrap(),
            "chmod" => set_mode(&path.unwrap(), mode_of(op, 0o644)),
            "fifo" => make_fifo(&path.unwrap()),
            other => panic!("操作の種類が不明: {other}"),
        }
    }
}

// ---------------------------------------------------------------- 結果の JSON

fn plain_value(value: &PyValue, root: &str) -> Value {
    match value {
        PyValue::Str(s) => Value::String(to_string(s.as_codepoints()).replace(root, "{r}")),
        PyValue::Int(int) => match int.to_i64() {
            Some(number) => json!(number),
            None => Value::String(int.to_decimal(0).unwrap()),
        },
        PyValue::List(items) => Value::Array(items.iter().map(|v| plain_value(v, root)).collect()),
        other => panic!("控えに入らない値: {other:?}"),
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

/// `root_key` は部品の木の根の鍵(`component:<種別>:<パス>`)。その足取りだけ、親の数を引く。
pub fn plain_entries(
    entries: &PyDict,
    root: &str,
    ancestors: usize,
    drop_routes: bool,
    root_key: Option<&str>,
) -> Value {
    let mut out = Map::new();
    for (key, value) in entries.iter() {
        let key = to_string(as_text(key).unwrap());
        if drop_routes
            && ["@route:", "@absolute:", "@route-end:", "@origin:"]
                .iter()
                .any(|prefix| key.starts_with(prefix))
        {
            continue;
        }
        let mut shown = plain_value(value, root);
        let mut name = key.clone();
        if key.starts_with("@origin:") {
            shown = json!([shown[0], "*", "*", "*"]);
        } else if let Some(rest) = key.strip_prefix("@route-end:") {
            if Some(rest) == root_key {
                let index = shown[1].as_i64().unwrap() - i64::try_from(ancestors).unwrap();
                shown = json!([shown[0], index]);
            }
        } else if key.starts_with("@route:") || key.starts_with("@absolute:@route:") {
            let (head, number) = key.rsplit_once(':').unwrap();
            let prefix = if key.starts_with("@route:") {
                "@route:"
            } else {
                "@absolute:@route:"
            };
            if Some(head)
                == root_key
                    .map(|root_key| format!("{prefix}{root_key}"))
                    .as_deref()
            {
                let number: usize = number.parse().unwrap();
                if number < ancestors {
                    continue;
                }
                name = format!("{head}:{}", number - ancestors);
            }
            if let Some(items) = shown.as_array_mut() {
                for position in masked_positions(&items.clone()) {
                    items[*position] = json!("*");
                }
            }
        } else if let Some(items) = shown.as_array_mut() {
            let positions = masked_positions(&items.clone());
            for position in positions {
                items[*position] = json!("*");
            }
        }
        out.insert(name.replace(root, "{r}"), shown);
    }
    Value::Object(out)
}

/// 部品の木の呼出しが、一時フォルダの深さのせいで余計に使った予算の項目数(`gen_cases.py` の
/// `obs_depth_ticks` と同じ)。根の足取り `@route:<根>:<n>`(n < ancestors)が 1 つにつき 1 回、
/// `{r}/…` を指す絶対パスの導入リンクが辿る空の要素 1 つと親の要素 `ancestors` 個で 1 + ancestors 回。
/// 足取りを捨てる回(drop_routes)も、捨てる前に数える。
fn depth_ticks(entries: &PyDict, root: &str, ancestors: usize, root_key: Option<&str>) -> usize {
    let Some(root_key) = root_key else {
        return 0;
    };
    let route_head = format!("@route:{root_key}");
    let mut total = 0;
    for (key, value) in entries.iter() {
        let key = to_string(as_text(key).unwrap());
        if let Some((head, number)) = key.rsplit_once(':')
            && key.starts_with("@route:")
            && head == route_head
            && number.parse::<usize>().unwrap() < ancestors
        {
            total += 1;
        }
        if let PyValue::List(items) = value
            && let Some(PyValue::Str(first)) = items.first()
            && to_string(first.as_codepoints()) == "link-step"
            && let Some(PyValue::Str(text)) = items.get(2)
        {
            let text = to_string(text.as_codepoints());
            if text == root || text.starts_with(&format!("{root}/")) {
                total += 1 + ancestors;
            }
        }
    }
    total
}

fn dict_eq(left: &PyDict, right: &PyDict) -> bool {
    left.len() == right.len()
        && left.iter().all(|(key, value)| {
            right
                .get(key)
                .ok()
                .flatten()
                .is_some_and(|v| v.py_eq(value))
        })
}

fn preload(budget: &mut EnvBudget, spec: &Value) {
    if let Some(count) = spec.get("entries").and_then(Value::as_u64) {
        budget.tick(count).unwrap();
    }
    if let Some(mut left) = spec.get("bytes").and_then(Value::as_u64) {
        while left > 0 {
            let chunk = left.min(MAX_FILE);
            budget.reserve(chunk).unwrap();
            left -= chunk;
        }
    }
}

fn error_json(error: &PyException, root: &str) -> Value {
    json!({
        "type": error.type_name,
        "msg": to_string(error.message.as_codepoints()).replace(root, "{r}"),
    })
}

/// 1 回の呼出し。前の回の控えと予算を受け取り、結果の JSON と、今回の控えと予算を返す。
fn execute(
    root: &Path,
    ancestors: usize,
    run: &Value,
    previous: &[PyDict],
    kept_budget: &mut Option<EnvBudget>,
    kept_adjust: &mut i64,
) -> (Value, PyDict) {
    if let Some(ops) = run.get("ops_before") {
        apply_ops(root, ops.as_array().unwrap());
    }
    let call = &run["call"];
    let root_text = root.to_str().unwrap();
    let sub = |text: &str| text.replace("{r}", root_text);
    let (mut budget, mut adjust) = if run.get("reuse_budget").is_some() {
        (
            kept_budget.take().expect("前の回の予算が無い"),
            *kept_adjust,
        )
    } else {
        (EnvBudget::new(), 0)
    };
    let component = str_of(call, "fn") == "component_tree";
    if let Some(spec) = run.get("preload") {
        // 部品の木では、先に親の足取りが `ancestors` 回、予算を使う。上限に当たる場所が深さで動かないよう、
        // その分を先に引いておき、記録のときに足し戻す。
        let offset = if component && spec.get("entries").is_some() {
            ancestors
        } else {
            0
        };
        let mut spec = spec.clone();
        if let Some(count) = spec.get("entries").and_then(Value::as_u64) {
            spec["entries"] = json!(count - u64::try_from(offset).unwrap());
            adjust = -i64::try_from(offset).unwrap();
        }
        preload(&mut budget, &spec);
    }
    let expected: Option<&PyDict> = run
        .get("expected_from")
        .and_then(Value::as_u64)
        .map(|index| &previous[usize::try_from(index).unwrap()]);
    let mut entries = PyDict::new();
    let mut blobs = call.get("blobs").is_some().then(Blobs::new);
    let flag = |name: &str| call.get(name).and_then(Value::as_bool).unwrap_or(false);
    // `tree` のフレームの深さ。固定期待が決めていなければ、浅い値。
    let frame_depth = run
        .get("depth")
        .and_then(Value::as_u64)
        .map_or(3, |depth| usize::try_from(depth).unwrap());
    let path = cps(&sub(str_of(call, "path")));
    let mut ret: Value = Value::Null;
    let result: Result<(), PyException> = match str_of(call, "fn") {
        "tree" => {
            let mut obs = Obs {
                host: &ProcessHost,
                budget: &mut budget,
                entries: &mut entries,
            };
            tree(
                &mut obs,
                &path,
                &cps(str_of(call, "key")),
                TreeOptions {
                    blobs: blobs.as_mut(),
                    optional: flag("optional"),
                    user_links: flag("user_links"),
                    expected,
                },
                frame_depth,
            )
        }
        "component_tree" => {
            let mut obs = Obs {
                host: &ProcessHost,
                budget: &mut budget,
                entries: &mut entries,
            };
            component_tree(
                &mut obs,
                &path,
                &cps(str_of(call, "kind")),
                expected,
                blobs.as_mut(),
            )
            .map(|key| {
                ret = Value::String(to_string(&key).replace(root_text, "{r}"));
            })
        }
        "record" => {
            let options = RecordOptions {
                expected,
                depth: usize::try_from(call.get("depth").and_then(Value::as_u64).unwrap_or(0))
                    .unwrap(),
                required: flag("required"),
                expected_identity: None,
            };
            record(
                &ProcessHost,
                &path,
                &cps(str_of(call, "key")),
                &mut budget,
                &mut entries,
                &options,
            )
            .map(|raw| {
                ret = raw.map_or(Value::Null, |bytes| {
                    Value::String(bytes.iter().map(|b| format!("{b:02x}")).collect())
                });
            })
        }
        other => panic!("呼出しが不明: {other}"),
    };
    let blob_list: Vec<Value> = blobs
        .iter()
        .flat_map(Blobs::iter)
        .map(|(key, raw)| {
            json!([
                to_string(key).replace(root_text, "{r}"),
                super::fsio::sha(raw),
                raw.len()
            ])
        })
        .collect();
    let root_key = component.then(|| {
        format!(
            "component:{}:{}",
            str_of(call, "kind"),
            sub(str_of(call, "path"))
        )
    });
    let extra = depth_ticks(&entries, root_text, ancestors, root_key.as_deref());
    adjust += i64::try_from(extra).unwrap();
    let mut shown = json!({
        "ret": ret,
        "entries": plain_entries(
            &entries,
            root_text,
            ancestors,
            run.get("drop_routes").is_some(),
            root_key.as_deref(),
        ),
        "blobs": blob_list,
        "budget": [i64::try_from(budget.entries()).unwrap() - adjust, budget.bytes()],
        "error": result.as_ref().err().map_or(Value::Null, |e| error_json(e, root_text)),
    });
    if run.get("summary").is_some() {
        // 大きな控えは、数と SHA-256(鍵を並べた、空白なしの JSON)だけを比べる。
        let text = serde_json::to_string(&shown["entries"]).unwrap();
        shown["entries"] = json!({
            "count": shown["entries"].as_object().unwrap().len(),
            "sha256": super::pyops::sha_hex(text.as_bytes()),
        });
    }
    if let Some(expected) = expected {
        shown["equals_expected"] = json!(dict_eq(&entries, expected));
    }
    *kept_budget = Some(budget);
    *kept_adjust = adjust;
    (shown, entries)
}

/// 食い違った欄だけを、期待と実際で並べる(控えの辞書は、鍵ごとに)。
fn describe_diff(want: &Value, got: &Value) -> String {
    if want["entries"].get("sha256").is_some() {
        return format!("期待: {want}\n実際: {got}");
    }
    let mut lines = Vec::new();
    for field in ["ret", "blobs", "budget", "error", "equals_expected"] {
        if want.get(field) != got.get(field) {
            lines.push(format!(
                "{field}\n  期待: {}\n  実際: {}",
                want.get(field).unwrap_or(&Value::Null),
                got.get(field).unwrap_or(&Value::Null)
            ));
        }
    }
    let (want_entries, got_entries) = (
        want["entries"].as_object().unwrap(),
        got["entries"].as_object().unwrap(),
    );
    for (key, value) in want_entries {
        match got_entries.get(key) {
            None => lines.push(format!("entries に無い: {key} = {value}")),
            Some(other) if other != value => lines.push(format!(
                "entries が違う: {key}\n  期待: {value}\n  実際: {other}"
            )),
            _ => {}
        }
    }
    for key in got_entries
        .keys()
        .filter(|key| !want_entries.contains_key(*key))
    {
        lines.push(format!("entries に余分: {key} = {}", got_entries[key]));
    }
    lines.join("\n")
}

/// 実際のファイルシステムで、シンボリックリンク自身の `lstat` の mode。
/// Linux は 0o120777。macOS は umask をリンクの mode に当てる(umask 022 なら 0o120755)。
/// 配置の外(`dir` の直下)に試しのリンクを作って測り、すぐ消す。
fn probe_link_mode(dir: &Path) -> u64 {
    let probe = dir.join("probe-link");
    symlink("x", &probe).unwrap();
    let mode = std::fs::symlink_metadata(&probe).unwrap().mode();
    std::fs::remove_file(&probe).unwrap();
    u64::from(mode)
}

/// 固定期待のうち、旧版を測った Linux のファイルシステムでしか成り立たない値だけを、
/// この機械で同じ物理入力から読める値に置き換える(ほかは 1 つも変えない)。
/// - リンク自身の mode(0o120777): 実際のリンクを `lstat` した mode(`link_mode`)。
/// - ディレクトリの `file-stat` の大きさ: 鍵が指す実際のディレクトリを `lstat` した大きさ(ext 系の 4096 とは限らない)。
fn portable_expectation(want: &Value, root: &Path, link_mode: u64) -> Value {
    const LINK_MODE_ON_LINUX: u64 = 0o120_777;
    const S_IFMT: u64 = 0o170_000;
    const S_IFDIR: u64 = 0o040_000;
    let mut want = want.clone();
    let Some(entries) = want.get_mut("entries").and_then(Value::as_object_mut) else {
        return want;
    };
    for (key, value) in entries.iter_mut() {
        let Some(items) = value.as_array_mut() else {
            continue;
        };
        for item in items.iter_mut().skip(1) {
            if item.as_u64() == Some(LINK_MODE_ON_LINUX) {
                *item = json!(link_mode);
            }
        }
        let is_directory = items.first().and_then(Value::as_str) == Some("file-stat")
            && items
                .get(3)
                .and_then(Value::as_u64)
                .map(|mode| mode & S_IFMT)
                == Some(S_IFDIR);
        if is_directory && let Some((_, rest)) = key.split_once("{r}") {
            let path = format!("{}{rest}", root.to_str().unwrap());
            let meta = std::fs::symlink_metadata(&path).unwrap();
            assert!(meta.is_dir(), "{key}: 固定期待はディレクトリのはず");
            items[4] = json!(meta.size());
        }
    }
    want
}

fn run_case(case: &Value) {
    let name = str_of(case, "name");
    let dir = Dir::new("case");
    let root = dir.0.join("case");
    std::fs::create_dir(&root).unwrap();
    // 固定期待を作った側が決めた根の mode(umask 002 の機械で作った値)。umask で変わらないよう、そろえる。
    set_mode(&root, 0o775);
    let link_mode = probe_link_mode(&dir.0);
    let ancestors = root.components().filter(|c| c.as_os_str() != "/").count();
    build_layout(&root, case["layout"].as_array().unwrap());
    let mut previous: Vec<PyDict> = Vec::new();
    let mut budget: Option<EnvBudget> = None;
    let mut adjust = 0i64;
    for (index, run) in case["runs"].as_array().unwrap().iter().enumerate() {
        let (shown, entries) = execute(&root, ancestors, run, &previous, &mut budget, &mut adjust);
        let want = &portable_expectation(&case["results"][index], &root, link_mode);
        assert!(
            &shown == want,
            "{name} の {index} 回目が旧版と違う\n{}",
            describe_diff(want, &shown)
        );
        previous.push(entries);
    }
}

#[test]
fn observation_matches_the_old_python_on_the_same_physical_input() {
    let cases = fixture!("observe_cases.json");
    let list = cases["cases"].as_array().unwrap();
    assert!(
        list.len() > 100,
        "固定期待の件数が少なすぎる: {}",
        list.len()
    );
    let mut names = BTreeSet::new();
    for case in list {
        assert!(
            names.insert(str_of(case, "name").to_owned()),
            "名前が重複している"
        );
        run_case(case);
    }
    // 少なくとも、3 つの関数がそれぞれ十分な件数で比べられている。
    for prefix in ["tree-", "component-", "record-"] {
        let count = names.iter().filter(|n| n.starts_with(prefix)).count();
        assert!(count >= 20, "{prefix} の件数が少ない: {count}");
    }
}

#[test]
fn recursion_depth_matches_the_old_python_around_the_limit() {
    // 深い木は、Rust の再帰も 1000 段近くになるので、大きなスタックで流す。
    super::testing::with_big_stack(|| {
        let cases = fixture!("depth_cases.json");
        let list = cases["cases"].as_array().unwrap();
        assert!(list.len() >= 8);
        for case in list {
            let runs = case["runs"].as_array().unwrap();
            // 通る深さと、通らない深さの両方がある(境目をまたいでいる)。
            let results = case["results"].as_array().unwrap();
            assert!(
                results.iter().any(|r| r["error"].is_null()),
                "{}",
                str_of(case, "name")
            );
            assert!(
                results.iter().any(|r| !r["error"].is_null()),
                "{}",
                str_of(case, "name")
            );
            assert_eq!(runs.len(), results.len());
            run_case(case);
        }
    });
}

#[test]
fn the_test_helpers_agree_with_the_values_the_code_writes() {
    // 固定期待を作る側と読む側の約束(印の位置)が、コードの書く値と同じ長さであることを確かめる。
    let id = super::fsio::Identity {
        dev: 1,
        ino: 2,
        mode: 0o100644,
        size: 3,
        mtime_ns: 4,
        ctime_ns: 5,
    };
    let stat = plain_value(&entry::file_stat(&id), "/x");
    assert_eq!(masked_positions(stat.as_array().unwrap()).len(), 4);
    assert_eq!(stat.as_array().unwrap().len(), 7);
    let mut d = PyDict::new();
    d.insert(text(cps("a")), entry::missing()).unwrap();
    assert_eq!(
        plain_entries(&d, "/x", 0, false, None),
        json!({"a": ["missing"]})
    );
}

// ---------------------------------------------------------------- 値そのものの検査

use std::os::unix::fs::MetadataExt;

/// `st_dev` の旧 Python の見え方(10 進の文字列)。Mac の `dev_t` は符号つき 32 ビットで、
/// `MetadataExt::dev` は符号を広げた値を返すので、符号つきに戻す。
fn dev_text(meta: &std::fs::Metadata) -> String {
    #[cfg(target_os = "macos")]
    {
        #[allow(clippy::cast_possible_wrap)]
        let signed = meta.dev() as i64;
        signed.to_string()
    }
    #[cfg(not(target_os = "macos"))]
    {
        meta.dev().to_string()
    }
}

fn stat_numbers(meta: &std::fs::Metadata) -> [Value; 6] {
    let ns = |sec: i64, nsec: i64| i128::from(sec) * 1_000_000_000 + i128::from(nsec);
    [
        json!(dev_text(meta)),
        json!(meta.ino()),
        json!(meta.mode()),
        json!(meta.size()),
        json!(ns(meta.mtime(), meta.mtime_nsec()).to_string()),
        json!(ns(meta.ctime(), meta.ctime_nsec()).to_string()),
    ]
}

/// 整数を、大きくても比べられるよう文字列にした JSON(`mtime_ns` が `i64` に収まらない機種のため)。
fn exact(value: &PyValue) -> Value {
    match value {
        PyValue::Int(int) => json!(int.to_decimal(0).unwrap()),
        PyValue::Str(s) => json!(to_string(s.as_codepoints())),
        PyValue::List(items) => Value::Array(items.iter().map(exact).collect()),
        other => panic!("{other:?}"),
    }
}

fn entry_of(entries: &PyDict, key: &str) -> Value {
    exact(entry::get(entries, &cps(key)).unwrap_or_else(|| panic!("{key} が無い")))
}

fn text_numbers(values: &[Value]) -> Vec<Value> {
    values
        .iter()
        .map(|v| match v {
            Value::Number(n) => json!(n.to_string()),
            other => other.clone(),
        })
        .collect()
}

#[test]
fn entries_carry_the_real_stat_values_in_the_old_positions() {
    let dir = Dir::new("values");
    let root = dir.0.join("case");
    std::fs::create_dir(&root).unwrap();
    build_layout(
        &root,
        json!([
            {"path": "p/a.txt", "kind": "file", "body": "abc", "mode": 416},
            {"path": "s/one", "kind": "link", "target": "../store/x"},
            {"path": "store/x/SKILL.md", "kind": "file", "body": "x"},
            {"path": "c/f", "kind": "file", "body": "cfg"},
            {"path": "c/l", "kind": "link", "target": "f"},
            {"path": "t/abs", "kind": "link", "target": "{r}/store/x"},
        ])
        .as_array()
        .unwrap(),
    );
    let at = |rel: &str| root.join(rel);
    let lstat = |rel: &str| std::fs::symlink_metadata(at(rel)).unwrap();
    let ancestors = root.components().filter(|c| c.as_os_str() != "/").count();
    let root_text = root.to_str().unwrap();

    // component_tree: file-stat・directory-stat・directory-step・link-step・installation-link。
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let mut obs = Obs {
        host: &ProcessHost,
        budget: &mut budget,
        entries: &mut entries,
    };
    let plugin_key = component_tree(
        &mut obs,
        &cps(&format!("{root_text}/p")),
        &cps("plugin"),
        None,
        None,
    )
    .unwrap();
    let plugin_key = to_string(&plugin_key);
    let file = stat_numbers(&lstat("p/a.txt"));
    let mut want = vec![json!("file-stat")];
    want.extend(text_numbers(&file));
    assert_eq!(
        entry_of(&entries, &format!("@stat:{plugin_key}/a.txt")),
        Value::Array(want)
    );
    let dir_meta = lstat("p");
    assert_eq!(
        entry_of(&entries, &format!("@stat:{plugin_key}")),
        json!([
            "directory-stat",
            dev_text(&dir_meta),
            dir_meta.ino().to_string(),
            dir_meta.mode().to_string()
        ])
    );
    assert_eq!(
        entry_of(&entries, &format!("@route:{plugin_key}:{}", ancestors)),
        json!([
            "directory-step",
            "p",
            dev_text(&dir_meta),
            dir_meta.ino().to_string(),
            dir_meta.mode().to_string()
        ])
    );
    assert_eq!(
        entry_of(&entries, &format!("{plugin_key}/a.txt")),
        json!(["file", "416", super::fsio::sha(b"abc")])
    );
    let mut obs = Obs {
        host: &ProcessHost,
        budget: &mut budget,
        entries: &mut entries,
    };
    let skill_key = to_string(
        &component_tree(
            &mut obs,
            &cps(&format!("{root_text}/s")),
            &cps("personal-skill"),
            None,
            None,
        )
        .unwrap(),
    );
    let link = lstat("s/one");
    let link_ids = [
        dev_text(&link),
        link.ino().to_string(),
        link.mode().to_string(),
    ];
    assert_eq!(
        entry_of(&entries, &format!("@link:{skill_key}/one")),
        json!([
            "installation-link",
            "../store/x",
            link_ids[0],
            link_ids[1],
            link_ids[2]
        ])
    );
    // 親 `s` を開いた後の、名前 `..`・`store`・`x` の足取りと、リンクの足取り(link-step)。
    let first_step = entry_of(&entries, &format!("@route:{skill_key}/one:0"));
    assert_eq!(
        first_step,
        json!([
            "link-step",
            "one",
            "../store/x",
            link_ids[0],
            link_ids[1],
            link_ids[2]
        ])
    );

    // record: configuration-link は、リンク自身の dev・ino と、対象の dev・ino・mode。
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let raw = record(
        &ProcessHost,
        &cps(&format!("{root_text}/c/l")),
        &cps("external:x"),
        &mut budget,
        &mut entries,
        &RecordOptions::default(),
    )
    .unwrap()
    .unwrap();
    assert_eq!(raw, b"cfg");
    let (link, target) = (lstat("c/l"), lstat("c/f"));
    assert_eq!(
        entry_of(&entries, "@file-link:0:external:x"),
        json!([
            "configuration-link",
            "f",
            dev_text(&link),
            link.ino().to_string(),
            dev_text(&target),
            target.ino().to_string(),
            target.mode().to_string()
        ])
    );

    // tree(user_links): installation-link は、リンク自身の dev・ino と、対象の dev・ino・mode。
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let mut obs = Obs {
        host: &ProcessHost,
        budget: &mut budget,
        entries: &mut entries,
    };
    tree(
        &mut obs,
        &cps(&format!("{root_text}/t")),
        &cps("k"),
        TreeOptions {
            user_links: true,
            ..TreeOptions::default()
        },
        3,
    )
    .unwrap();
    let (link, target) = (lstat("t/abs"), lstat("store/x"));
    assert_eq!(
        entry_of(&entries, "@link:k/abs"),
        json!([
            "installation-link",
            format!("{root_text}/store/x"),
            dev_text(&link),
            link.ino().to_string(),
            dev_text(&target),
            target.ino().to_string(),
            target.mode().to_string()
        ])
    );
    assert_eq!(
        entry_of(&entries, "k/abs/SKILL.md"),
        json!(["file", "420", super::fsio::sha(b"x")])
    );
}

#[test]
fn a_very_deep_tree_stops_with_a_recursion_error_instead_of_overflowing_the_stack() {
    // 製品の主スレッド(8 MiB)で 1000 段が収まることの確認は、`walk_at` の枠の大きさに依る。ここでは、
    // 試験用のスレッド(既定 2 MiB)に debug の大きな枠が収まらないので、大きなスタックで流す。
    super::testing::with_big_stack(deep_tree_stops_with_a_recursion_error);
}

fn deep_tree_stops_with_a_recursion_error() {
    use dev_workflow_platform::environment_guard as os;
    use std::os::fd::AsFd;
    let dir = Dir::new("deep");
    std::fs::create_dir(dir.0.join("t")).unwrap();
    // 相対の `openat` で積むので、パスの長さの上限(PATH_MAX)に当たらない。
    let mut fd = os::open_root().unwrap();
    for part in dir.0.join("t").components().skip(1) {
        fd = os::open_dir_at(fd.as_fd(), part.as_os_str().as_bytes()).unwrap();
    }
    for _ in 0..1500 {
        os::mkdir_at(fd.as_fd(), b"d", 0o755).unwrap();
        fd = os::open_dir_at(fd.as_fd(), b"d").unwrap();
    }
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let mut obs = Obs {
        host: &ProcessHost,
        budget: &mut budget,
        entries: &mut entries,
    };
    let error = tree(
        &mut obs,
        &dir.cps("t"),
        &cps("k"),
        TreeOptions::default(),
        3,
    )
    .unwrap_err();
    // 文面は、旧版の呼出しの順で決まる(`recursion_depth_matches_the_old_python_around_the_limit` が旧版と比べる)。
    assert_eq!(error.type_name, "RecursionError");
}

#[test]
fn the_recursion_limit_of_walk_at_follows_the_frame_and_the_first_native_call() {
    use super::fsio::walk_at;
    use dev_workflow_platform::environment_guard as os;
    use std::os::fd::AsFd;
    let dir = Dir::new("depth");
    std::fs::create_dir(dir.0.join("t")).unwrap();
    let mut fd = os::open_root().unwrap();
    for part in dir.0.join("t").components().skip(1) {
        fd = os::open_dir_at(fd.as_fd(), part.as_os_str().as_bytes()).unwrap();
    }
    // 空のディレクトリを旧版の `walk_at` に直接渡して測った境目(旧 Python 3.10.12): 998 まで通り、
    // 999 は `sorted` の中の C の呼出し、1000 は最初の `os.fstat`、1001 はフレームに入れない。
    const NATIVE: &str = "maximum recursion depth exceeded while calling a Python object";
    for (depth, want) in [
        (998usize, None),
        (999, Some(NATIVE)),
        (1000, Some(NATIVE)),
        (1001, Some("maximum recursion depth exceeded")),
    ] {
        let mut budget = EnvBudget::new();
        let mut entries = PyDict::new();
        let mut obs = Obs {
            host: &ProcessHost,
            budget: &mut budget,
            entries: &mut entries,
        };
        let result = walk_at(&mut obs, fd.as_fd(), &cps("k"), None, None, None, depth);
        match want {
            None => result.unwrap(),
            Some(text) => {
                let error = result.unwrap_err();
                assert_eq!(error.type_name, "RecursionError", "depth {depth}");
                assert_eq!(
                    to_string(error.message.as_codepoints()),
                    text,
                    "depth {depth}"
                );
            }
        }
    }
}

/// 旧版で通る最も深い木(`tree` を深さ 3 の呼出元から呼び、ディレクトリだけを 990 段)を、
/// 製品の主スレッドと同じ 8 MiB のスタックで走査できる。debug のビルドは枠が大きいので、
/// 製品と同じ release のビルド(`cargo test --release`)でだけ確かめる。
#[cfg(not(debug_assertions))]
#[test]
fn the_deepest_tree_the_old_version_accepts_fits_a_main_thread_sized_stack() {
    use dev_workflow_platform::environment_guard as os;
    use std::os::fd::AsFd;
    let dir = Dir::new("stack");
    let path = dir.cps("t");
    std::fs::create_dir(dir.0.join("t")).unwrap();
    let mut fd = os::open_root().unwrap();
    for part in dir.0.join("t").components().skip(1) {
        fd = os::open_dir_at(fd.as_fd(), part.as_os_str().as_bytes()).unwrap();
    }
    // 旧版の `walk_at` は、ディレクトリだけの木で、フレームが 998 まで通る(`tree` の深さ 3 なら、根が 4)。
    let levels = 998 - 4;
    for _ in 0..levels {
        os::mkdir_at(fd.as_fd(), b"d", 0o755).unwrap();
        fd = os::open_dir_at(fd.as_fd(), b"d").unwrap();
    }
    drop(fd);
    let handle = std::thread::Builder::new()
        .stack_size(8 * 1024 * 1024)
        .spawn(move || {
            let mut budget = EnvBudget::new();
            let mut entries = PyDict::new();
            let mut obs = Obs {
                host: &ProcessHost,
                budget: &mut budget,
                entries: &mut entries,
            };
            tree(&mut obs, &path, &cps("k"), TreeOptions::default(), 3).map(|()| entries.len())
        })
        .unwrap();
    let count = handle.join().unwrap().unwrap();
    assert_eq!(count, levels + 1);
}
