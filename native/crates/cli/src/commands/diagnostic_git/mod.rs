//! 診断(R4D)の内部部品。登録しない内部部品であり、製品のコマンドの表にも入口にも載せない。
//!
//! R4D-1 の範囲は `budget`(診断の予算)と `reftable`(専用コピーの reftable の読み取り)だけである。
//! 検査は `tests/diagnostic_reftable.rs` が `#[path]` で読み込む。

pub mod budget;
pub mod reftable;
