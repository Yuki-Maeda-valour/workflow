//! `verify-pair`(旧版の `run_pair`): 1 回の起動で 2 回照合する。
//! 1 回目(一覧なし)→ `verified` の行と flush → 親の `go` を期限つきで待つ → 一覧つきの 2 回目 →
//! 自分の本体(控えの隣の guard のコピー)の照らし直し(U2)。
//!
//! stop・違う語・EOF・時間切れは、何も出さずに終了コード 20 で終える。旧版は `os._exit(20)`(標準出力の後始末をしない)で
//! 終えるが、その時点で標準出力のバッファは空(`verified` を flush 済み)なので、戻り値 20 で同じ結果になる。
use super::args::Parsed;
use super::error::{PyException, from_io};
use super::fsio::{read_regular, site};
use super::pyjson::PyCtx;
use super::pyops::{Cps, lit, sha_hex};
use super::runtime::PyStream;
use super::snapshot::verify;
use super::specs::{Host, absolute};
use dev_workflow_platform::environment_guard as os;
use std::io;
use std::os::fd::{AsFd, BorrowedFd};
use std::time::Instant;

/// 旧版の `select` が受ける待ち時間の上限(秒): `_PyTime_t`(64 ビットのナノ秒)に収まる最大の `2^63` ナノ秒。
const PYTIME_MAX_NANOSECONDS: f64 = 9_223_372_036_854_775_808.0;
const OVERFLOW_MESSAGE: &str = "timestamp too large to convert to C _PyTime_t";
const GO: &[u8] = b"go\n";

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn argument(parsed: &Parsed, dest: &str) -> Cps {
    parsed
        .namespace
        .value(dest)
        .expect("`argparse` が必須の引数を確かめている")
        .py_str()
}

/// 旧版の `select` が `EINTR` で戻ったとき: `SIGINT` が来ていれば `KeyboardInterrupt`、来ていなければ再試行。
fn interrupted(error: &io::Error) -> Option<PyException> {
    (error.raw_os_error() == Some(os::EINTR))
        .then(|| os::take_sigint().then(|| PyException::new("KeyboardInterrupt", "")))
        .flatten()
}

fn is_interrupt(error: &io::Error) -> bool {
    error.raw_os_error() == Some(os::EINTR)
}

/// 標準入力から `go\n` の 3 バイトを、`go_timeout`(秒)の期限つきで読む。`Ok(true)` は `go\n`、`Ok(false)` は
/// stop・違う語・EOF・時間切れ(旧版は `os._exit(20)`)。
pub(super) fn wait_for_go(input: BorrowedFd<'_>, timeout: f64) -> Result<bool, PyException> {
    let started = Instant::now();
    let mut got: Vec<u8> = Vec::new();
    while got.len() < GO.len() {
        // `remaining = deadline - time.monotonic()`
        let remaining = timeout - started.elapsed().as_secs_f64();
        if remaining <= 0.0 {
            return Ok(false);
        }
        // `select.select` の待ち時間の変換(上向きに丸めたナノ秒が `_PyTime_t` に収まらなければ `OverflowError`)。
        if (remaining * 1e9).ceil() > PYTIME_MAX_NANOSECONDS {
            return Err(PyException::new("OverflowError", OVERFLOW_MESSAGE));
        }
        // 上向きに丸めたミリ秒。Mac は、1e8 秒を超えると `EINVAL`(`select` の上限)。
        let millis = (remaining * 1000.0).ceil() as i64;
        match os::wait_readable(input, millis) {
            Ok(true) => {}
            Ok(false) => return Ok(false),
            Err(error) if is_interrupt(&error) => match interrupted(&error) {
                Some(keyboard) => return Err(keyboard),
                None => continue,
            },
            Err(error) => return Err(from_io(&error, None)),
        }
        let mut chunk = vec![0u8; GO.len() - got.len()];
        let count = match os::retry(|| os::read(input, &mut chunk)) {
            Ok(count) => count,
            Err(error) => {
                return Err(interrupted(&error).unwrap_or_else(|| from_io(&error, None)));
            }
        };
        if count == 0 {
            return Ok(false);
        }
        got.extend_from_slice(&chunk[..count]);
    }
    Ok(got == GO)
}

/// 旧版の `run_pair(a)`。`ctx.depth` は、`run_pair` のフレームの深さ。
pub fn run_pair(
    host: &dyn Host,
    parsed: &Parsed,
    stdout: &mut PyStream<'_>,
    ctx: PyCtx,
) -> Result<i64, PyException> {
    let guard_sha = argument(parsed, "guard_sha256");
    let hex = |c: &u32| (0x30..=0x39).contains(c) || (0x61..=0x66).contains(c);
    if guard_sha.len() != 64 || !guard_sha.iter().all(hex) {
        return Err(stop("guard 本体の SHA-256 が必要"));
    }
    let Some(super::args::ArgValue::Int(go_timeout)) = parsed.namespace.value("go_timeout") else {
        unreachable!("`--go-timeout` は `type=int` の必須の引数")
    };
    if go_timeout.is_negative() || go_timeout.is_zero() {
        return Err(stop("go の待ち時間は正の整数でなければならない"));
    }
    let state = argument(parsed, "state");
    let expected = argument(parsed, "expect_sha256");
    let current = argument(parsed, "current_inventory");
    verify(host, &state, &expected, None, ctx)?;
    stdout.write_text(&lit("verified\n"))?;
    stdout.flush()?;
    // `time.monotonic() + a.go_timeout`: 大きすぎる整数は `OverflowError`(`verified` の後)。
    let timeout = go_timeout.to_f64()?;
    site("pair-after-verified");
    let input = host.go_input().map_err(|error| from_io(&error, None))?;
    if !wait_for_go(input.as_fd(), timeout)? {
        return Ok(20);
    }
    site("pair-after-go");
    verify(host, &state, &expected, Some(&current), ctx)?;
    // 本体(コピーの木の外)は 2 回目の読み込み器の hash の照合を通らないので、ここで控えた hash と照らす(U2)。
    let guard = absolute(host, &state)?
        .parent()
        .join(&lit("environment-guard.py"));
    let raw = read_regular(host, &guard.to_cps(), None)?;
    if lit(&sha_hex(&raw)) != guard_sha {
        return Err(stop("guard 本体が保持値と違う"));
    }
    Ok(0)
}
