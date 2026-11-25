//! 旧 Python の起動の時点の設定の読み方(本家 3.10.11 の `Python/initconfig.c`・`Python/preconfig.c`)。
//! - 環境変数は空なら設定なしと扱う(`_Py_GetEnv`)。
//! - 数の読み方は C の `strtol`・`strtoul`(10 進。前の空白と符号を許し、全体を読み切る)と同じにする。
//! - Rust は文字コードの設定(PYTHONIOENCODING など)を読まない(UTF-8 と surrogateescape に固定)。

/// C の `isspace`(C の locale)。
fn space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// `strtol`/`strtoul` の共通部分。(負か, 値, あふれたか)。数字が無い・後ろに残りがあれば None。
fn parse(text: &[u8]) -> Option<(bool, u128, bool)> {
    let mut rest = text;
    while rest.first().is_some_and(|&byte| space(byte)) {
        rest = &rest[1..];
    }
    let negative = rest.first() == Some(&b'-');
    if matches!(rest.first(), Some(b'+' | b'-')) {
        rest = &rest[1..];
    }
    if rest.is_empty() || !rest.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let mut value: u128 = 0;
    let mut overflow = false;
    for &digit in rest {
        value = value * 10 + u128::from(digit - b'0');
        if value > u128::from(u64::MAX) {
            overflow = true;
            value = u128::from(u64::MAX) + 1;
        }
    }
    Some((negative, value, overflow))
}

/// `_Py_str_to_int`(strtol で全体を読み、あふれず int に収まるとき)。
pub fn str_to_int(text: &[u8]) -> Option<i32> {
    let (negative, value, overflow) = parse(text)?;
    if overflow {
        return None;
    }
    let value = i128::try_from(value).ok()?;
    let value = if negative { -value } else { value };
    // long(64 ビット)を超えれば ERANGE。
    if value > i128::from(i64::MAX) || value < i128::from(i64::MIN) {
        return None;
    }
    i32::try_from(value).ok()
}

/// `_Py_get_env_flag`。読めない値と負の値は 1。
pub fn env_flag(value: Option<&[u8]>) -> i32 {
    match value {
        None => 0,
        Some(text) => match str_to_int(text) {
            Some(number) if number >= 0 => number,
            _ => 1,
        },
    }
}

/// `config_init_hash_seed` が受け付ける値か(`random` か、strtoul で 0〜4294967295)。
pub fn hash_seed_valid(value: Option<&[u8]>) -> bool {
    let Some(text) = value else {
        return true;
    };
    if text == b"random" {
        return true;
    }
    match parse(text) {
        None => false,
        Some((_, _, true)) => false,
        // strtoul は負の数を 2 の補数として返すので、0 のほかは範囲を超える。
        Some((true, value, false)) => value == 0,
        Some((false, value, false)) => value <= 4_294_967_295,
    }
}

/// `config_init_int_max_str_digits` が受け付ける値か(0 か 640 以上)。
pub fn int_max_str_digits_valid(value: Option<&[u8]>) -> bool {
    let Some(text) = value else {
        return true;
    };
    matches!(str_to_int(text), Some(number) if number == 0 || number >= 640)
}

/// 起動の時点で読む環境変数。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Config {
    pub hash_seed: Option<Vec<u8>>,
    pub int_max_str_digits: Option<Vec<u8>>,
    pub unbuffered: Option<Vec<u8>>,
    pub optimize: Option<Vec<u8>>,
}

fn get(name: &str) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    std::env::var_os(name)
        .map(OsStringExt::into_vec)
        .filter(|value| !value.is_empty())
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            hash_seed: get("PYTHONHASHSEED"),
            int_max_str_digits: get("PYTHONINTMAXSTRDIGITS"),
            unbuffered: get("PYTHONUNBUFFERED"),
            optimize: get("PYTHONOPTIMIZE"),
        }
    }

    /// PYTHONUNBUFFERED が有効か。
    pub fn unbuffered(&self) -> bool {
        env_flag(self.unbuffered.as_deref()) > 0
    }

    /// PYTHONOPTIMIZE が 2 以上なら docstring が無い。
    pub fn doc(&self) -> Option<&'static str> {
        (env_flag(self.optimize.as_deref()) < 2).then_some(super::legacy::DOC)
    }
}
