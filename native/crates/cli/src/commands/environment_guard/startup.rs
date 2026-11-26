//! 旧 Python が起動の時点で止まる経路(終了コード 1・標準エラーへ `Fatal Python error`)。
//!
//! 順序は旧 Python 3.10 の起動の順: `PYTHONHASHSEED` の誤り → `PYTHONINTMAXSTRDIGITS` の誤り(`runtime.rs`)→
//! 標準入力がディレクトリ → 標準出力がディレクトリ → 標準エラーがディレクトリ(診断を書けないので何も出さない)。
//! 診断の `Current thread 0x…` の値は、実行ごとに変わるので、形式(`0x` と 16 桁の 16 進)だけを旧版に合わせる。

/// 不正な `PYTHONHASHSEED` の出力(標準エラーへ)。終了コードは 1。
pub const HASH_SEED_FATAL: &str = "Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\nPython runtime state: preinitialized\n\n";

/// `PYTHONHASHSEED` が旧版の起動を止めない値か。未設定・空・`random` と、`strtoul(…, 10)` が末尾まで読める
/// `0` から `4294967295` までの値(先頭の空白・符号を許す。`-` は符号なしの負数の折り返しで比べる)。
pub fn hash_seed_is_valid(value: Option<&[u8]>) -> bool {
    let Some(text) = value else { return true };
    if text.is_empty() || text == b"random" {
        return true;
    }
    let mut at = 0;
    while at < text.len() && matches!(text[at], b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r') {
        at += 1;
    }
    let negative = text.get(at) == Some(&b'-');
    if matches!(text.get(at), Some(b'+' | b'-')) {
        at += 1;
    }
    let digits_start = at;
    let mut magnitude: u64 = 0;
    let mut overflow = false;
    while let Some(digit) = text.get(at).filter(|byte| byte.is_ascii_digit()) {
        match magnitude
            .checked_mul(10)
            .and_then(|value| value.checked_add(u64::from(digit - b'0')))
        {
            Some(next) => magnitude = next,
            None => overflow = true,
        }
        at += 1;
    }
    // 数字が無ければ `endptr` は先頭のまま(空でない文字列なので誤り)。末尾に余りがあっても誤り。
    // `ERANGE` は `ULONG_MAX` を返すので、範囲外。
    if at == digits_start || at != text.len() || overflow {
        return false;
    }
    let seed = if negative {
        magnitude.wrapping_neg()
    } else {
        magnitude
    };
    seed <= 4_294_967_295
}

/// 標準の fd(0・1・2)がディレクトリのときの、旧 Python の起動の診断。`Some(診断)` なら終了コード 1 で止まる
/// (診断が空なら、標準エラー自身がディレクトリで何も書けない)。`thread` は `Current thread` の値。
pub fn directory_fatal(directories: [bool; 3], thread: u64) -> Option<String> {
    let tail = format!(
        "\nCurrent thread 0x{thread:016x} (most recent call first):\n  <no Python frame>\n"
    );
    if directories[0] {
        return Some(format!(
            "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n{tail}"
        ));
    }
    if directories[1] {
        return Some(format!(
            "Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 1\n{tail}"
        ));
    }
    directories[2].then(String::new)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_hash_seed_follows_strtoul() {
        let ok: [&[u8]; 14] = [
            b"",
            b"random",
            b"0",
            b"1",
            b"4294967295",
            b" 5",
            b"+5",
            b"-0",
            b"\t\n5",
            b"007",
            b"\x0b\x0c\r 9",
            b"-18446744073709551615",
            b"-18446744069414584321",
            b"4294967295",
        ];
        for value in ok {
            assert!(hash_seed_is_valid(Some(value)), "{value:?}");
        }
        assert!(hash_seed_is_valid(None));
        let bad: [&[u8]; 17] = [
            b"abc",
            b"4294967296",
            b"-1",
            b"5 ",
            b"0x5",
            b"1_0",
            b"+",
            b"-",
            b" ",
            b"\xd9\xa5",
            b"RANDOM",
            b"random ",
            b"18446744073709551616",
            b"99999999999999999999999",
            b"5x",
            b"--5",
            b"-18446744073709551616",
        ];
        for value in bad {
            assert!(!hash_seed_is_valid(Some(value)), "{value:?}");
        }
    }

    #[test]
    fn directory_diagnostics_have_the_old_text_in_the_old_order() {
        let text = |dirs| directory_fatal(dirs, 0x7f12_3456_7000);
        assert_eq!(text([false, false, false]), None);
        let stdin = text([true, true, true]).unwrap();
        assert!(stdin.starts_with(
            "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n\nCurrent thread 0x00007f1234567000 (most recent call first):\n  <no Python frame>\n"
        ));
        let stdout = text([false, true, true]).unwrap();
        assert!(stdout.contains("can't initialize sys standard streams\n"));
        assert!(
            stdout.contains("IsADirectoryError: [Errno 21] Is a directory: 1\n\nCurrent thread")
        );
        assert_eq!(text([false, false, true]), Some(String::new()));
    }
}
