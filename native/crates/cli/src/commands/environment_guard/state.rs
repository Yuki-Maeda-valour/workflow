//! 旧版の控え(`environment.json`・`version 3`)の値の扱い: `load_verified` の検査、
//! `_component_descriptors`、`component_policy_digest`、`bootstrap` の控えと受領の組み立て。
//! ファイルの読取りは差し込み口(読み取ったバイト列を返す関数)で受け取る。
use super::error::PyException;
use super::pyjson::{DumpOptions, PyCtx, dump_state, dumps, load_state};
use super::pyops::{
    Cps, as_text, dict, get_attr_dict, in_str_set, index_zero, iterate, lit, sha_hex, slice_to,
    starts_with, text, text_of,
};
use super::pypath::{PyPath, isabs};
use super::pyvalue::{PyDict, PyValue};

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

pub const COMPONENT_KINDS: [&str; 8] = [
    "personal-skill",
    "enterprise-skill",
    "personal-command",
    "enterprise-command",
    "personal-agent",
    "enterprise-agent",
    "marketplace-manifest",
    "plugin",
];

/// `load_verified(state, expected)`。`expected` は外部に保持した SHA-256、`read` は控えの読取り
/// (`read_regular(state)`)の差し込み口。形式の検査は、読む前に行う(旧版の順)。
pub fn load_verified(
    expected: &[u32],
    read: impl FnOnce() -> Result<Vec<u8>, PyException>,
    ctx: PyCtx,
) -> Result<PyValue, PyException> {
    let hex = |c: &u32| (0x30..=0x39).contains(c) || (0x61..=0x66).contains(c);
    if expected.len() != 64 || !expected.iter().all(hex) {
        return Err(stop("外部に保持した SHA-256 が必要"));
    }
    let raw = read()?;
    if lit(&sha_hex(&raw)) != expected {
        return Err(stop("環境の控えが保持値と一致しない"));
    }
    let frame = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    let value = load_state(&raw, frame)?;
    let version_ok = value.as_dict().is_some_and(|d| {
        d.get_str("version")
            .is_some_and(|v| v.py_eq(&PyValue::from(3)))
    });
    if !version_ok {
        return Err(stop("環境の控えの形式が違う"));
    }
    Ok(value)
}

/// `_component_descriptors(value)`。
pub fn component_descriptors(value: &PyValue) -> Result<Vec<PyValue>, PyException> {
    let value = get_attr_dict(value)?;
    let specs = value
        .get_str("specs")
        .cloned()
        .unwrap_or_else(|| PyValue::Dict(PyDict::new()));
    let components = get_attr_dict(&specs)?.get_str("components").cloned();
    let Some(PyValue::List(components)) = components.filter(PyValue::truthy) else {
        return Err(stop("component の保持記述子が無い"));
    };
    let mut seen: Vec<(Cps, Cps)> = Vec::new();
    for item in &components {
        let invalid = || stop("component の記述子が不正");
        let PyValue::Dict(dict) = item else {
            return Err(invalid());
        };
        let kind = dict.get_str("kind").cloned().unwrap_or(PyValue::None);
        if !in_str_set(&kind, &COMPONENT_KINDS)? {
            return Err(invalid());
        }
        let path = dict.get_str("path").and_then(as_text);
        let Some(path) = path.filter(|p| isabs(p)) else {
            return Err(invalid());
        };
        let pair = (as_text(&kind).expect("str").to_vec(), path.to_vec());
        if seen.contains(&pair) {
            return Err(stop("component の記述子が重複している"));
        }
        seen.push(pair);
    }
    Ok(components)
}

fn item_dict(item: &PyValue) -> &PyDict {
    item.as_dict().expect("検査済みの記述子")
}

fn item_text(item: &PyValue, key: &str) -> Cps {
    as_text(item_dict(item).get_str(key).expect("検査済み"))
        .expect("str")
        .to_vec()
}

/// 控えの `inventory` の各項目から `{key: item[key] for key in (...) if key in item}` を作る。
fn active_sources(inventory: &PyValue) -> Result<Vec<PyValue>, PyException> {
    let mut result = Vec::new();
    for item in iterate(inventory)? {
        let mut picked = PyDict::new();
        for key in ["id", "installPath", "scope", "version"] {
            let present = match &item {
                PyValue::Dict(d) => d.contains_str(key),
                PyValue::Str(s) => s
                    .as_codepoints()
                    .windows(key.len())
                    .any(|w| w == lit(key).as_slice()),
                PyValue::List(list) => list.iter().any(|x| x.eq_in_container(&text_of(key))),
                PyValue::Bytes(_) => {
                    return Err(PyException::type_error(
                        "a bytes-like object is required, not 'str'",
                    ));
                }
                other => {
                    return Err(PyException::type_error(&format!(
                        "argument of type '{}' is not iterable",
                        other.type_name()
                    )));
                }
            };
            if !present {
                continue;
            }
            match &item {
                PyValue::Dict(d) => picked.insert_str(key, d.get_str(key).cloned().expect("ある")),
                PyValue::Str(_) => {
                    return Err(PyException::type_error("string indices must be integers"));
                }
                _ => {
                    return Err(PyException::type_error(
                        "list indices must be integers or slices, not str",
                    ));
                }
            }
        }
        result.push(PyValue::Dict(picked));
    }
    Ok(result)
}

/// `component_policy_digest(state, expected)` の、控えを読んだ後。控えの値だけで決まる。
pub fn component_policy_digest(value: &PyValue, ctx: PyCtx) -> Result<String, PyException> {
    let components = component_descriptors(value)?;
    let workflow: Vec<Cps> = components
        .iter()
        .filter(|item| {
            item_dict(item)
                .get_str("source")
                .is_some_and(|s| s.py_eq(&text_of("workflow")))
        })
        .map(|item| item_text(item, "path"))
        .collect();
    if workflow.len() != 1 {
        return Err(stop("配布物の component 記述子が一意でない"));
    }
    let mut rootkey = lit("component:plugin:");
    rootkey.extend_from_slice(&workflow[0]);
    let root_routes: Vec<Cps> = components
        .iter()
        .map(|item| {
            let mut route = lit("@route:component:");
            route.extend(item_text(item, "kind"));
            route.extend(lit(":"));
            route.extend(item_text(item, "path"));
            route.extend(lit(":"));
            route
        })
        .collect();
    let logical = |key: &[u32]| -> Cps {
        for prefix in ["", "@names:", "@link:"] {
            let mut start = lit(prefix);
            start.extend_from_slice(&rootkey);
            let mut start_slash = start.clone();
            start_slash.push(0x2f);
            if key == start || key.starts_with(&start_slash) {
                let mut result = lit(prefix);
                result.extend(lit("component:plugin:@workflow"));
                result.extend_from_slice(&key[start.len()..]);
                return result;
            }
        }
        key.to_vec()
    };
    let root = get_attr_dict(value)?;
    let entries = match root.get_str("entries") {
        Some(PyValue::Dict(entries)) => entries,
        Some(other) => {
            return Err(PyException::new(
                "AttributeError",
                &format!("'{}' object has no attribute 'items'", other.type_name()),
            ));
        }
        None => return Err(PyException::new("KeyError", "'entries'")),
    };
    let mut kept = PyDict::new();
    let mut root_slash = lit("@route:");
    root_slash.extend_from_slice(&rootkey);
    root_slash.push(0x2f);
    for (key, entry) in entries.iter() {
        let key = as_text(key).expect("保持値のキーは文字列");
        if starts_with(key, "component:") || starts_with(key, "@names:component:") {
            kept.insert(text(logical(key)), entry.clone())?;
        } else if starts_with(key, "@link:component:") {
            kept.insert(text(logical(key)), slice_to(entry, 2)?)?;
        } else if starts_with(key, "@route:component:") {
            if root_routes.iter().any(|route| key.starts_with(route))
                || key.starts_with(&root_slash)
            {
                continue;
            }
            let count = if index_zero(entry)?.py_eq(&text_of("link-step")) {
                3
            } else {
                2
            };
            kept.insert(text(key.to_vec()), slice_to(entry, count)?)?;
        }
    }
    let descriptors: Vec<PyValue> = components
        .iter()
        .map(|item| {
            let mut copy = item_dict(item).clone();
            if copy
                .get_str("source")
                .is_some_and(|s| s.py_eq(&text_of("workflow")))
            {
                copy.insert_str("path", text_of("@workflow"));
            }
            PyValue::Dict(copy)
        })
        .collect();
    let inventory = root
        .get_str("inventory")
        .cloned()
        .unwrap_or_else(|| PyValue::List(vec![]));
    let active = active_sources(&inventory)?;
    let digest_value = PyValue::Dict(dict(vec![
        ("descriptors", PyValue::List(descriptors)),
        ("entries", PyValue::Dict(kept)),
        ("active_sources", PyValue::List(active)),
    ]));
    Ok(sha_hex(&dump_state(
        &digest_value,
        PyCtx {
            depth: ctx.depth + 1,
            ..ctx
        },
    )?))
}

/// `bootstrap` が作る控えの値(`version 3` の各欄)。`specs` は `host_settings` を設定済みのもの。
pub struct StateParts<'a> {
    pub root: &'a PyPath,
    pub output: &'a PyPath,
    pub specs: &'a PyDict,
    pub inventory: Vec<PyValue>,
    pub entries: PyDict,
    pub guard_sha256: &'a str,
}

pub fn state_value(parts: StateParts<'_>) -> PyDict {
    dict(vec![
        ("version", PyValue::from(3)),
        ("root", text(parts.root.to_cps())),
        ("copy", text(parts.output.join(&lit("plugin")).to_cps())),
        ("specs", PyValue::Dict(parts.specs.clone())),
        (
            "host_settings",
            parts
                .specs
                .get_str("host_settings")
                .cloned()
                .unwrap_or(PyValue::None),
        ),
        ("inventory", PyValue::List(parts.inventory)),
        ("entries", PyValue::Dict(parts.entries)),
        ("guard_sha256", text_of(parts.guard_sha256)),
    ])
}

/// `dump(value)`: 控えのバイト列。
pub fn dump_state_bytes(value: &PyDict, ctx: PyCtx) -> Result<Vec<u8>, PyException> {
    dump_state(&PyValue::Dict(value.clone()), ctx)
}

/// 受領(標準出力の JSON 1 行。改行は `print` が足す)。
pub fn receipt(
    output: &PyPath,
    raw: &[u8],
    guard_sha256: &str,
    ctx: PyCtx,
) -> Result<Cps, PyException> {
    let value = PyValue::Dict(dict(vec![
        (
            "state",
            text(output.join(&lit("environment.json")).to_cps()),
        ),
        ("sha256", text_of(&sha_hex(raw))),
        (
            "guard",
            text(output.join(&lit("environment-guard.py")).to_cps()),
        ),
        ("guard_sha256", text_of(guard_sha256)),
        ("plugin", text(output.join(&lit("plugin")).to_cps())),
    ]));
    dumps(&value, DumpOptions::RECEIPT, ctx)
}

#[cfg(test)]
mod tests {
    use super::super::pystr::to_string;
    use super::super::settings::host_settings_specs;
    use super::super::specs::{BootstrapInput, bootstrap_specs};
    use super::super::testing::{
        cps_of, fake_host, fixture, from_hex, from_typed, not_found, result_json,
    };
    use super::*;
    use serde_json::json;

    fn expect_of(case: &serde_json::Value) -> serde_json::Value {
        if case.get("ok").is_some() {
            json!({"ok": case["ok"]})
        } else {
            json!({"error": case["error"]})
        }
    }

    #[test]
    fn load_verified_cases_match_python_3_10() {
        let cases = fixture!("state_cases.json");
        let list = cases["load"].as_array().unwrap();
        assert!(list.len() >= 20);
        for case in list {
            let expected = match from_typed(&case["expected"]) {
                PyValue::Str(s) => s.as_codepoints().to_vec(),
                // `expected or ''`: str でない値は空として扱われ、形式の誤りになる。
                _ => Vec::new(),
            };
            let raw = case["raw"].as_str().map(from_hex);
            let result = load_verified(
                &expected,
                || raw.ok_or_else(|| not_found(&lit("s"))),
                PyCtx::new(3),
            );
            assert_eq!(result_json(result), expect_of(case), "{}", case["label"]);
        }
    }

    #[test]
    fn component_digest_and_descriptors_match_python_3_10() {
        let cases = fixture!("state_cases.json");
        let list = cases["digest"].as_array().unwrap();
        assert!(list.len() >= 45, "{}", list.len());
        let mut digests = 0;
        for case in list {
            let value = from_typed(&case["value"]);
            let digest = component_policy_digest(&value, PyCtx::new(3)).map(|hex| text_of(&hex));
            let actual = result_json(digest);
            digests += usize::from(actual.get("ok").is_some());
            assert_eq!(
                actual,
                expect_of(&case["digest"]),
                "digest {}",
                case["label"]
            );
            let descriptors = component_descriptors(&value).map(PyValue::List);
            assert_eq!(
                result_json(descriptors),
                expect_of(&case["descriptors"]),
                "descriptors {}",
                case["label"]
            );
        }
        assert!(digests >= 10, "{digests}");
    }

    #[test]
    fn digest_depth_cases_match_python_3_10() {
        // `dump(value)` は Python の関数なので、`json.dumps` はその 1 段下。呼び出し側の深さの境界で比べる。
        let cases = fixture!("state_cases.json");
        let list = cases["digest_depth"]["cases"].as_array().unwrap();
        assert!(list.len() >= 15, "{}", list.len());
        let (mut ok, mut failed) = (0, 0);
        for case in list {
            // 基本の控えの entries に、複数キーの dict を葉にした k 段の list を足す。
            let mut value = from_typed(&cases["digest_depth"]["base"]);
            let mut leaf =
                PyValue::Dict(dict(vec![("a", PyValue::from(1)), ("b", PyValue::from(2))]));
            for _ in 0..case["k"].as_u64().unwrap() {
                leaf = PyValue::List(vec![leaf]);
            }
            let PyValue::Dict(root) = &mut value else {
                unreachable!()
            };
            let mut entries = root.get_str("entries").unwrap().as_dict().unwrap().clone();
            entries.insert_str("component:plugin:/dist/plugin/deep", leaf);
            root.insert_str("entries", PyValue::Dict(entries));
            let site = case["site"].as_u64().unwrap() as usize;
            let result = component_policy_digest(&value, PyCtx::new(site)).map(|hex| text_of(&hex));
            let actual = result_json(result);
            ok += usize::from(actual.get("ok").is_some());
            failed += usize::from(actual.get("error").is_some());
            assert_eq!(actual, expect_of(case), "k={} site={site}", case["k"]);
        }
        assert!(ok >= 3 && failed >= 3, "{ok} {failed}");
    }

    #[test]
    fn bootstrap_record_and_receipt_match_python_3_10() {
        let cases = fixture!("state_cases.json");
        let list = cases["bootstrap"].as_array().unwrap();
        assert!(list.len() >= 6);
        let (mut successes, mut failures) = (0, 0);
        for case in list {
            let host = fake_host(&json!({"HOME": [47, 104]}), &lit("/"));
            let arg = |value: &serde_json::Value| match value["t"].as_str().unwrap() {
                "empty" => super::super::args::ArgValue::EmptyList,
                _ => super::super::args::ArgValue::Str(
                    dev_workflow_core::loop_state::CodepointString::try_from_codepoints(cps_of(
                        &value["v"],
                    ))
                    .unwrap(),
                ),
            };
            let many = |name: &str| -> Vec<_> {
                case["args"][name]["v"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(arg)
                    .collect()
            };
            let input = BootstrapInput {
                root: arg(&case["args"]["root"]),
                output: super::super::args::ArgValue::Str(
                    dev_workflow_core::loop_state::CodepointString::from("/O/out"),
                ),
                setting: many("setting"),
                config: many("config"),
                shell: many("shell"),
                skills_dir: many("skills_dir"),
                settings_dir: many("settings_dir"),
                managed_dir: many("managed_dir"),
            };
            let built = bootstrap_specs(&host, &input);
            let entries = from_typed(&case["entries"]);
            let assembled = built.and_then(|mut built| {
                let hosts = host_settings_specs(&built.specs, entries.as_dict().unwrap())?;
                built.specs.insert_str("host_settings", hosts);
                Ok(built)
            });
            match (assembled, case.get("ok")) {
                (Ok(built), Some(ok)) => {
                    successes += 1;
                    let guard_sha = ok["guard_sha256"].as_str().unwrap();
                    let value = state_value(StateParts {
                        root: &built.root,
                        output: &built.output,
                        specs: &built.specs,
                        inventory: vec![],
                        entries: entries.as_dict().unwrap().clone(),
                        guard_sha256: guard_sha,
                    });
                    let raw = dump_state_bytes(&value, PyCtx::new(3)).unwrap();
                    assert_eq!(
                        String::from_utf8(raw.clone()).unwrap(),
                        ok["raw"].as_str().unwrap(),
                        "{}",
                        case["args"]
                    );
                    let line =
                        to_string(&receipt(&built.output, &raw, guard_sha, PyCtx::new(3)).unwrap());
                    let expect = ok["receipt"].as_str().unwrap().to_owned();
                    assert_eq!(format!("{line}\n"), expect, "{}", case["args"]);
                }
                (Err(error), None) => {
                    failures += 1;
                    assert_eq!(
                        super::super::testing::error_of(&error),
                        case["error"],
                        "{}",
                        case["args"]
                    );
                }
                (other, _) => panic!("{}: {:?}", case["args"], other.map(|_| ())),
            }
        }
        assert!(successes >= 4 && failures >= 2, "{successes} {failures}");
    }
}
