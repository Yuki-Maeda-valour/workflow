//! R4R: タスク本文の復元の内部部品。旧 helper
//! `plugins/dev-workflow/skills/do-task/scripts/restore-taskmd.py`(以下「旧 helper」)と
//! 同じ結果を返す。製品のコマンド登録には接続しない(検査専用入口 `r4r_contract` と、
//! 段階 3 の `implement-guard restore-taskmd` が同じプロセスで呼ぶ)。
//!
//! # 旧 helper の関数との対応
//!
//! | 旧 helper(行) | Rust |
//! |---|---|
//! | `path_field` :36-45 | `response::path_field` |
//! | `parse_arguments` :48-70 | `args::parse_request` |
//! | `identity` :73・`fingerprint` :77 | `operation::identity`・`operation::fingerprint` |
//! | `Restore.__init__` :84-111 | `Restore::new`(300 秒の期限の起点) |
//! | `on_signal` :113-115 | platform の `record_signal`(最初の信号だけを記録) |
//! | `checkpoint` :117-121 | `Restore::checkpoint` |
//! | `keep_fd` :123-125 | `Restore::keep` |
//! | `check_features` :127-147 | `Restore::check_features` と `realpath::realpath` |
//! | `reap_if_exited` :149-154 | `Restore::reap_if_exited` |
//! | `stop_child` :156-177 | `Restore::stop_child` |
//! | `check_version` :179-215 | `Restore::check_version`・`Restore::version_body` |
//! | `open_parent` :217-230 | `Restore::open_parent`・`Restore::open_parent_body` |
//! | `stat_at` :232-236 | `Restore::stat_at` |
//! | `check_chains` :238-242 | `Restore::check_chains` |
//! | `hash_fd` :244-266 | `Restore::hash_fd` |
//! | `read_source` :268-280 | `Restore::read_source`・`Restore::read_source_body` |
//! | `read_destination` :282-293 | `Restore::read_destination` |
//! | `create_file` :295-299 | `Restore::create_file` |
//! | `prepare_directory` :301-319 | `Restore::prepare_directory`・`Restore::prepare_body` |
//! | `copy_file` :321-351 | `Restore::copy_file`・`spawn_copy`・`copy_wait` |
//! | `aliases_match` :353-358 | `Restore::aliases_match` |
//! | `probe` :360-411 | `Restore::probe`・`probe_setup`・`probe_alias`・`probe_finish` |
//! | `check_prepared_name` :413-419 | `Restore::check_prepared_name` |
//! | `before_publish` :421-432 | `Restore::before_publish` |
//! | `publish` :434-451 | `Restore::publish`・`publish_masked` |
//! | `verify_published` :453-462 | `Restore::verify_published`・`check_published_name` |
//! | `remove_entry` :464-472 | `Restore::remove_entry` |
//! | `cleanup` :474-508 | `Restore::cleanup`・`cleanup_body` |
//! | `response` :510-523 | `Restore::response` |
//! | `run` :525-572 | `Restore::run`・`run_main` |
//! | `emit_result` :575-602 | `emit`(書き出しの 1 段目) |
//! | `main` :605-620 | 検査専用入口 `examples/r4r_contract.rs` |
//! | Python の終了時(`flush_io`・`Py_FinalizeEx`) | `exit_flush`・`exit_close`(書き出しの 2 段目) |
//! | `subprocess.Popen` の `poll`・`wait`・`terminate`・`kill` | `child::RealChild` |
//! | `_posixsubprocess`(fd を閉じる順・`execv`・`SIGCHLD` を無視した起動の失敗) | platform の `spawn` |
//! | `os.path.realpath` | `realpath::realpath` |
//!
//! # EINTR の扱い(CPython 3.10.12 の `Modules/posixmodule.c` などで確かめた)
//!
//! | Python の関数 | C の実装 | Rust |
//! |---|---|---|
//! | `os.open` | `os_open_impl` が `EINTR` で再試行 | `retry!`(`os_open_*`・`os_create`・`os_dev_null`) |
//! | `os.read` | `_Py_read`(`Python/fileutils.c`)が再試行 | `retry!`(`os_read`) |
//! | `os.write` | `_Py_write` が再試行 | `retry!`(`os_write`・`response::Stdout`) |
//! | `os.fstat` | `os_fstat_impl` が再試行 | `retry!`(`os_fstat`) |
//! | `os.fchmod` | `os_fchmod_impl` が再試行 | `retry!`(`os_fchmod`) |
//! | `os.ftruncate` | `os_ftruncate_impl` が再試行 | `retry!`(`os_ftruncate`) |
//! | `os.waitpid` | `os_waitpid_impl` が再試行 | `child::RealChild` |
//! | `select.select` | `select_select_impl` が残り時間を計り直して再試行 | `os_select` |
//! | `time.sleep` | `pysleep` が残り時間で再試行 | `std::thread::sleep` |
//! | `os.urandom` | `py_getrandom`(Mac は `py_getentropy`)が再試行。ENOSYS・EPERM なら以後 `dev_urandom`(開いた fd を保持し、open・read を再試行) | `retry!`(`token_hex`)と platform の `dev_urandom` |
//! | `os.stat`(`dir_fd`・`follow_symlinks` を含む) | `posix_do_stat` は再試行しない | `once!`(`os_stat_at`・`os_stat_path`) |
//! | `os.mkdir`・`os.unlink`・`os.rmdir`・`os.replace` | 再試行しない | `once!` |
//! | `os.lseek`・`os.access` | 再試行しない | `once!` |
//! | `os.close` | `os_close_impl` は意図して再試行しない | platform の `close` |
//! | `os.readlink`・`os.lstat`(`realpath` の中) | 再試行しない | `realpath::Links` |
//! | `os.kill` | 再試行しない | platform の `send_signal` |
//!
//! # 書き出しの 2 段
//!
//! 1 段目 `emit` は、TERM・INT・HUP を止めて応答を確定し、Python の `BufferedWriter` と同じ
//! 規則で書き込みと明示の flush を行い、mask を戻す。2 段目は Python の終了時と同じ順に、
//! `exit_flush`((1) `flush_io`・(2) `flush_std_files`)、入口が (2) の失敗の 2 行を出した後に
//! `exit_close`((3) 処理を既定へ戻す・(4) 閉じる時の 2 回の flush)を行う。
mod args;
mod child;
#[cfg(not(target_os = "macos"))]
mod errno_glibc;
pub mod errno_text;
mod operation;
mod realpath;
mod response;
#[cfg(test)]
mod tests;

pub use args::{ArgumentError, Request, parse_request};
pub use operation::Restore;
pub use response::{
    ARGUMENT_RESPONSE, Output, RawWrite, Stdout, unraisable_lines, write_stderr_lines,
};

use dev_workflow_platform::restore_taskmd as os;
use std::io;

/// 部品の開始(引数の検査の前)。`SIGXFSZ` を無視にし、プロセスの環境の `LC_CTYPE` を
/// 旧 helper の Python と同じく書き換える。ほかのスレッドが無い時に 1 回だけ呼ぶ。
pub fn start() {
    let _ = os::ignore_file_size_signal();
    os::coerce_c_locale();
}

/// 書き出しの 1 段目の結果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Emitted {
    /// 全部書けた。
    Written,
    /// 明示の flush が失敗し、溜めた物が残った(終了値 33)。
    Pending,
    /// 直接の書き込みが失敗した(終了値 33。溜めた物は無い)。
    DirectFailed,
    /// mask を設定できず、有効な応答を書かなかった(終了値 33)。
    MaskFailed,
}

/// 書き出しの 1 段目(`emit_result` :575-602)。終了値と状態を返す。
pub fn emit<W: RawWrite>(
    out: &mut Output<W>,
    operation: Option<&mut Restore>,
    rc: u8,
    result: &[u8],
) -> (u8, Emitted) {
    #[cfg(test)]
    let injected = tests::emit_point(tests::EmitPoint::Block);
    #[cfg(not(test))]
    let injected: Option<io::Error> = None;
    let mask = match injected {
        Some(_) => return (33, Emitted::MaskFailed),
        None => match os::block_recorded_signals() {
            Ok(mask) => mask,
            Err(_) => return (33, Emitted::MaskFailed),
        },
    };
    #[cfg(test)]
    tests::emit_point(tests::EmitPoint::Blocked);
    let (code, state) = emit_masked(out, operation, rc, result);
    #[cfg(test)]
    let injected = tests::emit_point(tests::EmitPoint::Unblock);
    #[cfg(not(test))]
    let injected: Option<io::Error> = None;
    if injected.is_none() {
        // 戻しの失敗は無視し、書いた応答と終了値を保つ。
        let _ = os::restore_signal_mask(&mask);
    }
    (code, state)
}

fn emit_masked<W: RawWrite>(
    out: &mut Output<W>,
    operation: Option<&mut Restore>,
    rc: u8,
    result: &[u8],
) -> (u8, Emitted) {
    let mut rc = rc;
    let mut owned = None;
    if let Some(operation) = operation {
        operation.response_stopped = operation::recorded_signal();
        operation.response_finalized = true;
        #[cfg(test)]
        tests::emit_point(tests::EmitPoint::Response);
        let (code, bytes) = operation.response();
        rc = code;
        owned = Some(bytes);
    }
    let result = owned.as_deref().unwrap_or(result);
    if out.write(result).is_err() {
        return (33, Emitted::DirectFailed);
    }
    #[cfg(test)]
    tests::emit_point(tests::EmitPoint::Flush);
    if out.flush().is_err() {
        return (33, Emitted::Pending);
    }
    (rc, Emitted::Written)
}

/// 書き出しの 2 段目の前半。(1) スクリプトを流し終えた所の flush(`flush_io`。失敗は表示しない)と
/// (2) `Py_FinalizeEx` の `flush_std_files` の flush。(2) が失敗したら、呼び出し元が 2 行を出して
/// 終了値を 120 にする。
pub fn exit_flush<W: RawWrite>(out: &mut Output<W>) -> (io::Result<()>, io::Result<()>) {
    let first = out.flush();
    let second = out.flush();
    (first, second)
}

/// 書き出しの 2 段目の後半。(3) 設定した TERM・INT・HUP の処理を既定へ戻す(溜めた物の有無に
/// 関係なく必ず行う)。(4) stdout を閉じる時の 2 回の flush(失敗は表示しない)。
pub fn exit_close<W: RawWrite>(
    out: &mut Output<W>,
    handlers_installed: bool,
) -> (io::Result<()>, io::Result<()>, io::Result<()>) {
    let reset = if handlers_installed {
        os::reset_recording_handlers()
    } else {
        Ok(())
    };
    let first = out.flush();
    let second = out.flush();
    (reset, first, second)
}
