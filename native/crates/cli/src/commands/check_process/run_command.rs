//! 旧版 `check-process.py` の親(`StopSignals`・`read_result`・`linux_command`・`stop_group`・`posix_command`・
//! `measure_stream`・`run_command`。`:43-258`)の写し。R4V(review-guard)が呼ぶ。
//! - Linux の方式は、監督の pid ではなく pidfd で信号を送る。pidfd が無いときに POSIX の方式へ切り替えない。
//! - POSIX の方式は、最後の KILL まで anchor を回収しない。EPERM を不在として扱わない。読み口は群の停止を
//!   確かめるまで閉じない(#289)。
//! - OS の操作と時計は [`Ops`] で受ける(製品は本物、差し替えは検査の中だけ)。
//! - `run_command` は、長く生きるスレッド(ふつうは main のスレッド)から呼ぶ。PDEATHSIG は、監督を起動した
//!   スレッドが終わると届くため。
//! - 記録と通知の `json.loads` の入れ子の上限は、旧版の親の Python のフレームの深さで決まる
//!   (`sys.getrecursionlimit()` から、`<module>` から `raw_decode` までのフレームの数と C の scanner の 1 を引く)。
//!   呼出元は、旧版で `run_command` を呼ぶまでのフレームの数(`<module>` を含み、`run_command` を含まない)を
//!   [`RunRequest::caller_frames`] に渡す。既定は [`CALLER_FRAMES`](検査用の親 `r4q_legacy_parent.py` の
//!   `<module>` → `main` の 2)。R4V は、旧版 `review-guard.py` の `<module>` から `run_command` を呼ぶ関数までの
//!   フレームを、旧版のソースで数えて渡す(呼出しの経路ごとに違えば、経路ごとに渡す)。再帰の上限を変える
//!   呼出元は [`RunRequest::recursion_limit`] も渡す(既定 1000)。

use super::loop_supervisor::ops::{
    Class, Handler, MonitorLaunch, OpenFlags, Ops, Platform, PyErr, Real, Saved, SelectFailure,
    WaitFailure,
};
use super::loop_supervisor::r4q_python::exception::{self, Filename};
use super::loop_supervisor::r4q_python::json;
use super::loop_supervisor::r4q_python::number::{Overflow, PyNumber};
use super::loop_supervisor::r4q_python::value::{self, PyValue};
use super::loop_supervisor::supervise::{SIGHUP, SIGINT, SIGKILL, SIGTERM};
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::Path;

const ESRCH: i32 = 3;
const EPERM: i32 = 1;
/// 旧版が包む文の頭。
pub const WRAP_PREFIX: &str = "品質プロセスの回収を確認できない: ";
/// Python の再帰の上限(`sys.getrecursionlimit()` の既定)。
pub const PYTHON_RECURSION_LIMIT: usize = 1000;
/// `run_command` を呼ぶ旧版の親の Python のフレームの数の既定(検査用の親 `r4q_legacy_parent.py` は、
/// `<module>` から呼んだ `main` が `run_command` を呼ぶ)。
pub const CALLER_FRAMES: usize = 2;

/// json.loads の入れ子の上限。`frames` は、旧版の親の `<module>` から `json.decoder.raw_decode` までの
/// Python のフレームの数。C の scanner の呼出しで 1 つ増える。
fn recursion_room(limit: usize, frames: usize) -> usize {
    limit.saturating_sub(frames + 1)
}

/// `read_result` の json.loads の入れ子の上限(呼出元の `caller_frames` と、`run_command`・`linux_command`・
/// `read_result`・`loads`・`decode`・`raw_decode` の 6 フレームと C の scanner)。既定の値で 991。
pub fn read_result_room(limit: usize, caller_frames: usize) -> usize {
    recursion_room(limit, caller_frames + 6)
}

/// anchor の通知の json.loads の入れ子の上限(呼出元の `caller_frames` と、`run_command`・`posix_command`・
/// `loads`・`decode`・`raw_decode` の 5 フレームと C の scanner)。既定の値で 992。
pub fn notice_room(limit: usize, caller_frames: usize) -> usize {
    recursion_room(limit, caller_frames + 5)
}

/// 監督の方式。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    Linux,
    Posix,
}

/// 監督の起動の仕方: プログラムと前に付ける引数(例 `python3` と `["-B", "<…>/check-process.py"]`、
/// 検査用の実行物と `["--r4q-check-process"]`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Monitor {
    pub program: OsString,
    pub prefix: Vec<OsString>,
}

/// `run_command` の引数。
pub struct RunRequest<'a> {
    pub argv: &'a [OsString],
    /// None は起動した側の作業フォルダ(旧版の Popen の cwd=None)。
    pub cwd: Option<&'a Path>,
    /// 渡した順。None は起動した側の環境。鍵の重複は辞書と同じ(後の値が最初の位置)。
    pub env: Option<&'a [(OsString, OsString)]>,
    pub timeout: PyNumber,
    pub directory: &'a Path,
    /// 既定 `Float(0.2)`。
    pub grace: PyNumber,
    /// None は OS で決める。
    pub method: Option<Method>,
    /// 既定 0。
    pub keep: usize,
    pub monitor: &'a Monitor,
    /// 既定 4300。
    pub int_max_str_digits: usize,
    /// 旧版で `run_command` を呼ぶまでの Python のフレームの数(`<module>` を含む)。既定 [`CALLER_FRAMES`]。
    pub caller_frames: usize,
    /// `sys.getrecursionlimit()`。既定 [`PYTHON_RECURSION_LIMIT`]。
    pub recursion_limit: usize,
}

/// `run_command` の誤り。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunError {
    /// `RecoveryError` の str。
    Recovery(String),
    /// `Interrupted` の str。
    Interrupted(String),
    /// 包まずに外へ出る例外の型の名前と str。
    Unwrapped { kind: String, message: String },
}

/// 停止の信号の記録(旧版の `StopSignals`)。入るときに TERM・INT・HUP へ「最初の 1 つを記録する」処理を置き、
/// 前の処理を控える。drop で前の処理へ戻す。
pub struct StopSignals {
    ops: Real,
    previous: Vec<(i32, Saved)>,
}

impl StopSignals {
    /// `__enter__`。
    pub fn enter() -> Self {
        let mut ops = Real::new();
        let previous = enter_stop(&mut ops);
        Self { ops, previous }
    }

    /// 受けた最初の番号(無ければ 0)。
    pub fn number(&self) -> i32 {
        self.ops.first_signal()
    }

    /// `check()`。
    pub fn check(&self) -> Result<(), RunError> {
        match self.number() {
            0 => Ok(()),
            number => Err(RunError::Interrupted(interrupted_text(number))),
        }
    }
}

impl Drop for StopSignals {
    fn drop(&mut self) {
        exit_stop(&mut self.ops, &self.previous);
    }
}

/// `StopSignals.__init__` と `__enter__`(操作の単位で行う)。
pub fn enter_stop<O: Ops>(ops: &mut O) -> Vec<(i32, Saved)> {
    ops.clear_first_signal();
    [SIGTERM, SIGINT, SIGHUP]
        .into_iter()
        .map(|number| (number, ops.set_handler(number, Handler::Receive)))
        .collect()
}

/// `StopSignals.__exit__`(控えた順に戻す)。
pub fn exit_stop<O: Ops>(ops: &mut O, previous: &[(i32, Saved)]) {
    for &(number, saved) in previous {
        ops.set_handler(number, Handler::Previous(saved));
    }
}

fn interrupted_text(number: i32) -> String {
    format!("終了通知 {number} を受けた")
}

/// `stop.check()`(操作の単位の記録を読む)。
fn stop_check<O: Ops>(ops: &O) -> Result<(), PyErr> {
    match ops.first_signal() {
        0 => Ok(()),
        number => Err(PyErr::new(Class::Interrupted, interrupted_text(number))),
    }
}

/// 親の中の値(引数を Python の値の形にしたもの)。
pub struct Request {
    pub argv: Vec<Vec<u8>>,
    pub cwd: Option<Vec<u8>>,
    /// 辞書にした環境(重なる鍵は後の値が最初の位置)。
    pub env: Option<Vec<(Vec<u8>, Vec<u8>)>>,
    pub timeout: PyNumber,
    pub grace: PyNumber,
    pub directory: Vec<u8>,
    pub method: Option<Method>,
    pub keep: usize,
    pub monitor: Vec<Vec<u8>>,
    pub int_max_str_digits: usize,
    pub caller_frames: usize,
    pub recursion_limit: usize,
}

impl Request {
    pub fn from_request(request: &RunRequest<'_>) -> Self {
        let bytes = |value: &OsString| value.as_bytes().to_vec();
        let env = request.env.map(|env| {
            let mut pairs: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
            for (key, value) in env {
                match pairs
                    .iter_mut()
                    .find(|(existing, _)| existing.as_slice() == key.as_bytes())
                {
                    Some(entry) => entry.1 = bytes(value),
                    None => pairs.push((bytes(key), bytes(value))),
                }
            }
            pairs
        });
        let mut monitor = vec![bytes(&request.monitor.program)];
        monitor.extend(request.monitor.prefix.iter().map(bytes));
        Self {
            argv: request.argv.iter().map(bytes).collect(),
            cwd: request.cwd.map(|cwd| cwd.as_os_str().as_bytes().to_vec()),
            env,
            timeout: request.timeout.clone(),
            grace: request.grace.clone(),
            directory: request.directory.as_os_str().as_bytes().to_vec(),
            method: request.method,
            keep: request.keep,
            monitor,
            int_max_str_digits: request.int_max_str_digits,
            caller_frames: request.caller_frames,
            recursion_limit: request.recursion_limit,
        }
    }
}

/// 旧版の `run_command(argv, cwd, env, timeout, directory, stop, grace=…, method=…, keep=…)`。
/// `stop` は、呼出元が入った [`StopSignals`]。
pub fn run_command(
    request: &RunRequest<'_>,
    stop: &StopSignals,
) -> Result<Vec<(PyValue, PyValue)>, RunError> {
    let _ = stop;
    let mut ops = Real::new();
    run_with(&mut ops, &Request::from_request(request))
}

/// `run_command` の本体(操作の単位で行う)。
pub fn run_with<O: Ops>(
    ops: &mut O,
    request: &Request,
) -> Result<Vec<(PyValue, PyValue)>, RunError> {
    stop_check(ops).map_err(finish_error)?;
    // 期限の検査は try の外: 大きすぎる int の OverflowError は包まずに外へ出る。
    let finite = |number: &PyNumber| {
        number.is_finite().map_err(|_| RunError::Unwrapped {
            kind: "OverflowError".to_owned(),
            message: Overflow::MESSAGE.to_owned(),
        })
    };
    use std::cmp::Ordering::{Greater, Less};
    let invalid = || RunError::Recovery("監督の期限が不正".to_owned());
    if !finite(&request.timeout)? || request.timeout.compare_zero() != Some(Greater) {
        return Err(invalid());
    }
    if !finite(&request.grace)? || request.grace.compare_zero() == Some(Less) {
        return Err(invalid());
    }
    let method = match request.method {
        Some(method) => method,
        None => match ops.platform() {
            Platform::Linux => Method::Linux,
            Platform::Darwin => Method::Posix,
        },
    };
    if method == Method::Linux && !ops.has_pidfd() {
        return Err(RunError::Recovery(
            "Linux の監督に必要な pidfd がない".to_owned(),
        ));
    }
    match measured(ops, request, method) {
        Ok(result) => Ok(result),
        Err(error) => Err(match error.class {
            Class::Os(_) | Class::Value(_) | Class::Overflow | Class::Subprocess(_) => {
                RunError::Recovery(format!("{WRAP_PREFIX}{}", error.message))
            }
            _ => finish_error(error),
        }),
    }
}

/// RecoveryError と Interrupted はそのまま、ほかは包まない例外。
fn finish_error(error: PyErr) -> RunError {
    match error.class {
        Class::Recovery => RunError::Recovery(error.message),
        Class::Interrupted => RunError::Interrupted(error.message),
        _ => RunError::Unwrapped {
            kind: error.type_name().to_owned(),
            message: error.message,
        },
    }
}

/// `with (directory / 'stdout').open('w+b') as stdout, (directory / 'stderr').open('w+b') as stderr:` の中。
fn measured<O: Ops>(
    ops: &mut O,
    request: &Request,
    method: Method,
) -> Result<Vec<(PyValue, PyValue)>, PyErr> {
    let open = |ops: &mut O, name: &[u8]| {
        let path = exception::path_child(&request.directory, name);
        ops.open_output(&path)
            .map_err(|failure| PyErr::os(&failure, Some(Filename::from_raw(&path))))
    };
    let stdout = open(ops, b"stdout")?;
    let stderr = match open(ops, b"stderr") {
        Ok(fd) => fd,
        Err(error) => {
            return match ops.close_output(stdout) {
                Ok(()) => Err(error),
                Err(failure) => Err(PyErr::os(&failure, None)),
            };
        }
    };
    let inner = (|| -> Result<Vec<(PyValue, PyValue)>, PyErr> {
        let launched = match method {
            Method::Linux => linux_command(ops, request, stdout, stderr),
            Method::Posix => posix_command(ops, request, stdout, stderr),
        };
        let mut result = match launched {
            Ok(result) => result,
            Err(error) if error.class == Class::Launch => vec![
                (PyValue::str("clean"), PyValue::Bool(true)),
                (PyValue::str("returncode"), PyValue::int(127)),
                (PyValue::str("timed_out"), PyValue::Bool(false)),
                (PyValue::str("signal"), PyValue::int(0)),
                (PyValue::str("recovery_method"), PyValue::str("not-started")),
                (
                    PyValue::str("recovery_scope"),
                    PyValue::str("no-command-started"),
                ),
            ],
            Err(error) => return Err(error),
        };
        stop_check(ops)?;
        for (name, fd) in [("stdout", stdout), ("stderr", stderr)] {
            let (digest, size, tail) = measure_stream(ops, fd, request.keep)?;
            value::dict_set(
                &mut result,
                PyValue::str(&format!("{name}_sha256")),
                PyValue::str(&digest),
            );
            if request.keep > 0 {
                value::dict_set(
                    &mut result,
                    PyValue::str(&format!("{name}_bytes")),
                    PyValue::int(size),
                );
                value::dict_set(
                    &mut result,
                    PyValue::str(&format!("{name}_tail")),
                    PyValue::Bytes(tail),
                );
            }
        }
        Ok(result)
    })();
    // with の終わり: stderr、stdout の順に閉じる。閉じる誤りは前の誤りを置き換える。
    let mut outcome = inner;
    for fd in [stderr, stdout] {
        if let Err(failure) = ops.close_output(fd) {
            outcome = Err(PyErr::os(&failure, None));
        }
    }
    outcome
}

/// `measure_stream(stream, keep)`。計った時点の長さだけを読む。
pub fn measure_stream<O: Ops>(
    ops: &mut O,
    fd: i32,
    keep: usize,
) -> Result<(String, i64, Vec<u8>), PyErr> {
    ops.rewind_output(fd)
        .map_err(|failure| PyErr::os(&failure, None))?;
    let mut digest = Sha256::new();
    let size = ops
        .fstat_output(fd)
        .map_err(|failure| PyErr::os(&failure, None))?;
    let mut remaining = size;
    let mut tail: Vec<u8> = Vec::new();
    while remaining > 0 {
        let want = remaining.min(1024 * 1024) as usize;
        let part = ops
            .read_output(fd, want)
            .map_err(|failure| PyErr::os(&failure, None))?;
        if part.is_empty() {
            return Err(PyErr::recovery("停止後の品質出力が短くなった"));
        }
        digest.update(&part);
        if keep > 0 {
            tail.extend_from_slice(&part);
            if tail.len() > keep {
                tail.drain(..tail.len() - keep);
            }
        }
        remaining -= part.len() as i64;
    }
    let hex: String = digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok((hex, size, tail))
}

fn str_of(raw: &[u8]) -> Vec<u32> {
    super::loop_supervisor::r4q_python::code_points(raw)
}

/// `TimeoutExpired(cmd, timeout)` の文の argv(旧版の `sys.executable` を `python3`、旧スクリプトを
/// `check-process.py` にした形。2026-11-11 17:58・21:35 の決定)。
fn fixed_argv(rest: &[Vec<u8>]) -> Vec<Vec<u32>> {
    let mut argv: Vec<Vec<u32>> = ["python3", "-B", "check-process.py"]
        .iter()
        .map(|text| text.chars().map(u32::from).collect())
        .collect();
    argv.extend(rest.iter().map(|arg| str_of(arg)));
    argv
}

fn timeout_error(rest: &[Vec<u8>], timeout: &PyNumber) -> PyErr {
    PyErr::new(
        Class::Subprocess("TimeoutExpired"),
        exception::timeout_expired_message(&fixed_argv(rest), timeout),
    )
}

/// `process.wait(timeout=…)`。
fn wait_for<O: Ops>(
    ops: &mut O,
    pid: i32,
    timeout: &PyNumber,
    rest: &[Vec<u8>],
) -> Result<i32, PyErr> {
    let seconds = timeout
        .to_f64()
        .map_err(|_| PyErr::new(Class::Overflow, Overflow::MESSAGE))?;
    match ops.wait_monitor(pid, Some(seconds)) {
        Ok(code) => Ok(code),
        Err(WaitFailure::Timeout) => Err(timeout_error(rest, timeout)),
        Err(WaitFailure::Os(failure)) => Err(PyErr::os(&failure, None)),
    }
}

/// `launch_monitor(...)`(OSError は LaunchError)。
fn launch<O: Ops>(ops: &mut O, launch: &MonitorLaunch) -> Result<i32, PyErr> {
    match ops.spawn_monitor(launch) {
        Ok(pid) => Ok(pid),
        Err(error) if error.errno().is_some() => Err(PyErr::new(Class::Launch, "")),
        Err(error) => Err(error),
    }
}

fn number_arg(number: &PyNumber) -> Vec<u8> {
    number.to_str().into_bytes()
}

fn seven() -> PyNumber {
    PyNumber::Int("7".to_owned())
}

/// `read_result(path, nonce)`。
pub fn read_result<O: Ops>(
    ops: &mut O,
    path: &[u8],
    nonce: &str,
    max_str_digits: usize,
    room: usize,
) -> Result<Vec<(PyValue, PyValue)>, PyErr> {
    let fd = ops
        .open(path, OpenFlags::Read)
        .map_err(|failure| PyErr::os(&failure, Some(Filename::from_raw(path))))?;
    let inner = (|| -> Result<PyValue, PyErr> {
        let info = ops.fstat(fd).map_err(|failure| PyErr::os(&failure, None))?;
        if !info.regular || info.size > 4096 {
            return Err(PyErr::recovery("回収結果の型または大きさが不正"));
        }
        let raw = ops
            .read(fd, 4097)
            .map_err(|failure| PyErr::os(&failure, None))?;
        if raw.len() > 4096 {
            return Err(PyErr::recovery("回収結果が大きすぎる"));
        }
        json::loads(&raw, max_str_digits, room).map_err(json_error)
    })();
    let value = match ops.close(fd) {
        Ok(()) => inner?,
        Err(failure) => return Err(PyErr::os(&failure, None)),
    };
    let PyValue::Dict(pairs) = value else {
        return Err(PyErr::recovery("回収結果の照合に失敗"));
    };
    // 旧版 `check-process.py:77-79` の条件(副作用が無いので、順は結果を変えない):
    // nonce が等しい str・clean is True・returncode と signal の型が int(bool は別の型)・
    // timed_out の型が bool・signal が 0・TERM・INT・HUP のどれか。
    let get = |name: &str| value::dict_get(&pairs, &PyValue::str(name)).cloned();
    let nonce_ok = get("nonce")
        .is_some_and(|found| found.py_eq(&PyValue::str(nonce)) && matches!(found, PyValue::Str(_)));
    let clean_ok = matches!(get("clean"), Some(PyValue::Bool(true)));
    let returncode_ok = matches!(get("returncode"), Some(PyValue::Int(_)));
    let timed_out_ok = matches!(get("timed_out"), Some(PyValue::Bool(_)));
    let signal_ok = matches!(get("signal"), Some(PyValue::Int(ref digits)) if ["0", "15", "2", "1"].contains(&digits.as_str()));
    if !(nonce_ok && clean_ok && returncode_ok && timed_out_ok && signal_ok) {
        return Err(PyErr::recovery("回収結果の照合に失敗"));
    }
    Ok(pairs)
}

fn json_error(error: json::JsonError) -> PyErr {
    if error.is_value_error() {
        let name = match error.type_name {
            "JSONDecodeError" => "JSONDecodeError",
            "UnicodeDecodeError" => "UnicodeDecodeError",
            _ => "ValueError",
        };
        PyErr::new(Class::Value(name), error.message)
    } else {
        PyErr::new(Class::Runtime("RecursionError"), error.message)
    }
}

/// `linux_command(...)`。
fn linux_command<O: Ops>(
    ops: &mut O,
    request: &Request,
    stdout: i32,
    stderr: i32,
) -> Result<Vec<(PyValue, PyValue)>, PyErr> {
    let result_path = exception::path_child(&request.directory, b"recovery.json");
    let nonce = ops.token_hex()?;
    let mut rest: Vec<Vec<u8>> = vec![
        b"--linux".to_vec(),
        b"--result".to_vec(),
        result_path.clone(),
        b"--nonce".to_vec(),
        nonce.clone().into_bytes(),
        b"--timeout".to_vec(),
        number_arg(&request.timeout),
        b"--grace".to_vec(),
        number_arg(&request.grace),
        b"--".to_vec(),
    ];
    rest.extend(request.argv.iter().cloned());
    let mut argv = request.monitor.clone();
    argv.extend(rest.iter().cloned());
    let pid = launch(
        ops,
        &MonitorLaunch {
            argv,
            cwd: request.cwd.clone(),
            env: request.env.clone(),
            stdout,
            stderr,
            pass_fds: Vec::new(),
            new_session: false,
        },
    )?;
    let total = request
        .timeout
        .add(&request.grace)
        .and_then(|sum| sum.add(&seven()))
        .map_err(|_| PyErr::new(Class::Overflow, Overflow::MESSAGE))?;
    let fd = match ops.pidfd_open(pid) {
        Ok(fd) => fd,
        Err(_) => {
            wait_for(ops, pid, &total, &rest)?;
            return Err(PyErr::recovery("監督の参照を保持できない"));
        }
    };
    let as_float = |number: &PyNumber| number.to_f64().unwrap_or(f64::INFINITY);
    let inner = (|| -> Result<Vec<(PyValue, PyValue)>, PyErr> {
        let mut deadline =
            ops.monotonic() + as_float(&request.timeout) + as_float(&request.grace) + 7.0;
        let mut sent = false;
        let returncode = loop {
            if let Some(code) = ops.poll_monitor(pid) {
                break code;
            }
            let stop = ops.first_signal();
            if stop != 0 && !sent {
                ops.pidfd_send_signal(fd, stop)
                    .map_err(|failure| PyErr::os(&failure, None))?;
                sent = true;
                let candidate = ops.monotonic() + as_float(&request.grace) + 7.0;
                if candidate < deadline {
                    deadline = candidate;
                }
            }
            if ops.monotonic() >= deadline {
                ops.pidfd_send_signal(fd, SIGKILL)
                    .map_err(|failure| PyErr::os(&failure, None))?;
                wait_for(ops, pid, &PyNumber::Int("2".to_owned()), &rest)?;
                return Err(PyErr::recovery("監督の終了を期限内に確認できない"));
            }
            ops.sleep(0.02)?;
        };
        if returncode != 0 {
            return Err(PyErr::recovery("監督が正常に終了しなかった"));
        }
        let mut result = read_result(
            ops,
            &result_path,
            &nonce,
            request.int_max_str_digits,
            read_result_room(request.recursion_limit, request.caller_frames),
        )?;
        let signal = match value::dict_get(&result, &PyValue::str("signal")) {
            Some(PyValue::Int(digits)) => digits.clone(),
            _ => "0".to_owned(),
        };
        let stop = ops.first_signal();
        if signal != "0" || stop != 0 {
            let number = if stop != 0 { stop.to_string() } else { signal };
            return Err(PyErr::new(
                Class::Interrupted,
                format!("終了通知 {number} を受けた"),
            ));
        }
        value::dict_set(
            &mut result,
            PyValue::str("recovery_method"),
            PyValue::str("linux-subreaper"),
        );
        value::dict_set(
            &mut result,
            PyValue::str("recovery_scope"),
            PyValue::str("owned-descendants"),
        );
        Ok(result)
    })();
    match ops.close(fd) {
        Ok(()) => inner,
        Err(failure) => Err(PyErr::os(&failure, None)),
    }
}

/// `group_exists(pgid)`。
fn group_exists<O: Ops>(ops: &mut O, pgid: i32) -> Result<bool, PyErr> {
    match ops.killpg(pgid, 0) {
        Ok(()) => Ok(true),
        Err(failure) if failure.errno == ESRCH => Ok(false),
        Err(failure) => Err(PyErr::os(&failure, None)),
    }
}

/// `stop_group(process, grace)`。最後の信号まで anchor を回収しない。回収の後は signal 0 だけ。
pub fn stop_group<O: Ops>(
    ops: &mut O,
    pid: i32,
    grace: &PyNumber,
    rest: &[Vec<u8>],
) -> Result<(), PyErr> {
    match ops.killpg(pid, SIGTERM) {
        Ok(()) => {}
        Err(failure) if failure.errno == ESRCH => {}
        Err(failure) => return Err(PyErr::os(&failure, None)),
    }
    let seconds = grace
        .to_f64()
        .map_err(|_| PyErr::new(Class::Overflow, Overflow::MESSAGE))?;
    ops.sleep(seconds)?;
    match ops.killpg(pid, SIGKILL) {
        Ok(()) => {}
        Err(failure) if failure.errno == ESRCH => {}
        Err(failure) => return Err(PyErr::os(&failure, None)),
    }
    wait_for(ops, pid, &PyNumber::Int("2".to_owned()), rest)?;
    let deadline = ops.monotonic() + 5.0;
    loop {
        match group_exists(ops, pid) {
            Ok(false) => return Ok(()),
            Ok(true) => {}
            // macOS では回収の直後の signal 0 が一時的に EPERM になる。不在とは扱わない。
            Err(error) if error.errno() == Some(EPERM) => {}
            Err(error) => return Err(error),
        }
        if ops.monotonic() >= deadline {
            return Err(PyErr::recovery("プロセスグループの不在を確認できない"));
        }
        ops.sleep(0.02)?;
    }
}

/// `posix_command(...)`。
fn posix_command<O: Ops>(
    ops: &mut O,
    request: &Request,
    stdout: i32,
    stderr: i32,
) -> Result<Vec<(PyValue, PyValue)>, PyErr> {
    let (read_fd, write_fd) = ops.pipe().map_err(|failure| PyErr::os(&failure, None))?;
    let nonce = ops.token_hex()?;
    let mut rest: Vec<Vec<u8>> = vec![
        b"--anchor-fd".to_vec(),
        write_fd.to_string().into_bytes(),
        b"--nonce".to_vec(),
        nonce.clone().into_bytes(),
        b"--".to_vec(),
    ];
    rest.extend(request.argv.iter().cloned());
    let mut argv = request.monitor.clone();
    argv.extend(rest.iter().cloned());
    let launched = launch(
        ops,
        &MonitorLaunch {
            argv,
            cwd: request.cwd.clone(),
            env: request.env.clone(),
            stdout,
            stderr,
            pass_fds: vec![write_fd],
            new_session: true,
        },
    );
    // except BaseException: os.close(read_fd); raise / finally: os.close(write_fd)
    let launched = match launched {
        Ok(pid) => Ok(pid),
        Err(error) => match ops.close(read_fd) {
            Ok(()) => Err(error),
            Err(failure) => Err(PyErr::os(&failure, None)),
        },
    };
    let launched = match ops.close(write_fd) {
        Ok(()) => launched,
        Err(failure) => Err(PyErr::os(&failure, None)),
    };
    let pid = launched?;
    let mut raw: Vec<u8> = Vec::new();
    let mut result: Option<Vec<(PyValue, PyValue)>> = None;
    let mut timed_out = false;
    let mut pending: Result<(), PyErr> = Ok(());
    let body = (|| -> Result<(), PyErr> {
        let deadline = ops.monotonic() + request.timeout.to_f64().unwrap_or(f64::INFINITY);
        while ops.first_signal() == 0 {
            let ready = select(ops, read_fd, 0.02)?;
            if ready {
                let chunk = ops
                    .read(read_fd, 4097)
                    .map_err(|failure| PyErr::os(&failure, None))?;
                if chunk.is_empty() {
                    return Err(PyErr::recovery("anchor が終了通知の前に失われた"));
                }
                raw.extend_from_slice(&chunk);
                if raw.len() > 4096 {
                    return Err(PyErr::recovery("anchor の通知が大きすぎる"));
                }
                if raw.contains(&b'\n') {
                    let value = json::loads(
                        &raw,
                        request.int_max_str_digits,
                        notice_room(request.recursion_limit, request.caller_frames),
                    )
                    .map_err(json_error)?;
                    let valid = match &value {
                        PyValue::Dict(pairs) => {
                            let nonce_ok = value::dict_get(pairs, &PyValue::str("nonce"))
                                .is_some_and(|found| {
                                    matches!(found, PyValue::Str(_))
                                        && found.py_eq(&PyValue::str(&nonce))
                                });
                            let code_ok = matches!(
                                value::dict_get(pairs, &PyValue::str("returncode")),
                                Some(PyValue::Int(_))
                            );
                            nonce_ok && code_ok
                        }
                        _ => false,
                    };
                    if !valid {
                        return Err(PyErr::recovery("anchor の通知が不正"));
                    }
                    if let PyValue::Dict(pairs) = value {
                        result = Some(pairs);
                    }
                    break;
                }
            }
            if ops.monotonic() >= deadline {
                timed_out = true;
                break;
            }
        }
        // コマンド終了の通知後も anchor は生存する。EOF は監督の異常終了。
        if select(ops, read_fd, 0.0)? {
            let byte = ops
                .read(read_fd, 1)
                .map_err(|failure| PyErr::os(&failure, None))?;
            if byte.is_empty() {
                return Err(PyErr::recovery("anchor が回収前に失われた"));
            }
        }
        Ok(())
    })();
    let mut problem: Option<PyErr> = None;
    match body {
        Ok(()) => {}
        Err(error)
            if matches!(
                error.class,
                Class::Os(_) | Class::Value(_) | Class::Recovery | Class::Interrupted
            ) =>
        {
            problem = Some(error);
        }
        Err(error) => pending = Err(error),
    }
    // finally: stop_group、その後に(内側の finally で)読み口を閉じる。どちらの誤りも前の誤りを置き換える。
    if let Err(error) = stop_group(ops, pid, &request.grace, &rest) {
        pending = Err(error);
    }
    if let Err(failure) = ops.close(read_fd) {
        pending = Err(PyErr::os(&failure, None));
    }
    pending?;
    if let Some(problem) = problem {
        return Err(PyErr::recovery(problem.message));
    }
    stop_check(ops)?;
    let returncode = result
        .as_ref()
        .and_then(|pairs| value::dict_get(pairs, &PyValue::str("returncode")).cloned())
        .unwrap_or_else(|| PyValue::int(-9));
    Ok(vec![
        (PyValue::str("clean"), PyValue::Bool(true)),
        (PyValue::str("returncode"), returncode),
        (PyValue::str("timed_out"), PyValue::Bool(timed_out)),
        (PyValue::str("signal"), PyValue::int(0)),
        (
            PyValue::str("recovery_method"),
            PyValue::str("posix-process-group"),
        ),
        (
            PyValue::str("recovery_scope"),
            PyValue::str("original-process-group-only"),
        ),
    ])
}

/// `select.select([fd], [], [], timeout)`。
fn select<O: Ops>(ops: &mut O, fd: i32, timeout: f64) -> Result<bool, PyErr> {
    match ops.select(fd, timeout) {
        Ok(ready) => Ok(ready),
        Err(SelectFailure::OutOfRange) => {
            Err(PyErr::value("filedescriptor out of range in select()"))
        }
        Err(SelectFailure::Os(failure)) => Err(PyErr::os(&failure, None)),
    }
}

/// 検査と R4V のための、Python の値の形の OsString(surrogateescape)。
pub fn os_string(raw: &[u8]) -> OsString {
    OsString::from_vec(raw.to_vec())
}
