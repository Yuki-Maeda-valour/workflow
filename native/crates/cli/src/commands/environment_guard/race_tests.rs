//! 細かい競合(`lstat` と `open` の間・走査の途中・読取りの前後)の検査。`fsio::site` のフックで、製品と同じ
//! 関数の中の定めた位置に、差し替え・追加・書き換えを差し込む(製品と example には注入の入口を作らない)。
//! システムコールが `EINTR` で中断されたときの再試行(`fsio::injected`)も、ここで確かめる。
use super::budget::EnvBudget;
use super::component::component_tree;
use super::dispatch::ProcessHost;
use super::entry;
use super::error::PyException;
use super::fsio::hooks::{Point, with, with_syscall};
use super::fsio::{Obs, TreeOptions, chmod_nofollow_at, fstat, identity, tree};
use super::observe_tests::{Dir, build_layout};
use super::pyops::Cps;
use super::pystr::{cps, to_string};
use super::pyvalue::PyDict;
use super::record::{RecordOptions, record};
use dev_workflow_platform::environment_guard as os;
use serde_json::{Value, json};
use std::cell::Cell;
use std::io;
use std::os::fd::AsFd;
use std::path::Path;
use std::rc::Rc;
use std::time::Duration;

fn layout(root: &Path, items: Value) {
    std::fs::create_dir_all(root).unwrap();
    build_layout(root, items.as_array().unwrap());
}

/// 位置 `name` の 1 回目だけ `action` を行うフックで `body` を流す。
fn once_at<T>(name: &'static str, action: impl Fn() + 'static, body: impl FnOnce() -> T) -> T {
    nth_at(name, 1, action, body)
}

/// 位置 `name` の `nth` 回目(1 から数える)だけ `action` を行うフックで `body` を流す。
fn nth_at<T>(
    name: &'static str,
    nth: usize,
    action: impl Fn() + 'static,
    body: impl FnOnce() -> T,
) -> T {
    let count = Rc::new(Cell::new(0usize));
    with(
        move |point| {
            if matches!(point, Point::Site(site) if *site == name) {
                count.set(count.get() + 1);
                if count.get() != nth {
                    return None;
                }
                // 内容や種類を変えても時刻が同じにならないよう、カーネルの時計が進むのを待つ。
                std::thread::sleep(Duration::from_millis(30));
                action();
            }
            None
        },
        body,
    )
}

/// 一時フォルダの根までの親の数(部品の木の足取りは、親 1 つにつき 1 回)。
fn ancestors(dir: &Dir) -> usize {
    dir.0.components().filter(|c| c.as_os_str() != "/").count()
}

fn swap_dir(root: &Path, name: &str) -> impl Fn() + 'static {
    let (from, aside) = (root.join(name), root.join(format!("{name}.aside")));
    move || {
        std::fs::rename(&from, &aside).unwrap();
        std::fs::create_dir(&from).unwrap();
    }
}

fn message(error: &PyException) -> String {
    to_string(error.message.as_codepoints())
}

fn run_tree(root: &Path, options: TreeOptions<'_>) -> (Result<(), PyException>, PyDict) {
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let result = {
        let mut obs = Obs {
            host: &ProcessHost,
            budget: &mut budget,
            entries: &mut entries,
        };
        tree(
            &mut obs,
            &cps(root.join("t").to_str().unwrap()),
            &cps("k"),
            options,
            3,
        )
    };
    (result, entries)
}

fn stop_text(result: Result<(), PyException>) -> String {
    let error = result.unwrap_err();
    assert_eq!(error.type_name, "Stop", "{error:?}");
    message(&error)
}

// ---------------------------------------------------------------- tree と walk_at

#[test]
fn the_tree_root_swapped_between_lstat_and_open_is_refused() {
    let dir = Dir::new("race-root-lstat");
    layout(
        &dir.0,
        json!([{"path": "t/a", "kind": "file", "body": "a"}]),
    );
    let (result, _) = once_at("tree-after-lstat", swap_dir(&dir.0, "t"), || {
        run_tree(&dir.0, TreeOptions::default())
    });
    assert_eq!(stop_text(result), "ディレクトリが差し替えられた");
}

#[test]
fn the_tree_root_swapped_after_the_walk_is_refused() {
    let dir = Dir::new("race-root-open");
    layout(
        &dir.0,
        json!([{"path": "t/a", "kind": "file", "body": "a"}]),
    );
    let (result, _) = once_at("tree-after-open", swap_dir(&dir.0, "t"), || {
        run_tree(&dir.0, TreeOptions::default())
    });
    assert_eq!(stop_text(result), "走査中にルートが差し替えられた");
}

#[test]
fn a_file_added_after_the_names_were_read_changes_the_set_and_stops() {
    let dir = Dir::new("race-added");
    layout(
        &dir.0,
        json!([{"path": "t/a", "kind": "file", "body": "a"}]),
    );
    let added = dir.0.join("t/new");
    let (result, entries) = once_at(
        "walk-after-scan",
        move || std::fs::write(&added, "n").unwrap(),
        || run_tree(&dir.0, TreeOptions::default()),
    );
    assert_eq!(stop_text(result), "走査中にディレクトリの集合が変わった");
    // 新しい名前の本文は、読んでいない。
    assert!(!entry::contains(&entries, &cps("k/new")));
}

#[test]
fn a_subdirectory_swapped_between_lstat_and_open_is_refused() {
    let dir = Dir::new("race-sub-before");
    layout(
        &dir.0,
        json!([{"path": "t/sub/x", "kind": "file", "body": "x"}]),
    );
    let swap = swap_dir(&dir.0.join("t"), "sub");
    let (result, _) = once_at("walk-after-lstat", swap, || {
        run_tree(&dir.0, TreeOptions::default())
    });
    assert_eq!(stop_text(result), "走査前にディレクトリが変わった");
}

#[test]
fn a_subdirectory_swapped_after_the_walk_is_refused() {
    let dir = Dir::new("race-sub-after");
    layout(
        &dir.0,
        json!([{"path": "t/sub/x", "kind": "file", "body": "x"}]),
    );
    let swap = swap_dir(&dir.0.join("t"), "sub");
    let (result, _) = once_at("walk-after-open-dir", swap, || {
        run_tree(&dir.0, TreeOptions::default())
    });
    assert_eq!(stop_text(result), "走査中にディレクトリが変わった");
}

fn user_links_layout(dir: &Dir) {
    layout(
        &dir.0,
        json!([
            {"path": "store/skill/s.md", "kind": "file", "body": "s"},
            {"path": "t/own", "kind": "file", "body": "o"},
            {"path": "t/link", "kind": "link", "target": "{r}/store/skill"},
        ]),
    );
}

fn run_links(dir: &Dir) -> (Result<(), PyException>, PyDict) {
    run_tree(
        &dir.0,
        TreeOptions {
            user_links: true,
            ..TreeOptions::default()
        },
    )
}

#[test]
fn an_installation_link_target_swapped_during_the_walk_is_refused() {
    let dir = Dir::new("race-link-target");
    user_links_layout(&dir);
    let swap = swap_dir(&dir.0.join("store"), "skill");
    let (result, _) = once_at("walk-link-in-target", swap, || run_links(&dir));
    assert_eq!(stop_text(result), "導入リンクの対象が走査中に変わった");
}

#[test]
fn an_installation_link_replaced_after_the_walk_is_refused() {
    let dir = Dir::new("race-link-self");
    user_links_layout(&dir);
    let link = dir.0.join("t/link");
    let target = dir.0.join("store/skill");
    let (result, _) = once_at(
        "walk-link-after-target",
        move || {
            std::fs::remove_file(&link).unwrap();
            std::os::unix::fs::symlink(&target, &link).unwrap();
        },
        || run_links(&dir),
    );
    assert_eq!(stop_text(result), "導入リンクが走査中に変わった");
}

#[test]
fn an_interrupted_system_call_is_retried_and_the_result_is_the_same() {
    let dir = Dir::new("race-eintr");
    user_links_layout(&dir);
    let (plain, plain_entries) = run_links(&dir);
    plain.unwrap();
    // 各システムコールの 1 回目だけ `EINTR` を返す。再試行すれば、結果は同じになる。
    for name in ["lstat", "fstat", "open_dir", "readlink"] {
        let seen = Rc::new(Cell::new(0));
        let counter = seen.clone();
        let (result, entries) = with_syscall(
            move |call| {
                if call == name && counter.get() == 0 {
                    counter.set(1);
                    return Some(io::Error::from_raw_os_error(os::EINTR));
                }
                None
            },
            || run_links(&dir),
        );
        result.unwrap_or_else(|error| panic!("{name}: {error:?}"));
        assert_eq!(seen.get(), 1, "{name} の呼出しが無かった");
        assert_eq!(entries.len(), plain_entries.len(), "{name}");
    }
}

#[test]
fn an_interrupted_chmod_is_retried_and_a_real_error_is_reported() {
    let dir = Dir::new("race-chmod");
    layout(
        &dir.0,
        json!([{"path": "f", "kind": "file", "body": "x", "mode": 420}]),
    );
    let fd = os::open_root().unwrap();
    // 親を辿って開く代わりに、実パスの親を 1 段ずつ開く。
    let mut current = fd;
    for part in dir.0.components().skip(1) {
        current = os::open_dir_at(
            current.as_fd(),
            part.as_os_str().to_str().unwrap().as_bytes(),
        )
        .unwrap();
    }
    let seen = Rc::new(Cell::new(0));
    let counter = seen.clone();
    with_syscall(
        move |call| {
            if call == "chmod" && counter.get() == 0 {
                counter.set(1);
                return Some(io::Error::from_raw_os_error(os::EINTR));
            }
            None
        },
        || chmod_nofollow_at(current.as_fd(), &cps("f"), 0o600).unwrap(),
    );
    assert_eq!(seen.get(), 1);
    let mode = std::fs::metadata(dir.0.join("f")).unwrap();
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(mode.permissions().mode() & 0o777, 0o600);
    // `EINTR` 以外の誤りは、再試行せずにそのまま出す。
    let error = with_syscall(
        |call| (call == "fstat").then(|| io::Error::from_raw_os_error(5)),
        || fstat(current.as_fd()).unwrap_err(),
    );
    assert_eq!(error.type_name, "OSError");
    assert_eq!(message(&error), "[Errno 5] Input/output error");
}

// ---------------------------------------------------------------- component_tree

fn run_component(
    root: &Path,
    path: &str,
    kind: &str,
    expected: Option<&PyDict>,
) -> (Result<Cps, PyException>, PyDict) {
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let result = {
        let mut obs = Obs {
            host: &ProcessHost,
            budget: &mut budget,
            entries: &mut entries,
        };
        component_tree(
            &mut obs,
            &cps(root.join(path).to_str().unwrap()),
            &cps(kind),
            expected,
            None,
        )
    };
    (result, entries)
}

fn component_layout(dir: &Dir) {
    layout(
        &dir.0,
        json!([
            {"path": "p/a.txt", "kind": "file", "body": "a"},
            {"path": "p/sub/b.txt", "kind": "file", "body": "b"},
            {"path": "s/one", "kind": "link", "target": "../store/x"},
            {"path": "store/x/SKILL.md", "kind": "file", "body": "x"},
        ]),
    );
}

fn component_stop(result: Result<Cps, PyException>) -> String {
    let error = result.unwrap_err();
    assert_eq!(error.type_name, "Stop", "{error:?}");
    message(&error)
}

#[test]
fn a_component_parent_swapped_between_lstat_and_open_is_refused() {
    let dir = Dir::new("race-comp-lstat");
    component_layout(&dir);
    // 足取りは、親の数の後の 1 回が `p` 自身。
    let nth = ancestors(&dir) + 1;
    let (result, _) = nth_at(
        "component-after-step-lstat",
        nth,
        swap_dir(&dir.0, "p"),
        || run_component(&dir.0, "p", "plugin", None),
    );
    assert_eq!(
        component_stop(result.map(|_| Vec::new())),
        "component の親が open 時に変わった"
    );
}

#[test]
fn a_component_parent_swapped_after_open_is_found_by_the_final_check() {
    let dir = Dir::new("race-comp-open");
    component_layout(&dir);
    // 根の 1 つ手前(`p` の親)ではなく、`p` 自身を開いた直後に差し替える。
    let nth = ancestors(&dir) + 1;
    let (result, _) = nth_at("component-after-open", nth, swap_dir(&dir.0, "p"), || {
        run_component(&dir.0, "p", "plugin", None)
    });
    assert_eq!(
        component_stop(result),
        "component の親または導入リンクが検査中に変わった"
    );
}

#[test]
fn a_file_added_after_the_names_were_compared_stops_without_reading_it() {
    let dir = Dir::new("race-comp-names");
    component_layout(&dir);
    let added = dir.0.join("p/new.txt");
    let (result, entries) = once_at(
        "component-after-names",
        move || {
            std::fs::write(&added, "n").unwrap();
            // 読めない本文にして、もし読めば別の理由で止まるようにする。
            std::fs::set_permissions(&added, std::os::unix::fs::PermissionsExt::from_mode(0o0))
                .unwrap();
        },
        || run_component(&dir.0, "p", "plugin", None),
    );
    assert_eq!(component_stop(result), "component の集合が検査中に変わった");
    assert!(
        !entries
            .iter()
            .any(|(key, _)| to_string(super::pyops::as_text(key).unwrap()).contains("new.txt")),
        "新しい名前に触れた"
    );
}

#[test]
fn a_file_rewritten_after_its_stat_was_kept_is_refused_before_reading() {
    let dir = Dir::new("race-comp-file");
    component_layout(&dir);
    let path = dir.0.join("p/a.txt");
    let (result, _) = once_at(
        "component-after-file-stat",
        move || std::fs::write(&path, "z").unwrap(),
        || run_component(&dir.0, "p", "plugin", None),
    );
    assert_eq!(component_stop(result), "本文読取前に監視対象が変わった");
}

#[test]
fn a_cached_file_is_still_rechecked_after_its_stat_for_every_logical_name() {
    let dir = Dir::new("race-comp-cached");
    layout(
        &dir.0,
        json!([
            {"path": "p/a", "kind": "file", "body": "same"},
            {"path": "p/b", "kind": "hardlink", "target": "p/a"},
        ]),
    );
    // 2 つ目の名前(`b`)は、本文を読み直さない(控えを使う)。それでも、読取りの後の照合は省かない。
    let path = dir.0.join("p/a");
    let (result, _) = nth_at(
        "component-after-file-stat",
        2,
        move || std::fs::write(&path, "diff").unwrap(),
        || run_component(&dir.0, "p", "plugin", None),
    );
    assert_eq!(component_stop(result), "component が読取中に変わった");
}

#[test]
fn a_file_replaced_by_a_link_between_the_walk_and_the_read_is_refused() {
    let dir = Dir::new("race-comp-kind");
    component_layout(&dir);
    let path = dir.0.join("p/a.txt");
    let (result, _) = once_at(
        "component-walk-after-lstat",
        move || {
            std::fs::remove_file(&path).unwrap();
            std::os::unix::fs::symlink("sub", &path).unwrap();
        },
        || run_component(&dir.0, "p", "plugin", None),
    );
    assert_eq!(
        component_stop(result),
        "component が読める通常ファイルでない"
    );
}

#[test]
fn an_installation_link_replaced_after_its_walk_is_refused() {
    let dir = Dir::new("race-comp-link");
    component_layout(&dir);
    let link = dir.0.join("s/one");
    let (result, _) = once_at(
        "component-link-after-walk",
        move || {
            std::fs::remove_file(&link).unwrap();
            // 同じ行き先でも字面を変える(inode が使い回されても、字面の照合で見つかる)。
            std::os::unix::fs::symlink("../store/./x", &link).unwrap();
        },
        || run_component(&dir.0, "s", "personal-skill", None),
    );
    assert_eq!(
        component_stop(result),
        "component の導入リンクが検査中に変わった"
    );
}

#[test]
fn a_manifest_created_between_the_two_missing_checks_is_refused() {
    let dir = Dir::new("race-comp-manifest");
    layout(
        &dir.0,
        json!([{"path": "m/.claude-plugin/other", "kind": "file", "body": "x"}]),
    );
    let created = dir.0.join("m/.claude-plugin/marketplace.json");
    let (result, _) = once_at(
        "component-manifest-after-missing",
        move || std::fs::write(&created, "{}").unwrap(),
        || {
            run_component(
                &dir.0,
                "m/.claude-plugin/marketplace.json",
                "marketplace-manifest",
                None,
            )
        },
    );
    assert_eq!(
        component_stop(result),
        "component の不在 entry が検査中に新設された"
    );
}

#[test]
fn a_manifest_parent_changed_while_its_file_is_read_is_refused() {
    let dir = Dir::new("race-comp-manifest-parent");
    layout(
        &dir.0,
        json!([{"path": "m/.claude-plugin/marketplace.json", "kind": "file", "body": "{}"}]),
    );
    let added = dir.0.join("m/.claude-plugin/extra");
    let (result, _) = once_at(
        "component-after-file-stat",
        move || std::fs::write(&added, "e").unwrap(),
        || {
            run_component(
                &dir.0,
                "m/.claude-plugin/marketplace.json",
                "marketplace-manifest",
                None,
            )
        },
    );
    assert_eq!(component_stop(result), "component の集合が検査中に変わった");
}

// ---------------------------------------------------------------- record

fn run_record(
    dir: &Dir,
    rel: &str,
    expected: Option<&PyDict>,
) -> (Result<Option<Vec<u8>>, PyException>, PyDict) {
    let mut budget = EnvBudget::new();
    let mut entries = PyDict::new();
    let result = record(
        &ProcessHost,
        &cps(dir.0.join(rel).to_str().unwrap()),
        &cps("external:x"),
        &mut budget,
        &mut entries,
        &RecordOptions {
            expected,
            ..RecordOptions::default()
        },
    );
    (result, entries)
}

fn record_stop(result: Result<Option<Vec<u8>>, PyException>) -> String {
    let error = result.unwrap_err();
    assert_eq!(error.type_name, "Stop", "{error:?}");
    message(&error)
}

#[test]
fn a_file_replaced_after_the_record_lstat_is_refused_before_reading() {
    let dir = Dir::new("race-rec-replace");
    layout(
        &dir.0,
        json!([{"path": "c/f", "kind": "file", "body": "same"}]),
    );
    let (from, to) = (dir.0.join("c/f"), dir.0.join("c/f.new"));
    let (result, entries) = once_at(
        "record-after-lstat",
        move || {
            std::fs::write(&to, "same").unwrap();
            std::fs::rename(&to, &from).unwrap();
        },
        || run_record(&dir, "c/f", None),
    );
    assert_eq!(record_stop(result), "本文読取前に監視対象が変わった");
    assert!(entries.is_empty());
}

#[test]
fn a_file_removed_after_the_record_lstat_is_an_error_not_a_missing_file() {
    let dir = Dir::new("race-rec-remove");
    layout(
        &dir.0,
        json!([{"path": "c/f", "kind": "file", "body": "x"}]),
    );
    let path = dir.0.join("c/f");
    let (result, entries) = once_at(
        "record-after-lstat",
        move || std::fs::remove_file(&path).unwrap(),
        || run_record(&dir, "c/f", None),
    );
    let error = result.unwrap_err();
    assert_eq!(error.type_name, "FileNotFoundError");
    // 何かを開いた後の不在は、「不在」として記録しない。
    assert!(entries.is_empty());
}

#[test]
fn a_link_target_swapped_before_it_is_read_is_refused() {
    let dir = Dir::new("race-rec-target");
    layout(
        &dir.0,
        json!([
            {"path": "c/f", "kind": "file", "body": "same"},
            {"path": "c/l", "kind": "link", "target": "f"},
        ]),
    );
    let (from, to) = (dir.0.join("c/f"), dir.0.join("c/f.new"));
    let (result, _) = once_at(
        "record-before-link-target",
        move || {
            std::fs::write(&to, "same").unwrap();
            std::fs::rename(&to, &from).unwrap();
        },
        || run_record(&dir, "c/l", None),
    );
    assert_eq!(record_stop(result), "本文読取前にリンク対象が変わった");
}

#[test]
fn a_link_target_replaced_after_it_was_read_is_refused() {
    let dir = Dir::new("race-rec-target-after");
    layout(
        &dir.0,
        json!([
            {"path": "c/f", "kind": "file", "body": "same"},
            {"path": "c/l", "kind": "link", "target": "f"},
        ]),
    );
    let (from, to) = (dir.0.join("c/f"), dir.0.join("c/f.new"));
    let (result, _) = once_at(
        "record-after-link-read",
        move || {
            std::fs::write(&to, "same").unwrap();
            std::fs::rename(&to, &from).unwrap();
        },
        || run_record(&dir, "c/l", None),
    );
    assert_eq!(
        record_stop(result),
        "利用者設定のリンク対象が読取中に変わった"
    );
}

#[test]
fn the_link_replaced_after_its_target_was_read_is_refused() {
    let dir = Dir::new("race-rec-link-after");
    layout(
        &dir.0,
        json!([
            {"path": "c/f", "kind": "file", "body": "same"},
            {"path": "c/l", "kind": "link", "target": "f"},
        ]),
    );
    let link = dir.0.join("c/l");
    let (result, _) = once_at(
        "record-after-link-read",
        move || {
            std::fs::remove_file(&link).unwrap();
            std::os::unix::fs::symlink("f", &link).unwrap();
        },
        || run_record(&dir, "c/l", None),
    );
    assert_eq!(record_stop(result), "利用者設定のリンクが読取中に変わった");
}

#[test]
fn identity_of_an_untouched_file_is_stable_across_two_reads() {
    // フックの無い経路が、製品と同じ結果を返すことの確認(上の競合の検査の対照)。
    let dir = Dir::new("race-control");
    layout(
        &dir.0,
        json!([{"path": "c/f", "kind": "file", "body": "same"}]),
    );
    let (first, entries_a) = run_record(&dir, "c/f", None);
    let (second, entries_b) = run_record(&dir, "c/f", Some(&entries_a));
    assert_eq!(first.unwrap(), second.unwrap());
    assert!(
        entry::get(&entries_a, &cps("external:x"))
            .unwrap()
            .py_eq(entry::get(&entries_b, &cps("external:x")).unwrap())
    );
    let _ = identity;
}
