//! 旧版の設定の検査(`check_host_setting`・`host_settings_specs`・`read_host_settings` の後半)と、
//! `host-allowlist` の規則(`split_rules`・`bash_rule`・並べ方・`unused=` の行)。
//! 設定ファイルの読直し(`record`)と予算(`tick`)は `SettingsObserver` で受け取る。
use super::args::ArgValue;
use super::error::PyException;
use super::pyjson::{Call, DumpOptions, PyCtx, dump_state, dumps, enter_call, strict_settings};
use super::pyops::{
    Cps, as_text, dict, ends_with, lit, sha_hex, slice_to, starts_with, text, text_of,
};
use super::pypath::{PyPath, isabs};
use super::pystr::{is_space, replace_controls, split_whitespace, strip, truncate};
use super::pyvalue::{PyDict, PyValue};

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

const FORBIDDEN_SETTINGS_ENV: [&str; 6] = [
    "CLAUDE_CONFIG_DIR",
    "HOME",
    "XDG_CONFIG_HOME",
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_SAFE_MODE",
    "CLAUDE_CODE_SHELL_PREFIX",
];

const SOURCE_ENV: [&str; 3] = [
    "CLAUDE_CODE_REMOTE_SETTINGS_PATH",
    "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    "CLAUDE_CODE_MOCK_REMOTE_SETTINGS",
];

fn is_one_of(key: &PyValue, names: &[&str]) -> bool {
    as_text(key).is_some_and(|k| names.iter().any(|n| lit(n) == k))
}

/// `check_host_setting(value, managed)`。
pub fn check_host_setting(value: &PyDict, managed: bool) -> Result<(), PyException> {
    check_host_setting_at(value, managed, None)
}

/// 呼び出し側の深さ(`caller_depth`)があるとき、旧版の関数のフレームと、中の呼出し(`env.items()`・`value.get`)が
/// 使う再帰の深さも数える。
pub fn check_host_setting_at(
    value: &PyDict,
    managed: bool,
    caller_depth: Option<usize>,
) -> Result<(), PyException> {
    if let Some(depth) = caller_depth {
        enter_call(depth + 1, Call::Frame)?;
    }
    let native = |extra: usize| -> Result<(), PyException> {
        match caller_depth {
            Some(depth) => enter_call(depth + 1 + extra, Call::Native),
            None => Ok(()),
        }
    };
    if let Some(env) = value.get_str("env") {
        let PyValue::Dict(env) = env else {
            return Err(stop("host-settings"));
        };
        native(1)?;
        for (key, item) in env.iter() {
            let forbidden = is_one_of(key, &FORBIDDEN_SETTINGS_ENV);
            if forbidden || is_one_of(key, &SOURCE_ENV) {
                return Err(stop(if forbidden {
                    "host-settings"
                } else {
                    "host-config-source"
                }));
            }
            native(1)?;
            if !item.is_str() {
                return Err(stop("host-settings"));
            }
        }
    }
    for key in ["policyHelper", "policyHelpers"] {
        if value.contains_str(key) {
            return Err(stop("host-settings"));
        }
    }
    native(1)?;
    let is_true = |key: &str| matches!(value.get_str(key), Some(PyValue::Bool(true)));
    if is_true("disableAllHooks") {
        return Err(stop("hooks-disabled"));
    }
    if managed {
        native(1)?;
        if is_true("allowManagedHooksOnly") {
            return Err(stop("hooks-disabled"));
        }
    }
    Ok(())
}

/// `held[:1] != ['file']`。
fn is_file_entry(held: &PyValue) -> Result<bool, PyException> {
    let head = slice_to(held, 1)?;
    Ok(head.py_eq(&PyValue::List(vec![text_of("file")])))
}

/// `host_settings_specs(specs, entries)`: 保持済み entries からホストが読む設定の記述子を作る。
pub fn host_settings_specs(specs: &PyDict, entries: &PyDict) -> Result<PyValue, PyException> {
    let paths = match specs.get_str("host_paths") {
        Some(PyValue::Dict(paths))
            if ["user_settings", "cache_settings"]
                .iter()
                .all(|k| paths.get_str(k).is_some_and(PyValue::is_str)) =>
        {
            paths
        }
        _ => return Err(stop("host-settings")),
    };
    let roots: Vec<Cps> = match specs.get_str("managed_roots") {
        Some(PyValue::List(roots)) => {
            let mut result = Vec::new();
            for root in roots {
                match as_text(root) {
                    Some(root) if isabs(root) => result.push(root.to_vec()),
                    _ => return Err(stop("host-settings")),
                }
            }
            result
        }
        _ => return Err(stop("host-settings")),
    };
    let item = |path: Cps, kind: &str| {
        PyValue::Dict(dict(vec![("path", text(path)), ("kind", text_of(kind))]))
    };
    let path_of = |key: &str| {
        as_text(paths.get_str(key).expect("確認済み"))
            .expect("str")
            .to_vec()
    };
    let mut result = vec![
        item(path_of("user_settings"), "user"),
        item(path_of("cache_settings"), "cache"),
    ];
    for root in roots {
        let base = PyPath::new(&root);
        result.push(item(
            base.join(&lit("managed-settings.json")).to_cps(),
            "managed",
        ));
        let directory = base.join(&lit("managed-settings.d")).to_cps();
        let mut prefix = lit("external:");
        prefix.extend_from_slice(&directory);
        prefix.push(0x2f);
        let mut names: Vec<Cps> = Vec::new();
        for (key, held) in entries.iter() {
            let key = as_text(key).expect("保持値のキーは文字列");
            if !(key.len() >= prefix.len() && key[..prefix.len()] == prefix[..])
                || !is_file_entry(held)?
            {
                continue;
            }
            let name = &key[prefix.len()..];
            if !name.contains(&0x2f) && name.first() != Some(&0x2e) && ends_with(name, ".json") {
                names.push(name.to_vec());
            }
        }
        names.sort();
        for name in names {
            result.push(item(
                PyPath::new(&directory).join(&name).to_cps(),
                "managed",
            ));
        }
    }
    Ok(PyValue::List(result))
}

/// `read_host_settings` が差し込む、設定ファイルの読直しと予算。
pub trait SettingsObserver {
    /// `budget.tick()`。
    fn tick(&mut self) -> Result<(), PyException>;
    /// `record(path, key, budget, local, expected)`: 設定本文を読み直し、`local` へ記録する。不在は `None`。
    fn record(
        &mut self,
        path: &[u32],
        key: &[u32],
        local: &mut PyDict,
        expected: &PyDict,
    ) -> Result<Option<Vec<u8>>, PyException>;
}

/// `related(table, key)`: この設定本文と、その最終 symlink の連鎖の鍵。
fn related(table: &PyDict, key: &[u32]) -> Vec<Cps> {
    let mut result = vec![key.to_vec()];
    for name in table.keys() {
        let name = as_text(name).expect("保持値のキーは文字列");
        if starts_with(name, "@file-link:") {
            // `name.split(':', 2)[-1]`: 最初の 2 つの `:` より後ろ。
            let mut parts = name.splitn(3, |&c| c == 0x3a);
            let last = parts.nth(2).unwrap_or(name);
            if last == key {
                result.push(name.to_vec());
            }
        }
    }
    result.sort();
    result.dedup();
    result
}

/// 辞書でない `table` に対する `related`(list は要素、str は 1 文字ずつ。`startswith` が無い要素は例外)。
fn related_value(table: &PyValue, key: &[u32]) -> Result<Vec<Cps>, PyException> {
    let mut result = vec![key.to_vec()];
    for name in super::pyops::iterate(table)? {
        let Some(name) = as_text(&name) else {
            return Err(PyException::new(
                "AttributeError",
                &format!(
                    "'{}' object has no attribute 'startswith'",
                    name.type_name()
                ),
            ));
        };
        if starts_with(name, "@file-link:")
            && name.splitn(3, |&c| c == 0x3a).nth(2).unwrap_or(name) == key
        {
            result.push(name.to_vec());
        }
    }
    result.sort();
    result.dedup();
    Ok(result)
}

/// 設定 1 件を読む(旧版の内側の関数 `load`)。
fn load(
    item: &PyValue,
    entries_value: &PyValue,
    observer: &mut dyn SettingsObserver,
    ctx: PyCtx,
) -> Result<(String, Cps, Option<PyDict>), PyException> {
    // `load` のフレームと、中の呼出しが使う再帰の深さ(旧版の呼出しごとに数える)。`ctx.depth` は `load` のフレーム。
    let l = ctx.depth;
    enter_call(l, Call::Frame)?;
    enter_call(l + 1, Call::Native)?;
    let valid = item.as_dict().filter(|d| {
        d.len() == 2
            && d.contains_str("path")
            && d.contains_str("kind")
            && d.get_str("path").is_some_and(PyValue::is_str)
    });
    let Some(item) = valid else {
        return Err(stop("host-settings"));
    };
    let path = as_text(item.get_str("path").expect("確認済み"))
        .expect("str")
        .to_vec();
    let kind_value = item.get_str("kind").expect("確認済み");
    let Some(kind) = ["user", "managed", "cache"]
        .into_iter()
        .find(|k| as_text(kind_value).is_some_and(|v| lit(k) == v))
    else {
        return Err(stop("host-settings"));
    };
    let mut key = lit("external:");
    key.extend_from_slice(&path);
    // `key not in entries`: dict は鍵、list は等しい要素、str は部分文字列で調べる。
    let present = match entries_value {
        PyValue::Dict(entries) => entries.get(&text(key.clone()))?.is_some(),
        PyValue::List(list) => list.iter().any(|x| x.eq_in_container(&text(key.clone()))),
        PyValue::Str(s) => s
            .as_codepoints()
            .windows(key.len())
            .any(|window| window == key.as_slice()),
        PyValue::Bytes(_) => {
            return Err(PyException::type_error(
                "a bytes-like object is required, not 'str'",
            ));
        }
        other => return Err(not_iterable_arg(other)),
    };
    if !present {
        return Err(stop("host-settings"));
    }
    let mut local = PyDict::new();
    // 辞書でない `entries` が鍵を含むのは、保持値が壊れたときだけ。`record` までは旧版と同じに進み、
    // 照合の後の `entries.get` で `AttributeError` になる。
    // 実物の流れでは届かない: 呼び出し側の `verify` が `entries` を `snapshot` の dict と比べるので、
    // 辞書でない `entries` はそこで拒否される。旧版との順だけを固定期待で確かめている。
    let empty = PyDict::new();
    let expected = match entries_value {
        PyValue::Dict(entries) => entries,
        _ => &empty,
    };
    enter_call(l + 1, Call::Frame)?;
    let raw = observer.record(&path, &key, &mut local, expected)?;
    // `related(table, key)` のフレームと、表が空でなければ最初の `name.startswith`。
    let related_calls = |nonempty: bool| -> Result<(), PyException> {
        enter_call(l + 1, Call::Frame)?;
        if nonempty {
            enter_call(l + 2, Call::Native)?;
        }
        Ok(())
    };
    related_calls(!matches!(entries_value, PyValue::Dict(d) if d.is_empty()))?;
    let held_keys = match entries_value {
        PyValue::Dict(entries) => related(entries, &key),
        other => related_value(other, &key)?,
    };
    related_calls(!local.is_empty())?;
    let local_keys = related(&local, &key);
    if held_keys != local_keys {
        return Err(stop("host-settings"));
    }
    let PyValue::Dict(entries) = entries_value else {
        return Err(PyException::new(
            "AttributeError",
            &format!(
                "'{}' object has no attribute 'get'",
                entries_value.type_name()
            ),
        ));
    };
    enter_call(l + 1, Call::Native)?;
    for local_key in &local_keys {
        let local_key = text(local_key.clone());
        let held = entries.get(&local_key)?.cloned().unwrap_or(PyValue::None);
        let mine = local.get(&local_key)?.cloned().unwrap_or(PyValue::None);
        if held.py_ne(&mine) {
            return Err(stop("host-settings"));
        }
    }
    let Some(raw) = raw else {
        return Ok((kind.to_owned(), path, None));
    };
    // `budget.tick()`: メソッドのフレームと、その中の `time.monotonic()`。
    let tick = |observer: &mut dyn SettingsObserver| -> Result<(), PyException> {
        enter_call(l + 1, Call::Frame)?;
        enter_call(l + 2, Call::Native)?;
        observer.tick()
    };
    tick(observer)?;
    let parsed = strict_settings(&raw, ctx)?;
    tick(observer)?;
    let PyValue::Dict(parsed) = parsed else {
        unreachable!("strict_settings は dict を返す")
    };
    check_host_setting_at(&parsed, kind != "user", Some(l))?;
    tick(observer)?;
    Ok((kind.to_owned(), path, Some(parsed)))
}

/// `read_host_settings` の、`verify()` より後ろ。`value` は照合済みの控え、`ctx.depth` は呼び出し側の深さ。
pub fn read_host_settings(
    value: &PyValue,
    observer: &mut dyn SettingsObserver,
    ctx: PyCtx,
) -> Result<PyDict, PyException> {
    let frame = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    // 旧版の最初の呼出し: `verify(...)` のフレーム、`value.get`・`isinstance`、`Budget()`(型の呼出し・`__init__`・
    // `time.monotonic()`)。`verify` の中身は呼び出し側(段階 2b)の責任で、ここは呼出しのフレームだけを数える。
    let r = frame.depth;
    enter_call(r, Call::Frame)?;
    enter_call(r + 1, Call::Frame)?;
    enter_call(r + 1, Call::Native)?;
    let value = super::pyops::get_attr_dict(value)?;
    let Some(PyValue::List(desc)) = value.get_str("host_settings") else {
        return Err(stop("host-settings"));
    };
    // `entries = value['entries']`(`load` が使うまで、中身は確かめない)。
    let entries = match value.get_str("entries") {
        Some(entries) => entries.clone(),
        None => return Err(PyException::new("KeyError", "'entries'")),
    };
    enter_call(r + 1, Call::Native)?;
    enter_call(r + 2, Call::Frame)?;
    enter_call(r + 3, Call::Native)?;
    let inner = PyCtx {
        depth: frame.depth + 1,
        ..frame
    };
    let mut user = PyDict::new();
    let mut cache = PyValue::None;
    let mut cache_seen = false;
    let mut managed = Vec::new();
    let mut checked: i64 = 0;
    let mut sources = Vec::new();
    let mut user_path: Option<Cps> = None;
    for item in desc {
        let (kind, path, loaded) = load(item, &entries, observer, inner)?;
        checked += 1;
        let drop_in =
            kind == "managed" && PyPath::new(&path).parent().name() == lit("managed-settings.d");
        let source_kind = if drop_in {
            "managed-drop-in"
        } else {
            kind.as_str()
        };
        let dumped = dump_state(
            &PyValue::List(vec![text_of(source_kind), text(path.clone())]),
            frame,
        )?;
        // `sha(raw)` のフレームと、`hashlib.sha256`・`hexdigest` の呼出し。
        enter_call(r + 1, Call::Frame)?;
        enter_call(r + 2, Call::Native)?;
        let digest = sha_hex(&dumped);
        sources.push(PyValue::Dict(dict(vec![
            ("kind", text_of(source_kind)),
            ("source", text_of(&digest)),
            ("present", PyValue::Bool(loaded.is_some())),
            (
                "settings",
                loaded.clone().map_or(PyValue::None, PyValue::Dict),
            ),
        ])));
        match kind.as_str() {
            "user" => {
                if user_path.is_some() {
                    return Err(stop("host-settings"));
                }
                user_path = Some(path);
                // `value or {}`
                user = loaded.unwrap_or_default();
            }
            "cache" => {
                if cache_seen {
                    return Err(stop("host-settings"));
                }
                cache_seen = true;
                cache = loaded.map_or(PyValue::None, PyValue::Dict);
            }
            _ => {
                if let Some(loaded) = loaded {
                    managed.push(PyValue::Dict(loaded));
                }
            }
        }
    }
    let Some(user_path) = user_path else {
        return Err(stop("host-settings"));
    };
    Ok(dict(vec![
        ("user_settings", text(user_path)),
        ("user", PyValue::Dict(user)),
        ("cache", cache),
        ("managed", PyValue::List(managed)),
        ("checked", PyValue::from(checked)),
        ("permission_sources", PyValue::List(sources)),
    ]))
}

fn not_iterable_arg(value: &PyValue) -> PyException {
    PyException::type_error(&format!(
        "argument of type '{}' is not iterable",
        value.type_name()
    ))
}

/// `host-settings` の標準出力の 1 行目(`{user_settings, checked}`)。
pub fn host_settings_output(result: &PyDict, ctx: PyCtx) -> Result<Cps, PyException> {
    let value = PyValue::Dict(dict(vec![
        (
            "user_settings",
            result
                .get_str("user_settings")
                .cloned()
                .unwrap_or(PyValue::None),
        ),
        (
            "checked",
            result.get_str("checked").cloned().unwrap_or(PyValue::None),
        ),
    ]));
    dumps(&value, DumpOptions::HOST, ctx)
}

// ---------------------------------------------------------------- host-allowlist

/// `split_rules(value)`。
pub fn split_rules(value: &[u32]) -> Vec<Cps> {
    let (mut rules, mut current, mut depth) = (Vec::new(), Vec::new(), 0usize);
    for &ch in value {
        if ch == 0x28 {
            depth += 1;
        } else if ch == 0x29 {
            depth = depth.saturating_sub(1);
        }
        if depth == 0 && (ch == 0x2c || is_space(ch)) {
            if !current.is_empty() {
                rules.push(std::mem::take(&mut current));
            }
        } else {
            current.push(ch);
        }
    }
    if !current.is_empty() {
        rules.push(current);
    }
    rules
}

#[derive(Debug, Eq, PartialEq)]
pub enum BashRule {
    /// 旧版の `'skip'`(Bash の規則でない)。
    Skip,
    /// 旧版の `None`(使えない規則)。
    Unused,
    Rule {
        kind: &'static str,
        words: Vec<Cps>,
    },
}

/// `bash_rule(rule)`。
pub fn bash_rule(rule: &[u32]) -> BashRule {
    if rule == lit("Bash") {
        return BashRule::Rule {
            kind: "all",
            words: vec![],
        };
    }
    if !(starts_with(rule, "Bash(") && ends_with(rule, ")")) {
        return BashRule::Skip;
    }
    let inner_raw = if rule.len() >= 6 {
        &rule[5..rule.len() - 1]
    } else {
        &[][..]
    };
    let inner = strip(inner_raw);
    if inner.is_empty() || inner == lit("*") {
        return BashRule::Unused;
    }
    let star = 0x2a;
    let prefix = |cut: usize| -> BashRule {
        let head = &inner[..inner.len() - cut];
        let words = split_whitespace(head);
        if words.is_empty() {
            BashRule::Unused
        } else {
            BashRule::Rule {
                kind: "prefix",
                words,
            }
        }
    };
    if ends_with(inner, ":*") && !inner[..inner.len() - 2].contains(&star) {
        return prefix(2);
    }
    if ends_with(inner, " *") && !inner[..inner.len() - 2].contains(&star) {
        return prefix(2);
    }
    if inner.contains(&star) {
        BashRule::Unused
    } else {
        BashRule::Rule {
            kind: "exact",
            words: split_whitespace(inner),
        }
    }
}

/// `host_allowlist` の、設定を読んだ後。`settings` は `read_host_settings` の戻り値。
/// 戻り値は、規則(dict の並び)と、`unused` の項目。
pub fn host_allowlist_rules(
    settings: &PyDict,
    values: &[ArgValue],
) -> Result<(Vec<PyValue>, Vec<Cps>), PyException> {
    let mut sources: Vec<(&str, Cps)> = Vec::new();
    for value in values {
        // `[]`(`--allowed-tools=--`)は、反復しても何も出ない。
        if let ArgValue::Str(value) = value {
            for rule in split_rules(value.as_codepoints()) {
                sources.push(("--allowed-tools", rule));
            }
        }
    }
    let user = settings.get_str("user");
    let permissions = match user {
        Some(PyValue::Dict(user)) => user.get_str("permissions").cloned(),
        _ => return Err(PyException::new("KeyError", "'user'")),
    };
    let allow = match &permissions {
        Some(PyValue::Dict(permissions)) => permissions.get_str("allow").cloned(),
        _ => Some(PyValue::List(vec![])),
    };
    if let Some(PyValue::List(allow)) = allow {
        for rule in allow {
            if let Some(rule) = as_text(&rule) {
                sources.push(("利用者の設定", rule.to_vec()));
            }
        }
    }
    let (mut out, mut unused) = (Vec::new(), Vec::new());
    let mut seen: Vec<(&'static str, Vec<Cps>)> = Vec::new();
    for (origin, rule) in sources {
        match bash_rule(strip(&rule)) {
            BashRule::Skip => {}
            BashRule::Unused => {
                let mut line = rule.clone();
                line.extend(lit("("));
                line.extend(lit(origin));
                line.extend(lit(")"));
                unused.push(line);
            }
            BashRule::Rule { kind, words } => {
                let key = (kind, words.clone());
                if !seen.contains(&key) {
                    seen.push(key);
                    out.push(PyValue::Dict(dict(vec![
                        ("kind", text_of(kind)),
                        (
                            "words",
                            PyValue::List(words.into_iter().map(text).collect()),
                        ),
                    ])));
                }
            }
        }
    }
    Ok((out, unused))
}

/// `host-allowlist` の標準出力の行: 規則の配列、続いて `unused=` の行(制御文字を空白にして 300 文字まで)。
pub fn host_allowlist_lines(
    rules: &[PyValue],
    unused: &[Cps],
    ctx: PyCtx,
) -> Result<Vec<Cps>, PyException> {
    let mut lines = vec![dumps(
        &PyValue::List(rules.to_vec()),
        DumpOptions::ALLOWLIST,
        ctx,
    )?];
    for item in unused {
        let mut line = lit("unused=");
        line.extend_from_slice(truncate(&replace_controls(item), 300));
        lines.push(line);
    }
    Ok(lines)
}

#[cfg(test)]
mod tests {
    use super::super::output::run_main;
    use super::super::pystr::decode_fs;
    use super::super::runtime::{Env, PyStream, StreamKind};
    use super::super::testing::{
        cps_of, error_of, fixture, from_hex, from_typed, not_found, result_json, to_typed,
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
    fn check_host_setting_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        for case in cases["check"].as_array().unwrap() {
            let value = from_typed(&case["value"]);
            let managed = case["managed"].as_bool().unwrap();
            let result =
                check_host_setting(value.as_dict().unwrap(), managed).map(|()| PyValue::None);
            assert_eq!(
                result_json(result),
                expect_of(case),
                "{} {managed}",
                case["value"]
            );
        }
    }

    #[test]
    fn host_settings_specs_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        for case in cases["specs"].as_array().unwrap() {
            let (specs, entries) = (from_typed(&case["specs"]), from_typed(&case["entries"]));
            let result = host_settings_specs(specs.as_dict().unwrap(), entries.as_dict().unwrap());
            assert_eq!(
                result_json(result),
                expect_of(case),
                "{} {}",
                case["specs"],
                case["entries"]
            );
        }
    }

    #[test]
    fn rule_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        assert!(cases["rules"].as_array().unwrap().len() > 250);
        for case in cases["rules"].as_array().unwrap() {
            let rules = split_rules(&cps_of(&case["text"]));
            let expect: Vec<Vec<u32>> = case["rules"]
                .as_array()
                .unwrap()
                .iter()
                .map(cps_of)
                .collect();
            assert_eq!(rules, expect, "{}", case["text"]);
        }
        for case in cases["bash"].as_array().unwrap() {
            let got = bash_rule(&cps_of(&case["text"]));
            let actual = match got {
                BashRule::Skip => json!("skip"),
                BashRule::Unused => json!(null),
                BashRule::Rule { kind, words } => json!([kind, words]),
            };
            assert_eq!(actual, case["result"], "{}", case["text"]);
        }
    }

    #[test]
    fn host_allowlist_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        let list = cases["allowlist"].as_array().unwrap();
        assert!(list.len() >= 60);
        let mut ok_runs = 0;
        for case in list {
            let user = from_typed(&case["user"]);
            let mut settings = PyDict::new();
            settings.insert_str("user_settings", text_of("/u"));
            settings.insert_str("user", user);
            let args: Vec<Vec<u32>> = case["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| decode_fs(a.as_str().unwrap().as_bytes()))
                .collect();
            let env = Env {
                columns: None,
                terminal_columns: 80,
                unbuffered: false,
                max_str_digits: 4300,
            };
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let status = {
                let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
                let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
                run_main(
                    &args,
                    &env,
                    &mut stdout,
                    &mut stderr,
                    &mut |parsed, stdout, _| {
                        let (rules, unused) = host_allowlist_rules(
                            &settings,
                            parsed.namespace.many("allowed_tools"),
                        )?;
                        for line in host_allowlist_lines(&rules, &unused, PyCtx::new(2))? {
                            stdout.print(&line)?;
                        }
                        Ok(0)
                    },
                )
            };
            ok_runs += usize::from(status == 0);
            let label = format!("{} {}", case["argv"], case["user"]);
            assert_eq!(i64::from(status), case["code"].as_i64().unwrap(), "{label}");
            assert_eq!(out, from_hex(case["stdout"].as_str().unwrap()), "{label}");
            assert_eq!(err, from_hex(case["stderr"].as_str().unwrap()), "{label}");
        }
        assert!(ok_runs > 20, "{ok_runs}");
    }

    /// 固定期待の偽物(`record` の戻りと記録)。
    struct Fake {
        fake: PyValue,
        calls: Vec<PyValue>,
    }

    impl SettingsObserver for Fake {
        fn tick(&mut self) -> Result<(), PyException> {
            Ok(())
        }

        fn record(
            &mut self,
            path: &[u32],
            _key: &[u32],
            local: &mut PyDict,
            _expected: &PyDict,
        ) -> Result<Option<Vec<u8>>, PyException> {
            self.calls.push(text(path.to_vec()));
            let table = self.fake.as_dict().unwrap();
            let Some(PyValue::Dict(item)) = table.get(&text(path.to_vec()))? else {
                return Err(not_found(path));
            };
            if item.contains_str("raise") {
                return Err(PyException::with_cps(
                    "PermissionError",
                    super::super::error::os_error_message(13, "Permission denied", Some(path)),
                ));
            }
            if let Some(PyValue::Dict(entries)) = item.get_str("local") {
                for (key, value) in entries.iter() {
                    local.insert(key.clone(), value.clone())?;
                }
            }
            match item.get_str("raw") {
                Some(PyValue::Str(hex)) => Ok(Some(from_hex(&super::super::pystr::to_string(
                    hex.as_codepoints(),
                )))),
                _ => Ok(None),
            }
        }
    }

    #[test]
    fn read_host_settings_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        let list = cases["read"].as_array().unwrap();
        assert!(list.len() >= 100, "{}", list.len());
        let (mut successes, mut failures) = (0, 0);
        for case in list {
            let value = from_typed(&case["value"]);
            // 固定期待の `raw` は 16 進の文字列。
            let mut fake = Fake {
                fake: from_typed(&case["fake"]),
                calls: Vec::new(),
            };
            let site = case
                .get("site")
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(3) as usize;
            let result = read_host_settings(&value, &mut fake, PyCtx::new(site));
            let label = format!("{} {}", case["value"], case["fake"]);
            match (result, case.get("ok")) {
                (Ok(done), Some(ok)) => {
                    successes += 1;
                    assert_eq!(&to_typed(&PyValue::Dict(done)), ok, "{label}");
                }
                (Err(error), None) => {
                    failures += 1;
                    assert_eq!(error_of(&error), case["error"], "{label}");
                }
                (other, _) => panic!("{label}: {:?}", other.map(|_| ())),
            }
            assert_eq!(
                to_typed(&PyValue::List(fake.calls)),
                case["records"],
                "記録の順 {label}"
            );
        }
        assert!(successes > 5 && failures > 20, "{successes} {failures}");
    }

    #[test]
    fn allowlist_rules_trim_unused_lines_to_300_after_replacing_controls() {
        let mut user = PyDict::new();
        let long = format!("Bash({}*)", "z".repeat(400));
        let mut permissions = PyDict::new();
        permissions.insert_str(
            "allow",
            PyValue::List(vec![text_of(&long), text_of("Bash(a\u{1}b*)")]),
        );
        user.insert_str("permissions", PyValue::Dict(permissions));
        let mut settings = PyDict::new();
        settings.insert_str("user", PyValue::Dict(user));
        let (rules, unused) = host_allowlist_rules(&settings, &[]).unwrap();
        assert!(rules.is_empty());
        let lines = host_allowlist_lines(&rules, &unused, PyCtx::new(2)).unwrap();
        assert_eq!(lines[0], lit("[]"));
        assert_eq!(lines[1].len(), "unused=".len() + 300);
        assert!(lines[2].starts_with(&lit("unused=Bash(a b*)")));
    }

    #[test]
    fn host_settings_output_cases_match_python_3_10() {
        let cases = fixture!("settings_cases.json");
        let list = cases["output"].as_array().unwrap();
        assert!(list.len() >= 40);
        for case in list {
            let checked = if case["big"].as_bool().unwrap() {
                let digits = case["checked"].as_str().unwrap();
                PyValue::Int(crate::environment_guard::pyvalue::PyInt::from_decimal(
                    false, digits,
                ))
            } else {
                PyValue::from(case["checked"].as_i64().unwrap())
            };
            let mut result = PyDict::new();
            result.insert_str("user_settings", text(cps_of(&case["user_settings"])));
            result.insert_str("checked", checked);
            let args: Vec<Vec<u32>> = ["host-settings", "--state", "s", "--expect-sha256", "h"]
                .iter()
                .map(|a| decode_fs(a.as_bytes()))
                .collect();
            let env = Env {
                columns: None,
                terminal_columns: 80,
                unbuffered: false,
                max_str_digits: 4300,
            };
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let status = {
                let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
                let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
                run_main(
                    &args,
                    &env,
                    &mut stdout,
                    &mut stderr,
                    &mut |_, stdout, _| {
                        stdout.print(&host_settings_output(&result, PyCtx::new(2))?)?;
                        Ok(0)
                    },
                )
            };
            let label = format!("{}", case["user_settings"]);
            assert_eq!(i64::from(status), case["code"].as_i64().unwrap(), "{label}");
            assert_eq!(out, from_hex(case["stdout"].as_str().unwrap()), "{label}");
            assert_eq!(err, from_hex(case["stderr"].as_str().unwrap()), "{label}");
        }
    }
}
