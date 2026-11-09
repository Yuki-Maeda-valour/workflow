#![forbid(unsafe_code)]
//! 製品CLIには登録しないC0c専用接続。通常起動はPythonの比較器を必ず実行する。
use dev_workflow_core::budget::{Budget, BudgetLimits};
use dev_workflow_core::process::*;
use dev_workflow_core::{CommandIo, CommandOutput, ExitCode, execute, execute_to};
use serde_json::{Value, json};
use std::ffi::OsString;
use std::io::{self, Write};
use std::os::unix::ffi::OsStringExt;
use std::path::Path;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|x| format!("{x:02x}")).collect()
}
fn unhex(value: &str) -> Vec<u8> {
    assert!(value.len().is_multiple_of(2));
    (0..value.len())
        .step_by(2)
        .map(|n| u8::from_str_radix(&value[n..n + 2], 16).unwrap())
        .collect()
}
fn number(v: &Value, name: &str) -> u64 {
    v[name].as_u64().expect(name)
}
fn raw(v: &Value, name: &str) -> Vec<u8> {
    unhex(v[name].as_str().expect(name))
}
fn status(s: ChildStatus) -> Value {
    match s {
        ChildStatus::Exited(c) => json!({"kind":"exit","value":c.as_u8()}),
        ChildStatus::Signaled(n) => json!({"kind":"signal","value":n}),
    }
}
fn cause(c: &RunCause<&str>) -> Value {
    let (kind, stream, errno, consumer) = match c {
        RunCause::InvalidDeadlines => ("invalid-deadlines", None, None, None),
        RunCause::Cancelled => ("cancelled", None, None, None),
        RunCause::Deadline => ("deadline", None, None, None),
        RunCause::Spawn(e) => ("spawn", None, e.raw_os_error(), None),
        RunCause::Setup(e) => ("setup", None, e.raw_os_error(), None),
        RunCause::Poll(e) => ("poll", None, e.raw_os_error(), None),
        RunCause::Wait(e) => ("wait", None, e.raw_os_error(), None),
        RunCause::InputClosed => ("input-closed", None, None, None),
        RunCause::Io { stream, error } => (
            "io",
            Some(match stream {
                ChildStream::Stdin => "stdin",
                ChildStream::Stdout => "stdout",
                ChildStream::Stderr => "stderr",
            }),
            error.raw_os_error(),
            None,
        ),
        RunCause::Consumer(e) => ("consumer", None, None, Some(*e)),
    };
    json!({"kind":kind,"stream":stream,"errno":errno,"consumer":consumer})
}
fn read_line(io: &mut CommandIo<'_>) -> Vec<u8> {
    let mut raw = Vec::new();
    loop {
        let mut bytes = [0; 4096];
        let n = io.read_input(&mut bytes).unwrap();
        assert!(n > 0);
        raw.extend_from_slice(&bytes[..n]);
        if let Some(end) = raw.iter().position(|b| *b == b'\n') {
            assert_eq!(end + 1, raw.len());
            raw.pop();
            return raw;
        }
    }
}
fn returned(io: &mut CommandIo<'_>, iteration: u64) -> Value {
    let mut notice = serde_json::to_vec(&json!({"returned":iteration})).unwrap();
    notice.push(b'\n');
    io.write_stdout(&notice).unwrap();
    // 外側の検査が実PIDの不在を測るまで、この接続は次の実行や終了へ進まない。
    let ack: Value = serde_json::from_slice(&read_line(io)).unwrap();
    assert_eq!(ack.as_object().unwrap().len(), 1);
    let absent = ack.get("direct_child_absent").unwrap().clone();
    assert!(absent.is_null() || absent == true);
    absent
}
fn record(name: &str, plan: &Value, io: &mut CommandIo<'_>) -> Value {
    let started = Instant::now();
    let time = |key: &str| started + Duration::from_millis(number(plan, key));
    let mut budget = Budget::new(
        BudgetLimits {
            items: 100,
            total_bytes: number(plan, "total"),
            file_bytes: 1,
        },
        time("budget_ms"),
    );
    budget.add_items(number(plan, "prior_items")).unwrap();
    budget.add_bytes(number(plan, "prior_bytes")).unwrap();
    let deadline = budget.deadline();
    let cancel = AtomicBool::new(plan["cancel"].as_str() == Some("before"));
    let input = raw(plan, "input_hex");
    let mut outputs = [Vec::new(), Vec::new()];
    let mut result = None;
    let mut direct_child_absent = Value::Null;
    for iteration in 0..number(plan, "repeat") {
        let argv = plan["argv_hex"].as_array().unwrap();
        let mut cmd = Command::new(OsString::from_vec(unhex(argv[0].as_str().unwrap())));
        for arg in &argv[1..] {
            cmd.arg(OsString::from_vec(unhex(arg.as_str().unwrap())));
        }
        cmd.current_dir(OsString::from_vec(raw(plan, "cwd_hex")));
        if plan["clear_env"].as_bool().unwrap() {
            cmd.env_clear();
        }
        for (key, value) in plan["env"].as_object().unwrap() {
            cmd.env(key, value.as_str().unwrap());
        }
        let mut counts = [0u64; 2];
        let out = run_child(
            &mut cmd,
            ChildInput {
                bytes: &input,
                broken_pipe: if plan["broken_pipe"] == "reject" {
                    BrokenPipePolicy::Reject
                } else {
                    BrokenPipePolicy::Close
                },
            },
            &mut budget,
            ChildDeadlines {
                execution: time("execution_ms"),
                cleanup: time("cleanup_ms"),
            },
            &cancel,
            |stream, chunk, budget| {
                let n = if stream == OutputStream::Stdout { 0 } else { 1 };
                let limit = number(
                    plan,
                    if n == 0 {
                        "stdout_limit"
                    } else {
                        "stderr_limit"
                    },
                );
                if counts[n] + chunk.len() as u64 > limit {
                    return Err(if n == 0 {
                        "stdout-limit"
                    } else {
                        "stderr-limit"
                    });
                }
                if plan["count"] == "pre"
                    && chunk.len() as u64
                        > budget.limits().total_bytes.saturating_sub(budget.bytes())
                {
                    return Err("total-limit");
                }
                if plan["count"] != "none" {
                    budget
                        .add_bytes(chunk.len() as u64)
                        .map_err(|_| "total-limit")?;
                }
                if plan["consumer"] == "refused" {
                    return Err("refused");
                }
                if plan["consumer"] == "write-failed" || plan["consumer"] == "flush-failed" {
                    struct Fails(bool);
                    impl Write for Fails {
                        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
                            if self.0 {
                                Ok(b.len())
                            } else {
                                Err(io::ErrorKind::BrokenPipe.into())
                            }
                        }
                        fn flush(&mut self) -> io::Result<()> {
                            Err(io::ErrorKind::BrokenPipe.into())
                        }
                    }
                    let mut err = Vec::new();
                    let mut sink = Fails(plan["consumer"] == "flush-failed");
                    let rc = execute_to(&mut io::empty(), &mut sink, &mut err, |io| {
                        assert!(io.write_stdout(chunk).is_err());
                        Ok(CommandOutput::stdout(b"must-not-write"))
                    });
                    assert_eq!(rc, ExitCode::RUNTIME);
                    return Err(if plan["consumer"] == "flush-failed" {
                        "flush-failed"
                    } else {
                        "write-failed"
                    });
                }
                outputs[n].extend_from_slice(chunk);
                counts[n] += chunk.len() as u64;
                if plan["cancel"] == "output" {
                    cancel.store(true, Ordering::Relaxed);
                }
                Ok(())
            },
        );
        direct_child_absent = returned(io, iteration);
        let failed = out.is_err();
        result = Some(out);
        if failed {
            break;
        }
    }
    let mut value = json!({"version":1,"case":name,"status":null,"cause":null,
        "cleanup":{"kill_errno":null,"wait_errno":null,"expired":false,"reaped":false,"pending":false},
        "output":{"stdout_hex":hex(&outputs[0]),"stderr_hex":hex(&outputs[1])},
        "usage":{"items":budget.items(),"bytes":budget.bytes()},"input":{"written":0,"closed_early":false},
        "deadline_unchanged":deadline==budget.deadline(),"direct_child_absent":direct_child_absent});
    match result.unwrap() {
        Ok(out) => {
            value["status"] = status(out.status);
            value["cleanup"]["reaped"] = json!(true);
            value["input"] =
                json!({"written":out.input_written,"closed_early":out.input_closed_early});
        }
        Err(mut failure) => {
            value["cause"] = cause(&failure.cause);
            value["status"] = failure.status.map(status).unwrap_or(Value::Null);
            value["input"] =
                json!({"written":failure.input_written,"closed_early":failure.input_closed_early});
            value["cleanup"] = json!({"kill_errno":failure.kill_error.as_ref().and_then(io::Error::raw_os_error),
                "wait_errno":failure.wait_error.as_ref().and_then(io::Error::raw_os_error),"expired":failure.cleanup_expired,
                "reaped":failure.status.is_some(),"pending":failure.pending_child.is_some()});
            // 回収不能を成功にせず、そのまま所有権を保持して外側の検査に失敗を知らせる。
            if let Some(mut child) = failure.pending_child.take() {
                let _ = child.kill();
                let _ = child.wait();
                panic!("unexpected unreaped child");
            }
        }
    }
    value
}
fn streaming() -> ExitCode {
    execute(|io| {
        let raw = read_line(io);
        let plan: Value = serde_json::from_slice(&raw).unwrap();
        let argv = plan["argv_hex"].as_array().unwrap();
        let mut cmd = Command::new(OsString::from_vec(unhex(argv[0].as_str().unwrap())));
        for arg in &argv[1..] {
            cmd.arg(OsString::from_vec(unhex(arg.as_str().unwrap())));
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut budget = Budget::new(
            BudgetLimits {
                items: 0,
                total_bytes: 100,
                file_bytes: 0,
            },
            deadline,
        );
        let input = vec![b'x'; 1_000_000];
        let mut first = true;
        let out = run_child(
            &mut cmd,
            ChildInput {
                bytes: &input,
                broken_pipe: BrokenPipePolicy::Reject,
            },
            &mut budget,
            ChildDeadlines {
                execution: deadline,
                cleanup: deadline + Duration::from_secs(1),
            },
            &AtomicBool::new(false),
            |stream, bytes, budget| {
                budget.add_bytes(bytes.len() as u64).unwrap();
                match stream {
                    OutputStream::Stdout => io.write_stdout(bytes).unwrap(),
                    OutputStream::Stderr => io.write_stderr(bytes).unwrap(),
                }
                if first {
                    assert_eq!(stream, OutputStream::Stdout);
                    assert_eq!(bytes, b"ready");
                    first = false;
                    let mut answer = [0; 3];
                    let mut offset = 0;
                    while offset < 3 {
                        let n = io.read_input(&mut answer[offset..]).unwrap();
                        assert!(n > 0);
                        offset += n;
                    }
                    assert_eq!(&answer, b"go\n");
                }
                Ok::<_, ()>(())
            },
        );
        assert_eq!(returned(io, 0), json!(true));
        let out = out.unwrap();
        assert_eq!(out.input_written, input.len());
        assert_eq!(out.status, ChildStatus::Exited(ExitCode::new(237)));
        Ok(CommandOutput {
            stdout: b"finished".to_vec(),
            stderr: Vec::new(),
            code: ExitCode::new(237),
        })
    })
}
fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--stream") {
        return streaming().into();
    }
    if args.get(1).map(String::as_str) == Some("--fixture") {
        return execute(|io| {
            let raw = read_line(io);
            let plan: Value = serde_json::from_slice(&raw).unwrap();
            let value = record(&args[2], &plan, io);
            let mut out = serde_json::to_vec(&value).unwrap();
            out.push(b'\n');
            Ok(CommandOutput::stdout(out))
        })
        .into();
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let mut child = Command::new("python3")
        .arg("-B")
        .arg(root.join("native/ci/check_c0c.py"))
        .arg("--fixture")
        .arg(std::env::current_exe().unwrap())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(180);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            assert!(status.success(), "C0c checker failed: {status}");
            return std::process::ExitCode::SUCCESS;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("C0c checker timed out");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
