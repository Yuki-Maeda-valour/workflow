//! 旧版の `bootstrap(a)`。作成の順は旧版どおり(設計書「観測の順序」9):
//! 観測 → 出力フォルダの `mkdir(0700)`(umask を受ける)→ 控えの `entries` の挿入順に 1 回だけ回り、
//! フォルダは `mkdir(mode)` とリンクを辿らない `chmod`、ファイルは排他の新規作成・`fchmod`・`fsync` →
//! guard のコピー → 控え → 照合 → 受領。フォルダとファイルは交互に並ぶ。既存の出力は開かない。
//!
//! guard のコピー(U2)は、同じ観測で保持した配布物の `skills/ship-task/scripts/environment-guard.py` のバイト列。
//! 配布物に無ければ、観測の直後(出力フォルダを作る前)に `Stop`(終了コード 20)で止める。旧版は、起動した guard 自身(`__file__`)を読み直す。
use super::args::{ArgValue, Namespace};
use super::error::PyException;
use super::fsio::{chmod_nofollow_at, mkdir_at, site, with_parent, write_new};
use super::inventory::Blobs;
use super::pyjson::{PyCtx, plugin_inventory};
use super::pyops::{Cps, as_text, lit, sha_hex};
use super::pyvalue::PyValue;
use super::settings::host_settings_specs;
use super::snapshot::{snapshot, verify};
use super::specs::{BootstrapInput, Host, bootstrap_specs};
use super::state::{StateParts, dump_state_bytes, receipt, state_value};

/// 配布物の中の guard の位置(`bootstrap` が控えに写すバイト列の出所。`plugin` の控えの鍵)。
pub const GUARD_KEY: &str = "plugin/skills/ship-task/scripts/environment-guard.py";

/// 旧版の `bootstrap` の `main` の中の深さ(`<module>`・`main`・`bootstrap`)。
pub const BOOTSTRAP_DEPTH: usize = 3;

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn arg(namespace: &Namespace, dest: &str) -> ArgValue {
    namespace
        .value(dest)
        .cloned()
        .expect("`argparse` が必須の引数を確かめている")
}

fn mode_of(entry: &PyValue) -> Result<u32, PyException> {
    // `entry[1]`(保持した mode)。
    let value = entry.as_list().and_then(|items| items.get(1));
    match value {
        Some(PyValue::Int(int)) => int
            .to_i64()
            .and_then(|mode| u32::try_from(mode).ok())
            .ok_or_else(|| {
                PyException::new("OverflowError", "Python int too large to convert to C int")
            }),
        _ => Err(PyException::new("IndexError", "list index out of range")),
    }
}

/// 旧版の `bootstrap(a)`。成功すると受領(標準出力の 1 行)を `print` する。
pub fn bootstrap(
    host: &dyn Host,
    namespace: &Namespace,
    print: &mut dyn FnMut(&[u32]) -> Result<(), PyException>,
    ctx: PyCtx,
) -> Result<(), PyException> {
    let frame = PyCtx {
        depth: BOOTSTRAP_DEPTH,
        ..ctx
    };
    let input = BootstrapInput {
        root: arg(namespace, "root"),
        output: arg(namespace, "output"),
        setting: namespace.many("setting").to_vec(),
        config: namespace.many("config").to_vec(),
        shell: namespace.many("shell").to_vec(),
        skills_dir: namespace.many("skills_dir").to_vec(),
        settings_dir: namespace.many("settings_dir").to_vec(),
        managed_dir: namespace.many("managed_dir").to_vec(),
    };
    let built = bootstrap_specs(host, &input)?;
    let (mut specs, root, output) = (built.specs, built.root, built.output);
    // `plugin_inventory(read_regular(a.inventory)) if a.inventory else []`
    let inventory: Vec<PyValue> = match namespace.value("inventory") {
        Some(path) if !path.py_str().is_empty() => {
            let raw = super::fsio::read_regular(host, &path.py_str(), None)?;
            plugin_inventory(&raw, frame)?
        }
        _ => Vec::new(),
    };
    let mut blobs = Blobs::new();
    let inventory_value = PyValue::List(inventory.clone());
    let entries = snapshot(
        host,
        &root.to_cps(),
        &mut specs,
        Some(&inventory_value),
        &mut blobs,
        None,
        frame.depth + 1,
        frame,
    )?;
    // 配布物の guard のバイト列(U2)。観測の直後(出力フォルダを作る前)に確かめ、無ければ止める。
    // 旧版はこの場合、起動した guard 自身(`__file__`)をコピーする(U2 の差)。バイト列は、同じ観測の保持値。
    let guard = blobs
        .get(&lit(GUARD_KEY))
        .cloned()
        .ok_or_else(|| stop("guard の配布元が配布物に無い"))?;
    site("bootstrap-after-snapshot");
    let settings = host_settings_specs(&specs, &entries)?;
    specs.insert_str("host_settings", settings);
    // 既存の state/copy は開かず、新しいフォルダだけ作る。
    with_parent(host, &output.to_cps(), |fd, name| mkdir_at(fd, name, 0o700))?;
    for (key, held) in entries.iter() {
        let name = as_text(key).expect("控えの鍵は文字列");
        if !(name == lit("plugin") || super::pyops::starts_with(name, "plugin/")) {
            continue;
        }
        let target = output.join(name).to_cps();
        let is_directory = held
            .as_list()
            .and_then(<[PyValue]>::first)
            .is_some_and(|kind| kind.py_eq(&PyValue::from("directory")));
        if is_directory {
            let mode = mode_of(held)?;
            with_parent(host, &target, |fd, leaf| {
                mkdir_at(fd, leaf, mode)?;
                chmod_nofollow_at(fd, leaf, mode)
            })?;
        } else {
            let raw = blobs.get(name).ok_or_else(|| {
                PyException::new("KeyError", &format!("'{}'", super::pystr::to_string(name)))
            })?;
            write_new(host, &target, raw, mode_of(held)?)?;
        }
    }
    let guard_sha = sha_hex(&guard);
    write_new(
        host,
        &output.join(&lit("environment-guard.py")).to_cps(),
        &guard,
        0o600,
    )?;
    let value = state_value(StateParts {
        root: &root,
        output: &output,
        specs: &specs,
        inventory,
        entries,
        guard_sha256: &guard_sha,
    });
    let raw = dump_state_bytes(&value, frame)?;
    let state_path: Cps = output.join(&lit("environment.json")).to_cps();
    write_new(host, &state_path, &raw, 0o600)?;
    site("bootstrap-before-verify");
    verify(host, &state_path, &lit(&sha_hex(&raw)), None, frame)?;
    let line = receipt(&output, &raw, &guard_sha, frame)?;
    print(&line)
}
