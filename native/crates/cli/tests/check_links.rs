#![deny(unsafe_code)]
//! R4U(#480)の専用比較を通常の `cargo test` で必ず流す。製品への登録は行わない。
//! - `--check-links-fixture <引数…>` を受けたときだけ、実際の標準入出力で検査対象として動く
//!   (旧版を `python3 check_links.py <引数…>` で起動したときと同じ動作)。
//! - それ以外では `native/ci/check_r4u.py` を、自分を検査対象として起動する。
//!   失敗・未実行・時間切れ(全体 900 秒)でテストを落とす。
#[path = "../src/commands/check_links/mod.rs"]
// 文字列だけの部品のうち、表を写して照らす関数は tests/check_links_units.rs だけが使う。
#[allow(dead_code)]
mod check_links;

use dev_workflow_platform::check_links::{capture_before_main, captured_before_main};
use std::ffi::OsStr;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

const FIXTURE_FLAG: &str = "--check-links-fixture";
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

struct Checker(Child);

impl Drop for Checker {
    fn drop(&mut self) {
        let _ = self.0.kill();
        self.0.wait().expect("専用比較器を回収できない");
    }
}

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.first().map(|arg| arg.as_os_str()) == Some(OsStr::new(FIXTURE_FLAG)) {
        return check_links::run(&args[1..]).into();
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
    let checker = root.join("native/ci/check_r4u.py");
    assert!(checker.is_file(), "R4U の比較器がない");
    let mut child = Checker(
        Command::new("python3")
            .arg("-B")
            .arg(&checker)
            .arg("--fixture")
            .arg(std::env::current_exe().expect("自分の実行物のパスを得られない"))
            .current_dir(&root)
            .spawn()
            .expect("R4U の比較器を起動できない"),
    );
    let deadline = Instant::now() + DEADLINE;
    loop {
        if let Some(status) = child.0.try_wait().expect("R4U の比較器の待機に失敗") {
            assert!(status.success(), "R4U の比較が不合格: {status}");
            return std::process::ExitCode::SUCCESS;
        }
        assert!(Instant::now() < deadline, "R4U の比較が時間切れ");
        std::thread::sleep(Duration::from_millis(20));
    }
}
