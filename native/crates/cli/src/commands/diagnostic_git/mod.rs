//! 診断(R4D)の内部部品。登録しない内部部品であり、製品のコマンドの表にも入口にも載せない。
//!
//! R4D-1 の範囲は `budget`(診断の予算)と `reftable`(専用コピーの reftable の読み取り)だけである。
//! 検査は `tests/diagnostic_reftable.rs` が `#[path]` で読み込む。
//! R4D-2 の範囲は `json`(厳密な JSON・base64・確認用の値・応答の符号化)と `reader`(上限付きの読み取り)である。
//! 旧版との比較は `tests/diagnostic_runtime.rs` が `#[path]` で読み込む。

pub mod budget;
pub mod reftable;

// R4D-2
pub mod json;
pub mod reader;
