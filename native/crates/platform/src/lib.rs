#![deny(unsafe_op_in_unsafe_fn)]

//! OS 固有の機能の境界。保持した実体を使う読み取り専用操作を提供する。

pub mod fs;

pub mod process;

pub mod check_links;
pub mod task_digest;

/// R4B専用の生バイト入出力と直接子の回収。
pub mod r4b_process;

/// R4R専用のタスク本文の復元の OS の操作。
pub mod restore_taskmd;

/// R4G専用。起動前の状態・fd相対の読取り・旧subprocessと旧bashの子の実行。
pub mod r4g;

/// loop-state専用の保持入力・保存・直接子の接続。
pub mod loop_state;

#[cfg(target_os = "linux")]
pub mod loop_state_watch;

/// R4E専用の起動前の状態の記録・信号・時刻・端末の OS の操作。
pub mod r4e;

/// S3I-A専用の implement-guard の検査専用入口の OS の操作。
pub mod implement_guard;

/// S3D専用の差分スナップショットの OS の操作。
pub mod diff_snapshot;

/// R4A残り5本専用の標準入出力・端末幅・スレッド値・SIGINT・permlog の OS の操作。
pub mod r4a_rest;

/// R4C専用の候補の採用の OS の操作。
pub mod adopt_candidate;

/// S2E専用のenvironment-guardのOSの操作。
pub mod environment_guard;

/// R4X-1専用のimplement-agentの検査専用入口のOSの操作。
pub mod implement_agent;
