//! Byte-preserving shell fields. Only diagnostic display copies are sanitized.
use std::ffi::{OsStr, OsString};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
pub fn os(value: &[u8]) -> OsString {
    OsString::from_vec(value.to_vec())
}
pub fn raw(value: &OsStr) -> &[u8] {
    value.as_bytes()
}
pub fn trim_lf(mut value: Vec<u8>) -> Vec<u8> {
    while value.last() == Some(&b'\n') {
        value.pop();
    }
    value
}
/// gnulib last_component: the start of the final name after leading slashes.
fn last_component(name: &[u8]) -> usize {
    let mut base = name.iter().take_while(|b| **b == b'/').count();
    let mut last_was_slash = false;
    for (i, b) in name.iter().enumerate().skip(base) {
        if *b == b'/' {
            last_was_slash = true;
        } else if last_was_slash {
            base = i;
            last_was_slash = false;
        }
    }
    base
}
/// gnulib base_len: length without trailing slashes (at least one byte).
fn base_len(name: &[u8]) -> usize {
    let mut len = name.len();
    while len > 1 && name[len - 1] == b'/' {
        len -= 1;
    }
    len
}
/// `"$(dirname -- PATH)"` with GNU coreutils, including the removal of
/// trailing newlines by command substitution.
pub fn dirname(path: &[u8]) -> Vec<u8> {
    let prefix = usize::from(path.first() == Some(&b'/'));
    let mut length = last_component(path);
    while prefix < length && path[length - 1] == b'/' {
        length -= 1;
    }
    let value = if length == 0 {
        b".".as_slice()
    } else {
        &path[..length]
    };
    trim_lf(value.to_vec())
}
/// `"$(basename -- PATH)"` with GNU coreutils and no suffix.
pub fn basename(path: &[u8]) -> Vec<u8> {
    let base = last_component(path);
    let name = if base == path.len() {
        path[..base_len(path)].to_vec()
    } else {
        let tail = &path[base..];
        let mut length = base_len(tail);
        if tail.get(length) == Some(&b'/') {
            length += 1;
        }
        tail[..length].to_vec()
    };
    // strip_trailing_slashes
    let start = last_component(&name);
    let start = if start == name.len() { 0 } else { start };
    let end = start + base_len(&name[start..]);
    trim_lf(name[..end].to_vec())
}
pub fn zrecords(value: &[u8]) -> Vec<Vec<u8>> {
    value
        .split_inclusive(|b| *b == 0)
        .filter_map(|v| v.strip_suffix(&[0]).map(<[u8]>::to_vec))
        .collect()
}
pub fn joined(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}
pub fn lower(value: &[u8]) -> Vec<u8> {
    value.iter().map(u8::to_ascii_lowercase).collect()
}
pub fn sanitize(value: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < value.len() {
        if value[i] == 0xc2 && value.get(i + 1).is_some_and(|b| (0x80..=0x9f).contains(b)) {
            out.push(b'?');
            i += 2;
        } else {
            out.push(if value[i] < 32 || value[i] == 127 {
                b'?'
            } else {
                value[i]
            });
            i += 1;
        }
    }
    out
}
pub fn display_value(value: &[u8]) -> Vec<u8> {
    let value = sanitize(value);
    let mut i = 0;
    let mut count = 0;
    while i < value.len() && count < 200 {
        i += match value[i] {
            0xc2..=0xdf => 2,
            0xe0..=0xef => 3,
            0xf0..=0xf4 => 4,
            _ => 1,
        };
        count += 1;
    }
    value[..i.min(value.len())].to_vec()
}
pub fn path_field(value: &[u8]) -> Vec<u8> {
    if !value
        .iter()
        .any(|b| *b < 32 || *b == 127 || *b == b'"' || *b == b'\\')
    {
        return value.to_vec();
    }
    let mut out = vec![b'"'];
    for b in value {
        match b {
            b'"' => out.extend(b"\\\""),
            b'\\' => out.extend(b"\\\\"),
            7 => out.extend(b"\\a"),
            8 => out.extend(b"\\b"),
            9 => out.extend(b"\\t"),
            10 => out.extend(b"\\n"),
            11 => out.extend(b"\\v"),
            12 => out.extend(b"\\f"),
            13 => out.extend(b"\\r"),
            0..=31 | 127 => out.extend(format!("\\{b:03o}").as_bytes()),
            _ => out.push(*b),
        }
    }
    out.push(b'"');
    out
}
pub fn section_line(out: &mut Vec<u8>, path: &[u8], fields: &[&[u8]]) {
    out.extend(path_field(path));
    for field in fields {
        out.push(b'\t');
        out.extend(*field);
    }
    out.push(b'\n');
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn display_vectors() {
        for c in 1..=31 {
            assert_eq!(sanitize(&[c]), b"?");
        }
        assert_eq!(sanitize(&[127]), b"?");
        for c in 0x80..=0x9f {
            assert_eq!(sanitize(&[0xc2, c]), b"?");
        }
        for s in ["日本語", "é", "€", "🙂"] {
            assert_eq!(sanitize(s.as_bytes()), s.as_bytes());
        }
        for n in [199, 200, 201] {
            assert_eq!(
                display_value("あ".repeat(n).as_bytes()),
                "あ".repeat(n.min(200)).as_bytes()
            );
        }
        let v = ["a".repeat(198).as_bytes(), &[0xc2, 0x80, 0xc2, 0x9f, b'z']].concat();
        assert_eq!(
            display_value(&v),
            ["a".repeat(198).as_bytes(), b"??"].concat()
        );
    }
    #[test]
    fn c_quotes_preserve_all_bytes() {
        assert_eq!(path_field(b"a\n\t\xff\\\""), b"\"a\\n\\t\xff\\\\\\\"\"");
    }
}
