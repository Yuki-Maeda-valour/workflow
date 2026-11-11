//! Python 3.10 の文字列の処理。文字は文字番号(surrogate を含む)の列で扱う。
//! 空白・十進数字・表示不能・NFC は Unicode 13.0.0 の表(`unicode_tables.rs`)に従う。
use super::error::PyException;
use super::pyvalue::PyInt;
use super::unicode_tables as tables;
use dev_workflow_core::loop_state::CodepointString;

/// バイト列を、UTF-8 と surrogateescape(読めないバイトは U+DC80〜U+DCFF)で文字番号の列にする。
pub fn decode_fs(bytes: &[u8]) -> Vec<u32> {
    let mut result = Vec::with_capacity(bytes.len());
    let mut rest = bytes;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(text) => {
                result.extend(text.chars().map(u32::from));
                break;
            }
            Err(error) => {
                let (valid, invalid) = rest.split_at(error.valid_up_to());
                result.extend(
                    std::str::from_utf8(valid)
                        .expect("検証済み")
                        .chars()
                        .map(u32::from),
                );
                let length = error.error_len().unwrap_or(invalid.len());
                result.extend(invalid[..length].iter().map(|&b| 0xdc00 + u32::from(b)));
                rest = &invalid[length..];
            }
        }
    }
    result
}

pub fn decode_fs_string(bytes: &[u8]) -> CodepointString {
    CodepointString::try_from_codepoints(decode_fs(bytes)).expect("範囲内")
}

/// 符号化できない surrogate の連なり(`UnicodeEncodeError` の範囲)。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EncodeFailure {
    pub start: usize,
    pub end: usize,
    pub text: Vec<u32>,
}

/// 文字番号の列を UTF-8 + surrogateescape で符号化する。U+DC80〜U+DCFF は元のバイトに戻る。
/// それ以外の surrogate に当たると、そこから surrogate の連なりの終わりまでが
/// `UnicodeEncodeError` の範囲になる(連なりの中の U+DC80〜U+DCFF も範囲に入る)。
pub fn encode_fs(cps: &[u32]) -> Result<Vec<u8>, EncodeFailure> {
    let mut out = Vec::with_capacity(cps.len());
    for (index, &cp) in cps.iter().enumerate() {
        match cp {
            0xdc80..=0xdcff => out.push((cp - 0xdc00) as u8),
            0xd800..=0xdfff => {
                let end = cps[index..]
                    .iter()
                    .position(|c| !(0xd800..=0xdfff).contains(c))
                    .map_or(cps.len(), |i| index + i);
                return Err(EncodeFailure {
                    start: index,
                    end,
                    text: cps[index..end].to_vec(),
                });
            }
            _ => {
                let c = char::from_u32(cp).expect("surrogate 以外は有効");
                let mut buffer = [0; 4];
                out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    Ok(out)
}

/// 標準エラーの符号化(`backslashreplace`)。surrogate は `\udXXX` の字面になる。
pub fn encode_backslashreplace(cps: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(cps.len());
    for &cp in cps {
        match char::from_u32(cp) {
            Some(c) => {
                let mut buffer = [0; 4];
                out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
            }
            None => out.extend_from_slice(format!("\\u{cp:04x}").as_bytes()),
        }
    }
    out
}

/// 標準出力の符号化の失敗を旧版の `UnicodeEncodeError` にする(U1: UTF-8 + surrogateescape)。
pub fn encode_error(failure: &EncodeFailure) -> PyException {
    let text = if failure.end == failure.start + 1 {
        format!(
            "'utf-8' codec can't encode character '\\u{:04x}' in position {}: surrogates not allowed",
            failure.text[0], failure.start
        )
    } else {
        format!(
            "'utf-8' codec can't encode characters in position {}-{}: surrogates not allowed",
            failure.start,
            failure.end - 1
        )
    };
    PyException::new("UnicodeEncodeError", &text)
}

/// 表示用の文字列(範囲外の文字番号と surrogate は U+FFFD)。
pub fn to_string(cps: &[u32]) -> String {
    super::error::cps_to_lossy(cps)
}

pub fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn in_ranges(code: u32, table: &[(u32, u32)]) -> bool {
    table
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

/// `str.isspace()` の 1 文字版。
pub fn is_space(code: u32) -> bool {
    in_ranges(code, tables::SPACE)
}

/// `unicodedata.decimal(chr(code), None)`。
pub fn decimal(code: u32) -> Option<u8> {
    let index = tables::DECIMAL_STARTS.partition_point(|&start| start <= code);
    let start = *tables::DECIMAL_STARTS.get(index.checked_sub(1)?)?;
    (code < start + 10).then(|| (code - start) as u8)
}

/// `str.isprintable()` の 1 文字版。
pub fn is_printable(code: u32) -> bool {
    !in_ranges(code, tables::NONPRINTABLE)
}

/// `unicodedata.combining(chr(code))`。
pub fn combining(code: u32) -> u8 {
    let index = tables::COMBINING.partition_point(|&(start, _, _)| start <= code);
    match index.checked_sub(1).map(|i| tables::COMBINING[i]) {
        Some((_, end, class)) if code <= end => class,
        _ => 0,
    }
}

/// `str.split()`(引数なし)。空白の連なりで区切り、空の要素は作らない。
pub fn split_whitespace(text: &[u32]) -> Vec<Vec<u32>> {
    text.split(|&c| is_space(c))
        .filter(|part| !part.is_empty())
        .map(<[u32]>::to_vec)
        .collect()
}

/// `str.strip()`(引数なし)。
pub fn strip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&c| !is_space(c))
        .unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |i| i + 1);
    &text[start..end]
}

/// `text[:count]`。
pub fn truncate(text: &[u32], count: usize) -> &[u32] {
    &text[..text.len().min(count)]
}

/// `re.sub(r'[\x00-\x1f\x7f]', ' ', text)`。
pub fn replace_controls(text: &[u32]) -> Vec<u32> {
    text.iter()
        .map(|&c| if c <= 0x1f || c == 0x7f { 0x20 } else { c })
        .collect()
}

/// `str.rstrip('/')` など、指定した 1 文字の末尾を落とす。
pub fn rstrip_char(text: &[u32], ch: u32) -> &[u32] {
    let end = text.iter().rposition(|&c| c != ch).map_or(0, |i| i + 1);
    &text[..end]
}

// ---- repr(str) ----

/// `repr(str)`。引用符は内容から選び、表示できない文字は `\x`・`\u`・`\U` の字面にする。
pub fn repr(text: &[u32]) -> Vec<u32> {
    let single = text.contains(&0x27);
    let double = text.contains(&0x22);
    let quote = if single && !double { 0x22 } else { 0x27 };
    let mut out = vec![quote];
    for &c in text {
        match c {
            0x0a => out.extend(cps("\\n")),
            0x0d => out.extend(cps("\\r")),
            0x09 => out.extend(cps("\\t")),
            0x5c => out.extend(cps("\\\\")),
            _ if c == quote => {
                out.push(0x5c);
                out.push(c);
            }
            0x20..=0x7e => out.push(c),
            _ if !is_printable(c) => {
                let escaped = if c <= 0xff {
                    format!("\\x{c:02x}")
                } else if c <= 0xffff {
                    format!("\\u{c:04x}")
                } else {
                    format!("\\U{c:08x}")
                };
                out.extend(cps(&escaped));
            }
            _ => out.push(c),
        }
    }
    out.push(quote);
    out
}

// ---- int(str) ----

/// `int(str)` の失敗。
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum IntError {
    /// `invalid literal for int() with base 10`。
    Invalid,
    /// 桁数の上限(`PYTHONINTMAXSTRDIGITS`)を超えた。
    TooManyDigits { limit: usize, digits: usize },
}

impl IntError {
    pub fn exception(&self, text: &[u32]) -> PyException {
        match self {
            IntError::Invalid => {
                let mut message = cps("invalid literal for int() with base 10: ");
                // PyErr_Format の `%.200R`: repr の先頭 200 文字だけを出す。
                message.extend(repr(text).into_iter().take(200));
                PyException::with_cps(
                    "ValueError",
                    CodepointString::try_from_codepoints(message).expect("範囲内"),
                )
            }
            IntError::TooManyDigits { limit, digits } => PyException::value_error(&format!(
                "Exceeds the limit ({limit}) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
            )),
        }
    }
}

fn ascii_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// `int(text)`。空白・符号・`_`・Unicode の十進数字を旧版どおりに扱う。
/// `max_digits` は桁数の上限(0 は無制限)。
pub fn parse_int(text: &[u32], max_digits: usize) -> Result<PyInt, IntError> {
    // _PyUnicode_TransformDecimalAndSpaceToASCII: ASCII(127 未満)はそのまま、空白は ' '、
    // 十進数字は ASCII の数字へ、ほかは '?' で打ち切る。
    let mut ascii: Vec<u8> = Vec::with_capacity(text.len());
    for &c in text {
        if c < 127 {
            ascii.push(c as u8);
        } else if is_space(c) {
            ascii.push(b' ');
        } else if let Some(d) = decimal(c) {
            ascii.push(b'0' + d);
        } else {
            ascii.push(b'?');
            break;
        }
    }
    // PyLong_FromString は C の文字列として読むので、途中の NUL でそこまでを読み終える。
    // 余りの判定(NUL を含む)は、桁数の上限の確認より後になる。
    let nul = ascii.iter().position(|&b| b == 0);
    let ascii = &ascii[..nul.unwrap_or(ascii.len())];
    let mut pos = 0;
    while pos < ascii.len() && ascii_space(ascii[pos]) {
        pos += 1;
    }
    let mut negative = false;
    if pos < ascii.len() && (ascii[pos] == b'+' || ascii[pos] == b'-') {
        negative = ascii[pos] == b'-';
        pos += 1;
    }
    if pos < ascii.len() && ascii[pos] == b'_' {
        return Err(IntError::Invalid);
    }
    let mut digits = String::new();
    let mut previous = 0u8;
    while pos < ascii.len() && (ascii[pos].is_ascii_digit() || ascii[pos] == b'_') {
        if ascii[pos] == b'_' {
            if previous == b'_' {
                return Err(IntError::Invalid);
            }
        } else {
            digits.push(ascii[pos] as char);
        }
        previous = ascii[pos];
        pos += 1;
    }
    if previous == b'_' {
        return Err(IntError::Invalid);
    }
    if digits.len() > 640 && max_digits > 0 && digits.len() > max_digits {
        return Err(IntError::TooManyDigits {
            limit: max_digits,
            digits: digits.len(),
        });
    }
    if digits.is_empty() {
        return Err(IntError::Invalid);
    }
    while pos < ascii.len() && ascii_space(ascii[pos]) {
        pos += 1;
    }
    if pos != ascii.len() || nul.is_some() {
        return Err(IntError::Invalid);
    }
    Ok(PyInt::from_decimal(negative, &digits))
}

// ---- NFC ----

const S_BASE: u32 = 0xac00;
const L_BASE: u32 = 0x1100;
const V_BASE: u32 = 0x1161;
const T_BASE: u32 = 0x11a7;
const L_COUNT: u32 = 19;
const V_COUNT: u32 = 21;
const T_COUNT: u32 = 28;
const N_COUNT: u32 = V_COUNT * T_COUNT;
const S_COUNT: u32 = L_COUNT * N_COUNT;

fn decompose_into(code: u32, out: &mut Vec<u32>) {
    if (S_BASE..S_BASE + S_COUNT).contains(&code) {
        let index = code - S_BASE;
        out.push(L_BASE + index / N_COUNT);
        out.push(V_BASE + (index % N_COUNT) / T_COUNT);
        if !index.is_multiple_of(T_COUNT) {
            out.push(T_BASE + index % T_COUNT);
        }
        return;
    }
    match tables::DECOMPOSITION.binary_search_by_key(&code, |entry| entry.0) {
        Ok(position) => {
            for &part in tables::DECOMPOSITION[position].1 {
                decompose_into(part, out);
            }
        }
        Err(_) => out.push(code),
    }
}

fn compose_pair(first: u32, second: u32) -> Option<u32> {
    if (L_BASE..L_BASE + L_COUNT).contains(&first) && (V_BASE..V_BASE + V_COUNT).contains(&second) {
        return Some(S_BASE + ((first - L_BASE) * V_COUNT + (second - V_BASE)) * T_COUNT);
    }
    if (S_BASE..S_BASE + S_COUNT).contains(&first)
        && (first - S_BASE).is_multiple_of(T_COUNT)
        && (T_BASE + 1..T_BASE + T_COUNT).contains(&second)
    {
        return Some(first + (second - T_BASE));
    }
    tables::COMPOSITION
        .binary_search_by_key(&(first, second), |entry| (entry.0, entry.1))
        .ok()
        .map(|position| tables::COMPOSITION[position].2)
}

/// `unicodedata.normalize('NFC', text)`。
pub fn nfc(text: &[u32]) -> Vec<u32> {
    let mut decomposed = Vec::with_capacity(text.len());
    for &c in text {
        decompose_into(c, &mut decomposed);
    }
    // 結合クラスが 0 でない連なりを、結合クラスの昇順に安定して並べ替える。
    let mut index = 0;
    while index < decomposed.len() {
        if combining(decomposed[index]) == 0 {
            index += 1;
            continue;
        }
        let start = index;
        while index < decomposed.len() && combining(decomposed[index]) != 0 {
            index += 1;
        }
        decomposed[start..index].sort_by_key(|&c| combining(c));
    }
    let mut out: Vec<u32> = Vec::with_capacity(decomposed.len());
    let mut starter: Option<usize> = None;
    let mut last_class = 0u8;
    for &c in &decomposed {
        let class = combining(c);
        if let Some(position) = starter {
            let blocked = out.len() - 1 > position && (last_class == 0 || last_class >= class);
            if !blocked && let Some(composed) = compose_pair(out[position], c) {
                out[position] = composed;
                continue;
            }
        }
        if class == 0 {
            starter = Some(out.len());
        }
        out.push(c);
        last_class = class;
    }
    out
}

/// `unicodedata.normalize('NFC', x) != x` の否定(NFC のままか)。
pub fn is_nfc(text: &[u32]) -> bool {
    nfc(text) == text
}

#[cfg(test)]
mod tests {
    use super::super::testing::{cps_of, fixture};
    use super::*;

    fn hex_list(text: &str) -> Vec<u32> {
        text.split_whitespace()
            .map(|part| u32::from_str_radix(part, 16).unwrap())
            .collect()
    }

    #[test]
    fn every_code_point_matches_python_3_10() {
        let expect = include_str!("../../../../../ci/s2e_fixtures/unicode_expect.txt");
        let mut space = vec![false; 0x110000];
        let mut decimal_value = vec![None; 0x110000];
        let mut nonprintable = vec![false; 0x110000];
        let mut combining_value = vec![0u8; 0x110000];
        let mut nfc_changes = vec![false; 0x110000];
        for line in expect.lines().filter(|l| !l.starts_with('#')) {
            let part: Vec<&str> = line.split(' ').collect();
            let start = usize::from_str_radix(part[1], 16).unwrap();
            let end = usize::from_str_radix(part[2], 16).unwrap();
            for code in start..=end {
                match part[0] {
                    "space" => space[code] = true,
                    "decimal" => decimal_value[code] = Some(part[3].parse::<u8>().unwrap()),
                    "nonprintable" => nonprintable[code] = true,
                    "combining" => combining_value[code] = part[3].parse().unwrap(),
                    "nfc_changes" => nfc_changes[code] = true,
                    other => panic!("未知の種類 {other}"),
                }
            }
        }
        for code in 0..0x110000u32 {
            let i = code as usize;
            assert_eq!(is_space(code), space[i], "space {code:x}");
            assert_eq!(decimal(code), decimal_value[i], "decimal {code:x}");
            assert_eq!(!is_printable(code), nonprintable[i], "printable {code:x}");
            assert_eq!(combining(code), combining_value[i], "combining {code:x}");
            assert_eq!(!is_nfc(&[code]), nfc_changes[i], "nfc {code:x}");
        }
    }

    #[test]
    fn nfc_sequences_match_python_3_10() {
        let cases = include_str!("../../../../../ci/s2e_fixtures/nfc_cases.txt");
        let mut count = 0;
        for line in cases.lines().filter(|l| !l.starts_with('#')) {
            let (input, output) = line.split_once(';').unwrap();
            let (input, output) = (hex_list(input), hex_list(output));
            assert_eq!(nfc(&input), output, "{line}");
            assert_eq!(is_nfc(&input), input == output, "{line}");
            count += 1;
        }
        assert!(count > 15000, "{count}");
    }

    #[test]
    fn nfc_covers_the_named_shapes() {
        // 基底文字と結合文字、結合クラスの逆順、合成除外、ハングル、NFC_QC が Maybe の文字。
        assert_eq!(nfc(&[0x41, 0x300]), [0xc0]);
        assert_eq!(nfc(&[0x65, 0x323, 0x301]), nfc(&[0x65, 0x301, 0x323]));
        assert_eq!(nfc(&[0x958]), [0x915, 0x93c]);
        assert_eq!(nfc(&[0x2126]), [0x3a9]);
        assert_eq!(nfc(&[0x1100, 0x1161, 0x11a8]), [0xac01]);
        assert_eq!(nfc(&[0xac00, 0x11a8]), [0xac01]);
        assert!(!is_nfc(&[0x41, 0x300]));
        assert!(is_nfc(&[0x300]));
        assert!(is_nfc(&[0xd800, 0x41]));
    }

    #[test]
    fn whitespace_helpers_use_the_python_definition() {
        let text = cps(" \u{3000}a\u{1c}b\u{85}\u{a0}c\u{200b}d ");
        let parts = split_whitespace(&text);
        assert_eq!(
            parts,
            vec![cps("a"), cps("b"), cps("c\u{200b}d")],
            "U+200B は空白でない"
        );
        assert_eq!(strip(&text), cps("a\u{1c}b\u{85}\u{a0}c\u{200b}d"));
        assert!(strip(&cps(" \t")).is_empty());
        assert_eq!(truncate(&cps("abcdef"), 3), cps("abc"));
        assert_eq!(truncate(&cps("ab"), 300), cps("ab"));
        assert_eq!(
            replace_controls(&[0x41, 0, 0x1f, 0x7f, 0x80]),
            [0x41, 0x20, 0x20, 0x20, 0x80]
        );
        assert_eq!(rstrip_char(&cps("a//"), 0x2f), cps("a"));
        assert!(rstrip_char(&cps("///"), 0x2f).is_empty());
    }

    #[test]
    fn repr_matches_python() {
        let r = |text: &str| to_string(&repr(&cps(text)));
        assert_eq!(r("abc"), "'abc'");
        assert_eq!(r("it's"), "\"it's\"");
        assert_eq!(r("it's \"x\""), "'it\\'s \"x\"'");
        assert_eq!(r("a\nb\tc\\"), "'a\\nb\\tc\\\\'");
        assert_eq!(
            r("\u{0}\u{7f}\u{a0}\u{ad}\u{100}"),
            "'\\x00\\x7f\\xa0\\xad\u{100}'"
        );
        assert_eq!(
            to_string(&repr(&[0xdc80, 0x1f600, 0x10ffff, 0x378])),
            "'\\udc80😀\\U0010ffff\\u0378'"
        );
    }

    #[test]
    fn fs_encoding_round_trips_surrogateescape() {
        assert_eq!(decode_fs(b"a\xffb"), [0x61, 0xdcff, 0x62]);
        assert_eq!(decode_fs(b"\xed\xa0\x80"), [0xdced, 0xdca0, 0xdc80]);
        assert_eq!(encode_fs(&[0x61, 0xdcff]).unwrap(), b"a\xff");
        assert_eq!(
            encode_fs(&[0x61, 0xd800]),
            Err(EncodeFailure {
                start: 1,
                end: 2,
                text: vec![0xd800]
            })
        );
        assert_eq!(
            encode_fs(&[0xdc80, 0xd800, 0xdc80, 0x41]),
            Err(EncodeFailure {
                start: 1,
                end: 3,
                text: vec![0xd800, 0xdc80]
            })
        );
        assert_eq!(
            encode_backslashreplace(&[0x61, 0xdc80, 0xd800, 0x3042]),
            "a\\udc80\\ud800あ".as_bytes()
        );
        assert_eq!(encode_fs(&cps("あ😀")).unwrap(), "あ😀".as_bytes());
    }

    #[test]
    fn parse_int_follows_python_syntax() {
        let ok = |text: &str| parse_int(&cps(text), 4300).map(|v| v.to_decimal(0).unwrap());
        assert_eq!(ok("42").unwrap(), "42");
        assert_eq!(ok(" \t+7_000\n").unwrap(), "7000");
        assert_eq!(ok("-0").unwrap(), "0");
        assert_eq!(ok("007").unwrap(), "7");
        assert_eq!(ok("١٢٣").unwrap(), "123");
        assert_eq!(ok("\u{3000}１２\u{a0}").unwrap(), "12");
        for bad in [
            "", " ", "+", "_1", "1_", "1__0", "0x10", "1.0", "1e3", "١_", "- 1", "1 2", "\u{1c}1",
            "٣x", "1\u{0}",
        ] {
            assert_eq!(ok(bad), Err(IntError::Invalid), "{bad:?}");
        }
        let long = "9".repeat(641);
        assert!(parse_int(&cps(&long), 641).is_ok());
        assert_eq!(
            parse_int(&cps(&"9".repeat(4301)), 4300),
            Err(IntError::TooManyDigits {
                limit: 4300,
                digits: 4301
            })
        );
        assert!(parse_int(&cps(&"9".repeat(4301)), 0).is_ok());
    }

    #[test]
    fn int_cases_match_python_3_10() {
        let cases = fixture!("int_cases.json");
        for case in cases.as_array().unwrap() {
            let text = cps_of(&case["text"]);
            let limit = case["limit"].as_u64().unwrap() as usize;
            match parse_int(&text, limit) {
                Ok(value) => assert_eq!(
                    value.to_decimal(0).unwrap(),
                    case["ok"].as_str().unwrap(),
                    "{:?}",
                    case["text"]
                ),
                Err(error) => assert_eq!(
                    to_string(error.exception(&text).message.as_codepoints()),
                    case["error"].as_str().unwrap(),
                    "{:?}",
                    case["text"]
                ),
            }
        }
    }

    #[test]
    fn repr_cases_match_python_3_10() {
        for case in fixture!("repr_cases.json").as_array().unwrap() {
            assert_eq!(
                repr(&cps_of(&case["text"])),
                cps_of(&case["repr"]),
                "{case}"
            );
        }
    }

    #[test]
    fn space_cases_match_python_3_10() {
        for case in fixture!("space_cases.json").as_array().unwrap() {
            let text = cps_of(&case["text"]);
            let split: Vec<Vec<u32>> = case["split"]
                .as_array()
                .unwrap()
                .iter()
                .map(cps_of)
                .collect();
            assert_eq!(split_whitespace(&text), split, "{case}");
            assert_eq!(strip(&text), cps_of(&case["strip"]), "{case}");
        }
    }
}
