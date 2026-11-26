//! environment-guard の移植(S2E・#470)。開発用ソースで、製品の登録表にはつなげない。
//! 段階 1: OS を呼ばない部品(引数・出力・Python の値・JSON・文字列・パス)。
//! 段階 2a: 記述子・設定・一覧・控えの値の組み立てと検査(OS を呼ばない形)と、`host-paths`。
//! 段階 2b-1a: 環境用の予算(`budget`)とファイル操作(`fsio`)。子の起動は platform にある。
//! 段階 2b-1b: 木の走査(fsio の walk_at・tree)・部品の木(component)・記録(record)・Git 設定の解析(gitconfig)・
//! 再帰の深さ(depth)・控えの値(entry)。
//! 段階 2b-1c: 控えの観測(`snapshot`・`verify`)・`bootstrap`・`host-settings`・`host-allowlist`・内部 reader(`reader`)。
//! 段階 2b-2a: `read`・`exec`・`hook`(`copy`)・`verify-pair`(`pair`)。
// 段階 1 の部品は、段階 2 の 9 コマンドが使う。
#![allow(dead_code)]
pub mod args;
pub mod bootstrap;
pub mod budget;
#[cfg(test)]
mod command_tests;
pub mod component;
pub mod copy;
#[cfg(test)]
mod copy_tests;
pub mod depth;
pub mod dispatch;
pub mod entry;
mod errno_table;
pub mod error;
pub mod fsio;
pub mod gitconfig;
pub mod inventory;
#[cfg(test)]
mod observe_tests;
pub mod output;
pub mod pair;
pub mod pyjson;
pub mod pyops;
pub mod pypath;
pub mod pystr;
pub mod pyvalue;
#[cfg(test)]
mod race_tests;
pub mod reader;
pub mod record;
pub mod runtime;
pub mod settings;
pub mod snapshot;
pub mod specs;
pub mod startup;
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

/// 旧 Python の起動時の環境を整える: PEP 538 の `LC_CTYPE`(環境を読む前に 1 回)、`SIGPIPE`・`SIGXFSZ` を無視、
/// `SIGINT` は既定の扱いのときだけ `KeyboardInterrupt` の元になる印だけを立てる(無視されていれば何もしない)。
/// 印を立てたときだけ `true`。`run_process` が設定の段階の Fatal を確かめた後に呼ぶ。比較用の隠しコマンド(example)も呼ぶ(何回呼んでも 1 回だけ行う)。
pub fn prepare_process() -> bool {
    // 2 回目以降は同じ結果を返す(`SIGINT` の扱いを 2 回入れ直すと、2 回目は「既に入っている」ので `false` になり、
    // `KeyboardInterrupt` への変換が外れる。比較用の隠しコマンドが先に呼んでから `run_process` へ進む入口がある)。
    static PREPARED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *PREPARED.get_or_init(|| {
        use dev_workflow_platform::environment_guard as os;
        os::coerce_c_locale();
        let _ = os::ignore_parent_signals();
        os::install_sigint_handler().unwrap_or(false)
    })
}

/// 設定の段階の Fatal を標準エラーへ直接書き、終了コード 1 を返す。旧版は `SIGPIPE` を無視にする前なので、
/// 親から受け継いだ扱いのまま書く(既定なら、読む側の閉じたパイプへ書くと信号で終わる。無視なら書込みが失敗して 1)。
/// Rust の標準ライブラリは `main` の前に無視へ変えるので、`main` の前に控えた扱いが無視でないときだけ既定へ戻す
/// (控えが無い機種は受け継いだ扱いが分からないので、既定へ戻す)。
fn config_fatal(text: &str) -> u8 {
    use dev_workflow_platform::environment_guard as os;
    if os::inherited_sigpipe() != Some(os::Disposition::Ignore) {
        let _ = os::signal_set_default(os::SIGPIPE);
    }
    let _ = std::io::stderr().write_all(text.as_bytes());
    1
}

/// プロセスの引数・環境・標準入出力で旧版の `main` と同じ流れを動かす。戻り値は終了コード。
/// `terminal_columns` は標準出力の端末の幅(端末でなければ 80)。
pub fn run_process(terminal_columns: usize, dispatch: &mut Dispatch<'_>) -> u8 {
    use dev_workflow_platform::environment_guard as os;
    let var = |name: &str| std::env::var_os(name);
    let columns = var("COLUMNS");
    let unbuffered = var("PYTHONUNBUFFERED");
    let digits = var("PYTHONINTMAXSTRDIGITS");
    // 設定の段階の Fatal(順序は旧 Python の起動の順)。旧 Python は、`SIGPIPE` を無視にする前にここで止まる。
    // 標準エラーが読む側の閉じたパイプなら、旧版は `SIGPIPE` の既定の扱いで信号(13)で終わる。
    // Rust の実行時は `main` の前に `SIGPIPE` を無視にするので、控えた受け継ぎの扱いが無視でなければ、診断を書く前に既定へ戻す(そのまま終える)
    // (`prepare_process` はこの後)。親が無視にしていれば無視のまま書く(旧版と同じ)。
    if !startup::hash_seed_is_valid(var("PYTHONHASHSEED").as_deref().map(OsStrExt::as_bytes)) {
        return config_fatal(startup::HASH_SEED_FATAL);
    }
    let env = Env::from_values(
        columns.as_deref().map(OsStrExt::as_bytes),
        terminal_columns,
        unbuffered.as_deref().map(OsStrExt::as_bytes),
        digits.as_deref().map(OsStrExt::as_bytes),
    );
    let Ok(env) = env else {
        return config_fatal(INT_MAX_STR_DIGITS_FATAL);
    };
    let interruptible = prepare_process();
    let interrupted = std::cell::Cell::new(false);
    let mut guarded =
        |parsed: &args::Parsed, stdout: &mut PyStream<'_>, stderr: &mut PyStream<'_>| {
            let result = dispatch(parsed, stdout, stderr);
            // 旧版は次のバイトコードの境目で `KeyboardInterrupt` を投げる。印が残っていれば、ここで投げる。
            let result = match result {
                Ok(_) if interruptible && os::take_sigint() => {
                    Err(error::PyException::new("KeyboardInterrupt", ""))
                }
                other => other,
            };
            if matches!(&result, Err(error) if error.type_name == "KeyboardInterrupt") {
                interrupted.set(true);
            }
            result
        };
    let is_directory = |fd: std::os::fd::BorrowedFd<'_>| {
        os::fstat(fd).is_ok_and(|stat| stat.kind() == os::S_IFDIR)
    };
    let directories = [
        is_directory(std::io::stdin().as_fd()),
        is_directory(std::io::stdout().as_fd()),
        is_directory(std::io::stderr().as_fd()),
    ];
    if let Some(diagnostic) = startup::directory_fatal(directories, os::current_thread_id()) {
        let _ = std::io::stderr().write_all(diagnostic.as_bytes());
        return 1;
    }
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
    let status = run_main(&args, &env, &mut stdout, &mut stderr, &mut guarded);
    if interrupted.get() {
        // 捕まえない `KeyboardInterrupt` の後は、旧版と同じく、信号で終わる(終われなければ 130)。
        return u8::try_from(os::die_by_sigint()).unwrap_or(130);
    }
    u8::try_from(status).unwrap_or(1)
}
