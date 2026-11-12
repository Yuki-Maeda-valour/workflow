//! 内部 reader 3 つ(`read_host_settings`・`read_components`・`component_policy_digest`)。
//! `host-check.py` が、控えの隣の guard を `verified_environment_guard` として読み込んで呼ぶ関数の、Rust 版。
//!
//! 返す値は Python の値と同じ意味を持つ型(`PyValue`)。失敗は、旧版の例外と同じ型名と理由の文字列を持つ
//! `PyException`(`main` が捕まえない型も表す)。`ctx.depth` は、呼び出し側のフレームの深さ
//! (reader の本体は 1 つ深い)。観測の範囲は設計書の表のとおり:
//!
//! | reader | 観測の範囲 |
//! |---|---|
//! | `read_host_settings` | 全体の照合(`verify`)→ 各設定の読み直しと関連する鍵の照合 |
//! | `read_components` | 控えの SHA-256 と形 → 部品の木だけ → 部品の鍵の集合の比較。全体の照合はしない |
//! | `component_policy_digest` | 控えの SHA-256 と形だけ。ファイルシステムを観測しない |
//!
//! 開発用 example だけが、比較用の隠しコマンド(`reader-host-settings`・`reader-components`・`reader-policy-digest`)
//! で、返した値を型の印つきの JSON で出す(`typed_json`)。製品の CLI には置かない。
use super::budget::EnvBudget;
use super::component::component_tree;
use super::error::PyException;
use super::fsio::{Obs, read_regular};
use super::inventory::Blobs;
use super::pyjson::PyCtx;
use super::pyops::{Cps, as_text, dict, lit, starts_with, text};
use super::pypath::PyPath;
use super::pystr::{decode_fs, to_string};
use super::pyvalue::{PyDict, PyValue, float_repr};
use super::record::{RecordOptions, record};
use super::settings::{self, SettingsObserver};
use super::snapshot::verify;
use super::specs::Host;
use super::state::{self, component_descriptors, load_verified};

/// `read_host_settings` が使う、設定ファイルの読み直しと予算。
struct EnvObserver<'a> {
    host: &'a dyn Host,
    budget: EnvBudget,
}

impl SettingsObserver for EnvObserver<'_> {
    fn tick(&mut self) -> Result<(), PyException> {
        self.budget.tick(0)
    }

    fn record(
        &mut self,
        path: &[u32],
        key: &[u32],
        local: &mut PyDict,
        expected: &PyDict,
    ) -> Result<Option<Vec<u8>>, PyException> {
        record(
            self.host,
            path,
            key,
            &mut self.budget,
            local,
            &RecordOptions {
                expected: Some(expected),
                ..RecordOptions::default()
            },
        )
    }
}

/// 旧版の `read_host_settings(state_path, expected_sha)`: 保持済みの設定だけを安全に読む。
/// 戻り値は `user_settings`・`user`・`cache`・`managed`・`checked`・`permission_sources`(各 `kind`・`source`・
/// `present`・`settings`)の辞書。
pub fn read_host_settings(
    host: &dyn Host,
    state: &[u32],
    expected: &[u32],
    ctx: PyCtx,
) -> Result<PyDict, PyException> {
    // `verify(...)` は `read_host_settings` の本体(呼び出し側の 1 つ下)から呼ばれる。
    let value = verify(
        host,
        state,
        expected,
        None,
        PyCtx {
            depth: ctx.depth + 1,
            ..ctx
        },
    )?;
    let mut observer = EnvObserver {
        host,
        budget: EnvBudget::new(),
    };
    settings::read_host_settings(&value, &mut observer, ctx)
}

/// 旧版の `component_policy_digest(state_path, expected_sha)`。控えの SHA-256 と形だけを見る。
pub fn component_policy_digest(
    host: &dyn Host,
    state: &[u32],
    expected: &[u32],
    ctx: PyCtx,
) -> Result<String, PyException> {
    let value = load_verified(
        expected,
        || read_regular(host, state, None),
        PyCtx {
            depth: ctx.depth + 1,
            ..ctx
        },
    )?;
    state::component_policy_digest(&value, ctx)
}

fn key_error(name: &str) -> PyException {
    PyException::new("KeyError", &format!("'{name}'"))
}

const WANTED_PREFIXES: [&str; 8] = [
    "component:",
    "@stat:component:",
    "@names:component:",
    "@link:component:",
    "@route:component:",
    "@route-end:component:",
    "@origin:component:",
    "@absolute:@route:component:",
];

/// 旧版の `read_components(state_path, expected_sha)`: 保持 SHA と記述子を照合し、同じ走査の同一 fd 由来のバイト列を返す。
/// 戻り値は、各部品の `key`・`kind`・`root`・`relative`・`path`・`raw`(バイト列)と、ある場合の `source`・`plugin`・
/// `entry` を持つ辞書の並び。
pub fn read_components(
    host: &dyn Host,
    state: &[u32],
    expected: &[u32],
    ctx: PyCtx,
) -> Result<Vec<PyValue>, PyException> {
    let value = load_verified(
        expected,
        || read_regular(host, state, None),
        PyCtx {
            depth: ctx.depth + 1,
            ..ctx
        },
    )?;
    let components = component_descriptors(&value)?;
    let root_dict = value.as_dict().expect("`load_verified` は辞書を返す");
    let mut entries = PyDict::new();
    let mut blobs = Blobs::new();
    let mut budget = EnvBudget::new();
    let mut result: Vec<PyValue> = Vec::new();
    for item in &components {
        let item = item
            .as_dict()
            .expect("`component_descriptors` が辞書を確かめた");
        let root = item
            .get_str("path")
            .and_then(as_text)
            .expect("検査済みの記述子")
            .to_vec();
        let kind = item
            .get_str("kind")
            .and_then(as_text)
            .expect("検査済みの記述子")
            .to_vec();
        // `value['entries']`
        let held = match root_dict.get_str("entries") {
            Some(PyValue::Dict(held)) => held,
            Some(other) => {
                // 届かない分岐: 旧版は `component_tree` の中で最初に添字を引く所で `TypeError`/`AttributeError` になる。
                return Err(PyException::type_error(&format!(
                    "argument of type '{}' is not iterable",
                    other.type_name()
                )));
            }
            None => return Err(key_error("entries")),
        };
        let rootkey = {
            let mut obs = Obs {
                host,
                budget: &mut budget,
                entries: &mut entries,
            };
            component_tree(&mut obs, &root, &kind, Some(held), Some(&mut blobs))?
        };
        let mut under = rootkey.clone();
        under.push(0x2f);
        for (key, raw) in blobs.iter() {
            if key != &rootkey && !key.starts_with(&under) {
                continue;
            }
            let relative: Cps = if key != &rootkey {
                key[rootkey.len() + 1..].to_vec()
            } else {
                PyPath::new(&root).name()
            };
            let path: Cps = if key == &rootkey {
                root.clone()
            } else {
                let mut path = root.clone();
                path.push(0x2f);
                path.extend_from_slice(&relative);
                path
            };
            let mut out = dict(vec![
                ("key", text(key.clone())),
                ("kind", text(kind.clone())),
                ("root", text(root.clone())),
                ("relative", text(relative)),
                ("path", text(path)),
                ("raw", PyValue::Bytes(raw.clone())),
            ]);
            for name in ["source", "plugin", "entry"] {
                if let Some(found) = item.get_str(name) {
                    out.insert_str(name, found.clone());
                }
            }
            result.push(PyValue::Dict(out));
        }
    }
    // `wanted = {k: v for k, v in value['entries'].items() if k.startswith((...))}`
    let held = match root_dict.get_str("entries") {
        Some(PyValue::Dict(held)) => held,
        Some(other) => {
            return Err(PyException::new(
                "AttributeError",
                &format!("'{}' object has no attribute 'items'", other.type_name()),
            ));
        }
        None => return Err(key_error("entries")),
    };
    let mut wanted = PyDict::new();
    for (key, held_value) in held.iter() {
        let name = as_text(key).expect("保持値のキーは文字列");
        if WANTED_PREFIXES
            .iter()
            .any(|prefix| starts_with(name, prefix))
        {
            wanted.insert(key.clone(), held_value.clone())?;
        }
    }
    if PyValue::Dict(entries).py_ne(&PyValue::Dict(wanted)) {
        return Err(PyException::stop("component の保持集合が変わった"));
    }
    Ok(result)
}

// ---------------------------------------------------------------- 比較用の型の印つきの JSON

fn json_string(text: &[u32], out: &mut String) {
    out.push('"');
    for &c in text {
        match c {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x20..=0x7e => out.push(char::from(u8::try_from(c).expect("ASCII"))),
            0x10000.. => {
                let v = c - 0x10000;
                out.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (v >> 10),
                    0xdc00 + (v & 0x3ff)
                ));
            }
            _ => out.push_str(&format!("\\u{c:04x}")),
        }
    }
    out.push('"');
}

/// 返した値を、型の印つきの JSON にする。`str` は `json.dumps(ensure_ascii=True)` と同じ字面、`bytes` は 16 進、
/// `float` は `repr`、`int` は十進(桁数に上限なし)、`dict` はキーと値の組の並び(辞書の順)。
pub fn typed_json(value: &PyValue) -> String {
    let mut out = String::new();
    typed_into(value, &mut out);
    out
}

fn typed_into(value: &PyValue, out: &mut String) {
    match value {
        PyValue::None => out.push_str("{\"t\":\"none\"}"),
        PyValue::Bool(b) => out.push_str(&format!("{{\"t\":\"bool\",\"v\":{b}}}")),
        PyValue::Int(int) => {
            let digits = int.to_decimal(0).expect("上限なしの十進");
            out.push_str(&format!("{{\"t\":\"int\",\"v\":\"{digits}\"}}"));
        }
        PyValue::Float(f) => {
            out.push_str(&format!("{{\"t\":\"float\",\"v\":\"{}\"}}", float_repr(*f)))
        }
        PyValue::Str(s) => {
            out.push_str("{\"t\":\"str\",\"v\":");
            json_string(s.as_codepoints(), out);
            out.push('}');
        }
        PyValue::Bytes(b) => {
            out.push_str("{\"t\":\"bytes\",\"v\":\"");
            for byte in b {
                out.push_str(&format!("{byte:02x}"));
            }
            out.push_str("\"}");
        }
        PyValue::List(items) => {
            out.push_str("{\"t\":\"list\",\"v\":[");
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                typed_into(item, out);
            }
            out.push_str("]}");
        }
        PyValue::Dict(d) => {
            out.push_str("{\"t\":\"dict\",\"v\":[");
            for (index, (key, item)) in d.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('[');
                typed_into(key, out);
                out.push(',');
                typed_into(item, out);
                out.push(']');
            }
            out.push_str("]}");
        }
    }
}

/// 失敗の JSON(`{"error":{"type":…,"msg":…}}`。`msg` は `json.dumps(ensure_ascii=True)` と同じ字面)。
pub fn error_json(error: &PyException) -> String {
    let mut out = String::from("{\"error\":{\"type\":");
    json_string(&lit(&error.type_name), &mut out);
    out.push_str(",\"msg\":");
    json_string(error.message.as_codepoints(), &mut out);
    out.push_str("}}");
    out
}

/// 隠しコマンドの名前。
pub const HIDDEN_COMMANDS: [&str; 3] = [
    "reader-host-settings",
    "reader-components",
    "reader-policy-digest",
];

/// 比較用の隠しコマンド: `reader-<名前> <state> <sha256> [呼び出し側の深さ]`。標準出力の 1 行の JSON
/// (`{"ok": 型の印つきの値}` か `{"error": …}`)を返す。引数の形が違えば `None`。
/// 呼び出し側の深さの既定は 2(旧版の `main` のフレーム)。
pub fn run_hidden(host: &dyn Host, args: &[Vec<u32>]) -> Option<String> {
    let command = to_string(args.first()?);
    if !HIDDEN_COMMANDS.contains(&command.as_str()) || !(3..=4).contains(&args.len()) {
        return None;
    }
    let depth = match args.get(3) {
        Some(text) => to_string(text).parse::<usize>().ok()?,
        None => 2,
    };
    let ctx = PyCtx {
        depth,
        max_str_digits: host.max_str_digits(),
    };
    let (state, sha) = (&args[1], &args[2]);
    let result: Result<PyValue, PyException> = match command.as_str() {
        "reader-host-settings" => read_host_settings(host, state, sha, ctx).map(PyValue::Dict),
        "reader-components" => read_components(host, state, sha, ctx).map(PyValue::List),
        _ => component_policy_digest(host, state, sha, ctx)
            .map(|digest| PyValue::from(digest.as_str())),
    };
    Some(match result {
        Ok(value) => format!("{{\"ok\":{}}}", typed_json(&value)),
        Err(error) => error_json(&error),
    })
}

/// プロセスの引数(生バイト)から `run_hidden` を呼ぶ。
pub fn run_hidden_os(host: &dyn Host, args: &[std::ffi::OsString]) -> Option<String> {
    use std::os::unix::ffi::OsStrExt;
    let args: Vec<Vec<u32>> = args.iter().map(|a| decode_fs(a.as_bytes())).collect();
    run_hidden(host, &args)
}
