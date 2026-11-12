//! R4E: Markdown の表の XLSX 出力(旧 `md_tables_to_xlsx.py`)の未登録の開発用実装。
//!
//! 製品の登録表(`commands/mod.rs`)には載せない。検査専用の入口 `examples/r4e_contract.rs`
//! だけが `#[path]` で取り込む。製品の `dev-workflow md-tables-to-xlsx` は「未知のコマンド」のまま。
//!
//! モジュール:
//! - `pyio`・`errors`: Python の入出力・起動・呼出履歴
//! - `args`: argparse と同じ引数の解析と `--help`
//! - `unicode13`(`native/fixtures/r4e/generate_unicode13.py` が生成): Unicode 13.0.0 の表
//! - `markdown`・`sheet_name`: 入力の読取り・表の取り出し・シート名
//! - `paths`: 出力先の事前検査と親の作成
//! - `workbook`・`xml`: ブックの組み立てと各部分の XML。既定テーマ(`theme1.xml`)・`app.xml` の文・
//!   書式は openpyxl 3.1.5(MIT ライセンス)から転記した(許諾は `NOTICE.md`)
//! - `zip`: ZIP の書き出し(`wb.save`)
//!
//! OS の部品(起動前の状態・信号・現地時刻・端末の幅・長いパスの現在地)は
//! `dev_workflow_platform::r4e` の安全な関数だけを使う。このモジュールの木は unsafe を使わない
//! (検査入口の `deny(unsafe_code)` の例外は、入口の 1 つの静的変数だけに留める)。
#![forbid(unsafe_code)]

pub mod args;
pub mod errors;
pub mod markdown;
pub mod paths;
pub mod pyio;
pub mod sheet_name;
pub mod unicode13;
pub mod workbook;
pub mod xml;
pub mod zip;
// R4B と共有する表(読み取りだけで再利用する)。`repr` は printable を、シート名の番号(`\d`)と
// `COLUMNS` の int() は decimal を使う。共有のファイルなので、このコマンドが使わない部品もあり、
// その部品の未使用の警告だけをここで許す。
#[path = "../r4b_support/unicode.rs"]
#[allow(dead_code)]
mod r4b_unicode;

use dev_workflow_core::ExitCode;
use dev_workflow_platform::r4e::StartupState;
use errors::frames;
use markdown::{Collected, Loaded};
use paths::{PyPath, Rejected};
use pyio::{Cleanup, Outcome, PyStr, Stream, Uncaught};
use std::ffi::OsString;
use std::fs::File;
use std::os::unix::ffi::OsStrExt;
use workbook::Workbook;
use zip::Clock;

/// 検査入口の不備(起動前の記録が無い)を示す終了値。旧版は返さない値にする。
pub const HARNESS_FAILURE: ExitCode = ExitCode::new(70);

/// 入口。`args` は呼出名を除いた引数、`startup` は `main` の前に記録した状態。
///
/// 旧版の `sys.stdout`・`sys.stderr` が無い状態は、`startup` の fd 1・fd 2 の
/// `python_valid()` で決める(標準ライブラリが `/dev/null` で埋めた後の fd では決めない)。
pub fn run(args: &[OsString], startup: &StartupState) -> ExitCode {
    match pyio::Runtime::start(startup) {
        Ok(runtime) => run_main(args, runtime),
        Err(code) => code,
    }
}

/// 起動の後の本体(旧版の `sys.exit(main())` と終了処理)。
pub fn run_main(args: &[OsString], mut runtime: pyio::Runtime) -> ExitCode {
    let outcome = main(args, &mut runtime, &zip::SystemClock, &|dest| {
        zip::create(dest)
    });
    runtime.finish(outcome)
}

/// 出力先を開く処理(製品は [`zip::create`])。
type Opener<'a> = &'a dyn Fn(&PyPath) -> Result<File, errors::PyException>;

/// 旧版の `sys.exit(main())`(241 行)の `main()` の呼出しまで。
///
/// # 届いた SIGINT の確かめ
///
/// 旧版は SIGINT の扱いを入れた後に届いた SIGINT を、次の呼出しや繰り返しの切れ目で
/// `KeyboardInterrupt` にする。Rust は旧版の 1 つの段の仕事をまとめて行うので、その仕事の後で、
/// 旧版なら次の Python の命令に当たる所(出力や終了の値を決める前)で印を確かめる。どの段で
/// `KeyboardInterrupt` になるかは途中の行なので比べない(決定 22:31)。確かめる所は次のとおり。
/// - 引数の解析の後、各入力の `is_file()` の後・読取り(解読)の後・表の取り出しの後、`WARN`・
///   表なしの `print` の前(`markdown::collect`)。
/// - 出力先の引数の前、出力先の事前検査の後(拒否の行の `print` の前)、ブックの組み立ての中、
///   親の作成の前後、XLSX の各部分と閉じる前(`zip::save`)、`OK` の `print` の前。
/// - `main` から戻った後(`sys.exit` を呼ぶ前)。この後に届いた分は、旧版でも終了の処理の中で
///   扱われずに終わる(書き込みで止まっている間を除く)。
///
/// 例外で終わる経路では、旧版が例外を処理する所(`except`・`finally`・`with` の後始末)に入る所でも
/// 確かめる(CPython は後始末の最初の命令で確かめる)。届いていれば、処理中の例外を `__context__` に
/// 持つ `KeyboardInterrupt` になる。
/// - 入力の読取り・解読の例外: `read_text` の `with` の後始末(`__exit__` は呼ばれない)。
/// - `is_file()` の誤り: `is_file` の `except OSError`。
/// - `write_xlsx` から出た例外: `main` の `except OutputError` の後始末(233 行)。
/// - 拒否の行の `print` の例外: `except OutputError as exc:` の後始末(235 行)。
///
/// 書き込みの単位(`print`・呼出履歴の表示)も、`str()` の所で確かめる(`pyio`)。旧版の C の呼出しの
/// 中で例外になる所(解読の誤りなど)は、その呼出しの前には確かめない(読取りの後・解読の前の確かめを除く)。
fn main(args: &[OsString], py: &mut pyio::Runtime, clock: &dyn Clock, open: Opener) -> Outcome {
    match main_body(args, py, clock, open) {
        Outcome::Exit(code) => match py.check_signals() {
            Ok(()) => Outcome::Exit(code),
            Err(interrupt) => Outcome::raised(interrupt.called_from(&[frames::MODULE])),
        },
        raised => raised,
    }
}

/// 旧版の `main()`(200〜237 行)。
fn main_body(
    args: &[OsString],
    py: &mut pyio::Runtime,
    clock: &dyn Clock,
    open: Opener,
) -> Outcome {
    let parsed = match args::parse(args, py) {
        Ok(parsed) => parsed,
        Err(outcome) => return outcome,
    };
    if let Err(error) = py.check_signals() {
        return Outcome::raised(
            error.called_from(&[frames::MODULE, args::frames::MAIN_PARSES_ARGS]),
        );
    }
    let loaded = match markdown::collect(py, &parsed.inputs) {
        Ok(Collected::Tables(loaded)) => loaded,
        Ok(Collected::Exit(code)) => return Outcome::Exit(code),
        Err(error) => return Outcome::raised(error),
    };
    if let Err(error) = py.check_signals() {
        return Outcome::raised(
            error.called_from(&[frames::MODULE, paths::frames::MAIN_PATH_OUTPUT]),
        );
    }
    let out = match paths::output_argument(&parsed.output) {
        Ok(out) => out,
        Err(error) => {
            return Outcome::raised(
                error.called_from(&[frames::MODULE, paths::frames::MAIN_PATH_OUTPUT]),
            );
        }
    };
    match write_xlsx(py, &loaded, &out, &parsed.source_root, clock, open) {
        // try: の中から出た例外は、except OutputError の後始末に入る所(233 行)で確かめる。
        Written::Done(Outcome::Raised(uncaught)) => {
            let Uncaught {
                exception,
                on_traceback_cleared,
            } = *uncaught;
            let stack = [frames::MODULE, frames::MAIN_CALLS_WRITE_XLSX];
            let exception = match py.enter_handler(exception, 1, &stack) {
                Ok(exception) | Err(exception) => exception,
            };
            Outcome::Raised(Box::new(Uncaught {
                exception,
                on_traceback_cleared,
            }))
        }
        Written::Done(outcome) => outcome,
        Written::Rejected(rejected) => print_rejection(py, rejected),
    }
}

/// `except OutputError as exc: print(f"ERROR 出力を拒否: {exc}", file=sys.stderr); return 2`
/// (234〜236 行)。`rejected` は `output_paths` から下の呼出履歴を持つ `OutputError`。
fn print_rejection(py: &mut pyio::Runtime, rejected: errors::PyException) -> Outcome {
    // main の段で捕まえる(呼出履歴は main の段まで)。後始末に入る所(233 行)で確かめる。
    let rejected = rejected.called_from(&[
        frames::MAIN_CALLS_WRITE_XLSX,
        paths::frames::WRITE_XLSX_CHECKS,
    ]);
    let rejected = match py.enter_handler(
        rejected,
        0,
        &[frames::MODULE, frames::MAIN_CALLS_WRITE_XLSX],
    ) {
        Ok(rejected) => rejected,
        Err(interrupt) => return Outcome::raised(interrupt),
    };
    let mut line = PyStr::from("ERROR 出力を拒否: ");
    line.push_py(&rejected.message);
    match py.print(Some(Stream::Stderr), &line) {
        Ok(()) => Outcome::Exit(2),
        // print の例外は処理中の OutputError を __context__ に持ち、`as exc` の後始末に入る所
        // (235 行)で確かめる。
        Err(error) => {
            let stack = [frames::MODULE, zip::frames::MAIN_PRINTS_ERROR];
            let error = error.called_from(&stack).with_context(rejected);
            Outcome::raised(match py.enter_handler(error, 1, &stack) {
                Ok(error) | Err(error) => error,
            })
        }
    }
}

/// [`write_xlsx`] の結果: `try:` の中の終わり方か、`main` が捕まえる `OutputError`。
enum Written {
    Done(Outcome),
    Rejected(errors::PyException),
}

/// 旧版の `write_xlsx`(171〜197 行)。
fn write_xlsx(
    py: &mut pyio::Runtime,
    loaded: &Loaded,
    out: &PyPath,
    source_roots: &[args::Value],
    clock: &dyn Clock,
    open: Opener,
) -> Written {
    match write_xlsx_body(py, loaded, out, source_roots, clock, open) {
        Ok(outcome) => Written::Done(outcome),
        Err(rejected) => Written::Rejected(rejected),
    }
}

/// [`write_xlsx`] の本体。拒否は `Err(OutputError)`。
fn write_xlsx_body(
    py: &mut pyio::Runtime,
    loaded: &Loaded,
    out: &PyPath,
    source_roots: &[args::Value],
    clock: &dyn Clock,
    open: Opener,
) -> Result<Outcome, errors::PyException> {
    let inside = |frame| [frames::MODULE, frames::MAIN_CALLS_WRITE_XLSX, frame];
    if let Err(error) = py.check_signals() {
        return Ok(Outcome::raised(
            error.called_from(&inside(paths::frames::WRITE_XLSX_CHECKS)),
        ));
    }
    let inputs = loaded
        .inputs
        .iter()
        .map(|input| PyPath::parse(input.as_bytes()))
        .collect::<Vec<_>>();
    let checked = paths::check_output(out, &inputs, source_roots);
    // 事前検査(旧版は Python の関数の木)の中で届いた SIGINT は、その中で KeyboardInterrupt になる。
    if let Err(error) = py.check_signals() {
        return Ok(Outcome::raised(
            error.called_from(&inside(paths::frames::WRITE_XLSX_CHECKS)),
        ));
    }
    let checked = match checked {
        Ok(checked) => checked,
        Err(Rejected::Output(rejected)) => return Err(rejected),
        Err(Rejected::Raised(error)) => {
            return Ok(Outcome::raised(
                error.called_from(&inside(paths::frames::WRITE_XLSX_CHECKS)),
            ));
        }
    };
    // wb = Workbook(): 作成の時刻(UTC)。
    let created = clock.now();
    let book = match Workbook::build(py, &loaded.tables) {
        Ok(book) => book,
        Err(error) => return Ok(Outcome::raised(error)),
    };
    if let Err(error) = py.check_signals() {
        return Ok(Outcome::raised(
            error.called_from(&inside(paths::frames::WRITE_XLSX_MKDIR)),
        ));
    }
    let made = paths::make_parents(&checked);
    // 親の作成(旧版は pathlib の Path.mkdir の再帰)の中で届いた SIGINT。
    if let Err(error) = py.check_signals() {
        return Ok(Outcome::raised(
            error.called_from(&inside(paths::frames::WRITE_XLSX_MKDIR)),
        ));
    }
    if let Err(error) = made {
        return Ok(Outcome::raised(
            error.called_from(&inside(paths::frames::WRITE_XLSX_MKDIR)),
        ));
    }
    if let Err(failure) = zip::save(py, &book, created, || open(&checked), clock) {
        let on_traceback_cleared = failure
            .archive
            .map(|archive| Box::new(move |py: &mut pyio::Runtime| archive.finalize(py)) as Cleanup);
        return Ok(Outcome::Raised(Box::new(Uncaught {
            exception: failure.exception,
            on_traceback_cleared,
        })));
    }
    // print の引数の len(wb.sheetnames)(property の呼出し)の所。
    if let Err(error) = py.check_signals() {
        return Ok(Outcome::raised(error.called_from(frames::PRINT_OK)));
    }
    let mut line = PyStr::from("OK ");
    line.push_py(&out.to_py());
    line.push_str(&format!(" にシート {} 件を出力", book.sheets.len()));
    Ok(match py.print(None, &line) {
        Ok(()) => Outcome::Exit(0),
        Err(error) => Outcome::raised(error.called_from(frames::PRINT_OK)),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_platform::r4e::{self, CivilTime};
    use pyio::fake::FakeOs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT: AtomicUsize = AtomicUsize::new(0);

    /// 試験の置き場(TMPDIR の下。終わったら消す)。
    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4e-main-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            let scratch = Self(path.canonicalize().unwrap());
            std::fs::create_dir(scratch.0.join("doc")).unwrap();
            std::fs::write(
                scratch.0.join("doc/v.md"),
                "# 概要\n| a | b |\n|---|---|\n| 1 | **2** |\n",
            )
            .unwrap();
            scratch
        }

        fn arg(&self, relative: &str) -> OsString {
            self.0.join(relative).into_os_string()
        }

        fn show(&self, text: &str) -> String {
            text.replace(&*self.0.to_string_lossy(), "@W")
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    struct FixedClock;

    impl Clock for FixedClock {
        fn now(&self) -> i64 {
            1_790_000_000
        }

        fn local(&self, _seconds: i64) -> std::io::Result<CivilTime> {
            Ok(CivilTime {
                year: 2026,
                month: 9,
                day: 21,
                hour: 15,
                minute: 33,
                second: 20,
            })
        }
    }

    /// 旧版の main を流し、終了値・標準出力・標準エラーを返す。
    fn run_with(args: &[OsString], open: Opener) -> (ExitCode, String, String) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        let mut runtime =
            pyio::Runtime::start_with(&state, &|_| None, Box::new(os.clone())).unwrap();
        let outcome = main(args, &mut runtime, &FixedClock, open);
        let code = runtime.finish(outcome);
        (code, os.written(Stream::Stdout), os.written(Stream::Stderr))
    }

    #[test]
    fn a_table_becomes_an_xlsx_and_the_ok_line() {
        let scratch = Scratch::new();
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out//book.xlsx"),
        ];
        let (code, stdout, stderr) = run_with(&args, &|dest| zip::create(dest));
        assert_eq!((code, stderr.as_str()), (ExitCode::SUCCESS, ""));
        assert_eq!(
            scratch.show(&stdout),
            "OK @W/out/book.xlsx にシート 1 件を出力\n"
        );
        let data = std::fs::read(scratch.0.join("out/book.xlsx")).unwrap();
        assert_eq!(&data[..4], b"PK\x03\x04");
        assert_eq!(&data[data.len() - 22..data.len() - 18], b"PK\x05\x06");
        let sheet = "xl/worksheets/sheet1.xml";
        let at = data
            .windows(sheet.len())
            .position(|window| window == sheet.as_bytes())
            .unwrap();
        // ローカルヘッダの時刻は Clock の現地時刻(2 秒単位)。
        assert_eq!(
            u16::from_le_bytes([data[at - 20], data[at - 19]]),
            (15 << 11) | (33 << 5) | 10
        );
    }

    #[test]
    fn a_rejected_output_prints_the_reason_and_creates_nothing() {
        let scratch = Scratch::new();
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("doc/sub/book.xlsx"),
        ];
        let (code, stdout, stderr) = run_with(&args, &|dest| zip::create(dest));
        assert_eq!((code, stdout.as_str()), (ExitCode::new(2), ""));
        assert_eq!(
            scratch.show(&stderr),
            "ERROR 出力を拒否: 出力先が正本ディレクトリ内にある: @W/doc/sub/book.xlsx\n"
        );
        assert!(!scratch.0.join("doc/sub").exists());
    }

    /// 標準エラーの最初の書き出しで SIGINT が届いた(`KeyboardInterrupt`)後の、旧版の main の結果。
    fn run_interrupted(args: &[OsString]) -> (ExitCode, String, String) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        os.steps(Stream::Stderr, &[pyio::fake::Step::Interrupt]);
        let mut runtime =
            pyio::Runtime::start_with(&state, &|_| None, Box::new(os.clone())).unwrap();
        let outcome = main(args, &mut runtime, &FixedClock, &|dest| zip::create(dest));
        let code = runtime.finish(outcome);
        (code, os.written(Stream::Stdout), os.written(Stream::Stderr))
    }

    const PRINT_ERROR_INTERRUPTED: &str = "\nDuring handling of the above exception, another exception occurred:\n\nTraceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 241, in <module>\n    sys.exit(main())\n  File \"md_tables_to_xlsx.py\", line 235, in main\n    print(f\"ERROR 出力を拒否: {exc}\", file=sys.stderr)\nKeyboardInterrupt\n";
    const WRITE_XLSX_HEAD: &str = "Traceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 233, in main\n    write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)\n  File \"md_tables_to_xlsx.py\", line 172, in write_xlsx\n    checked_out, = output_paths([out], inputs=inputs, source_roots=source_roots)\n";

    /// 拒否の行の print が例外になると、その例外は処理中の OutputError を __context__ に持つ
    /// (旧版で、標準エラーの一杯のパイプに SIGINT を 1 回送って実測した全文と同じ)。
    #[test]
    fn an_interrupted_rejection_line_shows_the_output_error_chain() {
        let scratch = Scratch::new();
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("doc/sub/book.xlsx"),
        ];
        let (code, stdout, stderr) = run_interrupted(&args);
        assert_eq!((code, stdout.as_str()), (ExitCode::new(130), ""));
        assert_eq!(
            scratch.show(&stderr),
            format!(
                "ERROR 出力を拒否: 出力先が正本ディレクトリ内にある: @W/doc/sub/book.xlsx\n{WRITE_XLSX_HEAD}  File \"md_tables_to_xlsx.py\", line 149, in output_paths\n    raise OutputError(f\"出力先が正本ディレクトリ内にある: {{path}}\")\n__main__.OutputError: 出力先が正本ディレクトリ内にある: @W/doc/sub/book.xlsx\n{PRINT_ERROR_INTERRUPTED}"
            )
        );

        // 正本を解決できない: 元の FileNotFoundError を __cause__ に持つ(raise … from exc)。
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/book.xlsx"),
            "--source-root".into(),
            scratch.arg("nope"),
        ];
        let (code, _, stderr) = run_interrupted(&args);
        assert_eq!(code, ExitCode::new(130));
        let missing = "[Errno 2] No such file or directory: '@W/nope'";
        assert_eq!(
            scratch.show(&stderr),
            format!(
                "ERROR 出力を拒否: 出力先または正本を解決できない: {missing}\nTraceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 125, in output_paths\n    roots = [Path(p).resolve(strict=True) for p in source_roots]\n  File \"md_tables_to_xlsx.py\", line 125, in <listcomp>\n    roots = [Path(p).resolve(strict=True) for p in source_roots]\n  File \"pathlib.py\", line 1077, in resolve\n    s = self._accessor.realpath(self, strict=strict)\n  File \"posixpath.py\", line 396, in realpath\n    path, ok = _joinrealpath(filename[:0], filename, strict, {{}})\n  File \"posixpath.py\", line 431, in _joinrealpath\n    st = os.lstat(newpath)\nFileNotFoundError: {missing}\n\nThe above exception was the direct cause of the following exception:\n\n{WRITE_XLSX_HEAD}  File \"md_tables_to_xlsx.py\", line 153, in output_paths\n    raise OutputError(f\"出力先または正本を解決できない: {{exc}}\") from exc\n__main__.OutputError: 出力先または正本を解決できない: {missing}\n{PRINT_ERROR_INTERRUPTED}"
            )
        );
    }

    /// 信号の印を `after` 回確かめた直後に SIGINT が届く(0 は最初から届いている)ときの、旧版の
    /// main の結果と、main の中で確かめた回数。
    fn run_signalled(
        args: &[OsString],
        unbuffered: bool,
        after: usize,
    ) -> (ExitCode, String, String, usize) {
        run_signalled_with(args, unbuffered, after, &[], &|dest| zip::create(dest))
    }

    /// [`run_signalled`] の、標準エラーの書き込みの動きと出力先を開く処理を決める形。
    fn run_signalled_with(
        args: &[OsString],
        unbuffered: bool,
        after: usize,
        stderr_steps: &[pyio::fake::Step],
        open: Opener,
    ) -> (ExitCode, String, String, usize) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        os.steps(Stream::Stderr, stderr_steps);
        let env: &dyn Fn(&str) -> Option<Vec<u8>> =
            &|name| (unbuffered && name == "PYTHONUNBUFFERED").then(|| b"1".to_vec());
        let mut runtime = pyio::Runtime::start_with(&state, env, Box::new(os.clone())).unwrap();
        {
            let mut fake = os.0.borrow_mut();
            fake.pending = after == 0;
            fake.sigint_after_checks = Some(after);
        }
        let outcome = main(args, &mut runtime, &FixedClock, open);
        // main の中で確かめた回数(終了の処理の書き出しの後の確かめは数えない)。
        let checks = os.0.borrow().checks;
        let code = runtime.finish(outcome);
        (
            code,
            os.written(Stream::Stdout),
            os.written(Stream::Stderr),
            checks,
        )
    }

    /// 呼出履歴の最後の段の行(KeyboardInterrupt になった所)。
    fn interrupted_at(stderr: &str) -> u32 {
        assert!(stderr.ends_with("KeyboardInterrupt\n"), "{stderr}");
        let line = stderr
            .lines()
            .rfind(|line| line.starts_with("  File "))
            .expect("呼出履歴の段");
        line.rsplit_once(", line ")
            .and_then(|(_, rest)| rest.split_once(','))
            .map(|(number, _)| number.parse().unwrap())
            .unwrap()
    }

    /// 処理の途中(書き込みで止まっていない間)のどこで SIGINT が届いても、旧版と同じく次の切れ目で
    /// KeyboardInterrupt になり、SIGINT で終わる。届いた所より後の出力は出さない。最後の確かめの
    /// 後に届いた分だけは、旧版と同じく扱われずに終わる。
    #[test]
    fn a_sigint_during_processing_is_never_dropped() {
        let scratch = Scratch::new();
        std::fs::write(scratch.0.join("doc/plain.md"), "# 表なし\n本文\n").unwrap();
        let no_tables = [
            scratch.arg("doc/plain.md"),
            "-o".into(),
            scratch.arg("out/a.xlsx"),
        ];
        let warn_then_none = [
            scratch.arg("doc/missing.md"),
            scratch.arg("doc/plain.md"),
            "-o".into(),
            scratch.arg("out/a.xlsx"),
        ];
        let rejected = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("doc/x.xlsx"),
        ];
        let written = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/new/b.xlsx"),
        ];
        // 読取り: 入力の繰り返し(211)・is_file の後(213)・decode の段に入る所(319)・解読の後(322)・
        // with を出た所(1134)・表の取り出しの後(216)。
        const READ: [u32; 6] = [211, 213, 319, 322, 1134, 216];
        // print(text) は、print の前・文の前・改行の前で確かめる(str() の所)。
        let no_tables_lines = [&[206][..], &READ, &[223, 223, 223, 241]].concat();
        let warn_lines = [&[206, 211][..], &READ, &[219, 219, 219, 241]].concat();
        // 出力先の引数の前(226)・write_xlsx の最初と事前検査の後(172)・except OutputError の
        // 後始末(233)・拒否の行の print(235)。
        let rejected_lines = [&[206][..], &READ, &[226, 172, 172, 233, 235, 235, 241]].concat();
        // ブックの組み立て(180)・親の作成の前後(195)・ZipFile を開く前(291)・各部分の前と書いた後
        // (1180)・閉じる前(276)と終わりの記録の書き出しの後(1937)・OK の print(197)。
        let written_lines = [
            &[206][..],
            &READ,
            &[
                226, 172, 172, 180, 195, 195, 291, 60, 1180, 62, 1180, 66, 1180, 203, 1180, 85,
                1180, 88, 1180, 89, 1180, 90, 1180, 166, 1180, 276, 1937, 197, 197, 197, 241,
            ],
        ]
        .concat();
        // (引数, 溜め方, 確かめる所の行, 届かなかったときの終了値)
        let cases: [(&[OsString], bool, &[u32], u8); 5] = [
            (&no_tables, false, &no_tables_lines, 1),
            (&no_tables, true, &no_tables_lines, 1),
            (&warn_then_none, true, &warn_lines, 2),
            (&rejected, true, &rejected_lines, 2),
            (&written, true, &written_lines, 0),
        ];
        for (args, unbuffered, lines, code) in cases {
            let label = format!("{:?} {unbuffered}", args.last().unwrap());
            let _ = std::fs::remove_dir_all(scratch.0.join("out"));
            let (finished, full, _, total) = run_signalled(args, unbuffered, usize::MAX);
            assert_eq!(finished, ExitCode::new(code), "{label}");
            let mut actual = Vec::new();
            for after in 0..total {
                let _ = std::fs::remove_dir_all(scratch.0.join("out"));
                let (finished, stdout, stderr, _) = run_signalled(args, unbuffered, after);
                assert_eq!(finished, ExitCode::new(130), "{label} {after}");
                actual.push(interrupted_at(&stderr));
                // 標準出力の行(表なし・OK)は、print の文の後に届いたときは改行の前まで、
                // print の後に届いたときは全部が出る。それより前に届いたときは出ない。
                let expected = match total - after {
                    1 => full.as_str(),
                    2 => full.trim_end_matches('\n'),
                    _ => "",
                };
                assert_eq!(stdout, expected, "{label} {after}");
            }
            assert_eq!(actual, lines, "{label}");
            // 最後の確かめの後に届いた分は、旧版と同じく扱われない。
            let (finished, _, _, _) = run_signalled(args, unbuffered, total);
            assert_eq!(finished, ExitCode::new(code), "{label}");
        }
    }

    /// 例外を処理する所(except・with の後始末)に入る所で届いていた SIGINT は、処理中の例外を
    /// __context__ に持つ KeyboardInterrupt になる。後始末の無い経路で、表示の途中で拾うと、
    /// 旧版と同じく表示が切れる(LD_PRELOAD で write の失敗と SIGINT を作った旧版の実測と同じ)。
    #[test]
    fn a_sigint_pending_at_an_exception_handler_becomes_keyboard_interrupt() {
        use pyio::fake::Step;
        let scratch = Scratch::new();
        let rejected = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("doc/x.xlsx"),
        ];
        // 拒否の行の print が EPIPE で失敗し、その間に届いた SIGINT は `as exc` の後始末(235 行)。
        // 確かめる所: 206・読取り(6)・226・172・172・233・print の文の前(235)。
        let (code, stdout, stderr, _) =
            run_signalled_with(&rejected, true, 12, &[Step::Fail(32)], &|dest| {
                zip::create(dest)
            });
        assert_eq!((code, stdout.as_str()), (ExitCode::new(130), ""));
        assert_eq!(
            scratch.show(&stderr),
            "Traceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 233, in main\n    write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)\n  File \"md_tables_to_xlsx.py\", line 172, in write_xlsx\n    checked_out, = output_paths([out], inputs=inputs, source_roots=source_roots)\n  File \"md_tables_to_xlsx.py\", line 149, in output_paths\n    raise OutputError(f\"出力先が正本ディレクトリ内にある: {path}\")\n__main__.OutputError: 出力先が正本ディレクトリ内にある: @W/doc/x.xlsx\n\nDuring handling of the above exception, another exception occurred:\n\nTraceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 235, in main\n    print(f\"ERROR 出力を拒否: {exc}\", file=sys.stderr)\nBrokenPipeError: [Errno 32] Broken pipe\n\nDuring handling of the above exception, another exception occurred:\n\nTraceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 241, in <module>\n    sys.exit(main())\n  File \"md_tables_to_xlsx.py\", line 235, in main\n    print(f\"ERROR 出力を拒否: {exc}\", file=sys.stderr)\nKeyboardInterrupt\n"
        );

        // write_xlsx から出た例外(出力先を開けない)は、except OutputError の後始末(233 行)。
        // 確かめる所: 206・読取り(6)・226・172・172・180・195・195・ZipFile を開く前(291)。
        let written = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/b.xlsx"),
        ];
        let denied: Opener = &|dest| {
            Err(errors::PyException::os_with_filename(
                13,
                errors::Filename::Str(dest.to_py()),
            ))
        };
        let (code, _, stderr, _) = run_signalled_with(&written, true, 14, &[], denied);
        assert_eq!(code, ExitCode::new(130));
        let (context, interrupt) = stderr
            .split_once("\nDuring handling of the above exception, another exception occurred:\n\n")
            .expect("連鎖");
        assert!(context.starts_with("Traceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 233, in main\n"));
        assert!(
            scratch
                .show(context)
                .ends_with("PermissionError: [Errno 13] Permission denied: '@W/out/b.xlsx'\n")
        );
        assert_eq!(
            interrupt,
            "Traceback (most recent call last):\n  File \"md_tables_to_xlsx.py\", line 241, in <module>\n    sys.exit(main())\n  File \"md_tables_to_xlsx.py\", line 233, in main\n    write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)\nKeyboardInterrupt\n"
        );
        // 届いていなければ、元の例外で終了 1。
        let (code, _, stderr, _) = run_signalled_with(&written, true, usize::MAX, &[], denied);
        assert_eq!(code, ExitCode::new(1));
        assert!(
            scratch
                .show(&stderr)
                .ends_with("PermissionError: [Errno 13] Permission denied: '@W/out/b.xlsx'\n")
        );
        assert!(!stderr.contains("KeyboardInterrupt"));

        // 後始末の無い経路(WARN の print が EPIPE)で、表示を始める所で届いていた SIGINT は、
        // 最初の書き込みの str() の所で拾われて表示が切れ、改行だけが出て終了 1(旧版の実測)。
        // 確かめる所: 206・211・211・読取り(5)・WARN の print の前(219)・文の前(219)。
        let warn = [
            scratch.arg("doc/none.md"),
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/c.xlsx"),
        ];
        let (code, stdout, stderr, _) =
            run_signalled_with(&warn, true, 10, &[Step::Fail(32)], &|dest| {
                zip::create(dest)
            });
        assert_eq!(
            (code, stdout.as_str(), stderr.as_str()),
            (ExitCode::new(1), "", "\n")
        );

        // is_file() の誤り(検索できないディレクトリの下の EACCES)は、is_file の except OSError の
        // 後始末(1322 行)。確かめる所: 206・入力の繰り返し(211)。
        use std::os::unix::fs::PermissionsExt;
        let locked = scratch.0.join("locked");
        std::fs::create_dir(&locked).unwrap();
        std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
        let hidden = [
            scratch.arg("locked/x.md"),
            "-o".into(),
            scratch.arg("out/d.xlsx"),
        ];
        let (code, _, stderr, _) = run_signalled(&hidden, true, 2);
        let (plain, _, plain_stderr, _) = run_signalled(&hidden, true, usize::MAX);
        std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(code, ExitCode::new(130));
        let (context, interrupt) = stderr
            .split_once("\nDuring handling of the above exception, another exception occurred:\n\n")
            .expect("連鎖");
        assert_eq!(
            scratch.show(context),
            "Traceback (most recent call last):\n  File \"pathlib.py\", line 1322, in is_file\n    return S_ISREG(self.stat().st_mode)\n  File \"pathlib.py\", line 1097, in stat\n    return self._accessor.stat(self, follow_symlinks=follow_symlinks)\nPermissionError: [Errno 13] Permission denied: '@W/locked/x.md'\n"
        );
        assert!(interrupt.ends_with(
            "  File \"pathlib.py\", line 1322, in is_file\n    return S_ISREG(self.stat().st_mode)\nKeyboardInterrupt\n"
        ));
        assert_eq!(plain, ExitCode::new(1));
        assert!(
            scratch
                .show(&plain_stderr)
                .ends_with("PermissionError: [Errno 13] Permission denied: '@W/locked/x.md'\n")
        );
    }

    #[test]
    fn an_empty_list_output_raises_the_type_error_at_line_226() {
        let scratch = Scratch::new();
        let args = [scratch.arg("doc/v.md"), "-o=--".into()];
        let (code, stdout, stderr) = run_with(&args, &|dest| zip::create(dest));
        assert_eq!((code, stdout.as_str()), (ExitCode::new(1), ""));
        assert!(
            stderr.contains(
                "  File \"md_tables_to_xlsx.py\", line 226, in main\n    out = Path(args.output)\n"
            ),
            "{stderr}"
        );
        assert!(
            stderr.ends_with("TypeError: expected str, bytes or os.PathLike object, not list\n")
        );
    }

    #[test]
    fn an_output_that_cannot_be_opened_raises_from_the_zipfile_open() {
        let scratch = Scratch::new();
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/book.xlsx"),
        ];
        let (code, stdout, stderr) = run_with(&args, &|dest| {
            Err(errors::PyException::os_with_filename(
                13,
                errors::Filename::Str(dest.to_py()),
            ))
        });
        assert_eq!((code, stdout.as_str()), (ExitCode::new(1), ""));
        assert!(scratch.show(&stderr).ends_with(
            "  File \"openpyxl/writer/excel.py\", line 291, in save_workbook\n    archive = ZipFile(filename, 'w', ZIP_DEFLATED, allowZip64=True)\n  File \"zipfile.py\", line 1251, in __init__\n    self.fp = io.open(file, filemode)\nPermissionError: [Errno 13] Permission denied: '@W/out/book.xlsx'\n"
        ));
        // 親は作った後(旧版と同じ順)。ファイルは作らない。
        assert!(scratch.0.join("out").is_dir());
        assert!(!scratch.0.join("out/book.xlsx").exists());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_failed_write_is_followed_by_the_zipfile_del_traceback() {
        let scratch = Scratch::new();
        let args = [
            scratch.arg("doc/v.md"),
            "-o".into(),
            scratch.arg("out/book.xlsx"),
        ];
        let (code, stdout, stderr) = run_with(&args, &|_| {
            Ok(File::options().write(true).open("/dev/full").unwrap())
        });
        assert_eq!((code, stdout.as_str()), (ExitCode::new(1), ""));
        let (first, second) = stderr
            .split_once("Exception ignored in: <function ZipFile.__del__ at 0x")
            .unwrap();
        assert!(first.starts_with("Traceback (most recent call last):\n"));
        assert!(first.ends_with(
            "  File \"zipfile.py\", line 1180, in close\n    self._fileobj.seek(self._zinfo.header_offset)\nOSError: [Errno 28] No space left on device\n"
        ));
        assert!(second.ends_with(
            "  File \"zipfile.py\", line 1943, in _fpclose\n    fp.close()\nOSError: [Errno 28] No space left on device\n"
        ));
    }
}
