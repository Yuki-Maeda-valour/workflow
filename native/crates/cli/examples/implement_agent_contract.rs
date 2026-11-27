//! R4X-1専用の検査入口。製品の登録表(`commands/mod.rs`)を変えず、旧版 `implement-agent.sh` と同じ
//! argv(プログラム名の後ろ)を受ける。製品の CLI には登録しない。
//!
//! 比較のための操作は、この実行物だけが受ける先頭の隠しの引数で有効にする(R4G の `--r4g-clock` と同じ形)。
//! - `--r4x1-stop <フォルダ>`: フォルダの `point` に書いた止め所(`grep`・`dirname`・`basename`・`boundary`)で、
//!   `stop-<地点>` の印を書いてから、フォルダの `fifo` の 1 バイトの合図を待つ。
//! - `--r4x1-tool <dirname|basename> <値>`: 390・391 行の道具の再現だけを流す(本物の道具と比べるため)。
//!
//! 先頭でない所の `--r4x1-stop` は、旧版と同じく不明な引数として扱う。
#![deny(unsafe_code)]
#[path = "../src/commands/implement_agent/mod.rs"]
mod implement_agent;
#[path = "../src/commands/restore_taskmd/mod.rs"]
#[allow(dead_code, unused_imports)]
mod restore_taskmd;

use std::ffi::OsString;
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::FileTypeExt;
use std::path::PathBuf;

// この実行物だけが、main の前の信号の扱いと止めを記録する(Rust の実行時は main の前に SIGPIPE を
// 無視にする)。製品の実行ファイルは同じ platform を組み込むが、この初期化を持たない。
// 初期化の表への登録が、このファイルで唯一の unsafe の項目。
#[allow(unsafe_code)]
#[used]
#[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
#[cfg_attr(target_os = "macos", unsafe(link_section = "__DATA,__mod_init_func"))]
static REMEMBER_STARTUP_STATE: extern "C" fn() =
    dev_workflow_platform::implement_agent::remember_startup_state;

fn stop_error(reason: &str) -> std::process::ExitCode {
    hidden_error("r4x-1-stop", reason)
}

/// 隠しの引数の誤り(旧版の文と区別できる専用の文)と終了値 2。
fn hidden_error(code: &str, reason: &str) -> std::process::ExitCode {
    let _ = std::io::stderr().write_all(format!("ERROR [{code}] {reason}\n").as_bytes());
    std::process::ExitCode::from(2)
}

/// `--r4x1-stop` のフォルダを確かめ、止め所の呼び返しを作る。
fn stop_hook(dir: &[u8]) -> Result<implement_agent::shell::StopHook, String> {
    let dir = PathBuf::from(std::ffi::OsStr::from_bytes(dir));
    if !dir.is_absolute() {
        return Err(format!("フォルダが絶対パスでない: {}", dir.display()));
    }
    let point = std::fs::read(dir.join("point"))
        .map_err(|e| format!("{} の point を読めない: {e}", dir.display()))?;
    let point = point.strip_suffix(b"\n").unwrap_or(&point);
    let point = implement_agent::shell::Point::from_name(point)
        .ok_or_else(|| format!("point の地点が不明: {}", String::from_utf8_lossy(point)))?;
    let fifo = dir.join("fifo");
    let kind = std::fs::symlink_metadata(&fifo)
        .map_err(|e| format!("{} を調べられない: {e}", fifo.display()))?;
    if !kind.file_type().is_fifo() {
        return Err(format!("{} が FIFO でない", fifo.display()));
    }
    Ok(Box::new(
        move |at: implement_agent::shell::Point, abort: &dyn Fn() -> bool| {
            if at != point {
                return;
            }
            // 読み手を先に開く(O_RDWR なので書き手を待たない)。次に印を書き、合図を待つ。
            let Ok(mut gate) = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&fifo)
            else {
                return;
            };
            let mark = dir.join(format!("stop-{}", at.name()));
            if let Ok(mut file) = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&mark)
            {
                let _ = file.write_all(format!("rust {}\n", std::process::id()).as_bytes());
            }
            let mut byte = [0u8; 1];
            loop {
                match gate.read(&mut byte) {
                    Ok(1) => break,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
                        if abort() {
                            break;
                        }
                    }
                    _ => break,
                }
            }
        },
    ))
}

fn main() -> std::process::ExitCode {
    let argv = std::env::args_os().skip(1).collect::<Vec<OsString>>();
    let first = argv.first().map(|a| a.as_bytes().to_vec());
    match first.as_deref() {
        Some(b"--r4x1-stop") => {
            let Some(dir) = argv.get(1) else {
                return stop_error("--r4x1-stop にフォルダが必要です");
            };
            let hook = match stop_hook(dir.as_bytes()) {
                Ok(hook) => hook,
                Err(reason) => return stop_error(&reason),
            };
            let rest = argv.get(2..).unwrap_or_default();
            std::process::ExitCode::from(implement_agent::main_entry(rest, Some(hook)))
        }
        Some(b"--r4x1-tool") => {
            let (Some(name), Some(value)) = (argv.get(1), argv.get(2)) else {
                return hidden_error("r4x-1-tool", "--r4x1-tool に道具の名前と値が必要です");
            };
            std::process::ExitCode::from(implement_agent::tool_entry(
                name.as_bytes(),
                value.as_bytes(),
            ))
        }
        _ => std::process::ExitCode::from(implement_agent::main_entry(&argv, None)),
    }
}
