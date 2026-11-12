//! `read`・`exec`・`hook`(旧版の `run_copy`)。照合してから、コピーの中の相対パスだけを扱う。
//!
//! - `read`: コピーの中の通常ファイルを `read_regular` で読み、標準出力へ生バイトで書く。
//! - `exec`: `python3` か `bash` だけを、PATH から探して子として起動し、子の終了コードを返す(信号で終われば、
//!   旧 Python の `subprocess.run` の `returncode`〈負の値〉)。SIGINT は旧 `subprocess` と同じ後始末
//!   (最初の `KeyboardInterrupt` で子を 0.25 秒だけ待ち、`kill` する)。
//! - `hook`: 引数の検査・照合・パスの検査・保持値の確認まで行い、判定器は実行せず止める(U3)。
use super::args::Parsed;
use super::error::{PyException, from_io};
use super::fsio::{read_regular, site};
use super::pyjson::PyCtx;
use super::pyops::{Cps, as_text, lit, not_subscriptable, sha_hex};
use super::pypath::PyPath;
use super::pystr::{decode_fs, encode_error, encode_fs};
use super::pyvalue::{PyDict, PyValue};
use super::runtime::PyStream;
use super::snapshot::verify;
use super::specs::Host;
use dev_workflow_core::loop_state::CodepointString;
use dev_workflow_platform::environment_guard as os;
use std::time::Duration;

/// `hook` が実行を許す、コピーの中の唯一のパス。
const HOOK_PATH: &str = "skills/ship-task/scripts/loop-permission.py";
/// 判定器を実行しない理由(U3)。
pub const HOOK_STOP: &str = "hook は R4P まで旧版の判定器で実行する";
/// 最初の `KeyboardInterrupt` の後、子の終了を待つ長さ(旧 `Popen._sigint_wait_secs`)。
const SIGINT_WAIT: Duration = Duration::from_millis(250);

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn argument(parsed: &Parsed, dest: &str) -> Cps {
    parsed
        .namespace
        .value(dest)
        .expect("`argparse` が必須の引数を確かめている")
        .py_str()
}

/// `Path(value['copy']) / rel` の `str`。
fn copy_path(value: &PyDict, rel: &PyPath) -> Result<Cps, PyException> {
    let Some(copy) = value.get_str("copy") else {
        return Err(PyException::new("KeyError", "'copy'"));
    };
    let Some(copy) = as_text(copy) else {
        // 届かない分岐: 控えの `copy` は、`bootstrap` が文字列で書く(照合は外部に保持した SHA-256 で通る)。
        return Err(PyException::type_error(&format!(
            "argument should be a str object or an os.PathLike object returning str, not <class '{}'>",
            copy.type_name()
        )));
    };
    Ok(PyPath::new(copy).join(&rel.to_cps()).to_cps())
}

/// `value['entries']['plugin/' + path][2]`(保持した本文の SHA-256)。
pub(super) fn held_sha(value: &PyDict, path: &[u32]) -> Result<PyValue, PyException> {
    let Some(entries) = value.get_str("entries") else {
        return Err(PyException::new("KeyError", "'entries'"));
    };
    let Some(entries) = entries.as_dict() else {
        return Err(not_subscriptable(entries));
    };
    let key = [lit("plugin/").as_slice(), path].concat();
    let Some(entry) = entries.get(&super::pyops::text(key.clone()))? else {
        return Err(PyException::with_cps(
            "KeyError",
            CodepointString::try_from_codepoints(super::pystr::repr(&key))
                .expect("文字番号は範囲内"),
        ));
    };
    match entry {
        PyValue::List(items) => items
            .get(2)
            .cloned()
            .ok_or_else(|| PyException::new("IndexError", "list index out of range")),
        PyValue::Str(text) => text
            .as_codepoints()
            .get(2)
            .map(|&c| super::pyops::text(vec![c]))
            .ok_or_else(|| PyException::new("IndexError", "string index out of range")),
        PyValue::Bytes(bytes) => bytes
            .get(2)
            .map(|&b| PyValue::from(i64::from(b)))
            .ok_or_else(|| PyException::new("IndexError", "index out of range")),
        PyValue::Dict(_) => Err(PyException::new("KeyError", "2")),
        other => Err(not_subscriptable(other)),
    }
}

/// `os.fsencode(text)`(`subprocess` が引数を文字列から作る所)。
fn fsencode(text: &[u32]) -> Result<Vec<u8>, PyException> {
    encode_fs(text).map_err(|failure| encode_error(&failure))
}

/// `dict[key] = value`(あれば同じ位置で置き換え、無ければ末尾へ)。
fn set_env(list: &mut Vec<(Vec<u8>, Vec<u8>)>, key: &[u8], value: &[u8]) {
    match list.iter_mut().find(|(name, _)| name == key) {
        Some(slot) => slot.1 = value.to_vec(),
        None => list.push((key.to_vec(), value.to_vec())),
    }
}

fn spawn_failure(error: &os::SpawnError, argv0: &[u8]) -> PyException {
    match error {
        os::SpawnError::Os { errno, .. } => {
            // `errpipe` の読取りが `SIGINT` で中断されたときは、`KeyboardInterrupt`。
            if *errno == os::EINTR && os::take_sigint() {
                return PyException::new("KeyboardInterrupt", "");
            }
            let filename = error.filename(argv0, None).map(decode_fs);
            from_io(
                &std::io::Error::from_raw_os_error(*errno),
                filename.as_deref(),
            )
        }
        os::SpawnError::EmbeddedNul => PyException::value_error("embedded null byte"),
        os::SpawnError::EmptyArgv => PyException::new("IndexError", "list index out of range"),
        os::SpawnError::IllegalEnvName => {
            PyException::value_error("illegal environment variable name")
        }
    }
}

/// `subprocess.run(argv, env=env).returncode`。信号で終われば負の値。
pub(super) fn run_child(argv: &[Vec<u8>], env: &[(Vec<u8>, Vec<u8>)]) -> Result<i64, PyException> {
    let mut child = os::spawn(&os::SpawnSpec {
        argv,
        env,
        cwd: None,
        stdin: os::Stdio::Inherit,
        stdout: os::Stdio::Inherit,
        stderr: os::Stdio::Inherit,
    })
    .map_err(|error| spawn_failure(&error, argv.first().map_or(&[], Vec::as_slice)))?;
    match child.wait() {
        Ok(code) => Ok(i64::from(code)),
        Err(error) if error.raw_os_error() == Some(os::EINTR) && os::take_sigint() => {
            // 最初の `KeyboardInterrupt`: ターミナルの `^C` は子へも届いている前提で、子が終わるのを
            // 0.25 秒だけ待つ(2 回目の `SIGINT` でも、結果は同じ `KeyboardInterrupt`)。
            let _ = child.wait_timeout(SIGINT_WAIT);
            // `subprocess.run` の `except:` の `process.kill()`。`with` を抜けるときは、もう待たない。
            let _ = child.kill();
            Err(PyException::new("KeyboardInterrupt", ""))
        }
        Err(error) => Err(from_io(&error, None)),
    }
}

/// 旧版の `run_copy(a)`(`a.command` は `verify`・`read`・`exec`・`hook`)。`ctx.depth` は、`run_copy` のフレームの深さ。
pub fn run_copy(
    host: &dyn Host,
    parsed: &Parsed,
    stdout: &mut PyStream<'_>,
    ctx: PyCtx,
) -> Result<i64, PyException> {
    let current = parsed
        .namespace
        .value("current_inventory")
        .map(|v| v.py_str());
    let value = verify(
        host,
        &argument(parsed, "state"),
        &argument(parsed, "expect_sha256"),
        current.as_deref(),
        ctx,
    )?;
    // 照合の後、判定器などを読むまでの間(旧版も同じ窓を持つ。コピーが書き換わると、保持値との違いで止まる)。
    site("hook-after-verify");
    let command = parsed.command.as_str();
    if command == "verify" {
        return Ok(0);
    }
    let value = value.as_dict().expect("`verify` は辞書だけを返す");
    let given = argument(parsed, "path");
    let rel = PyPath::new(&given);
    let parts = rel.parts();
    if rel.is_absolute() || parts.iter().any(|part| *part == lit("..")) || parts.is_empty() {
        return Err(stop("コピー内の相対パスが必要"));
    }
    let path = copy_path(value, &rel)?;
    match command {
        "hook" => {
            if given != lit(HOOK_PATH) {
                return Err(stop("hook は固定の権限判定器だけを実行する"));
            }
            let raw = read_regular(host, &path, None)?;
            let held = held_sha(value, &given)?;
            if super::pyops::text(lit(&sha_hex(&raw))).py_ne(&held) {
                return Err(stop("権限判定器が保持値と違う"));
            }
            // U3: 判定器は実行しない。R4P が、同じプロセスで Rust の判定器を呼ぶ形に置き換える。
            Err(stop(HOOK_STOP))
        }
        "read" => {
            let raw = read_regular(host, &path, None)?;
            stdout.write_buffer(&raw)?;
            Ok(0)
        }
        _ => {
            let words: Vec<Cps> = parsed
                .namespace
                .words("argv")
                .iter()
                .map(|word| word.as_codepoints().to_vec())
                .collect();
            let words = if words.first() == Some(&lit("--")) {
                &words[1..]
            } else {
                &words[..]
            };
            let Some(program) = words.first() else {
                return Err(stop("helper の実行形式は python3 または bash"));
            };
            if *program != lit("python3") && *program != lit("bash") {
                return Err(stop("helper の実行形式は python3 または bash"));
            }
            // `dict(os.environ, PYTHONDONTWRITEBYTECODE='1')`
            let mut env = host.process_env();
            set_env(&mut env, b"PYTHONDONTWRITEBYTECODE", b"1");
            let mut command_line = vec![fsencode(program)?];
            if *program == lit("python3") {
                command_line.push(b"-B".to_vec());
            }
            command_line.push(fsencode(&path)?);
            for word in &words[1..] {
                command_line.push(fsencode(word)?);
            }
            run_child(&command_line, &env)
        }
    }
}
