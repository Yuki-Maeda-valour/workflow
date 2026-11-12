//! Python 3.10.11 の `int()`・`float()` の受理と誤りの文、float の repr、int と float を区別する数。
//! 本家の `Objects/longobject.c`(`PyLong_FromString`・`PyLong_FromUnicodeObject`・`_PyLong_FromBytes`)・
//! `Objects/floatobject.c`(`PyFloat_FromString`)・`Python/pystrtod.c`・`Python/dtoa.c` の写し。

use super::value;
use std::cmp::Ordering;

/// `sys.int_info.default_max_str_digits`(PYTHONINTMAXSTRDIGITS が無いとき)。
pub const DEFAULT_MAX_STR_DIGITS: usize = 4300;
/// `_PY_LONG_MAX_STR_DIGITS_THRESHOLD`。これ以下の桁数は上限を見ない。
pub const MAX_STR_DIGITS_THRESHOLD: usize = 640;

/// C の `Py_ISSPACE`(ASCII の空白)。
pub fn ascii_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// `Py_UNICODE_ISSPACE`(127 以上の符号位置。Unicode 13.0.0 の空白)。
pub fn unicode_space_above_ascii(code: u32) -> bool {
    matches!(
        code,
        0x85 | 0xa0 | 0x1680 | 0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000
    )
}

/// `_PyUnicode_TransformDecimalAndSpaceToASCII`。127 未満はそのまま、空白は ` `、10 進数字は ASCII の
/// 数字にする。どれでもない文字は `?` にして、そこで切る。
pub fn transform_decimal_and_space(codes: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(codes.len());
    for &code in codes {
        if code < 127 {
            out.push(code as u8);
        } else if unicode_space_above_ascii(code) {
            out.push(b' ');
        } else if let Some(digit) = super::decimal(code) {
            out.push(b'0' + digit);
        } else {
            out.push(b'?');
            break;
        }
    }
    out
}

/// `PyLong_FromString(buffer, &end, 10)` の結果。
enum CLong {
    /// 値(正規化した 10 進)と、読み終えた位置。
    Value { digits: String, end: usize },
    /// 字面の誤り(`invalid literal`)。
    Invalid,
    /// 桁の上限(`Exceeds the limit`)。
    Limit { limit: usize, digits: usize },
}

/// `PyLong_FromString` の 10 進。`buffer` は C の文字列として読む(最初の NUL で終わる)。
/// `max_digits` は `sys.get_int_max_str_digits()`(0 は上限なし)。
fn long_from_string(buffer: &[u8], max_digits: usize) -> CLong {
    let length = buffer
        .iter()
        .position(|&byte| byte == 0)
        .unwrap_or(buffer.len());
    let text = &buffer[..length];
    let at = |index: usize| text.get(index).copied().unwrap_or(0);
    let mut index = 0;
    while index < length && ascii_space(text[index]) {
        index += 1;
    }
    let mut negative = false;
    if at(index) == b'+' {
        index += 1;
    } else if at(index) == b'-' {
        index += 1;
        negative = true;
    }
    if at(index) == b'_' {
        return CLong::Invalid;
    }
    let start = index;
    let mut prev = 0u8;
    let mut count = 0usize;
    let mut scan = index;
    while at(scan).is_ascii_digit() || at(scan) == b'_' {
        if at(scan) == b'_' {
            if prev == b'_' {
                return CLong::Invalid;
            }
        } else {
            count += 1;
        }
        prev = at(scan);
        scan += 1;
    }
    if prev == b'_' {
        return CLong::Invalid;
    }
    if count > MAX_STR_DIGITS_THRESHOLD && max_digits > 0 && count > max_digits {
        return CLong::Limit {
            limit: max_digits,
            digits: count,
        };
    }
    if scan == start {
        return CLong::Invalid;
    }
    let mut digits: String = text[start..scan]
        .iter()
        .filter(|byte| byte.is_ascii_digit())
        .map(|&byte| char::from(byte))
        .collect();
    let mut end = scan;
    while end < length && ascii_space(text[end]) {
        end += 1;
    }
    if end != length {
        return CLong::Invalid;
    }
    let trimmed = digits.trim_start_matches('0');
    digits = if trimmed.is_empty() {
        "0".to_owned()
    } else if negative {
        format!("-{trimmed}")
    } else {
        trimmed.to_owned()
    };
    CLong::Value { digits, end }
}

fn limit_message(limit: usize, digits: usize) -> String {
    format!(
        "Exceeds the limit ({limit}) for integer string conversion: value has {digits} digits; \
         use sys.set_int_max_str_digits() to increase the limit"
    )
}

/// `%.200R`(repr を 200 文字までにする)。
fn truncated_repr(repr: Vec<u32>) -> String {
    value::text(&repr[..repr.len().min(200)])
}

/// `int(text)`(text は str)。誤りは `ValueError` の文。
pub fn int_from_str(codes: &[u32], max_digits: usize) -> Result<String, String> {
    let ascii = transform_decimal_and_space(codes);
    let invalid = || {
        format!(
            "invalid literal for int() with base 10: {}",
            truncated_repr(value::str_repr(codes))
        )
    };
    match long_from_string(&ascii, max_digits) {
        CLong::Value { digits, end } if end == ascii.len() => Ok(digits),
        CLong::Value { .. } | CLong::Invalid => Err(invalid()),
        CLong::Limit { limit, digits } => Err(limit_message(limit, digits)),
    }
}

/// `int(raw)`(raw は bytes)。誤りは `ValueError` の文。
pub fn int_from_bytes(raw: &[u8], max_digits: usize) -> Result<String, String> {
    let invalid = || {
        format!(
            "invalid literal for int() with base 10: {}",
            truncated_repr(value::bytes_repr(&raw[..raw.len().min(200)]))
        )
    };
    match long_from_string(raw, max_digits) {
        CLong::Value { digits, end } if end == raw.len() => Ok(digits),
        CLong::Value { .. } | CLong::Invalid => Err(invalid()),
        CLong::Limit { limit, digits } => Err(limit_message(limit, digits)),
    }
}

/// `_Py_dg_strtod` が読む長さ(読めなければ 0)。
fn dtoa_prefix(text: &[u8]) -> usize {
    let at = |index: usize| text.get(index).copied().unwrap_or(0);
    let mut index = 0;
    if matches!(at(index), b'-' | b'+') {
        index += 1;
    }
    let zeros_start = index;
    while at(index) == b'0' {
        index += 1;
    }
    let mut leading_zeros = index != zeros_start;
    let digits_start = index;
    while at(index).is_ascii_digit() {
        index += 1;
    }
    let mut digits = index - digits_start;
    if at(index) == b'.' {
        index += 1;
        if digits == 0 {
            let zeros = index;
            while at(index) == b'0' {
                index += 1;
            }
            leading_zeros = leading_zeros || index != zeros;
        }
        let fraction = index;
        while at(index).is_ascii_digit() {
            index += 1;
        }
        digits += index - fraction;
    }
    if digits == 0 && !leading_zeros {
        return 0;
    }
    if matches!(at(index), b'e' | b'E') {
        let exponent = index;
        let mut probe = index + 1;
        if matches!(at(probe), b'-' | b'+') {
            probe += 1;
        }
        let exponent_digits = probe;
        while at(probe).is_ascii_digit() {
            probe += 1;
        }
        index = if probe == exponent_digits {
            exponent
        } else {
            probe
        };
    }
    index
}

/// `_Py_parse_inf_or_nan` が読む長さ(読めなければ 0)。
fn inf_or_nan_prefix(text: &[u8]) -> usize {
    let mut index = 0;
    if matches!(text.first(), Some(b'-' | b'+')) {
        index = 1;
    }
    let matches_at = |at: usize, word: &[u8]| {
        text.len() >= at + word.len() && text[at..at + word.len()].eq_ignore_ascii_case(word)
    };
    if matches_at(index, b"inf") {
        index += 3;
        if matches_at(index, b"inity") {
            index += 5;
        }
        index
    } else if matches_at(index, b"nan") {
        index + 3
    } else {
        0
    }
}

/// `float_from_string_inner`(空白を除き、全体が数の字面なら値)。
fn float_inner(text: &[u8]) -> Option<f64> {
    let mut start = 0;
    let mut last = text.len();
    while start < last && ascii_space(text[start]) {
        start += 1;
    }
    if start == last {
        return None;
    }
    while start < last - 1 && ascii_space(text[last - 1]) {
        last -= 1;
    }
    let core = &text[start..last];
    let mut end = dtoa_prefix(core);
    if end == 0 {
        end = inf_or_nan_prefix(core);
    }
    if end == 0 || end != core.len() {
        return None;
    }
    let ascii = std::str::from_utf8(core).ok()?;
    ascii.parse::<f64>().ok()
}

/// `_Py_string_to_number_with_underscores` と `float_from_string_inner`(ASCII のバイト列)。
fn float_from_ascii(text: &[u8]) -> Option<f64> {
    if !text.contains(&b'_') {
        return float_inner(text);
    }
    let mut cleaned = Vec::with_capacity(text.len());
    let mut prev = 0u8;
    let mut index = 0;
    while index < text.len() && text[index] != 0 {
        let byte = text[index];
        if byte == b'_' {
            if !prev.is_ascii_digit() {
                return None;
            }
        } else {
            cleaned.push(byte);
            if prev == b'_' && !byte.is_ascii_digit() {
                return None;
            }
        }
        prev = byte;
        index += 1;
    }
    if prev == b'_' || index != text.len() {
        return None;
    }
    float_inner(&cleaned)
}

/// `float(text)`(text は str)。誤りは `ValueError` の文。
pub fn float_from_str(codes: &[u32]) -> Result<f64, String> {
    float_from_ascii(&transform_decimal_and_space(codes)).ok_or_else(|| {
        format!(
            "could not convert string to float: {}",
            value::text(&value::str_repr(codes))
        )
    })
}

/// `float(raw)`(raw は bytes。json の数の字面に使う)。
pub fn float_from_bytes(raw: &[u8]) -> Result<f64, String> {
    float_from_ascii(raw).ok_or_else(|| {
        format!(
            "could not convert string to float: {}",
            value::text(&value::bytes_repr(raw))
        )
    })
}

/// `repr(float)`(`PyOS_double_to_string(x, 'r', 0, Py_DTSF_ADD_DOT_0)`)。最短の桁は Rust の `{:e}`。
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    // 最短の桁数は Rust の `{:e}` で決める。同じ桁数の中では、値に最も近い字面(`_Py_dg_dtoa` の mode 0)を選ぶ。
    // Rust の最短の字面が最も近いとは限らないので、その桁数で正しく丸めた字面が元の値へ戻るなら、それを使う。
    let shortest = format!("{:e}", value.abs());
    let length = shortest.split_once('e').map_or(1, |(mantissa, _)| {
        mantissa.chars().filter(|c| *c != '.').count()
    });
    let rounded = format!("{:.*e}", length - 1, value.abs());
    let scientific = if rounded.parse::<f64>() == Ok(value.abs()) {
        rounded
    } else {
        shortest
    };
    let (mantissa, exponent) = scientific.split_once('e').expect("{:e} の字面は e を含む");
    let exponent: i64 = exponent.parse().expect("{:e} の指数は整数");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let decpt = exponent + 1;
    let length = digits.len() as i64;
    let body = if decpt <= -4 || decpt > 16 {
        let mut text = digits[..1].to_owned();
        if digits.len() > 1 {
            text.push('.');
            text.push_str(&digits[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{text}e{sign}{:02}", exponent.abs())
    } else if decpt <= 0 {
        format!("0.{}{digits}", "0".repeat((-decpt) as usize))
    } else if decpt >= length {
        format!("{digits}{}.0", "0".repeat((decpt - length) as usize))
    } else {
        format!(
            "{}.{}",
            &digits[..decpt as usize],
            &digits[decpt as usize..]
        )
    };
    if value.is_sign_negative() {
        format!("-{body}")
    } else {
        body
    }
}

/// 整数の値の float の正確な 10 進(有限で小数部が 0 のときだけ)。
pub fn exact_integer_text(value: f64) -> Option<String> {
    if !value.is_finite() || value.fract() != 0.0 {
        return None;
    }
    if value == 0.0 {
        return Some("0".to_owned());
    }
    Some(format!("{value:.0}"))
}

/// `int too large to convert to float`(Python の `OverflowError`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Overflow;

impl Overflow {
    pub const MESSAGE: &'static str = "int too large to convert to float";
}

/// `float(int)`(`PyLong_AsDouble`。最も近い値へ丸め、範囲を超えれば `OverflowError`)。
pub fn int_to_f64(digits: &str) -> Result<f64, Overflow> {
    let value: f64 = digits.parse().map_err(|_| Overflow)?;
    if value.is_infinite() {
        Err(Overflow)
    } else {
        Ok(value)
    }
}

fn split_sign(digits: &str) -> (bool, &str) {
    match digits.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, digits),
    }
}

fn compare_magnitude(left: &str, right: &str) -> Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}

fn add_magnitude(left: &str, right: &str) -> String {
    let (left, right) = (left.as_bytes(), right.as_bytes());
    let mut out = Vec::with_capacity(left.len().max(right.len()) + 1);
    let mut carry = 0u8;
    let mut i = left.len();
    let mut j = right.len();
    while i > 0 || j > 0 || carry > 0 {
        let mut sum = carry;
        if i > 0 {
            i -= 1;
            sum += left[i] - b'0';
        }
        if j > 0 {
            j -= 1;
            sum += right[j] - b'0';
        }
        out.push(b'0' + sum % 10);
        carry = sum / 10;
    }
    out.reverse();
    String::from_utf8(out).expect("数字だけ")
}

/// `left - right`(left >= right の大きさ)。
fn sub_magnitude(left: &str, right: &str) -> String {
    let (left, right) = (left.as_bytes(), right.as_bytes());
    let mut out = Vec::with_capacity(left.len());
    let mut borrow = 0i8;
    let mut j = right.len();
    for i in (0..left.len()).rev() {
        let mut digit = (left[i] - b'0') as i8 - borrow;
        if j > 0 {
            j -= 1;
            digit -= (right[j] - b'0') as i8;
        }
        if digit < 0 {
            digit += 10;
            borrow = 1;
        } else {
            borrow = 0;
        }
        out.push(b'0' + digit as u8);
    }
    out.reverse();
    let text = String::from_utf8(out).expect("数字だけ");
    let trimmed = text.trim_start_matches('0');
    if trimmed.is_empty() {
        "0".to_owned()
    } else {
        trimmed.to_owned()
    }
}

/// 整数の和(正規化した 10 進どうし)。
pub fn int_add(left: &str, right: &str) -> String {
    let (left_negative, left_digits) = split_sign(left);
    let (right_negative, right_digits) = split_sign(right);
    let (negative, magnitude) = if left_negative == right_negative {
        (left_negative, add_magnitude(left_digits, right_digits))
    } else {
        match compare_magnitude(left_digits, right_digits) {
            Ordering::Less => (right_negative, sub_magnitude(right_digits, left_digits)),
            _ => (left_negative, sub_magnitude(left_digits, right_digits)),
        }
    };
    if negative && magnitude != "0" {
        format!("-{magnitude}")
    } else {
        magnitude
    }
}

/// Python の int と float を区別する数。int は正規化した 10 進の字面(桁の上限なし)。
#[derive(Clone, Debug, PartialEq)]
pub enum PyNumber {
    Int(String),
    Float(f64),
}

impl PyNumber {
    /// `str(x)`(int は桁をそのまま、float は repr)。
    pub fn to_str(&self) -> String {
        match self {
            PyNumber::Int(digits) => digits.clone(),
            PyNumber::Float(value) => float_repr(*value),
        }
    }

    /// float としての値(int は `PyLong_AsDouble`)。
    pub fn to_f64(&self) -> Result<f64, Overflow> {
        match self {
            PyNumber::Int(digits) => int_to_f64(digits),
            PyNumber::Float(value) => Ok(*value),
        }
    }

    /// `self + other`(int どうしは int、どちらかが float なら float)。
    pub fn add(&self, other: &PyNumber) -> Result<PyNumber, Overflow> {
        match (self, other) {
            (PyNumber::Int(left), PyNumber::Int(right)) => Ok(PyNumber::Int(int_add(left, right))),
            _ => Ok(PyNumber::Float(self.to_f64()? + other.to_f64()?)),
        }
    }

    /// `math.isfinite(x)`(int は float へ変える。大きすぎる int は `OverflowError`)。
    pub fn is_finite(&self) -> Result<bool, Overflow> {
        self.to_f64().map(f64::is_finite)
    }

    /// 0 との大小(NaN は None)。int と float の比べ方は Python と同じく値で比べる。
    pub fn compare_zero(&self) -> Option<Ordering> {
        match self {
            PyNumber::Int(digits) if digits == "0" => Some(Ordering::Equal),
            PyNumber::Int(digits) if digits.starts_with('-') => Some(Ordering::Less),
            PyNumber::Int(_) => Some(Ordering::Greater),
            PyNumber::Float(value) => value.partial_cmp(&0.0),
        }
    }
}
