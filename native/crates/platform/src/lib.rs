#![deny(unsafe_op_in_unsafe_fn)]

//! OS 固有の機能の境界。保持した実体を使う読み取り専用操作を提供する。

pub mod fs;

pub mod process;

pub mod task_digest;
