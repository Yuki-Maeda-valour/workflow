//! 旧Python 3.10.11の`int(str)`(10進)。argparseの`type=int`と、桁数の上限の環境変数に使う。

use super::text::{PyText, decimal, from_str, is_space, repr};

/// 旧Pythonの整数。桁の並びで持ち、u64を超える値も失わない。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyInt {
    pub negative: bool,
    /// 先頭の0を除いた10進の数字。0は`"0"`。
    pub digits: String,
}

impl PyInt {
    /// `str(int)`。
    pub fn to_text(&self) -> String {
        if self.negative && self.digits != "0" {
            format!("-{}", self.digits)
        } else {
            self.digits.clone()
        }
    }

    /// `i64`へ収まれば、その値。
    pub fn to_i64(&self) -> Option<i64> {
        let magnitude: i128 = self.digits.parse().ok()?;
        let value = if self.negative { -magnitude } else { magnitude };
        i64::try_from(value).ok()
    }
}

/// `int()`の`ValueError`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IntError {
    /// `invalid literal for int() with base 10: <repr>`
    Invalid,
    /// 桁数の上限(`Exceeds the limit ...`)。
    TooManyDigits { limit: u64, digits: u64 },
}

impl IntError {
    /// `str(ValueError)`。`%.200R`と同じく、元の文字列の`repr`を200文字で切る。
    pub fn message(&self, original: &[u32]) -> PyText {
        match self {
            Self::Invalid => {
                let mut text = from_str("invalid literal for int() with base 10: ");
                let shown = repr(original);
                text.extend(shown.iter().take(200));
                text
            }
            Self::TooManyDigits { limit, digits } => from_str(&format!(
                "Exceeds the limit ({limit}) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
            )),
        }
    }
}

/// `sys.int_info.str_digits_check_threshold`。
const THRESHOLD: u64 = 640;

/// 旧Pythonの`int(text)`。`max_digits`は`sys.get_int_max_str_digits()`(0は上限なし)。
///
/// `_PyUnicode_TransformDecimalAndSpaceToASCII`(Unicodeの空白は空白へ、10進の数字はASCIIへ、
/// ほかの非ASCIIは`?`へ)の後に、`PyLong_FromString`(前後のASCIIの空白・符号・数字の間の
/// `_`1個・桁数の上限)と同じ順に調べる。
pub fn parse(text: &[u32], max_digits: u64) -> Result<PyInt, IntError> {
    let ascii: Vec<u8> = text
        .iter()
        .map(|&code| {
            if code < 127 {
                // ASCII はそのまま(C の空白だけが前後で読み飛ばされる)。
                code as u8
            } else if is_space(code) {
                b' '
            } else if let Some(value) = decimal(code) {
                b'0' + value
            } else {
                b'?'
            }
        })
        .collect();
    // C の文字列として読むので、NUL の後は見えず、末尾に達しない(失敗)。
    let space = |byte: u8| matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r');
    let mut index = 0;
    while index < ascii.len() && space(ascii[index]) {
        index += 1;
    }
    let mut negative = false;
    if index < ascii.len() && (ascii[index] == b'+' || ascii[index] == b'-') {
        negative = ascii[index] == b'-';
        index += 1;
    }
    if index < ascii.len() && ascii[index] == b'_' {
        return Err(IntError::Invalid);
    }
    let start = index;
    let mut digits = String::new();
    let mut previous = 0u8;
    while index < ascii.len() && (ascii[index].is_ascii_digit() || ascii[index] == b'_') {
        if ascii[index] == b'_' {
            if previous == b'_' {
                return Err(IntError::Invalid);
            }
        } else {
            digits.push(char::from(ascii[index]));
        }
        previous = ascii[index];
        index += 1;
    }
    if previous == b'_' {
        return Err(IntError::Invalid);
    }
    let count = digits.len() as u64;
    if count > THRESHOLD && max_digits > 0 && count > max_digits {
        return Err(IntError::TooManyDigits {
            limit: max_digits,
            digits: count,
        });
    }
    if index == start {
        return Err(IntError::Invalid);
    }
    while index < ascii.len() && space(ascii[index]) {
        index += 1;
    }
    if index != ascii.len() {
        return Err(IntError::Invalid);
    }
    let trimmed = digits.trim_start_matches('0');
    Ok(PyInt {
        negative,
        digits: if trimmed.is_empty() {
            "0".to_string()
        } else {
            trimmed.to_string()
        },
    })
}

/// 旧Pythonの既定の桁数の上限(`PYTHONINTMAXSTRDIGITS`が無いとき)。
pub const DEFAULT_MAX_DIGITS: u64 = 4300;

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn parse_matches_python_for_signs_spaces_underscores_and_unicode_digits() {
        let cases = [
            "1_000",
            "_1",
            "1__0",
            "1_",
            " 12 ",
            "\u{661}\u{662}",
            "+0007",
            "-0",
            "1\u{a0}",
            "\u{3000} 3 ",
            "1 2",
            "",
            "\u{661}_\u{662}",
            "0x10",
            "1e3",
            "-",
            "+_1",
            "18446744073709551616",
            "-99999999999999999999",
            "\u{ff11}\u{ff12}",
            "1\u{200b}",
            "\u{1c}7\u{1f}",
            "1\u{b2}",
            "x",
        ];
        let script = "import sys\nfor s in sys.argv[1:]:\n    try: print('ok', int(s))\n    except ValueError as e: print('err', ascii(str(e)))";
        let output = Command::new("python3")
            .args(["-I", "-c", script])
            .args(cases)
            .env("PYTHONIOENCODING", "utf-8")
            .output()
            .unwrap();
        assert!(output.status.success());
        let expected = String::from_utf8(output.stdout).unwrap();
        let mut ours = String::new();
        for case in cases {
            let text = from_str(case);
            match parse(&text, DEFAULT_MAX_DIGITS) {
                Ok(value) => ours.push_str(&format!("ok {}\n", value.to_text())),
                Err(error) => {
                    let message: String = error
                        .message(&text)
                        .iter()
                        .map(|&code| char::from_u32(code).unwrap())
                        .collect();
                    ours.push_str(&format!("err {}\n", ascii(&message)));
                }
            }
        }
        assert_eq!(ours, expected);
    }

    /// Pythonの`ascii(str)`(比較のため、非ASCIIを\\uで書く)。
    fn ascii(text: &str) -> String {
        let quote = if text.contains('\'') && !text.contains('"') {
            '"'
        } else {
            '\''
        };
        let mut out = String::from(quote);
        for ch in text.chars() {
            match ch {
                _ if ch == quote => {
                    out.push('\\');
                    out.push(quote);
                }
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                ' '..='~' => out.push(ch),
                _ if (ch as u32) <= 0xff => out.push_str(&format!("\\x{:02x}", ch as u32)),
                _ if (ch as u32) <= 0xffff => out.push_str(&format!("\\u{:04x}", ch as u32)),
                _ => out.push_str(&format!("\\U{:08x}", ch as u32)),
            }
        }
        out.push(quote);
        out
    }

    #[test]
    fn digit_limit_counts_digits_and_is_checked_before_trailing_text() {
        let long: PyText = from_str(&("1".repeat(5000) + "x"));
        assert_eq!(
            parse(&long, DEFAULT_MAX_DIGITS),
            Err(IntError::TooManyDigits {
                limit: 4300,
                digits: 5000
            })
        );
        let grouped: PyText = from_str(&"1_".repeat(700)[..1399]);
        assert_eq!(
            parse(&grouped, 640).map(|value| value.digits.len()),
            Err(IntError::TooManyDigits {
                limit: 640,
                digits: 700
            })
            .map(|_: PyInt| 0)
        );
        assert_eq!(parse(&long[..5000], 0).unwrap().digits.len(), 5000);
        assert_eq!(
            parse(&from_str("-18446744073709551616"), 4300)
                .unwrap()
                .to_i64(),
            None
        );
        assert_eq!(parse(&from_str("-42"), 4300).unwrap().to_i64(), Some(-42));
        let message = IntError::Invalid.message(&from_str(&"a".repeat(300)));
        assert_eq!(
            message.len(),
            "invalid literal for int() with base 10: ".len() + 200
        );
    }
}
