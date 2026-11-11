//! 旧版の `plugin_inventory` と `_component_inventory` の判断。保持した本文(blobs)と、
//! `component_tree` を呼ぶ差し込み口を受け取る。評価の順と例外は旧版のコードの順に合わせる。
use super::error::PyException;
use super::pyjson::{PyCtx, strict_settings};
use super::pyops::{Cps, as_text, dict, iterate, lit, text, text_of};
use super::pypath::{PyPath, isabs};
use super::pyvalue::{PyDict, PyValue};
use super::specs::{Host, absolute};
use std::collections::HashMap;

#[allow(unused_imports)]
pub use super::pyjson::plugin_inventory;

/// 保持した本文。鍵は `component:…`・`external:…` などの字面。旧版の dict と同じく挿入順を保ち、
/// 同じ鍵への代入は最初の位置を保つ(`read_components` の結果の並びがこの順で決まる)。
#[derive(Clone, Debug, Default)]
pub struct Blobs {
    order: Vec<(Cps, Vec<u8>)>,
    index: HashMap<Cps, usize>,
}

impl Blobs {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, key: Cps, value: Vec<u8>) {
        match self.index.get(&key) {
            Some(&position) => self.order[position].1 = value,
            None => {
                self.index.insert(key.clone(), self.order.len());
                self.order.push((key, value));
            }
        }
    }

    pub fn get(&self, key: &[u32]) -> Option<&Vec<u8>> {
        self.index.get(key).map(|&position| &self.order[position].1)
    }

    pub fn contains(&self, key: &[u32]) -> bool {
        self.index.contains_key(key)
    }

    /// 挿入順。
    pub fn iter(&self) -> impl Iterator<Item = (&Cps, &Vec<u8>)> {
        self.order.iter().map(|(k, v)| (k, v))
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }
}

impl FromIterator<(Cps, Vec<u8>)> for Blobs {
    fn from_iter<T: IntoIterator<Item = (Cps, Vec<u8>)>>(iter: T) -> Self {
        let mut blobs = Self::new();
        for (key, value) in iter {
            blobs.insert(key, value);
        }
        blobs
    }
}

/// `component_tree(path, kind, budget, entries, blobs=blobs)` の差し込み口。戻り値は部品の鍵。
pub trait ComponentTree {
    fn component_tree(
        &mut self,
        path: &[u32],
        kind: &str,
        blobs: &mut Blobs,
    ) -> Result<Cps, PyException>;
}

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

/// `value[key]`(key は文字列)。dict 以外は旧版と同じ `TypeError`、無ければ `KeyError`。
fn getitem(value: &PyValue, key: &str) -> Result<PyValue, PyException> {
    match value {
        PyValue::Dict(dict) => dict
            .get_str(key)
            .cloned()
            .ok_or_else(|| PyException::new("KeyError", &format!("'{key}'"))),
        PyValue::Str(_) => Err(PyException::type_error("string indices must be integers")),
        PyValue::List(_) => Err(PyException::type_error(
            "list indices must be integers or slices, not str",
        )),
        PyValue::Bytes(_) => Err(PyException::type_error(
            "byte indices must be integers or slices, not str",
        )),
        other => Err(super::pyops::not_subscriptable(other)),
    }
}

/// `value.get(key)`。dict 以外は `AttributeError`。
fn get_or_none(value: &PyValue, key: &str) -> Result<PyValue, PyException> {
    match value {
        PyValue::Dict(dict) => Ok(dict.get_str(key).cloned().unwrap_or(PyValue::None)),
        other => Err(PyException::new(
            "AttributeError",
            &format!("'{}' object has no attribute 'get'", other.type_name()),
        )),
    }
}

fn str_of(value: &PyValue) -> Option<Cps> {
    as_text(value).map(<[u32]>::to_vec)
}

fn join(base: &[u32], tail: &str) -> Cps {
    PyPath::new(base).join(&lit(tail)).to_cps()
}

/// `specs['components']`。読むのは、旧版が最初に触れるとき(`add` か skills-dir の候補)から。
struct Components<'a> {
    specs: &'a mut PyDict,
}

impl Components<'_> {
    fn value(&self) -> Result<PyValue, PyException> {
        self.specs
            .get_str("components")
            .cloned()
            .ok_or_else(|| PyException::new("KeyError", "'components'"))
    }

    /// `for old in specs['components']`。
    fn items(&self) -> Result<Vec<PyValue>, PyException> {
        iterate(&self.value()?)
    }

    /// `specs['components'].append(item)`。
    fn append(&mut self, item: PyValue) -> Result<(), PyException> {
        match self.value()? {
            PyValue::List(mut list) => {
                list.push(item);
                self.specs.insert_str("components", PyValue::List(list));
                Ok(())
            }
            other => Err(append_error(&other)),
        }
    }

    /// 位置 `index` の記述子の `entry` へ追加する(`existing['entry'].append(selection)`)。
    fn append_entry(&mut self, index: usize, selection: PyValue) -> Result<(), PyException> {
        let PyValue::List(mut list) = self.value()? else {
            unreachable!("位置は list から得た")
        };
        let PyValue::Dict(mut existing) = list[index].clone() else {
            unreachable!("記述子は dict")
        };
        match existing.get_str("entry").cloned() {
            Some(PyValue::List(mut entry)) => {
                entry.push(selection);
                existing.insert_str("entry", PyValue::List(entry));
            }
            Some(other) => return Err(append_error(&other)),
            None => return Err(PyException::new("KeyError", "'entry'")),
        }
        list[index] = PyValue::Dict(existing);
        self.specs.insert_str("components", PyValue::List(list));
        Ok(())
    }
}

fn append_error(value: &PyValue) -> PyException {
    PyException::new(
        "AttributeError",
        &format!("'{}' object has no attribute 'append'", value.type_name()),
    )
}

/// `item in container`(`container` は旧版の `existing['entry']`)。
fn contains(container: &PyValue, item: &PyValue) -> Result<bool, PyException> {
    match container {
        PyValue::List(list) => Ok(list.iter().any(|x| x.eq_in_container(item))),
        PyValue::Dict(dict) => Ok(dict.get(item)?.is_some()),
        PyValue::Str(_) => Err(PyException::type_error(&format!(
            "'in <string>' requires string as left operand, not {}",
            item.type_name()
        ))),
        PyValue::Bytes(_) => Err(PyException::type_error(&format!(
            "a bytes-like object is required, not '{}'",
            item.type_name()
        ))),
        other => Err(PyException::type_error(&format!(
            "argument of type '{}' is not iterable",
            other.type_name()
        ))),
    }
}

/// `add(item)`: 同じ `(kind, path)` の記述子があれば、出所が食い違わないことを確かめて足さない。
fn add(components: &mut Components<'_>, item: PyDict) -> Result<(), PyException> {
    let item = PyValue::Dict(item);
    for old in components.items()? {
        // `(old['kind'], old['path']) == (item['kind'], item['path'])`: 4 つを先に取り出す。
        let (old_kind, old_path) = (getitem(&old, "kind")?, getitem(&old, "path")?);
        let (kind, path) = (getitem(&item, "kind")?, getitem(&item, "path")?);
        if old_kind.py_eq(&kind) && old_path.py_eq(&path) {
            let entry_differs = get_or_none(&item, "entry")?.py_ne(&get_or_none(&old, "entry")?);
            let plugin_differs = get_or_none(&old, "plugin")?.truthy()
                && get_or_none(&item, "plugin")?.truthy()
                && getitem(&old, "plugin")?.py_ne(&getitem(&item, "plugin")?);
            if entry_differs || plugin_differs {
                return Err(stop("component の出所が曖昧"));
            }
            return Ok(());
        }
    }
    components.append(item)
}

fn strict_dict(raw: &[u8], ctx: PyCtx) -> Result<PyDict, PyException> {
    match strict_settings(raw, ctx)? {
        PyValue::Dict(dict) => Ok(dict),
        _ => unreachable!("strict_settings は dict を返す"),
    }
}

fn blob_key(prefix: &str, path: &[u32]) -> Cps {
    let mut key = lit(prefix);
    key.extend_from_slice(path);
    key
}

fn plugin_item(path: Cps, plugin: Cps, source: PyValue) -> PyDict {
    dict(vec![
        ("kind", text_of("plugin")),
        ("path", text(path)),
        ("plugin", text(plugin)),
        ("source", source),
    ])
}

/// `_component_inventory(specs, inventory, budget, entries, blobs)`。`ctx.depth` は呼び出し側の深さ。
pub fn component_inventory(
    host: &dyn Host,
    specs: &mut PyDict,
    inventory: &[PyValue],
    blobs: &mut Blobs,
    tree: &mut dyn ComponentTree,
    ctx: PyCtx,
) -> Result<(), PyException> {
    let frame = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    // `specs.get('host_paths', {}).get('config')` が最初。
    let config = match specs.get_str("host_paths") {
        None => PyValue::None,
        Some(paths) => get_or_none(paths, "config")?,
    };
    let mut components = Components { specs };
    for item in inventory {
        let install = getitem(item, "installPath")?;
        let install_text = match &install {
            PyValue::Str(s) => s.as_codepoints().to_vec(),
            // 届かない分岐: 旧版は `str()` で文字列にして先へ進むが、ここは `TypeError` にする。
            // `_component_inventory` は `bootstrap`(控えの `expected` が無いとき)からだけ呼ばれ、
            // 一覧は `plugin_inventory` が「文字列の絶対パス」と確かめた物だけなので、実際には起きない。
            _ => {
                return Err(PyException::type_error(
                    "expected str, bytes or os.PathLike object",
                ));
            }
        };
        let path = absolute(host, &install_text)?.to_cps();
        let plugin = match get_or_none(item, "id")? {
            PyValue::Str(id) if !id.as_codepoints().is_empty() => id.as_codepoints().to_vec(),
            _ => return Err(stop("component の plugin ID が不明")),
        };
        if plugin.ends_with(&lit("@skills-dir")) {
            let name = plugin[..plugin.len() - lit("@skills-dir").len()].to_vec();
            if name.is_empty() || name.contains(&0x40) || install_text != path {
                return Err(stop("component の skills-dir ID が不明"));
            }
            let parent = PyPath::new(&path).parent().to_cps();
            let mut candidates = Vec::new();
            for descriptor in components.items()? {
                let kind = getitem(&descriptor, "kind")?;
                let is_skill = as_text(&kind)
                    .is_some_and(|k| k == lit("personal-skill") || k == lit("enterprise-skill"));
                if is_skill && getitem(&descriptor, "path")?.py_eq(&text(parent.clone())) {
                    candidates.push(descriptor);
                }
            }
            if candidates.len() != 1 {
                return Err(stop("component の skills-dir 出所が一意でない"));
            }
            let kind = str_of(&getitem(&candidates[0], "kind")?).expect("str");
            let mut manifest_key = lit("component:");
            manifest_key.extend_from_slice(&kind);
            manifest_key.extend(lit(":"));
            manifest_key.extend_from_slice(&path);
            manifest_key.extend(lit("/.claude-plugin/plugin.json"));
            let Some(raw) = blobs.get(&manifest_key) else {
                return Err(stop(
                    "component の skills-dir manifest が保持出所と一致しない",
                ));
            };
            let manifest = strict_dict(raw, frame)?;
            if manifest
                .get_str("name")
                .cloned()
                .unwrap_or(PyValue::None)
                .py_ne(&text(name))
            {
                return Err(stop(
                    "component の skills-dir manifest が保持出所と一致しない",
                ));
            }
            continue;
        }
        let source = match get_or_none(item, "scope")? {
            PyValue::None if !item.as_dict().is_some_and(|d| d.contains_str("scope")) => {
                text_of("plugin")
            }
            scope => scope,
        };
        add(
            &mut components,
            plugin_item(path.clone(), plugin.clone(), source),
        )?;
        let scope = get_or_none(item, "scope")?;
        if scope.py_eq(&text_of("synced")) || !plugin.contains(&0x40) {
            continue;
        }
        let at = plugin.iter().rposition(|&c| c == 0x40).expect("@ がある");
        let (name, marketplace) = (plugin[..at].to_vec(), plugin[at + 1..].to_vec());
        if name.is_empty() || marketplace.is_empty() || !config.truthy() {
            return Err(stop("component の marketplace 出所が不明"));
        }
        let Some(config_text) = str_of(&config) else {
            return Err(PyException::type_error(&format!(
                "expected str, bytes or os.PathLike object, not {}",
                config.type_name()
            )));
        };
        let registry_key = blob_key(
            "external:",
            &join(&config_text, "plugins/known_marketplaces.json"),
        );
        let Some(raw) = blobs.get(&registry_key) else {
            return Err(stop("component の marketplace registry が無い"));
        };
        let known = strict_dict(raw, frame)?;
        let source_entry = known.get(&text(marketplace.clone()))?.cloned();
        let location_raw = match &source_entry {
            Some(PyValue::Dict(source))
                if source
                    .get_str("installLocation")
                    .is_some_and(|v| as_text(v).is_some_and(isabs)) =>
            {
                str_of(source.get_str("installLocation").expect("確認済み")).expect("str")
            }
            _ => return Err(stop("component の marketplace 出所が不明")),
        };
        let location = absolute(host, &location_raw)?.to_cps();
        let manifest = join(&location, ".claude-plugin/marketplace.json");
        let key = tree.component_tree(&manifest, "marketplace-manifest", blobs)?;
        let Some(manifest_raw) = blobs.get(&key) else {
            return Err(stop("component の marketplace manifest が無い"));
        };
        let data = strict_dict(manifest_raw, frame)?;
        let Some(PyValue::List(definitions)) = data.get_str("plugins") else {
            return Err(stop("component の marketplace entry が不明"));
        };
        let wanted = text(name.clone());
        let matches: Vec<&PyDict> = definitions
            .iter()
            .filter_map(PyValue::as_dict)
            .filter(|entry| {
                entry
                    .get_str("name")
                    .cloned()
                    .unwrap_or(PyValue::None)
                    .py_eq(&wanted)
            })
            .collect();
        if matches.len() != 1 {
            return Err(stop("component の marketplace entry が一意でない"));
        }
        let entry = matches[0].clone();
        let declared = match entry.get_str("source").and_then(as_text) {
            Some(declared)
                if declared.starts_with(&lit("./"))
                    && !declared.split(|&c| c == 0x2f).any(|part| part == lit("..")) =>
            {
                declared.to_vec()
            }
            _ => {
                return Err(stop(
                    "component の marketplace source が root 内相対パスでない",
                ));
            }
        };
        let source_path = PyPath::new(&location).join(&declared).to_cps();
        add(
            &mut components,
            plugin_item(source_path, plugin.clone(), text_of("marketplace-source")),
        )?;
        let selection = PyValue::Dict(dict(vec![
            ("plugin", text(plugin.clone())),
            ("entry", PyValue::Dict(entry)),
        ]));
        // `next((old for old in specs['components'] if old['kind'] == ... and old['path'] == manifest), None)`
        let mut existing = None;
        for (index, old) in components.items()?.iter().enumerate() {
            if getitem(old, "kind")?.py_eq(&text_of("marketplace-manifest"))
                && getitem(old, "path")?.py_eq(&text(manifest.clone()))
            {
                existing = Some((index, old.clone()));
                break;
            }
        }
        match existing {
            None => add(
                &mut components,
                dict(vec![
                    ("kind", text_of("marketplace-manifest")),
                    ("path", text(manifest.clone())),
                    ("source", text(location.clone())),
                    ("entry", PyValue::List(vec![selection])),
                ]),
            )?,
            Some((index, old)) => {
                if !contains(&getitem(&old, "entry")?, &selection)? {
                    components.append_entry(index, selection)?;
                }
            }
        }
        let installed_key = blob_key(
            "external:",
            &join(&config_text, "plugins/installed_plugins.json"),
        );
        let Some(installed) = blobs.get(&installed_key) else {
            return Err(stop("component の installPath registry が無い"));
        };
        let registry = strict_dict(installed, frame)?;
        let candidates = match registry.get_str("plugins") {
            Some(PyValue::Dict(registry)) => registry.get(&text(plugin.clone()))?.cloned(),
            _ => None,
        };
        let Some(PyValue::List(candidates)) = candidates else {
            return Err(stop("component の installPath registry が不明"));
        };
        let wanted_path = text(path.clone());
        let scope_truthy = scope.truthy();
        let matches = candidates
            .iter()
            .filter_map(PyValue::as_dict)
            .filter(|c| {
                c.get_str("installPath")
                    .cloned()
                    .unwrap_or(PyValue::None)
                    .py_eq(&wanted_path)
                    && (!scope_truthy
                        || c.get_str("scope")
                            .cloned()
                            .unwrap_or(PyValue::None)
                            .py_eq(&scope))
            })
            .count();
        if matches != 1 {
            return Err(stop("component の installPath 出所が一意でない"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::super::pyops::as_text;
    use super::super::testing::{fake_host, fixture, from_hex, from_typed, result_json};
    use super::*;
    use serde_json::json;

    /// 固定期待の `component_tree`: 木ごとの鍵と本文。無ければ既定の鍵だけを返す。
    struct FakeTree(PyValue);

    impl ComponentTree for FakeTree {
        fn component_tree(
            &mut self,
            path: &[u32],
            kind: &str,
            blobs: &mut Blobs,
        ) -> Result<Cps, PyException> {
            let table = self.0.as_dict().unwrap();
            let Some(PyValue::Dict(item)) = table.get(&text(path.to_vec()))? else {
                let mut key = lit("component:");
                key.extend(lit(kind));
                key.extend(lit(":"));
                key.extend_from_slice(path);
                return Ok(key);
            };
            let key = as_text(item.get_str("key").unwrap()).unwrap().to_vec();
            if let Some(PyValue::Str(hex)) = item.get_str("blob") {
                blobs.insert(
                    key.clone(),
                    from_hex(&super::super::pystr::to_string(hex.as_codepoints())),
                );
            }
            Ok(key)
        }
    }

    #[test]
    fn blobs_keep_the_first_insertion_position_like_a_python_dict() {
        let (a, b, c) = (lit("a"), lit("b"), lit("c"));
        let mut blobs = Blobs::new();
        assert!(blobs.is_empty());
        blobs.insert(a.clone(), vec![1]);
        blobs.insert(b.clone(), vec![2]);
        blobs.insert(a.clone(), vec![3]);
        // 同じ鍵への代入は、最初の位置のまま値だけが替わる。
        assert_eq!(
            blobs.iter().map(|(k, _)| k.clone()).collect::<Vec<_>>(),
            vec![a.clone(), b.clone()]
        );
        assert_eq!(blobs.get(&a), Some(&vec![3]));
        assert_eq!(blobs.get(&b), Some(&vec![2]));
        assert_eq!(blobs.len(), 2);
        assert!(blobs.contains(&a) && blobs.contains(&b));
        assert_eq!(blobs.get(&c), None);
        assert!(!blobs.contains(&c));
        let collected: Blobs = vec![
            (b.clone(), vec![1]),
            (a.clone(), vec![2]),
            (b.clone(), vec![9]),
        ]
        .into_iter()
        .collect();
        assert_eq!(
            collected
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<Vec<_>>(),
            vec![(b, vec![9]), (a, vec![2])]
        );
    }

    #[test]
    fn component_inventory_cases_match_python_3_10() {
        let cases = fixture!("inventory_cases.json");
        let list = cases.as_array().unwrap();
        assert!(list.len() >= 50, "{}", list.len());
        let (mut successes, mut failures) = (0, 0);
        for case in list {
            let host = fake_host(&json!({"HOME": [47, 104]}), &lit("/"));
            let mut specs = from_typed(&case["specs"]).as_dict().unwrap().clone();
            let inventory = from_typed(&case["inventory"]);
            let mut blobs: Blobs = case["blobs"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (lit(k), from_hex(v.as_str().unwrap())))
                .collect();
            let mut tree = FakeTree(from_typed(&case["trees"]));
            let result = component_inventory(
                &host,
                &mut specs,
                inventory.as_list().unwrap(),
                &mut blobs,
                &mut tree,
                PyCtx::new(3),
            )
            .map(|()| PyValue::Dict(specs));
            let actual = result_json(result);
            successes += usize::from(actual.get("ok").is_some());
            failures += usize::from(actual.get("error").is_some());
            let expect = if case.get("ok").is_some() {
                json!({"ok": case["ok"]})
            } else {
                json!({"error": case["error"]})
            };
            assert_eq!(actual, expect, "{}", case["label"]);
        }
        assert!(successes > 10 && failures > 30, "{successes} {failures}");
    }
}
