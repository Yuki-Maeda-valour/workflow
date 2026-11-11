//! 旧版の `main()` から終了までの出力の流れ。引数の誤りと help、捕まえる例外の `ERROR [...]` の行、
//! 捕まえない例外の表示(U4)、終了時の flush(終了コード 120)。コマンドの本体は呼出し側が渡す。
use super::args::{ArgEnv, Exit, Parsed, parse_args};
use super::error::PyException;
use super::pystr::cps;
use super::runtime::{Env, PyStream, StreamKind, finalize};

/// `host-*` の 3 つのコマンドか。失敗の行が違う。
pub fn is_host_command(command: &str) -> bool {
    matches!(command, "host-paths" | "host-settings" | "host-allowlist")
}

/// `main` が捕まえる例外のときの、標準エラーの 1 行(改行は `print` が足す)。
pub fn error_line(command: &str, error: &PyException) -> Vec<u32> {
    let detail = error.detail();
    if is_host_command(command) {
        let code = if matches!(
            detail.as_str(),
            "host-config-path" | "host-settings" | "host-config-source" | "hooks-disabled"
        ) {
            detail.as_str()
        } else {
            "host-settings"
        };
        cps(&format!("ERROR [{code}] ホスト設定を安全に検査できない"))
    } else {
        cps(&format!("ERROR [environment-guard] {detail}"))
    }
}

/// `SystemExit(code)` が OS へ渡す終了コード。負の値(シグナルで終わった子)は 256 を足した値になる。
pub fn exit_status(code: i64) -> i32 {
    (code & 0xff) as i32
}

/// コマンドの本体。成功は `SystemExit` の値(`main` の戻り値)、失敗は旧版の例外。
pub type Dispatch<'a> =
    dyn FnMut(&Parsed, &mut PyStream<'_>, &mut PyStream<'_>) -> Result<i64, PyException> + 'a;

/// `python3 environment-guard.py <args>` が終了するまで。戻り値は終了コード。
pub fn run_main(
    args: &[Vec<u32>],
    env: &Env,
    stdout: &mut PyStream<'_>,
    stderr: &mut PyStream<'_>,
    dispatch: &mut Dispatch<'_>,
) -> i32 {
    let outcome = run_body(args, env, stdout, stderr, dispatch);
    // `main` の後の `flush_io`: 標準エラー、標準出力の順に flush し、失敗は黙って捨てる。
    // 捕まえない例外の表示と終了時の flush より前に行う。
    let _ = stderr.flush();
    let _ = stdout.flush();
    let status = match outcome {
        Ok(code) => exit_status(code),
        Err(error) => {
            // 捕まえない例外: 途中の行を除いた表示を標準エラーへ書き、終了コード 1。
            let _ = stderr.write_text(&error.uncaught_report());
            1
        }
    };
    finalize(stdout, stderr).unwrap_or(status)
}

fn run_body(
    args: &[Vec<u32>],
    env: &Env,
    stdout: &mut PyStream<'_>,
    stderr: &mut PyStream<'_>,
    dispatch: &mut Dispatch<'_>,
) -> Result<i64, PyException> {
    let arg_env = ArgEnv {
        columns: env.columns.clone(),
        terminal_columns: env.terminal_columns,
        max_str_digits: env.max_str_digits,
    };
    let parsed = match parse_args(args, &arg_env) {
        Ok(parsed) => parsed,
        Err(exit) => return finish_exit(&exit, stdout, stderr),
    };
    match dispatch(&parsed, stdout, stderr) {
        Ok(code) => Ok(code),
        Err(error) if error.caught_by_main() => {
            // `print(..., file=sys.stderr)` の失敗は、例外の処理中に起きた例外になる。
            let line = error_line(&parsed.command, &error);
            stderr
                .print(&line)
                .map_err(|failure| failure.during(error))?;
            Ok(20)
        }
        Err(error) => Err(error),
    }
}

/// `parser.exit(...)` までの出力。
fn finish_exit(
    exit: &Exit,
    stdout: &mut PyStream<'_>,
    stderr: &mut PyStream<'_>,
) -> Result<i64, PyException> {
    for (kind, text) in &exit.outputs {
        match kind {
            StreamKind::Stdout => stdout.write_text(text)?,
            StreamKind::Stderr => stderr.write_text(text)?,
        }
    }
    Ok(i64::from(exit.code))
}

#[cfg(test)]
mod tests {
    use super::super::pystr::{decode_fs, to_string};
    use super::super::testing::{EAGAIN, fixture, for_this_os};
    use super::*;
    use std::io::Write;

    struct Failing(i32);
    impl Write for Failing {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::from_raw_os_error(self.0))
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn env() -> Env {
        Env {
            columns: None,
            terminal_columns: 80,
            unbuffered: false,
            max_str_digits: 4300,
        }
    }

    fn drive(
        args: &[&str],
        stdout_raw: &mut dyn Write,
        stderr_raw: &mut dyn Write,
        dispatch: &mut Dispatch<'_>,
    ) -> i32 {
        let args: Vec<Vec<u32>> = args.iter().map(|a| decode_fs(a.as_bytes())).collect();
        let env = env();
        let mut stdout = PyStream::new(stdout_raw, StreamKind::Stdout, false, 4096, false);
        let mut stderr = PyStream::new(stderr_raw, StreamKind::Stderr, false, 4096, false);
        run_main(&args, &env, &mut stdout, &mut stderr, dispatch)
    }

    fn verify_args() -> [&'static str; 5] {
        ["verify", "--state", "s", "--expect-sha256", "h"]
    }

    #[test]
    fn help_and_usage_errors_use_the_argparse_exit_codes() {
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = drive(
            &["host-paths", "-h"],
            &mut out,
            &mut err,
            &mut |_, _, _| unreachable!(),
        );
        assert_eq!((code, err.len()), (0, 0));
        assert!(
            String::from_utf8(out)
                .unwrap()
                .starts_with("usage: environment-guard.py host-paths [-h]\n\noptions:\n")
        );
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = drive(
            &["verify"],
            &mut out,
            &mut err,
            &mut |_, _, _| unreachable!(),
        );
        assert_eq!((code, out.len()), (2, 0));
        assert!(String::from_utf8(err).unwrap().ends_with("environment-guard.py verify: error: the following arguments are required: --state, --expect-sha256\n"));
    }

    #[test]
    fn caught_exceptions_become_error_lines_with_exit_20() {
        for (command, args, error, line) in [
            (
                "verify",
                &verify_args()[..],
                PyException::stop("理由"),
                "ERROR [environment-guard] 理由\n",
            ),
            (
                "verify",
                &verify_args()[..],
                PyException::new("FileNotFoundError", "[Errno 2] x: '/y'"),
                "ERROR [environment-guard] 検査を完了できない (FileNotFoundError)\n",
            ),
            (
                "host-settings",
                &["host-settings", "--state", "s", "--expect-sha256", "h"][..],
                PyException::stop("hooks-disabled"),
                "ERROR [hooks-disabled] ホスト設定を安全に検査できない\n",
            ),
            (
                "host-settings",
                &["host-settings", "--state", "s", "--expect-sha256", "h"][..],
                PyException::stop("その他"),
                "ERROR [host-settings] ホスト設定を安全に検査できない\n",
            ),
            (
                "host-paths",
                &["host-paths"][..],
                PyException::new("KeyError", "'k'"),
                "ERROR [host-settings] ホスト設定を安全に検査できない\n",
            ),
        ] {
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let thrown = error.clone();
            let code = drive(args, &mut out, &mut err, &mut |parsed, _, _| {
                assert_eq!(parsed.command, command);
                Err(thrown.clone())
            });
            assert_eq!(code, 20);
            assert!(out.is_empty());
            assert_eq!(String::from_utf8(err).unwrap(), line);
        }
    }

    #[test]
    fn uncaught_exceptions_print_the_last_line_with_exit_1() {
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = drive(&verify_args(), &mut out, &mut err, &mut |_, stdout, _| {
            stdout.print(&cps("partial")).unwrap();
            Err(PyException::new(
                "AttributeError",
                "'NoneType' object has no attribute 'x'",
            ))
        });
        assert_eq!(code, 1);
        assert_eq!(String::from_utf8(out).unwrap(), "partial\n");
        assert_eq!(
            String::from_utf8(err).unwrap(),
            "Traceback (most recent call last):\nAttributeError: 'NoneType' object has no attribute 'x'\n"
        );
    }

    #[test]
    fn exit_status_wraps_like_the_operating_system() {
        assert_eq!(exit_status(0), 0);
        assert_eq!(exit_status(20), 20);
        assert_eq!(exit_status(-9), 247);
        assert_eq!(exit_status(256), 0);
        assert_eq!(exit_status(-1), 255);
    }

    #[test]
    fn a_failed_final_flush_overrides_every_exit_code_with_120() {
        for dispatch_code in [0, 20] {
            let (mut out, mut err) = (Failing(32), Vec::new());
            let code = drive(&verify_args(), &mut out, &mut err, &mut |_, stdout, _| {
                stdout.print(&cps("x")).unwrap();
                Ok(dispatch_code)
            });
            assert_eq!(code, 120);
            assert_eq!(
                to_string(&decode_fs(&err)),
                "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nBrokenPipeError: [Errno 32] Broken pipe\n"
            );
        }
    }

    /// 固定期待の `spec` が起こす旧版の例外。`None` は例外なし。
    fn thrown(spec: &str) -> Option<PyException> {
        let (kind, detail) = spec.split_once(':').unwrap_or((spec, ""));
        let key = |item: &str| PyException::new("KeyError", &format!("'{item}'"));
        Some(match kind {
            "ok" | "code" => return None,
            "stop" => PyException::stop(detail),
            "oserror" => {
                let (number, name) = detail.split_once(':').unwrap_or((detail, ""));
                let number: i32 = number.parse().unwrap();
                let reason = if number == 2 {
                    "No such file or directory"
                } else {
                    "Permission denied"
                };
                let name = (!name.is_empty()).then(|| cps(name));
                PyException::with_cps(
                    super::super::error::os_error_type(number),
                    super::super::error::os_error_message(number, reason, name.as_deref()),
                )
            }
            "value" => PyException::new("ValueError", detail),
            "key" => key(detail),
            "type" => PyException::new("TypeError", detail),
            "recursion" => PyException::new("RecursionError", detail),
            "index" => PyException::new("IndexError", detail),
            "attr" => PyException::new("AttributeError", detail),
            "overflow" => PyException::new("OverflowError", detail),
            "zero" => PyException::new("ZeroDivisionError", detail),
            "runtime" => PyException::new("RuntimeError", detail),
            "unicode" => PyException::new(
                "UnicodeDecodeError",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
            ),
            "timeout" => {
                PyException::new("TimeoutExpired", "Command 'cmd' timed out after 1 seconds")
            }
            "chain-context" => PyException::new("IndexError", "b").during(key("a")),
            "chain-cause" => PyException {
                explicit_cause: true,
                ..PyException::new("IndexError", "b").during(PyException {
                    unraised: true,
                    ..key("a")
                })
            },
            "chain-none" => PyException::new("IndexError", "b")
                .during(key("a"))
                .without_context(),
            "chain-caught" => PyException::new("ValueError", "v").during(key("a")),
            other => panic!("未知の spec {other}"),
        })
    }

    /// 固定期待の 1 件(`command`・`spec`・`unbuffered`)を、指定した出力先で動かす。
    fn run_spec_case(
        case: &serde_json::Value,
        stdout_raw: &mut dyn Write,
        stderr_raw: &mut dyn Write,
        tty: bool,
    ) -> i32 {
        let spec = case["spec"].as_str().unwrap();
        let command: Vec<&str> = case["command"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let args: Vec<Vec<u32>> = command.iter().map(|a| decode_fs(a.as_bytes())).collect();
        let env = Env {
            columns: Some(cps("80")),
            terminal_columns: 80,
            unbuffered: case["unbuffered"].as_bool().unwrap(),
            max_str_digits: 4300,
        };
        let error = thrown(spec);
        let code_spec: Option<i64> = spec.strip_prefix("code:").map(|n| n.parse().unwrap());
        let mut stdout = PyStream::new(stdout_raw, StreamKind::Stdout, env.unbuffered, 4096, tty);
        let mut stderr = PyStream::new(stderr_raw, StreamKind::Stderr, env.unbuffered, 4096, false);
        run_main(
            &args,
            &env,
            &mut stdout,
            &mut stderr,
            &mut |parsed, stdout, _| {
                if let Some(error) = &error {
                    return Err(error.clone());
                }
                if let Some(code) = code_spec {
                    return Ok(code);
                }
                match parsed.command.as_str() {
                    "host-paths" => stdout.print(&cps("{\"home\":\"/h\"}"))?,
                    "host-allowlist" => {
                        stdout.print(&cps("[{\"kind\": \"exact\", \"words\": [\"x\"]}]"))?;
                        stdout.print(&cps("unused=a(b)"))?;
                    }
                    _ => {}
                }
                Ok(0)
            },
        )
    }

    fn expect_bytes(case: &serde_json::Value, name: &str) -> String {
        use super::super::testing::from_hex;
        let bytes = match case[name].as_str() {
            Some(text) => text.as_bytes().to_vec(),
            None => from_hex(case[format!("{name}_hex")].as_str().unwrap()),
        };
        // OS で番号が変わる EAGAIN の `[Errno 11]` は、この OS の番号にして比べる。
        for_this_os(&String::from_utf8_lossy(&bytes))
    }

    fn check_case(case: &serde_json::Value, label: &str, status: i32, out: &[u8], err: &[u8]) {
        assert_eq!(i64::from(status), case["code"].as_i64().unwrap(), "{label}");
        assert_eq!(
            String::from_utf8_lossy(out),
            expect_bytes(case, "stdout"),
            "{label}"
        );
        assert_eq!(
            String::from_utf8_lossy(err),
            expect_bytes(case, "stderr"),
            "{label}"
        );
    }

    #[test]
    fn main_cases_match_python_3_10() {
        let cases = fixture!("main_cases.json");
        let cases = cases.as_array().unwrap();
        assert!(cases.len() > 600, "{}", cases.len());
        for case in cases {
            let mode = case["mode"].as_str().unwrap();
            let label = format!("{} {mode} {}", case["spec"], case["command"]);
            let (mut out_vec, mut err_vec) = (Vec::new(), Vec::new());
            let (mut out_full, mut out_closed, mut err_full) =
                (Failing(28), Failing(32), Failing(28));
            let stdout_raw: &mut dyn Write = match mode {
                "stdout-full" | "both-full" => &mut out_full,
                "stdout-closed" => &mut out_closed,
                _ => &mut out_vec,
            };
            let stderr_raw: &mut dyn Write = match mode {
                "stderr-full" | "both-full" => &mut err_full,
                _ => &mut err_vec,
            };
            let status = run_spec_case(case, stdout_raw, stderr_raw, false);
            check_case(case, &label, status, &out_vec, &err_vec);
        }
    }

    #[test]
    fn terminal_cases_match_python_3_10() {
        // 端末の標準出力は、バッファを使うときも改行ごとに flush する。満杯の端末(EAGAIN)では print の中で失敗する。
        let cases = fixture!("main_tty_cases.json");
        assert!(cases.as_array().unwrap().len() >= 40);
        for case in cases.as_array().unwrap() {
            let label = format!(
                "{} {} {} {}",
                case["spec"], case["mode"], case["command"], case["unbuffered"]
            );
            let (mut out_vec, mut err_vec) = (Vec::new(), Vec::new());
            let (mut closed, mut full) = (Failing(5), Failing(EAGAIN));
            // 切れた端末(EIO)は isatty が偽になる。満杯の端末(EAGAIN)は端末のまま。
            let (stdout_raw, tty): (&mut dyn Write, bool) = match case["mode"].as_str().unwrap() {
                "open" => (&mut out_vec, true),
                "closed-master" => (&mut closed, false),
                _ => (&mut full, true),
            };
            let status = run_spec_case(case, stdout_raw, &mut err_vec, tty);
            check_case(case, &label, status, &out_vec, &err_vec);
        }
    }

    #[test]
    fn nonblocking_full_pipe_cases_match_python_3_10() {
        let cases = fixture!("main_block_cases.json");
        assert!(cases.as_array().unwrap().len() >= 70);
        for case in cases.as_array().unwrap() {
            let label = format!(
                "{} {} {} {}",
                case["spec"], case["mode"], case["command"], case["unbuffered"]
            );
            let mode = case["mode"].as_str().unwrap();
            let (mut out_vec, mut err_vec) = (Vec::new(), Vec::new());
            let (mut out_block, mut err_block) = (Failing(EAGAIN), Failing(EAGAIN));
            let stdout_raw: &mut dyn Write = if mode == "stderr-block" {
                &mut out_vec
            } else {
                &mut out_block
            };
            let stderr_raw: &mut dyn Write = if mode == "stdout-block" {
                &mut err_vec
            } else {
                &mut err_block
            };
            let status = run_spec_case(case, stdout_raw, stderr_raw, false);
            check_case(case, &label, status, &out_vec, &err_vec);
        }
    }

    /// 2 本の `PyStream` が同じ書込み先(同じ fd)へ書く場合。
    struct Shared(std::rc::Rc<std::cell::RefCell<Vec<u8>>>);
    impl Write for Shared {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            self.0.borrow_mut().extend_from_slice(data);
            Ok(data.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn flush_after_main_cases_match_python_3_10() {
        // `main` の後に、標準エラー、標準出力の順に flush する(失敗は捨てる)。捕まえない例外の表示より前。
        let cases = fixture!("main_flush_cases.json");
        assert!(cases.as_array().unwrap().len() >= 90);
        for case in cases.as_array().unwrap() {
            let size = case["size"].as_u64().unwrap() as usize;
            let result = case["result"].as_str().unwrap();
            let mode = case["mode"].as_str().unwrap();
            let label = format!("{size} {result} {mode}");
            let shared = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
            let (mut out_vec, mut err_vec) = (Vec::new(), Vec::new());
            let (mut out_full, mut out_closed) = (Failing(28), Failing(32));
            let (mut shared_out, mut shared_err) = (Shared(shared.clone()), Shared(shared.clone()));
            let (stdout_raw, stderr_raw): (&mut dyn Write, &mut dyn Write) = match mode {
                "same-fd" => (&mut shared_out, &mut shared_err),
                "stdout-full" => (&mut out_full, &mut err_vec),
                "stdout-closed" => (&mut out_closed, &mut err_vec),
                _ => (&mut out_vec, &mut err_vec),
            };
            let env = env();
            let args: Vec<Vec<u32>> = ["verify", "--state", "s", "--expect-sha256", "h"]
                .iter()
                .map(|a| decode_fs(a.as_bytes()))
                .collect();
            let status = {
                let mut stdout = PyStream::new(stdout_raw, StreamKind::Stdout, false, 4096, false);
                let mut stderr = PyStream::new(stderr_raw, StreamKind::Stderr, false, 4096, false);
                run_main(
                    &args,
                    &env,
                    &mut stdout,
                    &mut stderr,
                    &mut |_, stdout, _| {
                        stdout.print(&vec![0x78; size])?;
                        match result {
                            "uncaught" => Err(PyException::new("IndexError", "i")),
                            "code20" => Ok(20),
                            _ => Ok(0),
                        }
                    },
                )
            };
            let (out, err) = if mode == "same-fd" {
                (shared.borrow().clone(), Vec::new())
            } else {
                (out_vec, err_vec)
            };
            check_case(case, &label, status, &out, &err);
        }
    }
}
