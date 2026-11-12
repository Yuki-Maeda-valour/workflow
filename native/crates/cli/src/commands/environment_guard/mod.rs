//! environment-guard の移植(S2E・#470)。開発用ソースで、製品の登録表にはつなげない。
//! 段階 1: OS を呼ばない部品(引数・出力・Python の値・JSON・文字列・パス)。
//! 段階 2a: 記述子・設定・一覧・控えの値の組み立てと検査(OS を呼ばない形)と、`host-paths`。
//! 段階 2b-1a: 環境用の予算(`budget`)とファイル操作(`fsio`)。子の起動は platform にある。
//! 9 コマンドの本体と内部 reader は、続きの段階が足す。
// 段階 1 の部品は、段階 2 の 9 コマンドが使う。
#![allow(dead_code)]
pub mod args;
pub mod budget;
pub mod dispatch;
mod errno_table;
pub mod error;
pub mod fsio;
pub mod inventory;
pub mod output;
pub mod pyjson;
pub mod pyops;
pub mod pypath;
pub mod pystr;
pub mod pyvalue;
pub mod runtime;
pub mod settings;
pub mod specs;
pub mod state;
#[cfg(test)]
mod testing;
mod unicode_tables;

use output::{Dispatch, run_main};
use pystr::decode_fs;
use runtime::{Env, INT_MAX_STR_DIGITS_FATAL, PyStream, StreamKind};
use std::fs::File;
use std::io::{IsTerminal, Write};
use std::os::fd::AsFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;

/// 標準入出力の 1 本を、`write(2)` を直接呼ぶ `File` と `st_blksize` にする。
fn raw_sink(fd: impl AsFd) -> std::io::Result<(File, usize)> {
    let file = File::from(fd.as_fd().try_clone_to_owned()?);
    let blksize = file
        .metadata()
        .map_or(0, |m| usize::try_from(m.blksize()).unwrap_or(0));
    Ok((file, blksize))
}

/// プロセスの引数・環境・標準入出力で旧版の `main` と同じ流れを動かす。戻り値は終了コード。
/// `terminal_columns` は標準出力の端末の幅(端末でなければ 80)。
pub fn run_process(terminal_columns: usize, dispatch: &mut Dispatch<'_>) -> u8 {
    let var = |name: &str| std::env::var_os(name);
    let columns = var("COLUMNS");
    let unbuffered = var("PYTHONUNBUFFERED");
    let digits = var("PYTHONINTMAXSTRDIGITS");
    let env = Env::from_values(
        columns.as_deref().map(OsStrExt::as_bytes),
        terminal_columns,
        unbuffered.as_deref().map(OsStrExt::as_bytes),
        digits.as_deref().map(OsStrExt::as_bytes),
    );
    let Ok(env) = env else {
        // 起動時の誤り。Python は標準エラーへ直接書いて、終了コード 1 で止まる。
        let _ = std::io::stderr().write_all(INT_MAX_STR_DIGITS_FATAL.as_bytes());
        return 1;
    };
    let (Ok((mut out_file, out_block)), Ok((mut err_file, err_block))) =
        (raw_sink(std::io::stdout()), raw_sink(std::io::stderr()))
    else {
        return 1;
    };
    let args: Vec<Vec<u32>> = std::env::args_os()
        .skip(1)
        .map(|arg| decode_fs(arg.as_bytes()))
        .collect();
    let mut stdout = PyStream::new(
        &mut out_file,
        StreamKind::Stdout,
        env.unbuffered,
        out_block,
        std::io::stdout().is_terminal(),
    );
    let mut stderr = PyStream::new(
        &mut err_file,
        StreamKind::Stderr,
        env.unbuffered,
        err_block,
        false,
    );
    let status = run_main(&args, &env, &mut stdout, &mut stderr, dispatch);
    u8::try_from(status).unwrap_or(1)
}
