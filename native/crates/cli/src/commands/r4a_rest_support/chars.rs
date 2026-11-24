//! Python の `str` を文字番号の列(`Vec<u32>`)で扱う部品。
//!
//! 環境由来の文字解釈は UTF-8 と surrogateescape に固定する(設計の承認差)。
//! 孤立 surrogate を失わないため、`String` ではなく文字番号の列を使う。

mod tables {
    include!("unicode/unicode13.rs");
}

pub type PyStr = Vec<u32>;

pub const UNICODE_VERSION: &str = tables::UNICODE_VERSION;
pub const TABLE_SHA256: &str = tables::TABLE_SHA256;

fn in_ranges(ranges: &[(u32, u32)], code: u32) -> bool {
    ranges
        .binary_search_by(|&(start, end)| {
            if code < start {
                std::cmp::Ordering::Greater
            } else if code > end {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `str.isprintable()` の 1 文字版(Unicode 13)。
pub fn is_printable(code: u32) -> bool {
    !in_ranges(&tables::NON_PRINTABLE, code)
}

/// `str.isspace()` の 1 文字版(`int()` の前後除去・正規表現 `\s`)。
pub fn is_space(code: u32) -> bool {
    in_ranges(&tables::SPACE, code)
}

/// `unicodedata.decimal(ch, None)` の 1 文字版。
pub fn decimal_value(code: u32) -> Option<u32> {
    let at = tables::DECIMAL_ZEROS.partition_point(|&zero| zero <= code);
    let zero = *tables::DECIMAL_ZEROS.get(at.checked_sub(1)?)?;
    (code - zero < 10).then_some(code - zero)
}

/// UTF-8 + surrogateescape で復号する。不正な各バイトは U+DC80..U+DCFF になる。
pub fn from_os_bytes(bytes: &[u8]) -> PyStr {
    let mut out = Vec::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        out.extend(chunk.valid().chars().map(u32::from));
        out.extend(chunk.invalid().iter().map(|&b| 0xdc00 + u32::from(b)));
    }
    out
}

/// 厳密な UTF-8 復号。成功しなければ `None`(呼出側が用途別の診断へ写す)。
pub fn from_utf8_strict(bytes: &[u8]) -> Option<PyStr> {
    std::str::from_utf8(bytes)
        .ok()
        .map(|text| text.chars().map(u32::from).collect())
}

/// 置換付きの復号(`errors="replace"`)。
pub fn from_utf8_replace(bytes: &[u8]) -> PyStr {
    String::from_utf8_lossy(bytes)
        .chars()
        .map(u32::from)
        .collect()
}

pub fn from_str(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

/// 符号化の失敗。`start..end`(end を含まない)が連続した孤立 surrogate の範囲。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EncodeError {
    pub start: usize,
    pub end: usize,
}

/// UTF-8 + surrogateescape で符号化する。U+DC80..U+DCFF 以外の孤立 surrogate は失敗。
pub fn to_os_bytes(text: &[u32]) -> Result<Vec<u8>, EncodeError> {
    let mut out = Vec::with_capacity(text.len());
    let mut at = 0;
    while at < text.len() {
        let code = text[at];
        match code {
            0xdc80..=0xdcff => out.push((code - 0xdc00) as u8),
            0xd800..=0xdfff => {
                let mut end = at + 1;
                while end < text.len()
                    && (0xd800..=0xdfff).contains(&text[end])
                    && !(0xdc80..=0xdcff).contains(&text[end])
                {
                    end += 1;
                }
                return Err(EncodeError { start: at, end });
            }
            _ => {
                let ch = char::from_u32(code).expect("scalar value");
                let mut buffer = [0_u8; 4];
                out.extend_from_slice(ch.encode_utf8(&mut buffer).as_bytes());
            }
        }
        at += 1;
    }
    Ok(out)
}

/// 厳密な UTF-8 符号化(`errors="strict"`)。孤立 surrogate は範囲付きで失敗。
pub fn to_utf8_strict(text: &[u32]) -> Result<Vec<u8>, EncodeError> {
    let mut out = Vec::with_capacity(text.len());
    let mut at = 0;
    while at < text.len() {
        if let Some(ch) = char::from_u32(text[at]) {
            let mut buffer = [0_u8; 4];
            out.extend_from_slice(ch.encode_utf8(&mut buffer).as_bytes());
            at += 1;
        } else {
            let mut end = at + 1;
            while end < text.len() && (0xd800..=0xdfff).contains(&text[end]) {
                end += 1;
            }
            return Err(EncodeError { start: at, end });
        }
    }
    Ok(out)
}

/// `repr(str)`。
pub fn repr(text: &[u32]) -> PyStr {
    let has_single = text.contains(&u32::from(b'\''));
    let has_double = text.contains(&u32::from(b'"'));
    let quote = if has_single && !has_double {
        b'"'
    } else {
        b'\''
    };
    let mut out = vec![u32::from(quote)];
    for &code in text {
        if code == u32::from(quote) || code == u32::from(b'\\') {
            out.push(u32::from(b'\\'));
            out.push(code);
        } else if code == 9 {
            out.extend(from_str("\\t"));
        } else if code == 10 {
            out.extend(from_str("\\n"));
        } else if code == 13 {
            out.extend(from_str("\\r"));
        } else if code < 0x20 || code == 0x7f {
            out.extend(from_str(&format!("\\x{code:02x}")));
        } else if code < 0x7f || is_printable(code) {
            out.push(code);
        } else if code < 0x100 {
            out.extend(from_str(&format!("\\x{code:02x}")));
        } else if code < 0x10000 {
            out.extend(from_str(&format!("\\u{code:04x}")));
        } else {
            out.extend(from_str(&format!("\\U{code:08x}")));
        }
    }
    out.push(u32::from(quote));
    out
}

/// `int(str)` の結果。桁数の上限は呼出側が `max_digits`(0 は無制限)で渡す。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum IntParse {
    Value(i128),
    /// 値が i128 を超えた(符号付き)。幅の用途では飽和として扱う。
    Huge(bool),
    Invalid,
    TooManyDigits,
}

/// Python の `int(text)`(底 10)。前後の Unicode 空白・符号・Unicode の10進数字・
/// 数字の間の単独のアンダースコアを許す。
pub fn parse_int(text: &[u32], max_digits: usize) -> IntParse {
    let mut start = 0;
    let mut end = text.len();
    while start < end && is_space(text[start]) {
        start += 1;
    }
    while end > start && is_space(text[end - 1]) {
        end -= 1;
    }
    let body = &text[start..end];
    let (negative, body) = match body.first() {
        Some(&43) => (false, &body[1..]),
        Some(&45) => (true, &body[1..]),
        _ => (false, body),
    };
    if body.is_empty() {
        return IntParse::Invalid;
    }
    let mut digits = Vec::with_capacity(body.len());
    let mut previous_underscore = true; // 先頭の `_` を拒否する
    for &code in body {
        if code == u32::from(b'_') {
            if previous_underscore {
                return IntParse::Invalid;
            }
            previous_underscore = true;
        } else if let Some(value) = decimal_value(code) {
            digits.push(value);
            previous_underscore = false;
        } else {
            return IntParse::Invalid;
        }
    }
    if previous_underscore {
        return IntParse::Invalid;
    }
    if max_digits != 0 && digits.len() > max_digits {
        return IntParse::TooManyDigits;
    }
    let mut value: i128 = 0;
    for digit in &digits {
        match value
            .checked_mul(10)
            .and_then(|v| v.checked_add(i128::from(*digit)))
        {
            Some(next) => value = next,
            None => return IntParse::Huge(negative),
        }
    }
    IntParse::Value(if negative { -value } else { value })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(text: &str) -> PyStr {
        from_str(text)
    }
    fn back(text: &[u32]) -> String {
        text.iter()
            .map(|&c| char::from_u32(c).unwrap_or('?'))
            .collect()
    }

    #[test]
    fn surrogateescape_round_trips_and_rejects_other_surrogates() {
        let decoded = from_os_bytes(b"a\xffb\xe2\x82");
        assert_eq!(decoded, [97, 0xdcff, 98, 0xdce2, 0xdc82]);
        assert_eq!(to_os_bytes(&decoded).unwrap(), b"a\xffb\xe2\x82");
        assert_eq!(
            to_os_bytes(&[97, 0xd800, 0xd801, 98]),
            Err(EncodeError { start: 1, end: 3 })
        );
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(back(&repr(&s("a'b"))), "\"a'b\"");
        assert_eq!(back(&repr(&s("a'\"b"))), "'a\\'\"b'");
        assert_eq!(back(&repr(&s("\t\n\u{7f}\u{a0}é"))), "'\\t\\n\\x7f\\xa0é'");
        assert_eq!(
            back(&repr(&[0xd800, 0x10ffff, 0x1f600])),
            "'\\ud800\\U0010ffff😀'"
        );
    }

    #[test]
    fn tables_agree_with_known_facts() {
        assert!(is_space(0x3000) && is_space(0x1c) && !is_space(0x200b));
        assert_eq!(decimal_value(0xff11), Some(1));
        assert_eq!(decimal_value(0x0660 + 9), Some(9));
        assert_eq!(decimal_value(0xb2), None);
        assert!(!is_printable(0xad) && is_printable(0x3042));
        assert_eq!(UNICODE_VERSION, "13.0.0");
    }

    #[test]
    fn int_parse_follows_python() {
        assert_eq!(parse_int(&s(" +1_0 "), 4300), IntParse::Value(10));
        assert_eq!(parse_int(&s("１２"), 4300), IntParse::Value(12));
        for bad in ["", "_1", "1_", "1__0", "0x1", "--1", "1.5", "\u{200b}1"] {
            assert_eq!(parse_int(&s(bad), 4300), IntParse::Invalid, "{bad:?}");
        }
        assert_eq!(
            parse_int(&s(&"9".repeat(641)), 640),
            IntParse::TooManyDigits
        );
        assert_eq!(parse_int(&s(&"9".repeat(641)), 0), IntParse::Huge(false));
    }
}
