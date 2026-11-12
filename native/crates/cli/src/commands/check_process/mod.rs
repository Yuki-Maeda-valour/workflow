//! R4Q(#494)の専用の処理。旧版 `ship-task/scripts/check-process.py` と同じ結果を返す Rust 版。
//! 製品 CLI の登録表(commands/mod.rs)には接続しない(未登録・未配布)。読み込む側は `#[path]` で読む。
//! - `cli`: 監督の内部 CLI(anchor と linux worker)と、旧 Python の起動からの入口 `run`。
//! - `run_command`: 親(`run_command`・`StopSignals`・3 つの誤り)。R4V(review-guard)が呼ぶ。
//! - loop-supervisor の部品(操作の単位・supervise・旧 Python の起動)は `loop_supervisor` から使う。
#![forbid(unsafe_code)]

pub mod cli;
pub mod run_command;

#[path = "../loop_supervisor/mod.rs"]
pub mod loop_supervisor;

pub use cli::run;
