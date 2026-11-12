//! S2Lの文字化はUTF-8+surrogateescape固定。整数設定とbufferingは旧版どおり。
use super::{
    error::{LegacyException, Result},
    json::{self, Text},
};
use std::{
    ffi::{OsStr, OsString},
    io::IsTerminal,
    os::unix::ffi::{OsStrExt, OsStringExt},
};
#[derive(Clone)]
pub struct LegacyRuntime {
    pub int_limit: usize,
    pub unbuffered: bool,
    pub stdout_terminal: bool,
}
/// 旧CPythonの起動の失敗。標準エラーへ出す全文(生バイト)と終了値1。
#[derive(Debug)]
pub struct StartupFailure(pub Vec<u8>);
const PREINITIALIZED: &str = "Python runtime state: preinitialized\n\n";
/// PYTHONHASHSEED を CPython3.10 の config_init_hash_seed(strtoul)と同じく読む。
fn hash_seed_valid(raw: &[u8]) -> bool {
    if raw.is_empty() || raw == b"random" {
        return true;
    }
    let mut rest = raw;
    while let [first, tail @ ..] = rest
        && matches!(first, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
    {
        rest = tail;
    }
    let negative = rest.first() == Some(&b'-');
    if matches!(rest.first(), Some(b'+' | b'-')) {
        rest = &rest[1..];
    }
    if rest.is_empty() || !rest.iter().all(u8::is_ascii_digit) {
        // 数字が無いか、数字の後に残りがある(*endptr != '\0')。
        return false;
    }
    let mut value: u64 = 0;
    for digit in rest {
        let Some(next) = value
            .checked_mul(10)
            .and_then(|v| v.checked_add(u64::from(digit - b'0')))
        else {
            // unsigned long に収まらないときだけ ERANGE(ULONG_MAX)になり、範囲の外。
            return false;
        };
        value = next;
    }
    // strtoul は負の値を unsigned long の上で折り返す(-x は 2^64 - x)。
    // -18446744069414584321〜-18446744073709551615 は 4294967295〜1 になり、範囲内に入る。
    let value = if negative {
        value.wrapping_neg()
    } else {
        value
    };
    value <= 4294967295
}
fn stdio_failure(lines: &str) -> StartupFailure {
    let thread = dev_workflow_platform::loop_state::signal::current_thread();
    StartupFailure(
        format!(
            "Fatal Python error: init_sys_streams: {lines}\n\nCurrent thread 0x{thread:016x} (most recent call first):\n  <no Python frame>\n"
        )
        .into_bytes(),
    )
}
impl LegacyRuntime {
    /// 旧CPythonの起動の順(PYTHONHASHSEED → PYTHONINTMAXSTRDIGITS → 標準入力 →
    /// 標準出力 → 標準エラー)で検査する。文字コードの設定は読まない(12:39 の承認)。
    pub fn from_environment() -> std::result::Result<Self, StartupFailure> {
        if let Some(seed) = std::env::var_os("PYTHONHASHSEED")
            && !hash_seed_valid(seed.as_bytes())
        {
            return Err(StartupFailure(
                format!(
                    "Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\n{PREINITIALIZED}"
                )
                .into_bytes(),
            ));
        }
        let invalid = || {
            StartupFailure(
                format!(
                    "Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\n{PREINITIALIZED}"
                )
                .into_bytes(),
            )
        };
        let int_limit = match std::env::var_os("PYTHONINTMAXSTRDIGITS") {
            Some(v) if !v.is_empty() => {
                let s = v.to_str().ok_or_else(invalid)?;
                // strtol は C の isspace(" \t\n\v\f\r")を先頭から飛ばす(\v も含む)。
                let s = s.trim_start_matches([' ', '\t', '\n', '\x0b', '\x0c', '\r']);
                let negative = s.starts_with('-');
                let digits = s
                    .strip_prefix('+')
                    .or_else(|| s.strip_prefix('-'))
                    .unwrap_or(s);
                if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
                    return Err(invalid());
                }
                let n = digits.parse::<u32>().map_err(|_| invalid())?;
                if (negative && n != 0) || n > i32::MAX as u32 || (n != 0 && n < 640) {
                    return Err(invalid());
                }
                n as usize
            }
            _ => 4300,
        };
        use dev_workflow_platform::loop_state::signal::is_directory;
        if is_directory(0) {
            return Err(stdio_failure(
                "<stdin> is a directory, cannot continue\nPython runtime state: core initialized",
            ));
        }
        for fd in [1, 2] {
            if is_directory(fd) {
                return Err(stdio_failure(&format!(
                    "can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: {fd}"
                )));
            }
        }
        let unbuffered = std::env::var_os("PYTHONUNBUFFERED").is_some_and(|v| {
            let s = v.as_bytes();
            if s.is_empty() {
                return false;
            }
            let start = s
                .iter()
                .position(|c| !matches!(c, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r'))
                .unwrap_or(s.len());
            let s = &s[start..];
            let s = s
                .strip_prefix(b"+")
                .or_else(|| s.strip_prefix(b"-"))
                .unwrap_or(s);
            s.is_empty() || !s.iter().all(u8::is_ascii_digit) || s.iter().any(|c| *c != b'0')
        });
        Ok(Self {
            int_limit,
            unbuffered,
            stdout_terminal: std::io::stdout().is_terminal(),
        })
    }
}
#[cfg(test)]
mod tests {
    #[test]
    fn hash_seed_follows_strtoul_and_range() {
        for (raw, valid) in [
            ("", true),
            ("random", true),
            ("0", true),
            ("4294967295", true),
            (" 12", true),
            ("\t+5", true),
            ("-0", true),
            ("00", true),
            ("4294967296", false),
            ("-1", false),
            ("12 ", false),
            ("0x10", false),
            ("1_0", false),
            ("+", false),
            (" ", false),
            ("Random", false),
            ("99999999999999999999999", false),
            ("-18446744073709551615", true),
            ("-18446744069414584321", true),
            ("-18446744069414584320", false),
            ("-18446744073709551616", false),
            ("18446744073709551615", false),
        ] {
            assert_eq!(super::hash_seed_valid(raw.as_bytes()), valid, "{raw:?}");
        }
    }
}
pub fn decode(raw: &[u8]) -> Text {
    let mut result = Vec::new();
    let mut rest = raw;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                result.extend(s.chars().map(u32::from));
                break;
            }
            Err(e) => {
                let (valid, invalid) = rest.split_at(e.valid_up_to());
                result.extend(std::str::from_utf8(valid).unwrap().chars().map(u32::from));
                let n = e.error_len().unwrap_or(invalid.len());
                result.extend(invalid[..n].iter().map(|&c| 0xdc00 + u32::from(c)));
                rest = &invalid[n..]
            }
        }
    }
    json::text(result)
}
/// bytes.strip()。空白は b" \t\n\r\x0b\x0c"(trim_ascii と違い \x0b も除く)。
pub fn py_bytes_strip(raw: &[u8]) -> &[u8] {
    let space = |c: &u8| matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c);
    let start = raw.iter().position(|c| !space(c)).unwrap_or(raw.len());
    let end = raw.iter().rposition(|c| !space(c)).map_or(start, |i| i + 1);
    &raw[start..end.max(start)]
}
/// str.isspace() の文字(char::is_whitespace と違い \x1c〜\x1f も含む)。
pub fn py_isspace(c: u32) -> bool {
    matches!(c, 9..=13 | 28..=32 | 0x85 | 0xa0 | 0x1680 | 0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000)
}
/// str.strip() を文字番号の上で行う。
pub fn py_text_strip(value: &Text) -> Text {
    let codes = value.as_codepoints();
    let start = codes
        .iter()
        .position(|c| !py_isspace(*c))
        .unwrap_or(codes.len());
    let end = codes
        .iter()
        .rposition(|c| !py_isspace(*c))
        .map_or(start, |i| i + 1);
    json::text(codes[start..end.max(start)].to_vec())
}
pub fn strict_utf8(raw: &[u8]) -> Result<&str> {
    std::str::from_utf8(raw).map_err(|e| {
        let start = e.valid_up_to();
        let end = start + e.error_len().unwrap_or(raw.len() - start);
        let reason = if e.error_len().is_none() {
            "unexpected end of data"
        } else if matches!(raw[start],0x80..=0xc1|0xf5..=0xff) {
            "invalid start byte"
        } else {
            "invalid continuation byte"
        };
        let detail = if end == start + 1 {
            format!("byte 0x{:02x} in position {start}", raw[start])
        } else {
            format!("bytes in position {start}-{}", end - 1)
        };
        LegacyException::Python {
            kind: "UnicodeDecodeError",
            reason: format!("'utf-8' codec can't decode {detail}: {reason}"),
            location: "decode",
        }
    })
}
pub fn strict_ascii(raw: &[u8]) -> Result<()> {
    if let Some((at, b)) = raw.iter().enumerate().find(|(_, b)| **b >= 128) {
        Err(LegacyException::Python {
            kind: "UnicodeDecodeError",
            reason: format!(
                "'ascii' codec can't decode byte 0x{b:02x} in position {at}: ordinal not in range(128)"
            ),
            location: "decode",
        })
    } else {
        Ok(())
    }
}
pub fn bytes(value: &Text) -> Result<Vec<u8>> {
    encode(value, "utf-8", "surrogateescape")
}
pub fn os(value: &Text) -> Result<OsString> {
    Ok(OsString::from_vec(bytes(value)?))
}
pub fn os_text(value: &OsStr) -> Text {
    decode(value.as_bytes())
}
pub fn utf8(value: &Text) -> String {
    value
        .as_codepoints()
        .iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}
pub fn encode(value: &Text, _encoding: &str, errors: &str) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    for (index, &c) in value.as_codepoints().iter().enumerate() {
        if let Some(ch) = char::from_u32(c) {
            let mut buf = [0; 4];
            out.extend(ch.encode_utf8(&mut buf).as_bytes());
        } else if errors == "surrogateescape" && (0xdc80..=0xdcff).contains(&c) {
            out.push((c - 0xdc00) as u8)
        } else if errors == "backslashreplace" {
            out.extend(format!("\\u{c:04x}").as_bytes());
        } else {
            return Err(LegacyException::Python {
                kind: "UnicodeEncodeError",
                reason: {
                    let end = index
                        + value.as_codepoints()[index..]
                            .iter()
                            .take_while(|&&c| {
                                (0xd800..=0xdfff).contains(&c)
                                    && !(errors == "surrogateescape"
                                        && (0xdc80..=0xdcff).contains(&c))
                            })
                            .count();
                    if end > index + 1 {
                        format!(
                            "'utf-8' codec can't encode characters in position {index}-{}: surrogates not allowed",
                            end - 1
                        )
                    } else {
                        format!(
                            "'utf-8' codec can't encode character '\\u{c:04x}' in position {index}: surrogates not allowed"
                        )
                    }
                },
                location: "output",
            });
        }
    }
    Ok(out)
}
