//! R4Gの専用互換処理。旧Python 3.10と旧bashの入出力・起動・診断を再現する部品。
//!
//! 製品のコマンド登録には接続しない。検査専用の入口`examples/r4g_contract.rs`だけが`#[path]`で使う。
//! 文字コードは2026-11-11 22:24の利用者決定に従い、UTF-8と`surrogateescape`に固定する。
//! 標準エラーは`backslashreplace`で書く。

// R4Bの部品のうち、意味が一致する関数だけを変更せずに使う(surrogateescape・repr・Unicode 13.0)。
#[allow(dead_code)]
#[path = "../r4b_support/bytes.rs"]
pub mod bytes;
#[allow(dead_code)]
#[path = "../r4b_support/unicode.rs"]
mod unicode;

pub mod bash;
pub mod json;
pub mod pyint;
pub mod python_io;
pub mod python_startup;
pub mod strerror;
pub mod text;
pub mod traceback;

/// 時刻(旧Pythonの`time.monotonic()`の秒)。検査専用の入口は、決めた値の並びへ差し替える。
pub trait Clock {
    fn now(&mut self) -> f64;
}

/// 本物の単調時計。最初に読んだ時点からの秒に、起点の値を足す。
pub struct MonotonicClock {
    origin: std::time::Instant,
}

impl Default for MonotonicClock {
    fn default() -> Self {
        Self {
            origin: std::time::Instant::now(),
        }
    }
}

impl Clock for MonotonicClock {
    fn now(&mut self) -> f64 {
        self.origin.elapsed().as_secs_f64()
    }
}

/// 決めた値を順に返す時計。使い切った後は最後の値を返し続ける。
pub struct ScriptedClock {
    values: Vec<f64>,
    next: usize,
}

impl ScriptedClock {
    pub fn new(values: Vec<f64>) -> Option<Self> {
        (!values.is_empty() && values.iter().all(|value| value.is_finite()))
            .then_some(Self { values, next: 0 })
    }
}

impl Clock for ScriptedClock {
    fn now(&mut self) -> f64 {
        let value = self.values[self.next.min(self.values.len() - 1)];
        self.next += 1;
        value
    }
}
