//! R4U(#480)の専用の処理。旧版 update-doc/scripts/check_links.py と同じ結果を返す部品を集める。
//! 製品 CLI の登録表(commands/mod.rs)には接続しない(未登録・未配布)。
//! - 文字列だけの部品: パスの整え方・照合・読取りの復号・repr・OS の文・呼出履歴の定型。
//! - OS の部品(platform の check_links): 走査・読取り・解決・存在の確かめ・書込み・信号。
//! - 入口 `run`: 生の引数を受けて、旧 Python の起動・本処理・終了の処理を同じ順で行い、終了値を返す。
//!   出力は Python を写した書き手(`pyio`)が、実行の途中で行う。
#![forbid(unsafe_code)]

pub mod display;
pub mod fsops;
pub mod legacy;
pub mod matcher;
pub mod oserror;
pub mod path;
pub mod posix;
pub mod pyio;
pub mod startup;
pub mod text;
pub mod traceback;

#[path = "../resolve_task_dir/bytes.rs"]
// surrogateescape の符号位置と repr だけを使う。JSON の表記などの関数と、harness=false では走らない
// 既存の単体テストの import は使わない。
#[allow(dead_code, unused_imports)]
mod bytes;
#[path = "../resolve_task_dir/unicode.rs"]
// str.isprintable の表だけを使う。decimal は使わない。
#[allow(dead_code)]
mod unicode;

use dev_workflow_core::ExitCode;
use dev_workflow_platform::check_links as os;
use display::Stage;
use fsops::errno as fs_errno;
use pyio::{Py, Stream};
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use traceback::Exception;

/// Python の文字列(ファイル名は surrogateescape で復号したもの)の符号位置の列。
pub fn code_points(raw: &[u8]) -> Vec<u32> {
    bytes::characters(raw)
}

/// Python 3.10 の repr(str)。引数は surrogateescape で符号化したバイト列。
pub fn repr(raw: &[u8]) -> String {
    String::from_utf8(bytes::repr(raw))
        .expect("repr は surrogate を \\udcXX にするので UTF-8 になる")
}

/// Python 3.10(Unicode 13.0.0)の str.isprintable。
pub fn printable(code: u32) -> bool {
    unicode::printable(code)
}

/// OS エラーの文。Linux は glibc の固定表(配布物の musl の文を使わない)、Mac は OS の文。
pub fn strerror(errno: i32) -> String {
    if cfg!(target_os = "macos") {
        os::os_strerror(errno)
    } else {
        oserror::glibc_strerror(errno)
    }
}

/// 標準入出力の 1 つを `create_stdio` と同じ順で確かめる。ディレクトリなら起動の失敗の例外を返す。
fn open_stream(fd: i32, unbuffered: bool) -> Result<Option<Stream>, Exception> {
    if os::std_valid_at_start(fd) == Some(false) {
        // 起動の前に閉じていた(is_valid_fd が偽): Python では sys.stdout などが None になり、書込みは黙って捨てられる。
        return Ok(None);
    }
    match os::fstat_std(fd) {
        // FileIO(fd) の EISDIR。ファイル名は fd の番号(int の repr)。
        Ok(meta) if meta.kind == os::Kind::Directory => Err(Exception {
            type_name: oserror::exception_type(fs_errno::EISDIR),
            value: format!(
                "[Errno {}] {}: {fd}",
                fs_errno::EISDIR,
                strerror(fs_errno::EISDIR)
            ),
        }),
        Ok(meta) => Ok(Some(Stream::new(
            fd,
            unbuffered,
            os::isatty_std(fd),
            meta.blksize,
        ))),
        Err(_) => Ok(Some(Stream::new(fd, unbuffered, os::isatty_std(fd), 0))),
    }
}

/// 控えの無い呼出しの診断(呼出元の誤り)。
pub const NOT_CAPTURED: &str = "R4U の入口: main の前の控えが無い。\
    呼出元の実行物に dev_workflow_platform::check_links::capture_before_main を main の前に呼ぶ static を置く";

/// 旧版を `python3 check_links.py <引数…>` で起動したときと同じ動作をして、終了値を返す。
/// SIGINT で中断したときは、自分へ SIGINT を送って終わる(終わらなければ 130)。
///
/// # Panics
/// main の前の控え(`capture_before_main`)が無いときは、何も書かずに panic する(呼出元の誤り)。
/// 控えが無いと、受け継いだ SIGPIPE の扱いも閉じていた fd 0〜2 も分からず、旧版と同じに動けないため。
pub fn run(args: &[OsString]) -> ExitCode {
    assert!(os::captured_before_main(), "{NOT_CAPTURED}");
    // 旧 Python は SIGPIPE を無視にする前(起動の判定の 1・2)の診断を、親から受け継いだ扱いのまま書く。
    // Rust の起動処理は main の前に無視にするので、main の前に控えた扱いへ戻す。
    os::restore_start_sigpipe();
    let config = startup::Config::from_env();
    if !startup::hash_seed_valid(config.hash_seed.as_deref()) {
        display::fatal(
            Stage::Preinitialized,
            "config_init_hash_seed",
            "PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]",
            None,
        );
        return ExitCode::RUNTIME;
    }
    if !startup::int_max_str_digits_valid(config.int_max_str_digits.as_deref()) {
        display::fatal(
            Stage::Preinitialized,
            "config_init_int_max_str_digits",
            "PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.",
            None,
        );
        return ExitCode::RUNTIME;
    }
    // _PySignal_Init: SIGPIPE と SIGXFSZ を無視にし、SIGINT が既定なら捕まえる。
    os::ignore_sigpipe_and_sigxfsz();
    os::install_sigint();
    // init_sys_streams: 標準入力がディレクトリか → stdin・stdout・stderr の順に作る。
    if os::std_valid_at_start(0) == Some(true)
        && os::fstat_std(0).is_ok_and(|meta| meta.kind == os::Kind::Directory)
    {
        display::fatal(
            Stage::CoreInitialized,
            "init_sys_streams",
            "<stdin> is a directory, cannot continue",
            None,
        );
        return ExitCode::RUNTIME;
    }
    let unbuffered = config.unbuffered();
    let streams = open_stream(1, unbuffered)
        .and_then(|stdout| open_stream(2, unbuffered).map(|stderr| (stdout, stderr)));
    let (stdout, stderr) = match streams {
        Ok(streams) => streams,
        Err(exception) => {
            display::fatal(
                Stage::CoreInitialized,
                "init_sys_streams",
                "can't initialize sys standard streams",
                Some(exception),
            );
            return ExitCode::RUNTIME;
        }
    };
    let mut py = Py {
        stdout,
        stderr,
        err: None,
    };
    let argv: Vec<Vec<u8>> = args.iter().cloned().map(OsString::into_vec).collect();
    let outcome = legacy::main(&mut py, &argv, config.doc());
    ExitCode::new(finish(&mut py, outcome))
}

/// 本処理の後の順(`_PyRun_SimpleFileObject` の flush_io → 呼出履歴 → `Py_FinalizeEx` → `exit_sigint`)。
fn finish(py: &mut Py, outcome: Result<u8, fsops::Raised>) -> u8 {
    py.flush_io();
    let (code, interrupted) = match outcome {
        Ok(code) => (code, false),
        Err(raised) => {
            display::display(py, &raised.0);
            (1, raised.is_interrupt())
        }
    };
    let flushed = display::flush_std_files(py);
    os::finish_sigint();
    py.close_all();
    let code = if flushed { code } else { 120 };
    if interrupted {
        return os::exit_by_sigint();
    }
    code
}
