#![deny(unsafe_code)]
//! R4Q(#494)の検査用の実行物(`harness = false`)。製品への登録は行わない。引数で動きを変える。
//! - `--r4q-loop-supervisor <旧版と同じ引数…>`: loop-supervisor として動く(`python3 loop-supervisor.py …` と同じ)。
//! - `--r4q-check-process <旧版と同じ引数…>`: check-process の監督として動く(`python3 check-process.py …`)。
//! - `--r4q-run-command <要求の JSON のパス>`: `StopSignals` に入って `run_command` を呼び、結果か誤りを JSON で
//!   標準出力へ出す(設計の「検査用の要求と結果の JSON の形」)。
//! - `--r4q-compare`: `native/ci/check_r4q.py --rust <自分>` と `native/ci/test_r4q.py` を起動し、どちらかの
//!   失敗・未実行・時間切れでテストを落とす(CI はジョブ `r4q`)。
//! - 引数なし(素の `cargo test --locked`。CI の build): 比較を流していないことと流し方を 1 行出して終了 0。
//! - ほかの引数は受け付けない。
#[path = "../src/commands/check_process/mod.rs"]
// 単位 3 の比較器と R4V が使う関数のうち、この実行物が呼ばないものがある。
#[allow(dead_code)]
mod check_process;

use check_process::loop_supervisor;
use check_process::loop_supervisor::r4q_python::number::{DEFAULT_MAX_STR_DIGITS, PyNumber};
use check_process::loop_supervisor::r4q_python::value::{self, PyValue};
use check_process::run_command::{
    CALLER_FRAMES, Method, Monitor, PYTHON_RECURSION_LIMIT, RunError, RunRequest, StopSignals,
    os_string, run_command,
};
use dev_workflow_platform::check_links::capture_before_main;
use serde_json::{Value, json};
use std::ffi::{OsStr, OsString};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode};
use std::time::{Duration, Instant};

const LOOP_SUPERVISOR_FLAG: &str = "--r4q-loop-supervisor";
const CHECK_PROCESS_FLAG: &str = "--r4q-check-process";
const RUN_COMMAND_FLAG: &str = "--r4q-run-command";
/// 比較器と自己検査を起動するテストの引数(CI のジョブ `r4q` が渡す)。
const COMPARE_FLAG: &str = "--r4q-compare";
/// 引数が無いときに出す 1 行。比較器の結果の行の頭(`R4Q の比較: `)とは別の字面にする。
const NOT_RUN: &str = "R4Q の比較は流していない: 流すときは R4Q_PYTHON=<本家 3.10.11 の Python> を渡して \
     cargo test --locked -p dev-workflow-cli --test r4q -- --r4q-compare(CI はジョブ r4q の \
     python3 -B native/ci/build.py r4q --platform <機械>)";
/// 比較全体の上限。
const DEADLINE: Duration = Duration::from_secs(3000);

/// main の前に、親から受け継いだ SIGPIPE の扱いと fd 0〜2 が使えるかを控える(旧 Python の起動の判定のため)。
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

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("リポジトリの直下を求められない")
}

fn unhex(text: &str) -> Result<Vec<u8>, String> {
    if !text.len().is_multiple_of(2) {
        return Err(format!("16 進の長さが奇数: {text}"));
    }
    (0..text.len())
        .step_by(2)
        .map(|at| {
            u8::from_str_radix(&text[at..at + 2], 16).map_err(|_| format!("16 進でない: {text}"))
        })
        .collect()
}

fn hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// 要求の文字列(UTF-8〈surrogatepass〉の 16 進)を、旧版の str と同じ OsString(surrogateescape)にする。
fn os_text(value: &Value) -> Result<OsString, String> {
    let raw = unhex(value.as_str().ok_or("文字列でない")?)?;
    let codes = loop_supervisor::r4q_python::codec::decode_surrogatepass(&raw, "utf-8")
        .map_err(|error| error.message())?;
    let bytes = value::encode_surrogateescape(&codes).ok_or("surrogateescape で表せない")?;
    Ok(os_string(&bytes))
}

fn number(value: &Value) -> Result<PyNumber, String> {
    let map = value.as_object().ok_or("数の形でない")?;
    match (map.get("int"), map.get("float")) {
        (Some(Value::String(digits)), None) => Ok(PyNumber::Int(digits.clone())),
        (None, Some(Value::String(repr))) => Ok(PyNumber::Float(match repr.as_str() {
            "nan" => f64::NAN,
            "inf" => f64::INFINITY,
            "-inf" => f64::NEG_INFINITY,
            other => other
                .parse()
                .map_err(|_| format!("float の repr でない: {other}"))?,
        })),
        _ => Err("数は {\"int\": …} か {\"float\": …}".to_owned()),
    }
}

/// 結果の値の形(設計の結果の JSON)。
fn value_json(value: &PyValue) -> Value {
    match value {
        PyValue::None => json!({"none": null}),
        PyValue::Bool(flag) => json!({"bool": flag}),
        PyValue::Int(digits) => json!({"int": digits}),
        PyValue::Float(number) => {
            json!({"float": loop_supervisor::r4q_python::number::float_repr(*number)})
        }
        PyValue::Str(codes) => json!({"str": hex(&value::encode_surrogatepass(codes))}),
        PyValue::Bytes(raw) => json!({"bytes": hex(raw)}),
        PyValue::List(items) => json!({"list": items.iter().map(value_json).collect::<Vec<_>>()}),
        PyValue::Dict(pairs) => json!({"dict": pairs
            .iter()
            .map(|(key, item)| json!([value_json(key), value_json(item)]))
            .collect::<Vec<_>>()}),
    }
}

const KEYS: [&str; 10] = [
    "argv",
    "cwd",
    "env",
    "timeout",
    "grace",
    "method",
    "keep",
    "directory",
    "monitor",
    "int_max_str_digits",
];

/// `--r4q-run-command <要求>`。
fn run_command_mode(path: &Path) -> Result<Value, String> {
    let request: Value =
        serde_json::from_slice(&std::fs::read(path).map_err(|error| error.to_string())?)
            .map_err(|error| error.to_string())?;
    let map = request.as_object().ok_or("要求が辞書でない")?;
    let mut keys: Vec<&str> = map.keys().map(String::as_str).collect();
    keys.sort_unstable();
    let mut want = KEYS.to_vec();
    want.sort_unstable();
    if keys != want {
        return Err(format!("要求の鍵が違う: {keys:?}"));
    }
    let argv = map["argv"]
        .as_array()
        .ok_or("argv が配列でない")?
        .iter()
        .map(os_text)
        .collect::<Result<Vec<_>, _>>()?;
    let cwd = match &map["cwd"] {
        Value::Null => None,
        other => Some(PathBuf::from(os_text(other)?)),
    };
    let env = match &map["env"] {
        Value::Null => None,
        Value::Array(pairs) => Some(
            pairs
                .iter()
                .map(|pair| Ok((os_text(&pair[0])?, os_text(&pair[1])?)))
                .collect::<Result<Vec<_>, String>>()?,
        ),
        _ => return Err("env の形が違う".to_owned()),
    };
    let method = match &map["method"] {
        Value::Null => None,
        Value::String(name) if name == "linux" => Some(Method::Linux),
        Value::String(name) if name == "posix" => Some(Method::Posix),
        _ => return Err("method は linux か posix か null".to_owned()),
    };
    let monitor = match map["monitor"].as_str() {
        Some("rust") => Monitor {
            program: std::env::current_exe()
                .map_err(|error| error.to_string())?
                .into_os_string(),
            prefix: vec![OsString::from(CHECK_PROCESS_FLAG)],
        },
        Some("legacy") => Monitor {
            program: std::env::var_os("R4Q_PYTHON").unwrap_or_else(|| OsString::from("python3")),
            prefix: vec![
                OsString::from("-B"),
                root()
                    .join("plugins/dev-workflow/skills/ship-task/scripts/check-process.py")
                    .into_os_string(),
            ],
        },
        _ => return Err("monitor は legacy か rust".to_owned()),
    };
    let directory = PathBuf::from(os_text(&map["directory"])?);
    let keep = map["keep"].as_u64().ok_or("keep が整数でない")? as usize;
    let digits = map["int_max_str_digits"]
        .as_u64()
        .map_or(DEFAULT_MAX_STR_DIGITS, |value| value as usize);
    let request = RunRequest {
        argv: &argv,
        cwd: cwd.as_deref(),
        env: env.as_deref(),
        timeout: number(&map["timeout"])?,
        directory: &directory,
        grace: number(&map["grace"])?,
        method,
        keep,
        monitor: &monitor,
        int_max_str_digits: digits,
        caller_frames: CALLER_FRAMES,
        recursion_limit: PYTHON_RECURSION_LIMIT,
    };
    let stop = StopSignals::enter();
    let result = run_command(&request, &stop);
    drop(stop);
    Ok(match result {
        Ok(pairs) => json!({"result": pairs
            .iter()
            .map(|(key, item)| json!([value_json(key), value_json(item)]))
            .collect::<Vec<_>>()}),
        Err(error) => {
            let (kind, message) = match error {
                RunError::Recovery(message) => ("RecoveryError".to_owned(), message),
                RunError::Interrupted(message) => ("Interrupted".to_owned(), message),
                RunError::Unwrapped { kind, message } => (kind, message),
            };
            json!({"error": {"type": kind, "message": hex(message.as_bytes())}})
        }
    })
}

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

/// `--r4q-compare`。比較器と自己検査を起動し、両方の結果を待つ。
fn compare() -> ExitCode {
    let root = root();
    let exe = std::env::current_exe().expect("自分の実行物のパスを得られない");
    let mut checkers = [
        spawn(
            "R4Q の比較器",
            &root,
            "native/ci/check_r4q.py",
            &[OsStr::new("--rust"), exe.as_os_str()],
        ),
        spawn(
            "R4Q の比較器の自己検査",
            &root,
            "native/ci/test_r4q.py",
            &[],
        ),
    ];
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
            "R4Q の比較が時間切れ(全体 {} 秒){}",
            DEADLINE.as_secs(),
            failures.join("・")
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(failures.is_empty(), "{}", failures.join("・"));
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let first = args.first().map(OsString::as_os_str);
    if first == Some(OsStr::new(LOOP_SUPERVISOR_FLAG)) {
        return ExitCode::from(loop_supervisor::run(&args[1..]));
    }
    if first == Some(OsStr::new(CHECK_PROCESS_FLAG)) {
        return ExitCode::from(check_process::run(&args[1..]));
    }
    if first == Some(OsStr::new(RUN_COMMAND_FLAG)) && args.len() == 2 {
        return match run_command_mode(Path::new(&args[1])) {
            Ok(result) => {
                let mut out = std::io::stdout().lock();
                serde_json::to_writer(&mut out, &result).expect("結果を書けない");
                out.write_all(b"\n").expect("結果を書けない");
                ExitCode::SUCCESS
            }
            Err(message) => {
                eprintln!("要求を読めない: {message}");
                ExitCode::from(2)
            }
        };
    }
    if args.is_empty() {
        println!("{NOT_RUN}");
        return ExitCode::SUCCESS;
    }
    if args.len() == 1 && args[0] == COMPARE_FLAG {
        return compare();
    }
    eprintln!(
        "受け付ける引数は {LOOP_SUPERVISOR_FLAG}・{CHECK_PROCESS_FLAG}・{RUN_COMMAND_FLAG} <要求>・{COMPARE_FLAG} \
         か、引数なし: {args:?}"
    );
    ExitCode::from(2)
}
