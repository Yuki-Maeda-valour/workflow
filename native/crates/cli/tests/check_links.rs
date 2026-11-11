#![deny(unsafe_code)]
//! R4U(#480)の専用比較を通常の `cargo test` で必ず流す。製品への登録は行わない。
//! - `--check-links-fixture <引数…>` を受けたときだけ、実際の標準入出力で検査対象として動く
//!   (旧版を `python3 check_links.py <引数…>` で起動したときと同じ動作)。
//! - `--check-links-matcher <生成入力の JSON>` を受けたときは、2 種類の照合を生成入力の全件に当てた結果を
//!   JSON で出す(比較器が、旧版の関数を 1 回の Python の起動で当てた結果・固定期待と比べる)。
//! - それ以外では `native/ci/check_r4u.py`(自分を検査対象として渡す)と `native/ci/test_r4u.py`
//!   (比較器の自己検査)を必ず起動する。どちらかの失敗・未実行・時間切れ(全体 900 秒)でテストを落とす。
#[path = "../src/commands/check_links/mod.rs"]
// 文字列だけの部品のうち、表を写して照らす関数は tests/check_links_units.rs だけが使う。
#[allow(dead_code)]
mod check_links;

use check_links::matcher::{self, Found};
use dev_workflow_platform::check_links::{capture_before_main, captured_before_main};
use serde_json::{Value, json};
use std::ffi::OsStr;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

const FIXTURE_FLAG: &str = "--check-links-fixture";
const MATCHER_FLAG: &str = "--check-links-matcher";
/// 比較全体の上限。task-digest と同じく、Mac Intel の変動に備えて余裕を持たせる。
const DEADLINE: Duration = Duration::from_secs(900);

/// main の前に、親から受け継いだ SIGPIPE の扱いと fd 0〜2 が使えるかを控える(旧 Python の起動の判定のため)。
/// 製品の実行物には置かない。製品へつなぐときは、配布の担当が入口に同じ static を置く。
// link_section は unsafe な属性。この static のためだけに許す。
#[cfg(target_os = "linux")]
#[allow(unsafe_code)]
#[used]
#[unsafe(link_section = ".init_array")]
static CAPTURE_BEFORE_MAIN: unsafe extern "C" fn() = capture_before_main;

// link_section は unsafe な属性。この static のためだけに許す。
#[cfg(target_os = "macos")]
#[allow(unsafe_code)]
#[used]
#[unsafe(link_section = "__DATA,__mod_init_func,mod_init_funcs")]
static CAPTURE_BEFORE_MAIN: unsafe extern "C" fn() = capture_before_main;

struct Checker {
    name: &'static str,
    child: Child,
    done: bool,
}

impl Drop for Checker {
    fn drop(&mut self) {
        if !self.done {
            let _ = self.child.kill();
            self.child.wait().expect("専用比較器を回収できない");
        }
    }
}

fn char_offset(text: &str, byte: usize) -> usize {
    text[..byte].chars().count()
}

/// 生成入力の全件に 2 種類の照合を当て、固定期待(matcher-corpus.json)の `links`・`codes` と同じ形で出す。
fn matcher_fixture(path: &Path) -> std::process::ExitCode {
    let corpus: Value = serde_json::from_slice(&std::fs::read(path).expect("生成入力を読めない"))
        .expect("生成入力が JSON でない");
    let cases = corpus["cases"].as_array().expect("cases が配列でない");
    let results: Vec<Value> = cases
        .iter()
        .map(|case| {
            let text = case["text"].as_str().expect("text が文字列でない");
            let links: Vec<Value> = matcher::links(text)
                .into_iter()
                .map(|Found { start, group }| {
                    json!([
                        char_offset(text, start),
                        matcher::line_number(text, start),
                        group,
                        matcher::link_target(group)
                    ])
                })
                .collect();
            let codes: Vec<Value> = matcher::code_paths(text)
                .into_iter()
                .map(|Found { start, group }| {
                    json!([
                        char_offset(text, start),
                        matcher::line_number(text, start),
                        group,
                        matcher::code_skipped(group)
                    ])
                })
                .collect();
            json!({"links": links, "codes": codes})
        })
        .collect();
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, &results).expect("照合の結果を書けない");
    out.write_all(b"\n").expect("照合の結果を書けない");
    std::process::ExitCode::SUCCESS
}

fn spawn(name: &'static str, root: &Path, script: &str, args: &[&OsStr]) -> Checker {
    let path = root.join(script);
    assert!(path.is_file(), "{name} がない: {}", path.display());
    let child = Command::new("python3")
        .arg("-B")
        .arg(&path)
        .args(args)
        .current_dir(root)
        .spawn()
        .unwrap_or_else(|error| panic!("{name} を起動できない: {error}"));
    Checker {
        name,
        child,
        done: false,
    }
}

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.first().map(|arg| arg.as_os_str()) == Some(OsStr::new(FIXTURE_FLAG)) {
        return check_links::run(&args[1..]).into();
    }
    if args.first().map(|arg| arg.as_os_str()) == Some(OsStr::new(MATCHER_FLAG)) {
        assert_eq!(args.len(), 2, "生成入力の JSON を 1 つ渡す");
        return matcher_fixture(Path::new(&args[1]));
    }
    assert!(args.is_empty(), "専用比較を省く引数は受け付けない");
    assert!(
        captured_before_main(),
        "main の前の控えが実行物に入っていない"
    );
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("リポジトリの直下を求められない");
    let exe = std::env::current_exe().expect("自分の実行物のパスを得られない");
    let mut checkers = [
        spawn(
            "R4U の比較器",
            &root,
            "native/ci/check_r4u.py",
            &[OsStr::new("--fixture"), exe.as_os_str()],
        ),
        spawn(
            "R4U の比較器の自己検査",
            &root,
            "native/ci/test_r4u.py",
            &[],
        ),
    ];
    // 片方が落ちても、もう片方の結果まで待ってから判定する(Mac の CI は 1 回が長いので、1 回で両方の結果を出す)。
    let deadline = Instant::now() + DEADLINE;
    let mut failures = Vec::new();
    while checkers.iter().any(|checker| !checker.done) {
        for checker in checkers.iter_mut().filter(|checker| !checker.done) {
            if let Some(status) = checker.child.try_wait().expect("専用比較器の待機に失敗")
            {
                checker.done = true;
                if !status.success() {
                    failures.push(format!("{} が不合格: {status}", checker.name));
                }
            }
        }
        assert!(
            Instant::now() < deadline,
            "R4U の比較が時間切れ(全体 {} 秒){}",
            DEADLINE.as_secs(),
            failures.join("・")
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(failures.is_empty(), "{}", failures.join("・"));
    std::process::ExitCode::SUCCESS
}
