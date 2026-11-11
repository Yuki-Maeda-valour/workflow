//! Python の filesystem encoding + surrogateescape と JSON の表記。
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;

pub fn raw(value: &OsStr) -> &[u8] {
    value.as_bytes()
}

/// 無効な UTF-8 の各バイトは U+DC80..U+DCFF として並べる。
pub fn characters(bytes: &[u8]) -> Vec<u32> {
    let mut result = Vec::new();
    let mut rest = bytes;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(text) => {
                result.extend(text.chars().map(u32::from));
                break;
            }
            Err(error) => {
                let (valid, invalid) = rest.split_at(error.valid_up_to());
                result.extend(std::str::from_utf8(valid).unwrap().chars().map(u32::from));
                let length = error.error_len().unwrap_or(invalid.len());
                result.extend(
                    invalid[..length]
                        .iter()
                        .map(|&byte| 0xdc00 + u32::from(byte)),
                );
                rest = &invalid[length..];
            }
        }
    }
    result
}

pub fn json(bytes: &[u8]) -> Vec<u8> {
    let mut result = vec![b'"'];
    for &byte in bytes {
        match byte {
            b'"' => result.extend_from_slice(b"\\\""),
            b'\\' => result.extend_from_slice(b"\\\\"),
            b'\n' => result.extend_from_slice(b"\\n"),
            b'\r' => result.extend_from_slice(b"\\r"),
            b'\t' => result.extend_from_slice(b"\\t"),
            8 => result.extend_from_slice(b"\\b"),
            12 => result.extend_from_slice(b"\\f"),
            0..=31 => result.extend_from_slice(format!("\\u{byte:04x}").as_bytes()),
            _ => result.push(byte),
        }
    }
    result.push(b'"');
    result
}

/// Python の repr(str)。引用符は内容から選び、surrogate を文字列として残す。
pub fn repr(bytes: &[u8]) -> Vec<u8> {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut result = vec![quote];
    for code in characters(bytes) {
        match code {
            10 => result.extend_from_slice(b"\\n"),
            13 => result.extend_from_slice(b"\\r"),
            9 => result.extend_from_slice(b"\\t"),
            92 => result.extend_from_slice(b"\\\\"),
            _ if code == u32::from(quote) => result.extend_from_slice(&[b'\\', quote]),
            _ if !super::unicode::printable(code) => {
                let escaped = if code <= 0xff {
                    format!("\\x{code:02x}")
                } else if code <= 0xffff {
                    format!("\\u{code:04x}")
                } else {
                    format!("\\U{code:08x}")
                };
                result.extend_from_slice(escaped.as_bytes());
            }
            _ => {
                let mut buffer = [0; 4];
                result.extend_from_slice(
                    char::from_u32(code)
                        .unwrap()
                        .encode_utf8(&mut buffer)
                        .as_bytes(),
                );
            }
        }
    }
    result.push(quote);
    result
}

pub fn concat(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// Python の標準エラーは surrogate を backslashreplace で書く。
pub fn stderr(bytes: &[u8]) -> Vec<u8> {
    let mut result = Vec::new();
    for code in characters(bytes) {
        if (0xdc80..=0xdcff).contains(&code) {
            result.extend_from_slice(format!("\\u{code:04x}").as_bytes());
        } else {
            let mut buffer = [0; 4];
            result.extend_from_slice(
                char::from_u32(code)
                    .unwrap()
                    .encode_utf8(&mut buffer)
                    .as_bytes(),
            );
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surrogate_order_and_json_preserve_bytes() {
        assert!(characters(b"\xff") < characters("\u{e000}".as_bytes()));
        assert_eq!(characters(b"\xed\xa0\x80"), [0xdced, 0xdca0, 0xdc80]);
        assert_eq!(json(b"a\xff\n\0\"\\"), b"\"a\xff\\n\\u0000\\\"\\\\\"");
        assert_eq!(repr(b"x\xff\n'"), b"\"x\\udcff\\n'\"");
    }
}
