//! 旧版の `snapshot`・`verify`(と、その中の `load_verified` の呼出し)。
//!
//! - 控えの並び(旧版の順): `files` → `directories` → `user_directories` → `components`(記述子の順)→ 一覧から足した
//!   部品(保持値が無いときだけ)→ 配布物の部品の旧名の写し(`plugin/...`)→ `configs`(後入れ先出し)。
//! - Git 設定の `include` の字面の判定は、照合のとき(保持値がある)だけ字面で行い、起動時は必ず解析する。
//! - 保持した Git 設定が変わったときは、参照先を読む前に止める。
//! - `verify` は、呼ぶたびに控えを読み直して SHA-256 を照らし、元の集合全体とコピーの木を完全に比べる。
//!   失敗の後に、説明のための追加の読取りや Git の起動をしない。
//!
//! 保持値(JSON から読んだ値)の形が崩れているときの例外は、`bootstrap` が作った控えでは起きない形
//! (控えの SHA-256 は外部に保持され、`bootstrap` だけが書く)なので、型の違う `specs`・`entries` などは
//! 代表的な `TypeError`・`KeyError` にとどめる(コメントの「届かない分岐」)。
use super::budget::EnvBudget;
use super::component::component_tree;
use super::depth;
use super::entry;
use super::error::PyException;
use super::fsio::{Obs, TreeOptions, read_regular, tree, tree_for};
use super::gitconfig::includes;
use super::inventory::{Blobs, ComponentTree, component_inventory};
use super::pyjson::{PyCtx, plugin_inventory};
use super::pyops::{Cps, as_text, iterate, lit, starts_with, text};
use super::pypath::PyPath;
use super::pyvalue::{PyDict, PyValue};
use super::record::{RecordOptions, record};
use super::specs::{Host, absolute};
use super::state::load_verified;
use std::collections::HashSet;

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn key_error(name: &str) -> PyException {
    PyException::new("KeyError", &format!("'{name}'"))
}

/// `value[name]`(`value` は控えから読んだ辞書)。
fn field(value: &PyDict, name: &str) -> Result<PyValue, PyException> {
    value.get_str(name).cloned().ok_or_else(|| key_error(name))
}

/// 届かない分岐: 控えの各欄が辞書でない。旧版は欄ごとに別の `TypeError` になるが、`bootstrap` は必ず辞書を書く。
fn not_a_dict(value: &PyValue) -> PyException {
    PyException::type_error(&format!(
        "argument of type '{}' is not iterable",
        value.type_name()
    ))
}

fn concat_error(item: &PyValue) -> PyException {
    PyException::type_error(&format!(
        "can only concatenate str (not \"{}\") to str",
        item.type_name()
    ))
}

/// `'<prefix>' + path`(`path` が文字列でなければ `TypeError`)。
fn prefixed(prefix: &str, path: &PyValue) -> Result<(Cps, Cps), PyException> {
    let Some(path) = as_text(path) else {
        return Err(concat_error(path));
    };
    let mut key = lit(prefix);
    key.extend_from_slice(path);
    Ok((path.to_vec(), key))
}

/// `specs[name]`。
fn spec_list(specs: &PyDict, name: &str) -> Result<Vec<PyValue>, PyException> {
    iterate(&field(specs, name)?)
}

/// `inventory or []` を反復した値。
fn inventory_items(inventory: Option<&PyValue>) -> Result<Vec<PyValue>, PyException> {
    match inventory {
        Some(value) if value.truthy() => iterate(value),
        _ => Ok(Vec::new()),
    }
}

/// `_component_inventory` が呼ぶ `component_tree(path, kind, budget, entries, blobs=blobs)` の差し込み口。
struct Held<'o, 'a> {
    obs: &'o mut Obs<'a>,
}

impl ComponentTree for Held<'_, '_> {
    fn component_tree(
        &mut self,
        path: &[u32],
        kind: &str,
        blobs: &mut Blobs,
    ) -> Result<Cps, PyException> {
        component_tree(self.obs, path, &lit(kind), None, Some(blobs))
    }
}

/// `hold_components(components)`: 記述子ごとに部品の木を保持する。
fn hold_components(
    obs: &mut Obs<'_>,
    components: &[PyValue],
    expected: Option<&PyDict>,
    blobs: &mut Blobs,
) -> Result<(), PyException> {
    for component in components {
        let invalid = || stop("component の記述子が不正");
        let PyValue::Dict(dict) = component else {
            return Err(invalid());
        };
        let kind = dict.get_str("kind").and_then(as_text).ok_or_else(invalid)?;
        let path = dict.get_str("path").and_then(as_text).ok_or_else(invalid)?;
        component_tree(obs, path, kind, expected, Some(blobs))?;
    }
    Ok(())
}

/// `b'include' in raw.lower()`(`bytes.lower` は ASCII だけを小文字にする)。
pub fn mentions_include(raw: &[u8]) -> bool {
    let needle = b"include";
    raw.windows(needle.len())
        .any(|window| window.eq_ignore_ascii_case(needle))
}

/// 旧版の `snapshot(root, specs, inventory, blobs, expected)`。`depth` は `snapshot` の本体のフレームの深さ
/// (`tree` は 1 つ深い)。旧版の `snapshot` は最初に `root = absolute(root)` を通すので、配布物の根は `bootstrap` でも
/// `verify` でも必ず `Path` で `tree` に渡る(旧 Python 3.10.12 で、深さ 989〜994 に置いて、根を文字列でも `Path` でも
/// 同じ結果になることを測った)。
/// `specs` は、一覧から足す部品で書き換わる(旧版も同じ辞書を書き換える)。
#[allow(clippy::too_many_arguments)]
pub fn snapshot(
    host: &dyn Host,
    root: &[u32],
    specs: &mut PyDict,
    inventory: Option<&PyValue>,
    blobs: &mut Blobs,
    expected: Option<&PyDict>,
    depth: usize,
    ctx: PyCtx,
) -> Result<PyDict, PyException> {
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let root = absolute(host, root)?;
    let root_text = root.to_cps();
    let tree_depth = depth + 1;
    let process_env = host.process_env();
    // 全配布物を含める。scripts の sibling imports/source もこのコピーで閉じる。
    let has_components = specs.contains_str("components");
    if !has_components {
        let mut obs = Obs {
            host,
            budget: &mut budget,
            entries: &mut entries,
        };
        tree_for(
            &mut obs,
            &root_text,
            &lit("plugin"),
            TreeOptions {
                blobs: Some(blobs),
                ..TreeOptions::default()
            },
            tree_depth,
            // `absolute(root)` が返す `Path` を渡す(`verify` も同じ)。
            true,
        )?;
    }
    for path in spec_list(specs, "files")? {
        let (path, key) = prefixed("external:", &path)?;
        let raw = record(
            host,
            &path,
            &key,
            &mut budget,
            &mut entries,
            &RecordOptions {
                expected,
                ..RecordOptions::default()
            },
        )?;
        if let Some(raw) = raw {
            blobs.insert(key, raw);
        }
    }
    for path in spec_list(specs, "directories")? {
        let (path, key) = prefixed("external:", &path)?;
        let mut obs = Obs {
            host,
            budget: &mut budget,
            entries: &mut entries,
        };
        tree(
            &mut obs,
            &path,
            &key,
            TreeOptions {
                optional: true,
                ..TreeOptions::default()
            },
            tree_depth,
        )?;
    }
    let user_directories = match specs.get_str("user_directories") {
        Some(value) => iterate(value)?,
        None => Vec::new(),
    };
    for path in user_directories {
        let (path, key) = prefixed("external:", &path)?;
        let mut obs = Obs {
            host,
            budget: &mut budget,
            entries: &mut entries,
        };
        tree(
            &mut obs,
            &path,
            &key,
            TreeOptions {
                optional: true,
                user_links: true,
                expected,
                ..TreeOptions::default()
            },
            tree_depth,
        )?;
    }
    // 一覧の skills-dir を、同じ予算・同じ走査で先に保持した本文だけへ結ぶ。
    let component_list = |specs: &PyDict| -> Result<Vec<PyValue>, PyException> {
        match specs.get_str("components") {
            Some(value) => iterate(value),
            None => Ok(Vec::new()),
        }
    };
    let held = component_list(specs)?;
    let held_count = held.len();
    {
        let mut obs = Obs {
            host,
            budget: &mut budget,
            entries: &mut entries,
        };
        hold_components(&mut obs, &held, expected, blobs)?;
        if has_components && expected.is_none() {
            let inventory = inventory_items(inventory)?;
            component_inventory(
                host,
                specs,
                &inventory,
                blobs,
                &mut Held { obs: &mut obs },
                PyCtx { depth, ..ctx },
            )?;
            let all = component_list(specs)?;
            hold_components(&mut obs, &all[held_count.min(all.len())..], expected, blobs)?;
        }
    }
    if has_components {
        let mut rootkey = lit("component:plugin:");
        rootkey.extend_from_slice(&root_text);
        let is_directory = entry::get(&entries, &rootkey).is_some_and(|held| {
            held.as_list()
                .and_then(<[PyValue]>::first)
                .is_some_and(|kind| kind.py_eq(&PyValue::from("directory")))
        });
        if !is_directory {
            return Err(stop("配布物の component 記述子が無い"));
        }
        let mut under = rootkey.clone();
        under.push(0x2f);
        let items: Vec<(Cps, PyValue)> = entries
            .iter()
            .map(|(key, value)| {
                (
                    as_text(key).expect("控えの鍵は文字列").to_vec(),
                    value.clone(),
                )
            })
            .collect();
        for (key, value) in items {
            if key == rootkey || starts_with_cps(&key, &under) {
                let mut legacy = lit("plugin");
                legacy.extend_from_slice(&key[rootkey.len()..]);
                entry::put(&mut entries, &legacy, value);
                if let Some(raw) = blobs.get(&key).cloned() {
                    blobs.insert(legacy, raw);
                }
            }
        }
    }
    let mut pending: Vec<PyValue> = spec_list(specs, "configs")?;
    let mut seen: HashSet<Cps> = HashSet::new();
    while !pending.is_empty() {
        budget.tick(0)?;
        let item = pending.pop().expect("空でない");
        let Some(item) = as_text(&item) else {
            // 届かない分岐: `absolute` は `str(...)` で文字列にして先へ進むが、`bootstrap` は文字列だけを書く。
            return Err(PyException::type_error(
                "expected str, bytes or os.PathLike object",
            ));
        };
        let path = absolute(host, item)?.to_cps();
        if !seen.insert(path.clone()) {
            continue;
        }
        let mut key = lit("external:");
        key.extend_from_slice(&path);
        if path == lit("/dev/null") {
            entry::put(
                &mut entries,
                &key,
                PyValue::List(vec![PyValue::from("null-device")]),
            );
            continue;
        }
        let raw = record(
            host,
            &path,
            &key,
            &mut budget,
            &mut entries,
            &RecordOptions {
                expected,
                ..RecordOptions::default()
            },
        )?;
        // 新しい include 宛先を探索する前に、既知の設定本文との一致を要求する。
        if let Some(expected) = expected {
            let now = entry::get(&entries, &key).cloned();
            let held = entry::get(expected, &key).cloned();
            let (Some(now), Some(held)) = (now, held) else {
                return Err(key_error(&super::pystr::to_string(&key)));
            };
            if now.py_ne(&held) {
                return Err(stop(
                    "保持済み Git 設定が変わったため参照先を読まず停止した",
                ));
            }
        }
        if let Some(raw) = raw {
            // 照合のとき(`expected` がある)は、上で控えと同じバイト列だと確かめてある。起動時に Git が解析できた
            // そのバイト列に include の字面が無ければ、行き先は作れないので、Git を起動しない。
            // 起動時の控え(`expected` が無い)は必ず Git で解析する。
            if expected.is_none() || mentions_include(&raw) {
                let found = includes(
                    host,
                    &PyPath::new(&path),
                    &raw,
                    Some(&mut budget),
                    &process_env,
                )?;
                pending.extend(found.into_iter().map(text));
            }
        }
    }
    // 古い控えは、従来どおり installed tree を照合する。
    if !has_components {
        for item in inventory_items(inventory)? {
            let install = match &item {
                PyValue::Dict(dict) => dict
                    .get_str("installPath")
                    .cloned()
                    .ok_or_else(|| key_error("installPath"))?,
                other => {
                    return Err(PyException::type_error(&format!(
                        "'{}' object is not subscriptable",
                        other.type_name()
                    )));
                }
            };
            let Some(install) = as_text(&install) else {
                return Err(PyException::type_error(
                    "expected str, bytes or os.PathLike object",
                ));
            };
            let path = absolute(host, install)?.to_cps();
            if path != root_text {
                let mut key = lit("installed:");
                key.extend_from_slice(&path);
                let mut obs = Obs {
                    host,
                    budget: &mut budget,
                    entries: &mut entries,
                };
                tree(&mut obs, &path, &key, TreeOptions::default(), tree_depth)?;
            }
        }
    }
    Ok(entries)
}

fn starts_with_cps(text: &[u32], prefix: &[u32]) -> bool {
    text.len() >= prefix.len() && text[..prefix.len()] == prefix[..]
}

/// 旧版の `verify(state, expected, current_inventory)`。`ctx.depth` は呼び出し側のフレームの深さ
/// (`verify` の本体は 1 つ深い)。戻り値は、読み直した控えの値。
pub fn verify(
    host: &dyn Host,
    state: &[u32],
    expected_sha: &[u32],
    current_inventory: Option<&[u32]>,
    ctx: PyCtx,
) -> Result<PyValue, PyException> {
    let frame = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    depth::frame(Some(frame.depth))?;
    let value = load_verified(expected_sha, || read_regular(host, state, None), frame)?;
    let dict = value.as_dict().expect("`load_verified` は辞書だけを返す");
    if let Some(path) = current_inventory {
        // `plugin_inventory(read_regular(current_inventory)) != value['inventory']`
        let raw = read_regular(host, path, None)?;
        let current = plugin_inventory(&raw, frame)?;
        let held = field(dict, "inventory")?;
        if PyValue::List(current).py_ne(&held) {
            return Err(stop("有効 plugin の一覧が変わった"));
        }
    }
    let root = field(dict, "root")?;
    let mut specs = match field(dict, "specs")? {
        PyValue::Dict(specs) => specs,
        other => return Err(not_a_dict(&other)),
    };
    let inventory = field(dict, "inventory")?;
    let entries = match field(dict, "entries")? {
        PyValue::Dict(entries) => entries,
        other => return Err(not_a_dict(&other)),
    };
    let Some(root_text) = as_text(&root) else {
        // 届かない分岐: `str(root)` が型によって別の文字列になる。`bootstrap` は文字列だけを書く。
        return Err(PyException::type_error(
            "expected str, bytes or os.PathLike object",
        ));
    };
    let mut blobs = Blobs::new();
    let now = snapshot(
        host,
        root_text,
        &mut specs,
        Some(&inventory),
        &mut blobs,
        Some(&entries),
        frame.depth + 1,
        frame,
    )?;
    if PyValue::Dict(now).py_ne(&PyValue::Dict(entries.clone())) {
        return Err(stop("配布物または利用者環境が変わった"));
    }
    // 信頼コピーの木を、元の集合の `plugin` だけと完全に比べる。
    let copy = field(dict, "copy")?;
    let copy_text = as_text(&copy).map(<[u32]>::to_vec);
    let mut copied = PyDict::new();
    {
        let Some(copy_text) = copy_text else {
            return Err(PyException::type_error(
                "expected str, bytes or os.PathLike object",
            ));
        };
        let mut budget = EnvBudget::new();
        let mut obs = Obs {
            host,
            budget: &mut budget,
            entries: &mut copied,
        };
        tree(
            &mut obs,
            &copy_text,
            &lit("plugin"),
            TreeOptions::default(),
            frame.depth + 1,
        )?;
    }
    let mut wanted = PyDict::new();
    for (key, held) in entries.iter() {
        let name = as_text(key).expect("控えの鍵は文字列");
        if name == lit("plugin") || starts_with(name, "plugin/") {
            wanted.insert(key.clone(), held.clone())?;
        }
    }
    if PyValue::Dict(copied).py_ne(&PyValue::Dict(wanted)) {
        return Err(stop("信頼コピーが変わった"));
    }
    Ok(value)
}
