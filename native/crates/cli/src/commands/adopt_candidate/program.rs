//! 旧 `main()` 全体。起動の失敗 → 引数の解析 → 採用 → 出力の順で、旧 Python の振る舞いに合わせる。
use super::args::{self, Arguments};
use super::flow::{Candidates, Ops, Precheck, adopt};
use super::output;
use super::plan::{self, Stop};
use super::runtime::{
    Env, Program, Sink, StreamKind, Streams, emit, startup_failure, unbuffered, write_all,
};
use super::{diagnostics, runtime};
use std::ffi::OsString;

/// 実行に必要な境界をまとめたもの。
pub(crate) struct Context<'a, O: Ops, C: Candidates, P: Precheck<O::Dir>> {
    pub(crate) ops: &'a O,
    pub(crate) candidates: &'a C,
    pub(crate) precheck: &'a P,
    pub(crate) env: &'a dyn Env,
    /// 標準入力・標準出力・標準エラーがディレクトリか。
    pub(crate) directory: [bool; 3],
    /// `Current thread` に出す値。
    pub(crate) thread: u64,
    /// 標準出力・標準エラーの `st_blksize`(`BufferedWriter` の大きさ)。
    pub(crate) blksize: [i64; 3],
    /// 標準出力の端末の桁数(端末でなければ 80)。
    pub(crate) terminal_columns: usize,
    /// 標準出力が端末か(起動したときに決まる。旧 Python は、そのとき `line_buffering` を決める)。
    pub(crate) stdout_terminal: bool,
}

fn width(env: &dyn Env, terminal: usize) -> i64 {
    let text = |name: &str| {
        env.get(name)
            .and_then(|value| String::from_utf8(value).ok())
    };
    args::layout_width(
        text("COLUMNS").as_deref(),
        text("PYTHONINTMAXSTRDIGITS").as_deref(),
        || terminal,
    )
}

/// 旧 `main()` の全体を行い、終了値を返す。出力は `out`・`err` へ、旧 Python と同じ故障の扱いで書く。
pub(crate) fn run<O: Ops, C: Candidates, P: Precheck<O::Dir>>(
    context: &Context<'_, O, C, P>,
    args: &[OsString],
    out: &mut dyn Sink,
    err: &mut dyn Sink,
) -> i32 {
    let env = context.env;
    let streams = if unbuffered(env) {
        Streams::unbuffered()
    } else {
        Streams {
            out: (StreamKind::Block, context.blksize[1]),
            err: (StreamKind::Block, context.blksize[2]),
            out_terminal: context.stdout_terminal,
        }
    };
    if let Some(warning) = runtime::locale_coercion(env).and_then(|c| c.warning) {
        let _ = write_all(err, warning.as_bytes());
    }
    if let Some(fatal) = startup_failure(env, context.directory, context.thread) {
        let _ = write_all(err, &fatal.stderr);
        return 1;
    }
    let description = !runtime::docstrings_stripped(env);
    let arguments: Arguments =
        match args::parse_with(args, width(env, context.terminal_columns), description) {
            Ok(arguments) => arguments,
            Err(output) => {
                let program = if output.code.as_u8() == 0 {
                    Program::Help(output.stdout)
                } else {
                    Program::UsageError(output.stderr)
                };
                return emit(program, streams, out, err);
            }
        };
    let loop_iteration = context.ops.loop_iteration();
    let cwd = || {
        std::env::current_dir()
            .map(|path| std::os::unix::ffi::OsStrExt::as_bytes(path.as_os_str()).to_vec())
            .map_err(|error| diagnostics::os_error_from(&error, None))
    };
    let result = plan::plan(&arguments, loop_iteration.as_deref(), &cwd).and_then(|planned| {
        adopt(
            context.ops,
            context.candidates,
            context.precheck,
            &planned,
            &arguments,
        )
    });
    let program = match result {
        Ok(adopted) => Program::Success(
            output::success(
                &adopted.path,
                adopted.tracked,
                &adopted.sha256,
                adopted.warnings,
            )
            .stdout,
        ),
        Err(Stop::Caught(message)) => Program::Stop(message),
        Err(Stop::Uncaught(kind)) => Program::Uncaught(kind),
        Err(Stop::Interrupted) => {
            // 実物は、ここで信号により終わる(戻らない)。代役は戻るので、終了値は 130(SIGINT の慣例)にする。
            context.ops.interrupt_exit();
            return 130;
        }
    };
    emit(program, streams, out, err)
}
