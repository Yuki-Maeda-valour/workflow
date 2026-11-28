//! 診断(R4D)の内部部品。登録しない内部部品であり、製品のコマンドの表にも入口にも載せない。
//!
//! R4D-1 の範囲は `budget`(診断の予算)と `reftable`(専用コピーの reftable の読み取り)だけである。
//! 検査は `tests/diagnostic_reftable.rs` が `#[path]` で読み込む。
//!
//! R4D-3 の範囲は `state`(保護記録の形式の検査)・`paths`(実名と REV の検査)・`private`(参照の名前・中身・解決)である。
//! ファイルの読み書きも Git の起動もしない(子の起動と参照の解決の結果は trait で受け取る)。
//! 検査は `tests/diagnostic_state.rs` が `#[path]` で読み込む。結び付け・専用の管理領域・子の起動の本物の実装は R4D-3b(#544)。

pub mod budget;
pub mod reftable;

pub mod paths;
pub mod private;
pub mod state;
