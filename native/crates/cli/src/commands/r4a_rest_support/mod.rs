//! R4A残り5本だけが使う、Python 3.10互換の小さな処理。
//!
//! このモジュールは製品コマンド表へ登録しない。環境由来の文字列は
//! UTF-8 と surrogateescape に固定する。明示的な strict / replace は
//! 呼出側が選ぶ。

pub mod args;
pub mod chars;
pub mod driver;
pub mod exception;
pub mod frames;
pub mod helper;
pub mod json;
pub mod nfc;
pub mod runtime;
pub mod startup;
pub mod stream;
pub mod text;

/// CPython 3.10 の環境 flag(`PYTHONUNBUFFERED`)。ASCII の先頭空白・符号・ゼロ列だけを 0 とする。
pub fn unbuffered_from(value: Option<&std::ffi::OsStr>) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Some(value) = value else { return false };
    let mut rest = value.as_bytes();
    if rest.is_empty() {
        return false;
    }
    while rest
        .first()
        .is_some_and(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 11 | 12))
    {
        rest = &rest[1..];
    }
    if rest.first().is_some_and(|b| matches!(b, b'+' | b'-')) {
        rest = &rest[1..];
    }
    rest.is_empty() || !rest.iter().all(|b| *b == b'0')
}

/// C の `strtol`/`strtoul` に当たる字句の読取り。前の空白・符号・10 進数字を読み、文字列の最後まで使い切ったときだけ成功。
/// 戻り値は (負か, 絶対値の飽和した値)。
fn strto_full(value: &[u8]) -> Option<(bool, u128)> {
    let mut at = 0;
    while matches!(
        value.get(at),
        Some(b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
    ) {
        at += 1;
    }
    let negative = matches!(value.get(at), Some(b'-'));
    if matches!(value.get(at), Some(b'+' | b'-')) {
        at += 1;
    }
    let first = at;
    let mut number = 0_u128;
    while let Some(byte) = value.get(at).filter(|byte| byte.is_ascii_digit()) {
        number = number
            .saturating_mul(10)
            .saturating_add(u128::from(byte - b'0'));
        at += 1;
    }
    (at > first && at == value.len()).then_some((negative, number))
}

/// `PYTHONOPTIMIZE` の最適化の水準。未設定・空は 0。数として読めない・負・int の範囲外は 1(`-O` と同じ)。
/// 2 以上(`-OO` 相当)では docstring が消え、`__doc__` が `None` になる。
pub fn optimize_level(value: Option<&std::ffi::OsStr>) -> i32 {
    use std::os::unix::ffi::OsStrExt;
    let Some(value) = value else { return 0 };
    let bytes = value.as_bytes();
    if bytes.is_empty() {
        return 0;
    }
    match strto_full(bytes) {
        Some((false, number)) if number <= i32::MAX as u128 => number as i32,
        Some((true, 0)) => 0,
        _ => 1,
    }
}

/// CPython 3.10 の起動前 `PYTHONHASHSEED` 拒否(`random` か 0〜4294967295 の整数でなければ起動に失敗する)。
/// `Current thread` の行は無く、全文を旧版に合わせる。値は出力を変えないので、有効な値は何もしない。
pub fn hash_seed_startup_error(value: Option<&std::ffi::OsStr>) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStrExt;
    let bytes = value?.as_bytes();
    let valid = bytes.is_empty()
        || bytes == b"random"
        || matches!(strto_full(bytes), Some((false, n)) if n <= 4_294_967_295)
        || matches!(strto_full(bytes), Some((true, 0)));
    (!valid).then_some(
        b"Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\nPython runtime state: preinitialized\n\n".to_vec(),
    )
}

/// CPython 3.10 の起動前 `PYTHONINTMAXSTRDIGITS` 拒否を、今回の5本だけで
/// 共通に再現する。文字コード設定の承認差とは別の固定診断である。
pub fn int_limit_startup_error(value: Option<&std::ffi::OsStr>) -> Option<Vec<u8>> {
    let (valid, _) = int_limit(value?);
    (!valid).then_some(b"Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\nPython runtime state: preinitialized\n\n".to_vec())
}

/// `PYTHONINTMAXSTRDIGITS` から整数の文字列変換の上限を決める(未設定・空は 4300・0 は無制限)。
pub fn int_max_digits(value: Option<&std::ffi::OsStr>) -> usize {
    match value {
        None => 4300,
        Some(value) => match int_limit(value) {
            (true, Some(number)) => number as usize,
            _ => 4300,
        },
    }
}

/// (有効か, 設定された値)。
fn int_limit(value: &std::ffi::OsStr) -> (bool, Option<i128>) {
    use std::os::unix::ffi::OsStrExt;
    let value = value.as_bytes();
    let mut at = 0;
    while matches!(
        value.get(at),
        Some(b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
    ) {
        at += 1;
    }
    let negative = matches!(value.get(at), Some(b'-'));
    if matches!(value.get(at), Some(b'+' | b'-')) {
        at += 1;
    }
    let first = at;
    let mut number = 0_i128;
    while let Some(byte) = value.get(at).filter(|byte| byte.is_ascii_digit()) {
        number = number
            .saturating_mul(10)
            .saturating_add(i128::from(byte - b'0'));
        at += 1;
    }
    if negative {
        number = -number;
    }
    let valid = value.is_empty()
        || (at > first
            && at == value.len()
            && number >= 0
            && number <= i128::from(i32::MAX)
            && (number == 0 || number >= 640));
    (valid, (valid && !value.is_empty()).then_some(number))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;
    #[test]
    fn optimize_level_and_hash_seed_follow_cpython() {
        let level = |v: &str| optimize_level(Some(OsStr::new(v)));
        assert_eq!(optimize_level(None), 0);
        for (text, want) in [
            ("", 0),
            ("0", 0),
            ("1", 1),
            ("2", 2),
            ("3", 3),
            ("100", 100),
            ("2147483647", 2147483647),
            ("2147483648", 1),
            ("99999999999", 1),
            ("-5", 1),
            ("-1", 1),
            ("x", 1),
            (" 2", 2),
            ("2 ", 1),
            ("+2", 2),
            ("1.5", 1),
            ("0x2", 1),
            (" ", 1),
            ("\t3", 3),
            ("00", 0),
            ("02", 2),
        ] {
            assert_eq!(level(text), want, "{text:?}");
        }
        for ok in [
            "random",
            "0",
            "-0",
            "+0",
            " 4",
            "4294967295",
            "",
            "00",
            "+1",
        ] {
            assert!(
                hash_seed_startup_error(Some(OsStr::new(ok))).is_none(),
                "{ok:?}"
            );
        }
        for bad in [
            "4294967296",
            "-1",
            "x",
            "4 ",
            "0x1",
            "1_0",
            "randomx",
            "Random",
        ] {
            assert!(
                hash_seed_startup_error(Some(OsStr::new(bad))).is_some(),
                "{bad:?}"
            );
        }
        assert!(hash_seed_startup_error(None).is_none());
    }

    #[test]
    fn python_integer_limit_lexical_forms() {
        for value in [
            "",
            " 640",
            "+640",
            "0640",
            "-0",
            "\t640",
            "2147483647",
            "+0",
        ] {
            assert!(
                int_limit_startup_error(Some(OsStr::new(value))).is_none(),
                "{value:?}"
            );
        }
        for value in ["639", "640 ", "0x280", "2147483648", "0_640", "640junk"] {
            assert!(
                int_limit_startup_error(Some(OsStr::new(value))).is_some(),
                "{value:?}"
            );
        }
    }
}
