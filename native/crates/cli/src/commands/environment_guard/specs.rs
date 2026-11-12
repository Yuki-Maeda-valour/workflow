//! 旧版の `host_path`・`host_paths`・`default_specs` と、`bootstrap` の記述子の組み立て。
//! 環境変数・作業場所・利用者データベースは `Host` で受け取る(OS を呼ばない)。
use super::args::ArgValue;
use super::error::PyException;
use super::pyops::{Cps, dict, lit, text, text_of};
use super::pypath::{PyPath, UserLookup, expanduser};
use super::pystr::is_nfc;
use super::pyvalue::{PyDict, PyValue};

/// 旧版が `os.environ`・`os.getcwd()`・`pwd` から読むもの。
pub trait Host {
    /// `os.environ.get(name)`(文字番号。未設定は `None`、空文字は `Some(空)`)。
    fn env(&self, name: &str) -> Option<Cps>;
    /// `os.getcwd()`。
    fn cwd(&self) -> Result<Cps, PyException>;
    fn users(&self) -> &dyn UserLookup;
    /// `dict(os.environ)`(Git 設定の解析の子の環境の元。名前と値の生バイト)。
    fn process_env(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        super::gitconfig::process_environment()
    }
    /// `PYTHONINTMAXSTRDIGITS`(整数の文字列変換の上限。0 は無制限)。起動時の誤りは入口が止めるので、既定に戻す。
    fn max_str_digits(&self) -> usize {
        use std::os::unix::ffi::OsStrExt;
        let value = std::env::var_os("PYTHONINTMAXSTRDIGITS");
        super::runtime::int_max_str_digits_from(value.as_deref().map(OsStrExt::as_bytes))
            .unwrap_or(4300)
    }
}

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

/// `absolute(path)`: `Path(os.path.expanduser(str(path)))` で、相対なら作業場所の下。symlink は解決しない。
pub fn absolute(host: &dyn Host, path: &[u32]) -> Result<PyPath, PyException> {
    let home = host.env("HOME");
    let expanded = expanduser(path, home.as_deref(), host.users())?;
    let value = PyPath::new(&expanded);
    if value.is_absolute() {
        return Ok(value);
    }
    Ok(PyPath::new(&host.cwd()?).join(&value.to_cps()))
}

/// `host_path(value, name)`: ホストが起動時に使うパスだけの、作業場所に依らない解決。
pub fn host_path(value: Option<&PyValue>) -> Result<Cps, PyException> {
    let host_error = || stop("host-config-path");
    let Some(PyValue::Str(text)) = value else {
        return Err(host_error());
    };
    let text = text.as_codepoints();
    if text.is_empty() || text.first() == Some(&0x7e) || text.starts_with(&[0x2f, 0x2f]) {
        return Err(host_error());
    }
    if !super::pypath::isabs(text) || !is_nfc(text) {
        return Err(host_error());
    }
    let path = PyPath::new(text);
    if path.has_part(&lit("..")) {
        return Err(host_error());
    }
    Ok(path.to_cps())
}

fn env_value(host: &dyn Host, name: &str) -> Option<PyValue> {
    host.env(name).map(text)
}

fn join_str(base: &[u32], tail: &str) -> Cps {
    PyPath::new(base).join(&lit(tail)).to_cps()
}

/// `host_paths(environ)`。
pub fn host_paths(host: &dyn Host) -> Result<PyDict, PyException> {
    let home = host_path(env_value(host, "HOME").as_ref())?;
    let config = match env_value(host, "CLAUDE_CONFIG_DIR") {
        Some(value) => host_path(Some(&value))?,
        None => join_str(&home, ".claude"),
    };
    // 旧版の並び: home, config, user_settings, cache_settings。
    let user_settings = join_str(&config, "settings.json");
    let cache_settings = join_str(&config, "remote-settings.json");
    Ok(dict(vec![
        ("home", text(home)),
        ("config", text(config)),
        ("user_settings", text(user_settings)),
        ("cache_settings", text(cache_settings)),
    ]))
}

fn strings(items: Vec<Cps>) -> PyValue {
    PyValue::List(items.into_iter().map(text).collect())
}

fn component(kind: &str, path: Cps) -> PyValue {
    PyValue::Dict(dict(vec![("kind", text_of(kind)), ("path", text(path))]))
}

fn truthy_env(host: &dyn Host, name: &str) -> Option<Cps> {
    host.env(name).filter(|value| !value.is_empty())
}

/// `default_specs()`。
pub fn default_specs(host: &dyn Host) -> Result<PyDict, PyException> {
    let paths = host_paths(host)?;
    let get = |key: &str| -> Cps {
        super::pyops::as_text(paths.get_str(key).expect("host_paths の欄"))
            .expect("文字列")
            .to_vec()
    };
    let home = get("home");
    let xdg = match truthy_env(host, "XDG_CONFIG_HOME") {
        Some(value) => absolute(host, &value)?,
        None => absolute(host, &join_str(&home, ".config"))?,
    };
    let config = get("config");
    let mut files: Vec<Cps> = vec![
        get("user_settings"),
        get("cache_settings"),
        join_str(&config, "plugins/installed_plugins.json"),
        join_str(&config, "plugins/known_marketplaces.json"),
        lit("/etc/claude-code/managed-settings.json"),
    ];
    for name in [
        ".profile",
        ".bash_profile",
        ".bash_login",
        ".bashrc",
        ".bash_aliases",
        ".bash_logout",
        ".zshenv",
        ".zprofile",
        ".zshrc",
        ".zlogin",
        ".zlogout",
    ] {
        files.push(join_str(&home, name));
    }
    for path in ["/etc/profile", "/etc/bash.bashrc", "/etc/zsh/zshenv"] {
        files.push(lit(path));
    }
    for name in ["BASH_ENV", "ENV"] {
        if let Some(value) = truthy_env(host, name) {
            files.push(absolute(host, &value)?.to_cps());
        }
    }
    if let Some(value) = truthy_env(host, "ZDOTDIR") {
        let base = absolute(host, &value)?;
        for name in [".zshenv", ".zprofile", ".zshrc", ".zlogin"] {
            files.push(base.join(&lit(name)).to_cps());
        }
    }
    let mut configs: Vec<Cps> = match truthy_env(host, "GIT_CONFIG_GLOBAL") {
        Some(value) => vec![absolute(host, &value)?.to_cps()],
        None => vec![
            join_str(&home, ".gitconfig"),
            xdg.join(&lit("git/config")).to_cps(),
        ],
    };
    let nosystem = host.env("GIT_CONFIG_NOSYSTEM");
    let skip_system =
        matches!(&nosystem, Some(v) if [lit("1"), lit("true"), lit("yes")].contains(v));
    if !skip_system {
        let system = truthy_env(host, "GIT_CONFIG_SYSTEM").unwrap_or_else(|| lit("/etc/gitconfig"));
        configs.push(absolute(host, &system)?.to_cps());
    }
    let mut specs = PyDict::new();
    specs.insert_str("files", strings(files));
    specs.insert_str("configs", strings(configs));
    specs.insert_str(
        "directories",
        strings(vec![lit("/etc/claude-code/managed-settings.d")]),
    );
    specs.insert_str("user_directories", PyValue::List(vec![]));
    specs.insert_str("host_paths", PyValue::Dict(paths));
    specs.insert_str("managed_roots", strings(vec![lit("/etc/claude-code")]));
    specs.insert_str(
        "components",
        PyValue::List(vec![
            component("personal-skill", join_str(&config, "skills")),
            component("personal-command", join_str(&config, "commands")),
            component("personal-agent", join_str(&config, "agents")),
            component("enterprise-skill", lit("/etc/claude-code/.claude/skills")),
            component("enterprise-agent", lit("/etc/claude-code/.claude/agents")),
            component(
                "enterprise-command",
                lit("/etc/claude-code/.claude/commands"),
            ),
        ]),
    );
    specs.insert_str("host_settings", PyValue::List(vec![]));
    Ok(specs)
}

/// `bootstrap` の引数(`argparse` が作った値)。
pub struct BootstrapInput {
    pub root: ArgValue,
    pub output: ArgValue,
    pub setting: Vec<ArgValue>,
    pub config: Vec<ArgValue>,
    pub shell: Vec<ArgValue>,
    pub skills_dir: Vec<ArgValue>,
    pub settings_dir: Vec<ArgValue>,
    pub managed_dir: Vec<ArgValue>,
}

/// 組み立てた記述子と、配布物・コピー先のパス。
pub struct BootstrapSpecs {
    pub specs: PyDict,
    pub root: PyPath,
    pub output: PyPath,
}

fn push_list(specs: &mut PyDict, key: &str, items: Vec<PyValue>) {
    let mut current = match specs.get_str(key) {
        Some(PyValue::List(list)) => list.clone(),
        _ => Vec::new(),
    };
    current.extend(items);
    specs.insert_str(key, PyValue::List(current));
}

/// `bootstrap` の、観測より前の部分。`..` と配布物の内側の拒否、記述子の追加と重複の除去まで。
pub fn bootstrap_specs(
    host: &dyn Host,
    input: &BootstrapInput,
) -> Result<BootstrapSpecs, PyException> {
    let root = absolute(host, &input.root.py_str())?;
    let output = absolute(host, &input.output.py_str())?;
    let dotdot = lit("..");
    if root.has_part(&dotdot) || output.has_part(&dotdot) {
        return Err(stop("配布元とコピー先は .. を含まないパスで指定する"));
    }
    if output == root || output.parents().contains(&root) {
        return Err(stop("コピー先が配布物の内側にある"));
    }
    let mut specs = default_specs(host)?;
    let abs_all = |values: &[ArgValue]| -> Result<Vec<PyValue>, PyException> {
        values
            .iter()
            .map(|v| Ok(text(absolute(host, &v.py_str())?.to_cps())))
            .collect()
    };
    let mut files = abs_all(&input.setting)?;
    files.extend(abs_all(&input.shell)?);
    push_list(&mut specs, "files", files);
    push_list(&mut specs, "configs", abs_all(&input.config)?);
    let skills = abs_all(&input.skills_dir)?
        .into_iter()
        .map(|path| {
            PyValue::Dict(dict(vec![
                ("kind", text_of("personal-skill")),
                ("path", path),
            ]))
        })
        .collect();
    push_list(&mut specs, "components", skills);
    push_list(
        &mut specs,
        "components",
        vec![PyValue::Dict(dict(vec![
            ("kind", text_of("plugin")),
            ("path", text(root.to_cps())),
            ("source", text_of("workflow")),
        ]))],
    );
    push_list(&mut specs, "directories", abs_all(&input.settings_dir)?);
    for managed in &input.managed_dir {
        let managed_root = match managed {
            ArgValue::Str(value) => host_path(Some(&PyValue::Str(value.clone())))?,
            ArgValue::EmptyList | ArgValue::Int(_) => return Err(stop("host-config-path")),
        };
        push_list(
            &mut specs,
            "managed_roots",
            vec![text(managed_root.clone())],
        );
        push_list(
            &mut specs,
            "files",
            vec![text(join_str(&managed_root, "managed-settings.json"))],
        );
        push_list(
            &mut specs,
            "directories",
            vec![text(join_str(&managed_root, "managed-settings.d"))],
        );
        let enterprise = [
            ("skill", "skills"),
            ("agent", "agents"),
            ("command", "commands"),
        ]
        .iter()
        .map(|(kind, folder)| {
            let path = PyPath::new(&managed_root)
                .join(&lit(".claude"))
                .join(&lit(folder))
                .to_cps();
            component(&format!("enterprise-{kind}"), path)
        })
        .collect();
        push_list(&mut specs, "components", enterprise);
    }
    let deduped = dedupe_components(
        specs
            .get_str("components")
            .cloned()
            .unwrap_or(PyValue::List(vec![])),
    )?;
    specs.insert_str("components", deduped);
    Ok(BootstrapSpecs {
        specs,
        root,
        output,
    })
}

/// `list({(kind, path): item ...}.values())`: 同じ `(kind, path)` は最初の位置に、最後の値を置く。
pub fn dedupe_components(components: PyValue) -> Result<PyValue, PyException> {
    let PyValue::List(items) = components else {
        return Ok(components);
    };
    let mut keyed = PyDict::new();
    for item in items {
        let key = {
            let dict = item.as_dict().expect("記述子は dict");
            PyValue::List(vec![
                dict.get_str("kind").cloned().unwrap_or(PyValue::None),
                dict.get_str("path").cloned().unwrap_or(PyValue::None),
            ])
        };
        // list は hash できないので、キーは文字列の組を 1 つの文字列にして使う。
        let joined = {
            let PyValue::List(parts) = &key else {
                unreachable!()
            };
            let mut cps: Cps = Vec::new();
            for part in parts {
                cps.extend_from_slice(super::pyops::as_text(part).unwrap_or(&[]));
                cps.push(0);
            }
            text(cps)
        };
        keyed.insert(joined, item)?;
    }
    Ok(PyValue::List(keyed.values().cloned().collect()))
}

#[cfg(test)]
mod tests {
    use super::super::testing::{cps_of, fake_host, fixture, from_typed, result_json, to_typed};
    use super::*;
    use serde_json::json;

    /// 固定期待の引数の値(`typed_arg_list`)。
    fn arg_value(value: &serde_json::Value) -> ArgValue {
        match value["t"].as_str().unwrap() {
            "empty" => ArgValue::EmptyList,
            _ => ArgValue::Str(
                dev_workflow_core::loop_state::CodepointString::try_from_codepoints(cps_of(
                    &value["v"],
                ))
                .unwrap(),
            ),
        }
    }

    fn arg_list(value: &serde_json::Value) -> Vec<ArgValue> {
        value["v"]
            .as_array()
            .unwrap()
            .iter()
            .map(arg_value)
            .collect()
    }

    #[test]
    fn host_path_cases_match_python_3_10() {
        let cases = fixture!("specs_cases.json");
        for case in cases["host_path"].as_array().unwrap() {
            let value = from_typed(&case["value"]);
            let result = host_path(Some(&value)).map(text);
            let mut actual = result_json(result);
            // 戻り値は常に str。
            if let Some(ok) = actual.get_mut("ok") {
                assert_eq!(ok["t"], "str");
            }
            assert_eq!(actual, expect_of(case), "{}", case["value"]);
        }
        assert!(cases["host_path"].as_array().unwrap().len() >= 25);
    }

    fn expect_of(case: &serde_json::Value) -> serde_json::Value {
        if case.get("ok").is_some() {
            json!({"ok": case["ok"]})
        } else {
            json!({"error": case["error"]})
        }
    }

    #[test]
    fn host_paths_cases_match_python_3_10() {
        let cases = fixture!("specs_cases.json");
        for case in cases["host_paths"].as_array().unwrap() {
            let host = fake_host(&case["env"], &lit("/"));
            let actual = result_json(host_paths(&host).map(PyValue::Dict));
            assert_eq!(actual, expect_of(case), "{}", case["env"]);
        }
    }

    #[test]
    fn default_specs_cases_match_python_3_10() {
        let cases = fixture!("specs_cases.json");
        let list = cases["default_specs"].as_array().unwrap();
        assert!(list.len() >= 150);
        let mut successes = 0;
        for case in list {
            let host = fake_host(&case["env"], &cps_of(&case["cwd"]));
            let actual = result_json(default_specs(&host).map(PyValue::Dict));
            successes += usize::from(actual.get("ok").is_some());
            assert_eq!(actual, expect_of(case), "{}", case["env"]);
        }
        assert!(successes > 20, "{successes}");
    }

    #[test]
    fn bootstrap_specs_cases_match_python_3_10() {
        let cases = fixture!("specs_cases.json");
        let list = cases["bootstrap"].as_array().unwrap();
        assert!(list.len() >= 150);
        let (mut successes, mut failures) = (0, 0);
        for case in list {
            let host = fake_host(&case["env"], &cps_of(&case["cwd"]));
            let args = &case["args"];
            let one = |name: &str| arg_value(&args[name]);
            let input = BootstrapInput {
                root: one("root"),
                output: one("output"),
                setting: arg_list(&args["setting"]),
                config: arg_list(&args["config"]),
                shell: arg_list(&args["shell"]),
                skills_dir: arg_list(&args["skills_dir"]),
                settings_dir: arg_list(&args["settings_dir"]),
                managed_dir: arg_list(&args["managed_dir"]),
            };
            match (bootstrap_specs(&host, &input), case.get("ok")) {
                (Ok(built), Some(ok)) => {
                    successes += 1;
                    assert_eq!(built.root.to_cps(), cps_of(&ok["root"]), "{args}");
                    assert_eq!(built.output.to_cps(), cps_of(&ok["output"]), "{args}");
                    assert_eq!(to_typed(&PyValue::Dict(built.specs)), ok["specs"], "{args}");
                }
                (Err(error), None) => {
                    failures += 1;
                    assert_eq!(
                        super::super::testing::error_of(&error),
                        case["error"],
                        "{args}"
                    );
                }
                (other, _) => panic!("{args}: {:?}", other.map(|_| ())),
            }
        }
        assert!(successes > 20 && failures > 20, "{successes} {failures}");
    }

    #[test]
    fn dedupe_keeps_the_first_position_and_the_last_value() {
        let first = PyValue::Dict(dict(vec![
            ("kind", text_of("a")),
            ("path", text_of("/p")),
            ("n", PyValue::from(1)),
        ]));
        let other = PyValue::Dict(dict(vec![("kind", text_of("b")), ("path", text_of("/p"))]));
        let last = PyValue::Dict(dict(vec![
            ("kind", text_of("a")),
            ("path", text_of("/p")),
            ("n", PyValue::from(2)),
        ]));
        let result =
            dedupe_components(PyValue::List(vec![first, other.clone(), last.clone()])).unwrap();
        assert_eq!(
            to_typed(&result),
            to_typed(&PyValue::List(vec![last, other]))
        );
    }
}
