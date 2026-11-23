//! S3I-A専用の検査入口。製品の登録表(`commands/mod.rs`)を変えず、旧版 `implement-guard.sh` と同じ
//! argv(プログラム名の後ろ)を受ける。製品の CLI には登録しない。
#![deny(unsafe_code)]
#[path = "../src/commands/implement_guard/mod.rs"]
mod implement_guard;
#[path = "../src/commands/restore_taskmd/mod.rs"]
mod restore_taskmd;

// この実行物だけが、main の前の SIGPIPE の扱いを記録する(Rust の実行時は main の前に SIGPIPE を
// 無視にする)。製品の実行ファイルは同じ platform を組み込むが、この初期化を持たない。
// 初期化の表への登録が、このファイルで唯一の unsafe の項目。
#[allow(unsafe_code)]
#[used]
#[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
#[cfg_attr(target_os = "macos", unsafe(link_section = "__DATA,__mod_init_func"))]
static REMEMBER_STARTUP_STATE: extern "C" fn() =
    dev_workflow_platform::implement_guard::remember_startup_state;

fn main() -> std::process::ExitCode {
    let argv = std::env::args_os().skip(1).collect::<Vec<_>>();
    std::process::ExitCode::from(implement_guard::main_entry(&argv))
}
