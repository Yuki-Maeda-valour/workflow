use super::LegacyIntLimit;
pub(super) fn equals(v: &[u32], s: &str) -> bool {
    v.iter().copied().eq(s.chars().map(u32::from))
}
fn ascii(v: &[u32]) -> Option<String> {
    v.iter()
        .map(|&c| if c < 128 { Some(c as u8 as char) } else { None })
        .collect()
}
fn unsigned(v: &str) -> &str {
    v.strip_prefix(['+', '-']).unwrap_or(v)
}
fn digits(s: &str, base: u32, underscores: bool) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| (underscores && c == '_') || c.is_digit(base))
}
fn sexagesimal_int(s: &str) -> bool {
    let mut parts = s.split(':');
    let first = parts.next().unwrap_or("");
    if !first.starts_with(|c: char| ('1'..='9').contains(&c)) || !digits(first, 10, true) {
        return false;
    }
    let mut count = 0;
    for p in parts {
        if p.is_empty()
            || p.len() > 2
            || !digits(p, 10, false)
            || p.parse::<u32>().unwrap_or(60) >= 60
        {
            return false;
        }
        count += 1;
    }
    count > 0
}
fn implicit_int(s: &str) -> bool {
    let s = unsigned(s);
    if let Some(p) = s.strip_prefix("0b") {
        digits(p, 2, true)
    } else if let Some(p) = s.strip_prefix("0x") {
        digits(p, 16, true)
    } else if s.starts_with('0') && s.len() > 1 {
        digits(&s[1..], 8, true)
    } else {
        s == "0"
            || (s.starts_with(|c: char| ('1'..='9').contains(&c)) && digits(s, 10, true))
            || sexagesimal_int(s)
    }
}
fn implicit_float(s: &str) -> bool {
    let t = unsigned(s);
    if matches!(t, ".inf" | ".Inf" | ".INF") || matches!(s, ".nan" | ".NaN" | ".NAN") {
        return true;
    }
    let (mantissa, exponent) = match t.find(['e', 'E']) {
        Some(i) => (&t[..i], Some(&t[i + 1..])),
        None => (t, None),
    };
    if let Some(exp) = exponent
        && (!exp.starts_with(['+', '-']) || !digits(&exp[1..], 10, false))
    {
        return false;
    }
    let Some((a, b)) = mantissa.split_once('.') else {
        return false;
    };
    if b.contains('.') || !b.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return false;
    }
    if a.is_empty() {
        return !s.starts_with(['+', '-']) && b.starts_with(|c: char| c.is_ascii_digit());
    }
    if a.contains(':') {
        return exponent.is_none() && sexagesimal_int_allow_zero(a);
    }
    a.starts_with(|c: char| c.is_ascii_digit()) && digits(a, 10, true)
}
fn sexagesimal_int_allow_zero(s: &str) -> bool {
    let mut parts = s.split(':');
    let first = parts.next().unwrap_or("");
    first.starts_with(|c: char| c.is_ascii_digit())
        && digits(first, 10, true)
        && parts.all(|p| {
            !p.is_empty()
                && p.len() <= 2
                && digits(p, 10, false)
                && p.parse::<u32>().unwrap_or(60) < 60
        })
}
pub(super) fn resolve(value: &[u32]) -> &'static str {
    let Some(s) = ascii(value) else { return "str" };
    if s.starts_with('\n') {
        return "str";
    }
    // Python regex '$' permits one final LF, including a quoted plain-tag value.
    let full = s.as_str();
    let s = s.strip_suffix('\n').unwrap_or(&s);
    match s {
        "yes" | "Yes" | "YES" | "no" | "No" | "NO" | "true" | "True" | "TRUE" | "false"
        | "False" | "FALSE" | "on" | "On" | "ON" | "off" | "Off" | "OFF" => "bool",
        "" | "~" | "null" | "Null" | "NULL" => "null",
        "<<" => "merge",
        "=" => "value",
        "!" | "&" | "*" => "yaml",
        _ if implicit_float(s) => "float",
        _ if implicit_int(s) => "int",
        _ if timestamp(full, false).is_some() => "timestamp",
        _ => "str",
    }
}
// Python 3.10 Unicode 13 decimal digits. Supplementary digits are intentionally
// recognized; Rust's Unicode version and char::is_numeric are not substitutes.
const DECIMAL_ZEROES: &[u32] = &[
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
    0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
    0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
    0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650,
    0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16b50, 0x1d7ce,
    0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
];
fn decimal(c: u32) -> Option<u32> {
    DECIMAL_ZEROES.iter().find_map(|&z| {
        if (z..z + 10).contains(&c) {
            Some(c - z)
        } else {
            None
        }
    })
}
fn whitespace(c: u32) -> bool {
    matches!(c,9..=13|0x20|0x85|0xa0|0x1680|0x2000..=0x200a|0x2028|0x2029|0x202f|0x205f|0x3000)
}
fn trim(v: &[u32]) -> &[u32] {
    let start = v.iter().position(|&c| !whitespace(c)).unwrap_or(v.len());
    let end = v
        .iter()
        .rposition(|&c| !whitespace(c))
        .map_or(start, |i| i + 1);
    &v[start..end]
}
fn int_part(v: &[u32], base: u32, limit: LegacyIntLimit) -> bool {
    let mut v = trim(v);
    if v.first().is_some_and(|c| [43, 45].contains(c)) {
        v = &v[1..];
    }
    if base != 10
        && v.len() >= 2
        && decimal(v[0]) == Some(0)
        && match base {
            2 => [66, 98].contains(&v[1]),
            8 => [79, 111].contains(&v[1]),
            16 => [88, 120].contains(&v[1]),
            _ => false,
        }
    {
        v = &v[2..];
    }
    !v.is_empty()
        && (base != 10 || limit.0 == 0 || v.len() <= limit.0 as usize)
        && v.iter().all(|&c| {
            decimal(c).is_some_and(|d| d < base) || (base == 16 && matches!(c,65..=70|97..=102))
        })
}
fn integer(v: &[u32], limit: LegacyIntLimit) -> bool {
    let v: Vec<_> = v.iter().copied().filter(|&c| c != 95).collect();
    let mut v = v.as_slice();
    if v.is_empty() {
        return false;
    }
    if matches!(v[0], 43 | 45) {
        v = &v[1..];
    }
    if v.is_empty() {
        return false;
    }
    if v == [48] {
        true
    } else if v.starts_with(&[48, 98]) {
        int_part(&v[2..], 2, limit)
    } else if v.starts_with(&[48, 120]) {
        int_part(&v[2..], 16, limit)
    } else if v[0] == 48 {
        int_part(v, 8, limit)
    } else if v.contains(&58) {
        v.split(|&c| c == 58).all(|p| int_part(p, 10, limit))
    } else {
        int_part(v, 10, limit)
    }
}
fn float_part(v: &[u32]) -> bool {
    let v = trim(v);
    let mut s = String::new();
    for &c in v {
        if let Some(d) = decimal(c) {
            s.push(char::from(b'0' + d as u8));
        } else if c < 128 {
            s.push((c as u8 as char).to_ascii_lowercase());
        } else {
            return false;
        }
    }
    let t = unsigned(&s);
    if matches!(t, "inf" | "infinity" | "nan") {
        return true;
    }
    if t.is_empty() {
        return false;
    }
    let (mantissa, exp) = match t.find('e') {
        Some(i) => (&t[..i], Some(&t[i + 1..])),
        None => (t, None),
    };
    if let Some(exp) = exp
        && !digits(unsigned(exp), 10, false)
    {
        return false;
    }
    let mut points = 0;
    let mut n = 0;
    for c in mantissa.chars() {
        if c == '.' {
            points += 1;
        } else if c.is_ascii_digit() {
            n += 1;
        } else {
            return false;
        }
    }
    points <= 1 && n > 0
}
fn float(v: &[u32]) -> bool {
    let v: Vec<_> = v
        .iter()
        .copied()
        .filter(|&c| c != 95)
        .map(|c| if matches!(c, 65..=90) { c + 32 } else { c })
        .collect();
    let mut v = v.as_slice();
    if v.first().is_some_and(|c| [43, 45].contains(c)) {
        v = &v[1..];
    }
    if equals(v, ".inf") || equals(v, ".nan") {
        return true;
    }
    if v.contains(&58) {
        let mut base = 1.0f64;
        for p in v.rsplit(|&c| c == 58) {
            if !base.is_finite() || !float_part(p) {
                return false;
            }
            base *= 60.0;
        }
        true
    } else {
        float_part(v)
    }
}
fn binary(v: &[u32]) -> bool {
    if v.iter().any(|&c| c > 127) {
        return false;
    }
    let mut quad = 0;
    let mut pads = 0;
    for &c in v {
        if matches!(c,65..=90|97..=122|48..=57|43|47) {
            quad = (quad + 1) % 4;
            pads = 0;
        } else if c == 61 && quad >= 2 {
            pads += 1;
            if quad + pads >= 4 {
                return true;
            }
        }
    }
    quad == 0
}
fn number(s: &[u8], i: &mut usize, min: usize, max: usize) -> Option<u32> {
    let start = *i;
    let mut n = 0;
    while *i < s.len() && *i - start < max && s[*i].is_ascii_digit() {
        n = n * 10 + u32::from(s[*i] - b'0');
        *i += 1;
    }
    (*i - start >= min).then_some(n)
}
fn take(s: &[u8], i: &mut usize, c: u8) -> Option<()> {
    if s.get(*i) == Some(&c) {
        *i += 1;
        Some(())
    } else {
        None
    }
}
/// Returns syntactic match and semantic validity separately (resolution must not
/// reject an invalid date before the constructor sees its timestamp tag).
fn timestamp(text: &str, explicit: bool) -> Option<bool> {
    let text = text.strip_suffix('\n').unwrap_or(text);
    let s = text.as_bytes();
    let mut i = 0;
    let year = number(s, &mut i, 4, 4)?;
    take(s, &mut i, b'-')?;
    let month_start = i;
    let month = number(s, &mut i, 1, 2)?;
    let month_len = i - month_start;
    take(s, &mut i, b'-')?;
    let day_start = i;
    let day = number(s, &mut i, 1, 2)?;
    let day_len = i - day_start;
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let maxday = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if leap {
                29
            } else {
                28
            }
        }
        _ => 0,
    };
    let mut valid = year > 0 && day > 0 && day <= maxday;
    if i == s.len() {
        return if explicit || month_len == 2 && day_len == 2 {
            Some(valid)
        } else {
            None
        };
    }
    if matches!(s.get(i), Some(b'T' | b't')) {
        i += 1;
    } else {
        let start = i;
        while matches!(s.get(i), Some(b' ' | b'\t')) {
            i += 1;
        }
        if i == start {
            return None;
        }
    }
    let hour = number(s, &mut i, 1, 2)?;
    take(s, &mut i, b':')?;
    let minute = number(s, &mut i, 2, 2)?;
    take(s, &mut i, b':')?;
    let second = number(s, &mut i, 2, 2)?;
    valid &= hour < 24 && minute < 60 && second < 60;
    if s.get(i) == Some(&b'.') {
        i += 1;
        while s.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
    }
    if i < s.len() {
        while matches!(s.get(i), Some(b' ' | b'\t')) {
            i += 1;
        }
        if s.get(i) == Some(&b'Z') {
            i += 1;
        } else {
            if !matches!(s.get(i), Some(b'+' | b'-')) {
                return None;
            }
            i += 1;
            let h = number(s, &mut i, 1, 2)?;
            let m = if s.get(i) == Some(&b':') {
                i += 1;
                number(s, &mut i, 2, 2)?
            } else {
                0
            };
            valid &= h * 60 + m < 1440;
        }
    }
    (i == s.len()).then_some(valid)
}
pub(super) fn validate(tag: &str, v: &[u32], limit: LegacyIntLimit) -> bool {
    match tag {
        "null" => true,
        "str" => true,
        "bool" => ascii(v).is_some_and(|s| {
            matches!(
                s.to_ascii_lowercase().as_str(),
                "yes" | "no" | "true" | "false" | "on" | "off"
            )
        }),
        "int" => integer(v, limit),
        "float" => float(v),
        "binary" => binary(v),
        "timestamp" => ascii(v).is_some_and(|s| timestamp(&s, true) == Some(true)),
        _ => false,
    }
}
