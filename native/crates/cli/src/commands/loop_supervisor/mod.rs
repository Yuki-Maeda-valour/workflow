//! R4Q(#494)の専用の処理。旧版 `ship-task/scripts/loop-supervisor.py` と同じ結果を返す Rust 版。
//! 製品 CLI の登録表(commands/mod.rs)には接続しない(未登録・未配布)。読み込む側は `#[path]` で読む。
//! - `ops`: 操作の単位(設計の「比べ方の決まり」の表)の trait と本物の実装、Python の例外の値。
//! - `supervise`: `setup`・`signal_owned`・`reap`・`supervise`(check-process の linux worker からも呼ぶ)。
//! - `cli`: `--check` と監督の CLI(`main`)と、旧 Python の起動からの入口 `run`。
//! - `runtime`: 旧 Python の起動と終了の再現(R4U の `check_links` の部品を使う)。
#![forbid(unsafe_code)]

pub mod cli;
pub mod ops;
pub mod runtime;
pub mod supervise;

#[path = "../r4q_python/mod.rs"]
pub mod r4q_python;

#[path = "../check_links/mod.rs"]
// 旧 Python の書き手(pyio)・診断(display)・起動の読み方(startup)・呼出履歴の型(traceback)だけを使う。
// r4q_python も check_links の oserror・path・traceback と resolve_task_dir の bytes・unicode を
// `#[path]` で読むので、同じファイルが 2 回読まれる(どちらも状態を持たない関数と型だけ)。
#[allow(dead_code, unused_imports, clippy::duplicate_mod)]
pub mod check_links;

pub use cli::run;
