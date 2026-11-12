#![deny(unsafe_op_in_unsafe_fn)]

//! OS 固有の機能の境界。保持した実体を使う読み取り専用操作を提供する。

pub mod fs;

pub mod process;

pub mod task_digest;

/// R4B専用の生バイト入出力と直接子の回収。
pub mod r4b_process;

/// R4R専用のタスク本文の復元の OS の操作。
pub mod restore_taskmd;

/// R4E専用の起動前の状態の記録・信号・時刻・端末の OS の操作。
pub mod r4e;
