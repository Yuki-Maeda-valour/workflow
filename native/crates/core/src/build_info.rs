//! ビルド時に決まる情報。実行中に外部の道具や設定ファイルを読まない。

pub const NATIVE_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SOURCE_INPUT_SHA256: &str = env!("DEV_WORKFLOW_SOURCE_INPUT_SHA256");
pub const RUSTC: &str = env!("DEV_WORKFLOW_RUSTC");
