#![forbid(unsafe_code)]
#![cfg(unix)]

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT: AtomicUsize = AtomicUsize::new(0);

type Tree = BTreeMap<PathBuf, (u32, Vec<u8>)>;

fn snapshot(root: &Path) -> Tree {
    fn visit(root: &Path, path: &Path, tree: &mut Tree) {
        let meta = fs::symlink_metadata(path).unwrap();
        let data = if meta.is_symlink() {
            fs::read_link(path).unwrap().as_os_str().as_bytes().to_vec()
        } else if meta.is_file() {
            fs::read(path).unwrap()
        } else {
            for entry in fs::read_dir(path).unwrap() {
                visit(root, &entry.unwrap().path(), tree)
            }
            Vec::new()
        };
        tree.insert(
            path.strip_prefix(root).unwrap().to_path_buf(),
            (meta.permissions().mode(), data),
        );
    }
    let mut tree = Tree::new();
    visit(root, root, &mut tree);
    tree
}

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "resolve-task-dir-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn state(&self, directory: impl AsRef<Path>, name: &str) {
        let directory = self.0.join(directory);
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join(name), b"# task\nunchanged\0\xff").unwrap();
    }
    fn run(&self, args: &[&OsStr]) -> Output {
        let before = snapshot(&self.0);
        let output = Command::new(env!("CARGO_BIN_EXE_dev-workflow"))
            .arg("resolve-task-dir")
            .args(args)
            .current_dir(&self.0)
            .env_clear()
            .env("COLUMNS", "80")
            .output()
            .unwrap();
        assert_eq!(snapshot(&self.0), before);
        output
    }
    fn resolve(&self, task_dir: Option<&OsStr>) -> Output {
        let mut args = vec![OsStr::new("--project-root"), OsStr::new(".")];
        if let Some(value) = task_dir {
            args.extend([OsStr::new("--task-dir"), value]);
        }
        self.run(&args)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap()
    }
}

fn payload(output: &Output, code: i32) -> serde_json::Value {
    assert_eq!(output.status.code(), Some(code), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert!(output.stdout.ends_with(b"\n"));
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn default_detection_priority_and_exit_two_are_read_only() {
    let f = Fixture::new();
    let result = payload(&f.resolve(None), 0);
    assert_eq!(result["task_dir"], "docs/tasks");
    assert_eq!(result["source"], "default");
    assert!(!f.0.join("docs").exists());
    f.state("task", "進行中_項目.md");
    assert_eq!(payload(&f.resolve(None), 0)["source"], "detected");
    f.state("docs/task", "候補_項目.md");
    assert_eq!(
        payload(&f.resolve(None), 2)["candidates"],
        serde_json::json!(["docs/task", "task"])
    );
    assert_eq!(
        payload(&f.resolve(Some(OsStr::new("explicit"))), 0)["source"],
        "profile"
    );
}

#[test]
fn link_parent_file_and_placeholder_boundaries() {
    let f = Fixture::new();
    fs::create_dir_all(f.0.join("a/b")).unwrap();
    symlink("a/b", f.0.join("link")).unwrap();
    assert_eq!(
        payload(&f.resolve(Some(OsStr::new("link/../tasks"))), 0)["task_dir"],
        "a/tasks"
    );
    fs::write(f.0.join("file"), b"data").unwrap();
    symlink("file", f.0.join("file-link")).unwrap();
    symlink("missing", f.0.join("broken")).unwrap();
    symlink("/", f.0.join("external")).unwrap();
    symlink("cycle", f.0.join("cycle")).unwrap();
    for value in [
        "file/..",
        "file-link/..",
        "broken/x",
        "external",
        "cycle/x",
        "",
        "{{X}}/../docs",
    ] {
        assert!(payload(&f.resolve(Some(OsStr::new(value))), 1)["error"].is_string());
    }
    assert_eq!(
        payload(&f.resolve(Some(OsStr::new("missing/../x"))), 0)["task_dir"],
        "x"
    );
}

#[test]
fn state_links_empty_suffixes_newlines_and_deep_files_are_ignored() {
    let f = Fixture::new();
    for name in ["進行中_.md", "進行中_a\nb.md", "完了_x.md\n"] {
        f.state("task", name);
    }
    f.state("docs/archive/deep", "進行中_項目.md");
    fs::write(f.0.join("進行中_実体.md"), b"task").unwrap();
    symlink("../進行中_実体.md", f.0.join("task/進行中_リンク.md")).unwrap();
    assert_eq!(payload(&f.resolve(None), 0)["source"], "default");
}

#[test]
fn invalid_utf8_and_control_bytes_survive_json() {
    let f = Fixture::new();
    let name = OsString::from_vec(b"task-\xff\n\t\"\\".to_vec());
    let output = f.resolve(Some(&name));
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stderr.is_empty());
    assert!(
        output
            .stdout
            .windows(b"task-\xff\\n\\t\\\"\\\\".len())
            .any(|part| part == b"task-\xff\\n\\t\\\"\\\\")
    );
    assert!(output.stdout.starts_with(b"{\"path\": "));
}

#[test]
fn argument_errors_use_argparse_stream_and_help_order() {
    let f = Fixture::new();
    for args in [
        vec![],
        vec![OsStr::new("--project-root")],
        vec![OsStr::new("--task-dir"), OsStr::new("--help")],
    ] {
        let output = f.run(&args);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert!(
            output
                .stderr
                .starts_with(b"usage: dev-workflow resolve-task-dir")
        );
    }
    for args in [
        [OsStr::new("--unknown"), OsStr::new("--help")],
        [OsStr::new("-hh"), OsStr::new("--project-root")],
    ] {
        let output = f.run(&args);
        assert_eq!(output.status.code(), Some(0));
        assert!(output.stderr.is_empty());
        assert!(output.stdout.starts_with(b"usage:"));
    }
}
