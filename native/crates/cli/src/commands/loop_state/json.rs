// CPython 3.10 call-order adaptation; see CPYTHON-LICENSE in this directory.
//! Python JSON値。文字番号・任意精度整数・非有限数を失わない。
use dev_workflow_core::loop_state::CodepointString;
use std::cmp::Ordering;
pub type Text = CodepointString;
pub fn text(codes: Vec<u32>) -> Text {
    Text::try_from_codepoints(codes).unwrap()
}
pub fn points(value: &str) -> Text {
    Text::from(value)
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PyInteger {
    negative: bool,
    digits: String,
}
impl PyInteger {
    pub fn parse(value: &str, limit: usize) -> Result<Self, ()> {
        let value =
            value.trim_matches(|c: char| c.is_whitespace() && !matches!(c, '\u{1c}'..='\u{1f}'));
        let (negative, value) = if let Some(v) = value.strip_prefix('-') {
            (true, v)
        } else {
            (false, value.strip_prefix('+').unwrap_or(value))
        };
        let mut digits = String::new();
        let mut previous = false;
        for c in value.chars() {
            if c == '_' && previous {
                previous = false;
                continue;
            }
            let d = decimal(c as u32).ok_or(())?;
            digits.push(char::from(b'0' + d));
            previous = true;
        }
        if !previous || (limit != 0 && digits.len() > limit) {
            return Err(());
        }
        let digits = digits.trim_start_matches('0');
        Ok(Self {
            negative: negative && !digits.is_empty(),
            digits: if digits.is_empty() {
                "0".into()
            } else {
                digits.into()
            },
        })
    }
    pub fn positive(&self) -> bool {
        !self.negative && self.digits != "0"
    }
    pub fn cmp_u128(&self, other: u128) -> Ordering {
        if self.negative {
            return Ordering::Less;
        }
        let other = other.to_string();
        self.digits
            .len()
            .cmp(&other.len())
            .then_with(|| self.digits.cmp(&other))
    }
    pub fn compare(&self, other: &Self) -> Ordering {
        match (self.negative, other.negative) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            _ => {
                let order = self
                    .digits
                    .len()
                    .cmp(&other.digits.len())
                    .then_with(|| self.digits.cmp(&other.digits));
                if self.negative {
                    order.reverse()
                } else {
                    order
                }
            }
        }
    }
    pub fn to_f64(&self) -> f64 {
        self.to_string().parse().unwrap_or(if self.negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        })
    }
    pub fn to_usize(&self) -> Option<usize> {
        if self.negative {
            None
        } else {
            self.digits.parse().ok()
        }
    }
    fn equals_float(&self, value: f64) -> bool {
        if !value.is_finite()
            || value.fract() != 0.0
            || (value.is_sign_negative() && value != 0.0) != self.negative
        {
            return false;
        }
        let bits = value.abs().to_bits();
        let exponent = ((bits >> 52) & 2047) as i32 - 1023;
        let significand = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
        let mut digits = if exponent < 0 {
            "0".into()
        } else if exponent <= 52 {
            (significand >> (52 - exponent)).to_string()
        } else {
            significand.to_string()
        };
        for _ in 52..exponent {
            let mut carry = 0;
            let mut raw = digits.into_bytes();
            for d in raw.iter_mut().rev() {
                let v = (*d - b'0') * 2 + carry;
                *d = b'0' + v % 10;
                carry = v / 10;
            }
            if carry != 0 {
                raw.insert(0, b'0' + carry);
            }
            digits = String::from_utf8(raw).unwrap();
        }
        digits == self.digits
    }
}
impl From<i128> for PyInteger {
    fn from(value: i128) -> Self {
        Self::parse(&value.to_string(), 0).unwrap()
    }
}
impl std::fmt::Display for PyInteger {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.negative {
            f.write_str("-")?;
        }
        f.write_str(&self.digits)
    }
}
#[path = "../r4b_support/unicode.rs"]
mod unicode;
pub fn decimal(code: u32) -> Option<u8> {
    unicode::decimal(code)
}
pub fn printable(code: u32) -> bool {
    unicode::printable(code)
}
#[derive(Clone, Debug)]
pub enum PyValue {
    Null,
    Bool(bool),
    Integer(PyInteger),
    Float(f64),
    String(Text),
    List(Vec<PyValue>),
    Dict(Vec<(Text, PyValue)>),
}
impl PartialEq for PyValue {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Null, Self::Null) => true,
            (Self::Bool(a), Self::Bool(b)) => a == b,
            (Self::Integer(a), Self::Integer(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a == b,
            (Self::Integer(a), Self::Float(b)) | (Self::Float(b), Self::Integer(a)) => {
                a.equals_float(*b)
            }
            (Self::Bool(a), Self::Integer(b)) | (Self::Integer(b), Self::Bool(a)) => {
                *b == PyInteger::from(i128::from(*a))
            }
            (Self::Bool(a), Self::Float(b)) | (Self::Float(b), Self::Bool(a)) => {
                *b == f64::from(*a)
            }
            (Self::String(a), Self::String(b)) => a == b,
            (Self::List(a), Self::List(b)) => {
                a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.container_equal(y))
            }
            (Self::Dict(a), Self::Dict(b)) => {
                a.len() == b.len()
                    && a.iter().all(|(k, v)| {
                        b.iter()
                            .find(|(key, _)| key == k)
                            .is_some_and(|(_, x)| v.container_equal(x))
                    })
            }
            _ => false,
        }
    }
}
impl From<&str> for PyValue {
    fn from(value: &str) -> Self {
        Self::String(points(value))
    }
}
impl From<String> for PyValue {
    fn from(value: String) -> Self {
        Self::from(value.as_str())
    }
}
impl From<Text> for PyValue {
    fn from(value: Text) -> Self {
        Self::String(value)
    }
}
impl From<i128> for PyValue {
    fn from(value: i128) -> Self {
        Self::Integer(value.into())
    }
}
impl From<bool> for PyValue {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}
impl PyValue {
    pub fn python_type(&self) -> &'static str {
        match self {
            Self::Null => "NoneType",
            Self::Bool(_) => "bool",
            Self::Integer(_) => "int",
            Self::Float(_) => "float",
            Self::String(_) => "str",
            Self::List(_) => "list",
            Self::Dict(_) => "dict",
        }
    }
    fn container_equal(&self, other: &Self) -> bool {
        matches!((self,other),(Self::Float(a),Self::Float(b))if a.is_nan()&&b.is_nan())
            || self == other
    }
    pub fn object(rows: impl IntoIterator<Item = (impl AsRef<str>, Self)>) -> Self {
        Self::Dict(
            rows.into_iter()
                .map(|(k, v)| (points(k.as_ref()), v))
                .collect(),
        )
    }
    pub fn get(&self, key: &str) -> &Self {
        static NONE: PyValue = PyValue::Null;
        self.get_opt(key).unwrap_or(&NONE)
    }
    pub fn get_opt(&self, key: &str) -> Option<&Self> {
        let Self::Dict(rows) = self else { return None };
        rows.iter()
            .find(|(k, _)| {
                k.as_codepoints()
                    .iter()
                    .copied()
                    .eq(key.chars().map(u32::from))
            })
            .map(|(_, v)| v)
    }
    pub fn get_mut(&mut self, key: &str) -> Option<&mut Self> {
        let Self::Dict(rows) = self else { return None };
        rows.iter_mut()
            .find(|(k, _)| {
                k.as_codepoints()
                    .iter()
                    .copied()
                    .eq(key.chars().map(u32::from))
            })
            .map(|(_, v)| v)
    }
    pub fn set(&mut self, key: &str, value: Self) {
        if let Some(old) = self.get_mut(key) {
            *old = value;
        } else if let Self::Dict(rows) = self {
            rows.push((points(key), value));
        }
    }
    pub fn remove(&mut self, key: &str) {
        if let Self::Dict(rows) = self {
            rows.retain(|(k, _)| *k != points(key));
        }
    }
    pub fn list(&self) -> Option<&[Self]> {
        if let Self::List(v) = self {
            Some(v)
        } else {
            None
        }
    }
    pub fn list_mut(&mut self) -> Option<&mut Vec<Self>> {
        if let Self::List(v) = self {
            Some(v)
        } else {
            None
        }
    }
    pub fn string(&self) -> Option<&Text> {
        if let Self::String(v) = self {
            Some(v)
        } else {
            None
        }
    }
    pub fn is_dict(&self) -> bool {
        matches!(self, Self::Dict(_))
    }
    pub fn truth(&self) -> bool {
        match self {
            Self::Null => false,
            Self::Bool(v) => *v,
            Self::Integer(v) => v.digits != "0",
            Self::Float(v) => *v != 0.0,
            Self::String(v) => !v.as_codepoints().is_empty(),
            Self::List(v) => !v.is_empty(),
            Self::Dict(v) => !v.is_empty(),
        }
    }
    pub fn is(&self, s: &str) -> bool {
        self == &Self::from(s)
    }
}
pub fn quote(value: &Text) -> String {
    let mut out = String::from("\"");
    for &c in value.as_codepoints() {
        match c {
            34 => out.push_str("\\\""),
            92 => out.push_str("\\\\"),
            8 => out.push_str("\\b"),
            9 => out.push_str("\\t"),
            10 => out.push_str("\\n"),
            12 => out.push_str("\\f"),
            13 => out.push_str("\\r"),
            32..=126 => out.push(char::from_u32(c).unwrap()),
            0..=65535 => out.push_str(&format!("\\u{c:04x}")),
            _ => {
                let x = c - 65536;
                out.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (x >> 10),
                    0xdc00 + (x & 1023)
                ));
            }
        }
    }
    out.push('"');
    out
}
pub fn dump(value: &PyValue) -> String {
    match value {
        PyValue::Null => "null".into(),
        PyValue::Bool(v) => v.to_string(),
        PyValue::Integer(v) => v.to_string(),
        PyValue::Float(v) => {
            if v.is_nan() {
                "NaN".into()
            } else if *v == f64::INFINITY {
                "Infinity".into()
            } else if *v == f64::NEG_INFINITY {
                "-Infinity".into()
            } else {
                let mut s = serde_json::to_string(v).unwrap();
                if *v != 0.0 && (v.abs() < 1e-4 || v.abs() >= 1e16) && !s.contains('e') {
                    let negative = s.starts_with('-');
                    let unsigned = s.trim_start_matches('-');
                    let point = unsigned.find('.').unwrap_or(unsigned.len());
                    let digits = unsigned.bytes().filter(|c| *c != b'.').collect::<Vec<_>>();
                    let first = digits.iter().position(|c| *c != b'0').unwrap();
                    let exponent = point as isize - first as isize - 1;
                    let mut coefficient = String::from_utf8(digits[first..].to_vec()).unwrap();
                    while coefficient.len() > 1 && coefficient.ends_with('0') {
                        coefficient.pop();
                    }
                    if coefficient.len() > 1 {
                        coefficient.insert(1, '.');
                    }
                    s = format!(
                        "{}{}e{exponent}",
                        if negative { "-" } else { "" },
                        coefficient
                    );
                }

                if let Some(i) = s.find('e') {
                    let exponent = s[i + 1..].parse::<i32>().unwrap();
                    s = format!(
                        "{}e{}{:02}",
                        &s[..i],
                        if exponent < 0 { "-" } else { "+" },
                        exponent.abs()
                    );
                }
                s
            }
        }
        PyValue::String(v) => quote(v),
        PyValue::List(v) => format!("[{}]", v.iter().map(dump).collect::<Vec<_>>().join(",")),
        PyValue::Dict(v) => {
            let mut rows = v.iter().collect::<Vec<_>>();
            rows.sort_by(|(a, _), (b, _)| a.as_codepoints().cmp(b.as_codepoints()));
            format!(
                "{{{}}}",
                rows.into_iter()
                    .map(|(k, v)| format!("{}:{}", quote(k), dump(v)))
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
    }
}
/// json.dumpのgeneratorを再開する文脈で、yieldしたchunkだけを書き込む。
pub fn dump_stream(
    value: &PyValue,
    write: &mut dyn FnMut(&str) -> super::error::Result<()>,
) -> super::error::Result<()> {
    use super::error::LegacyException as E;
    fn fault(
        depth: usize,
        extra: usize,
        comparison: bool,
        function: &'static str,
        line: u32,
        source: &'static str,
    ) -> super::error::Result<()> {
        if depth + extra > 1000 {
            Err(E::Python {
                kind: "RecursionError",
                reason: format!(
                    "maximum recursion depth exceeded{}",
                    if comparison && depth + extra - 1 == 1000 {
                        " in comparison"
                    } else {
                        " while calling a Python object"
                    }
                ),
                location: "json.dump",
            }
            .external("json/encoder.py", function, line, source))
        } else {
            Ok(())
        }
    }
    fn encode(
        value: &PyValue,
        depth: usize,
        write: &mut dyn FnMut(&str) -> super::error::Result<()>,
    ) -> super::error::Result<()> {
        let send = |chunk: &str, write: &mut dyn FnMut(&str) -> super::error::Result<()>| {
            write(chunk).map_err(|e| e.external("json/__init__.py", "dump", 180, "fp.write(chunk)"))
        };
        match value {
            PyValue::List(rows) => {
                if rows.is_empty() {
                    return send("[]", write);
                }
                fault(
                    depth,
                    1,
                    false,
                    "_iterencode_list",
                    283,
                    "markerid = id(lst)",
                )?;
                for (index, value) in rows.iter().enumerate() {
                    let prefix = if index == 0 { "[" } else { "," };
                    if matches!(value, PyValue::List(_) | PyValue::Dict(_)) {
                        send(prefix, write)?;
                        // generator の中で起きた例外だけが、yield from のフレームを通る。
                        // fp.write の失敗は dump の中で起き、generator のフレームを持たない。
                        encode(value, depth + 1, write).map_err(|e| {
                            if e.location() == "json.dump" {
                                e.external(
                                    "json/encoder.py",
                                    "_iterencode_list",
                                    325,
                                    "yield from chunks",
                                )
                            } else {
                                e
                            }
                        })?;
                    } else {
                        fault(
                            depth,
                            if matches!(value, PyValue::Float(_)) {
                                2
                            } else {
                                1
                            },
                            false,
                            "_iterencode_list",
                            298,
                            "if isinstance(value, str):",
                        )?;
                        send(&(prefix.to_owned() + &dump(value)), write)?;
                    }
                }
                send("]", write)
            }
            PyValue::Dict(rows) => {
                if rows.is_empty() {
                    return send("{}", write);
                }
                fault(
                    depth,
                    1,
                    false,
                    "_iterencode_dict",
                    338,
                    "markerid = id(dct)",
                )?;
                send("{", write)?;
                fault(
                    depth,
                    if rows.len() > 1 { 3 } else { 2 },
                    rows.len() > 1,
                    "_iterencode_dict",
                    353,
                    "items = sorted(dct.items())",
                )?;
                let mut rows = rows.iter().collect::<Vec<_>>();
                rows.sort_by(|(a, _), (b, _)| a.as_codepoints().cmp(b.as_codepoints()));
                for (index, (key, value)) in rows.into_iter().enumerate() {
                    if index != 0 {
                        send(",", write)?
                    }
                    send(&quote(key), write)?;
                    send(":", write)?;
                    if matches!(value, PyValue::List(_) | PyValue::Dict(_)) {
                        // generator の中で起きた例外だけが、yield from のフレームを通る。
                        // fp.write の失敗は dump の中で起き、generator のフレームを持たない。
                        encode(value, depth + 1, write).map_err(|e| {
                            if e.location() == "json.dump" {
                                e.external(
                                    "json/encoder.py",
                                    "_iterencode_dict",
                                    405,
                                    "yield from chunks",
                                )
                            } else {
                                e
                            }
                        })?;
                    } else {
                        fault(
                            depth,
                            if matches!(value, PyValue::Float(_)) {
                                2
                            } else {
                                1
                            },
                            false,
                            "_iterencode_dict",
                            380,
                            "yield _encoder(key)",
                        )?;
                        send(&dump(value), write)?;
                    }
                }
                send("}", write)
            }
            _ => send(&dump(value), write),
        }
    }
    encode(value, 7, write).map_err(|e| {
        if e.location() == "json.dump" {
            e.external(
                "json/encoder.py",
                "_iterencode",
                if matches!(value, PyValue::Dict(_)) {
                    431
                } else {
                    429
                },
                if matches!(value, PyValue::Dict(_)) {
                    "yield from _iterencode_dict(o, _current_indent_level)"
                } else {
                    "yield from _iterencode_list(o, _current_indent_level)"
                },
            )
            .external("json/__init__.py", "dump", 179, "for chunk in iterable:")
        } else {
            e
        }
    })
}
pub fn load(raw: &[u8], limit: usize) -> Result<PyValue, LoadError> {
    load_at_depth(raw, limit, 4)
}
#[derive(Debug)]
pub enum LoadError {
    Invalid(super::error::LegacyException),
    Recursion(&'static str, bool),
}
pub fn load_at_depth(raw: &[u8], limit: usize, entry_depth: usize) -> Result<PyValue, LoadError> {
    let codes = decode_json(raw).map_err(LoadError::Invalid)?;
    let mut parser = Parser {
        codes: &codes,
        at: 0,
        limit,
        depth: entry_depth + 4,
        recursion: None,
        invalid: None,
    };
    let value = parser.value().map_err(|_| {
        parser
            .recursion
            .map(|(s, b)| LoadError::Recursion(s, b))
            .unwrap_or_else(|| {
                LoadError::Invalid(
                    parser
                        .invalid
                        .take()
                        .unwrap_or_else(|| json_error(&codes, "Expecting value", parser.at, 355)),
                )
            })
    })?;
    parser.space();
    if parser.at != codes.len() {
        return Err(LoadError::Invalid(json_error(
            &codes,
            "Extra data",
            parser.at,
            340,
        )));
    }
    Ok(value)
}
// json.loads(bytes) uses its own BOM/NUL detection and surrogatepass.
// These failures are retained as the explicit cause of read_state's Stop.
fn json_error(
    codes: &[u32],
    message: &str,
    position: usize,
    line: u32,
) -> super::error::LegacyException {
    let lineno = codes[..position].iter().filter(|c| **c == 10).count() + 1;
    let colno = position
        - codes[..position]
            .iter()
            .rposition(|c| *c == 10)
            .map_or(0, |p| p + 1)
        + 1;
    let error = super::error::LegacyException::Python {
        kind: "json.decoder.JSONDecodeError",
        reason: format!("{message}: line {lineno} column {colno} (char {position})"),
        location: "json.loads",
    };
    if line == 340 {
        error.external(
            "json/decoder.py",
            "decode",
            340,
            r#"raise JSONDecodeError("Extra data", s, end)"#,
        )
    } else {
        error
            .external(
                "json/decoder.py",
                "raw_decode",
                line,
                if line == 355 {
                    r#"raise JSONDecodeError("Expecting value", s, err.value) from None"#
                } else {
                    "obj, end = self.scan_once(s, idx)"
                },
            )
            .external(
                "json/decoder.py",
                "decode",
                337,
                "obj, end = self.raw_decode(s, idx=_w(s, 0).end())",
            )
    }
    .external(
        "json/__init__.py",
        "loads",
        346,
        "return _default_decoder.decode(s)",
    )
}
fn decode_error(
    raw: &[u8],
    codec: &str,
    start: usize,
    end: usize,
    reason: &str,
    sig: bool,
) -> super::error::LegacyException {
    let detail = if end == start + 1 {
        format!("byte 0x{:02x} in position {start}", raw[start])
    } else {
        format!("bytes in position {start}-{}", end - 1)
    };
    let error = super::error::LegacyException::Python {
        kind: "UnicodeDecodeError",
        reason: format!("'{codec}' codec can't decode {detail}: {reason}"),
        location: "json.loads",
    };
    let error = if sig {
        error.external(
            "encodings/utf_8_sig.py",
            "decode",
            23,
            "(output, consumed) = codecs.utf_8_decode(input, errors, True)",
        )
    } else {
        error
    };
    error.external(
        "json/__init__.py",
        "loads",
        341,
        "s = s.decode(detect_encoding(s), 'surrogatepass')",
    )
}
fn decode_json(input: &[u8]) -> Result<Vec<u32>, super::error::LegacyException> {
    let (offset, width, be) = if input.starts_with(&[0, 0, 254, 255]) {
        (4, 4, true)
    } else if input.starts_with(&[255, 254, 0, 0]) {
        (4, 4, false)
    } else if input.starts_with(&[254, 255]) {
        (2, 2, true)
    } else if input.starts_with(&[255, 254]) {
        (2, 2, false)
    } else if input.starts_with(&[239, 187, 191]) {
        (3, 1, false)
    } else if input.len() >= 4 && input[0] == 0 {
        (0, if input[1] == 0 { 4 } else { 2 }, true)
    } else if input.len() >= 4 && input[1] == 0 {
        (0, if input[2] == 0 && input[3] == 0 { 4 } else { 2 }, false)
    } else if input.len() == 2 && input[0] == 0 {
        (0, 2, true)
    } else if input.len() == 2 && input[1] == 0 {
        (0, 2, false)
    } else {
        (0, 1, false)
    };
    let raw = &input[offset..];
    if width == 1 {
        let mut result = Vec::new();
        let mut at = 0;
        while at < raw.len() {
            match std::str::from_utf8(&raw[at..]) {
                Ok(v) => {
                    result.extend(v.chars().map(u32::from));
                    break;
                }
                Err(error) => {
                    let end = at + error.valid_up_to();
                    result.extend(
                        std::str::from_utf8(&raw[at..end])
                            .unwrap()
                            .chars()
                            .map(u32::from),
                    );
                    at = end;
                    if raw[at..].len() >= 3
                        && raw[at] == 0xed
                        && (0xa0..=0xbf).contains(&raw[at + 1])
                        && (0x80..=0xbf).contains(&raw[at + 2])
                    {
                        result.push(
                            (u32::from(raw[at] & 15) << 12)
                                | (u32::from(raw[at + 1] & 63) << 6)
                                | u32::from(raw[at + 2] & 63),
                        );
                        at += 3;
                    } else {
                        let end = error.error_len().map_or(raw.len(), |len| at + len);
                        let reason = if error.error_len().is_none() {
                            "unexpected end of data"
                        } else if matches!(raw[at],0x80..=0xc1|0xf5..=0xff) {
                            "invalid start byte"
                        } else {
                            "invalid continuation byte"
                        };
                        return Err(decode_error(raw, "utf-8", at, end, reason, offset == 3));
                    }
                }
            }
        }
        return Ok(result);
    }
    let codec = match (width, be) {
        (2, true) => "utf-16-be",
        (2, false) => "utf-16-le",
        (4, true) => "utf-32-be",
        _ => "utf-32-le",
    };
    let mut result = Vec::new();
    for (index, chunk) in raw.chunks_exact(width).enumerate() {
        let mut n = 0u32;
        if be {
            for &c in chunk {
                n = n * 256 + u32::from(c);
            }
        } else {
            for &c in chunk.iter().rev() {
                n = n * 256 + u32::from(c);
            }
        }
        if n > 0x10ffff {
            let at = offset + index * width;
            return Err(decode_error(
                input,
                codec,
                at,
                at + width,
                "code point not in range(0x110000)",
                false,
            ));
        }
        result.push(n);
    }
    if !raw.len().is_multiple_of(width) {
        let at = offset + raw.len() / width * width;
        return Err(decode_error(
            input,
            codec,
            at,
            input.len(),
            "truncated data",
            false,
        ));
    }
    if width == 2 {
        let mut decoded = Vec::new();
        let mut at = 0;
        while at < result.len() {
            let c = result[at];
            if (0xd800..=0xdbff).contains(&c)
                && result
                    .get(at + 1)
                    .is_some_and(|v| (0xdc00..=0xdfff).contains(v))
            {
                decoded.push(0x10000 + ((c - 0xd800) << 10) + (result[at + 1] - 0xdc00));
                at += 2;
            } else {
                decoded.push(c);
                at += 1;
            }
        }
        return Ok(decoded);
    }
    Ok(result)
}
struct Parser<'a> {
    codes: &'a [u32],
    at: usize,
    limit: usize,
    depth: usize,
    recursion: Option<(&'static str, bool)>,
    invalid: Option<super::error::LegacyException>,
}
impl Parser<'_> {
    fn syntax<T>(&mut self, message: &str, position: usize) -> Result<T, ()> {
        self.invalid = Some(json_error(self.codes, message, position, 353));
        self.recursion = if self.depth + 1 > 1000 {
            Some((
                "maximum recursion depth exceeded while calling a Python object",
                false,
            ))
        } else if self.depth + 2 > 1000 {
            Some(("maximum recursion depth exceeded", false))
        } else if self.depth + 3 > 1000 {
            Some((
                "maximum recursion depth exceeded while calling a Python object",
                true,
            ))
        } else {
            None
        };
        Err(())
    }
    fn constant(&mut self) -> Result<(), ()> {
        if self.depth + 1 > 1000 {
            self.recursion = Some((
                "maximum recursion depth exceeded while calling a Python object",
                false,
            ));
            Err(())
        } else {
            Ok(())
        }
    }
    fn space(&mut self) {
        while self
            .codes
            .get(self.at)
            .is_some_and(|c| [9, 10, 13, 32].contains(c))
        {
            self.at += 1;
        }
    }
    fn eat(&mut self, c: u32) -> bool {
        if self.codes.get(self.at) == Some(&c) {
            self.at += 1;
            true
        } else {
            false
        }
    }
    fn literal(&mut self, s: &str) -> bool {
        let codes = s.chars().map(u32::from).collect::<Vec<_>>();
        if self.codes[self.at..].starts_with(&codes) {
            self.at += codes.len();
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> Result<Text, ()> {
        let start = self.at;
        if !self.eat(34) {
            return self.syntax("Expecting property name enclosed in double quotes", self.at);
        }
        let mut codes = Vec::new();
        loop {
            let Some(&c) = self.codes.get(self.at) else {
                return self.syntax("Unterminated string starting at", start);
            };
            self.at += 1;
            match c {
                34 => return Ok(text(codes)),
                0..=31 => return self.syntax("Invalid control character at", self.at - 1),
                92 => {
                    let Some(&c) = self.codes.get(self.at) else {
                        return self.syntax("Unterminated string starting at", start);
                    };
                    self.at += 1;
                    let c = match c {
                        34 | 47 | 92 => c,
                        98 => 8,
                        102 => 12,
                        110 => 10,
                        114 => 13,
                        116 => 9,
                        117 => self.hex()?,
                        _ => return self.syntax("Invalid \\escape", self.at - 2),
                    };
                    if (0xd800..=0xdbff).contains(&c)
                        && self.codes.get(self.at..self.at + 2) == Some(&[92, 117])
                    {
                        // The C scanner only folds a valid low surrogate. An invalid
                        // second escape is scanned normally on the next iteration.
                        let low = self.codes.get(self.at + 2..self.at + 6).and_then(|digits| {
                            digits.iter().try_fold(0u32, |n, c| {
                                char::from_u32(*c)
                                    .and_then(|c| c.to_digit(16))
                                    .map(|v| n * 16 + v)
                            })
                        });
                        if let Some(low) = low.filter(|v| (0xdc00..=0xdfff).contains(v)) {
                            codes.push(0x10000 + ((c - 0xd800) << 10) + (low - 0xdc00));
                            self.at += 6;
                            continue;
                        }
                    }
                    codes.push(c);
                }
                _ => codes.push(c),
            }
        }
    }
    fn hex(&mut self) -> Result<u32, ()> {
        let position = self.at - 1;
        let mut n = 0;
        for _ in 0..4 {
            let Some(c) = self
                .codes
                .get(self.at)
                .and_then(|c| char::from_u32(*c))
                .and_then(|c| c.to_digit(16))
            else {
                return self.syntax("Invalid \\uXXXX escape", position);
            };
            self.at += 1;
            n = n * 16 + c;
        }
        Ok(n)
    }
    fn value(&mut self) -> Result<PyValue, ()> {
        self.space();
        let container = matches!(self.codes.get(self.at), Some(91 | 123));
        if container {
            self.depth += 1;
            if self.depth > 1000 {
                self.recursion = Some((
                    if self.codes[self.at] == 91 {
                        "maximum recursion depth exceeded while decoding a JSON array from a unicode string"
                    } else {
                        "maximum recursion depth exceeded while decoding a JSON object from a unicode string"
                    },
                    false,
                ));
                return Err(());
            }
        }
        let value = match self.codes.get(self.at).copied() {
            Some(34) => PyValue::String(self.string()?),
            Some(91) => {
                self.at += 1;
                self.space();
                let mut list = Vec::new();
                if !self.eat(93) {
                    loop {
                        list.push(self.value()?);
                        self.space();
                        if self.eat(93) {
                            break;
                        }
                        if !self.eat(44) {
                            return self.syntax("Expecting ',' delimiter", self.at);
                        }
                    }
                }
                PyValue::List(list)
            }
            Some(123) => {
                self.at += 1;
                self.space();
                let mut rows: Vec<(Text, PyValue)> = Vec::new();
                if !self.eat(125) {
                    loop {
                        self.space();
                        let key = self.string()?;
                        self.space();
                        if !self.eat(58) {
                            return self.syntax("Expecting ':' delimiter", self.at);
                        }
                        let value = self.value()?;
                        if let Some((_, v)) = rows.iter_mut().find(|(k, _)| *k == key) {
                            *v = value
                        } else {
                            rows.push((key, value))
                        }
                        self.space();
                        if self.eat(125) {
                            break;
                        }
                        if !self.eat(44) {
                            return self.syntax("Expecting ',' delimiter", self.at);
                        }
                    }
                }
                PyValue::Dict(rows)
            }
            _ if self.literal("null") => PyValue::Null,
            _ if self.literal("true") => PyValue::Bool(true),
            _ if self.literal("false") => PyValue::Bool(false),
            _ if self.literal("NaN") => {
                self.constant()?;
                PyValue::Float(f64::NAN)
            }
            _ if self.literal("Infinity") => {
                self.constant()?;
                PyValue::Float(f64::INFINITY)
            }
            _ if self.literal("-Infinity") => {
                self.constant()?;
                PyValue::Float(f64::NEG_INFINITY)
            }
            _ => {
                let start = self.at;
                self.eat(45);
                if !self.eat(48) {
                    let first = self.codes.get(self.at).copied().unwrap_or(0);
                    if !(49..=57).contains(&first) {
                        self.at = start;
                        return Err(());
                    }
                    self.at += 1;
                    while self
                        .codes
                        .get(self.at)
                        .is_some_and(|c| (48..=57).contains(c))
                    {
                        self.at += 1
                    }
                }
                let mut float = false;
                let fraction = self.at;
                if self.eat(46) {
                    let start = self.at;
                    while self
                        .codes
                        .get(self.at)
                        .is_some_and(|c| (48..=57).contains(c))
                    {
                        self.at += 1
                    }
                    if start == self.at {
                        self.at = fraction;
                    } else {
                        float = true;
                    }
                }
                let exponent = self.at;
                if self.eat(101) || self.eat(69) {
                    if !self.eat(43) {
                        self.eat(45);
                    }
                    let start = self.at;
                    while self
                        .codes
                        .get(self.at)
                        .is_some_and(|c| (48..=57).contains(c))
                    {
                        self.at += 1
                    }
                    if start == self.at {
                        self.at = exponent;
                    } else {
                        float = true;
                    }
                }
                let s = self.codes[start..self.at]
                    .iter()
                    .map(|c| char::from_u32(*c).unwrap())
                    .collect::<String>();
                if float {
                    PyValue::Float(s.parse().map_err(|_| ())?)
                } else {
                    PyValue::Integer(PyInteger::parse(&s, self.limit).map_err(|_| {
                        let digits = s.trim_start_matches('-').len();
                        self.invalid = Some(super::error::LegacyException::Python {
                            kind: "ValueError",
                            reason: format!("Exceeds the limit ({}) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit", self.limit),
                            location: "json.loads",
                        }.external("json/decoder.py", "raw_decode", 353, "obj, end = self.scan_once(s, idx)")
                         .external("json/decoder.py", "decode", 337, "obj, end = self.raw_decode(s, idx=_w(s, 0).end())")
                         .external("json/__init__.py", "loads", 346, "return _default_decoder.decode(s)"));
                    })?)
                }
            }
        };
        if container {
            self.depth -= 1;
        }
        Ok(value)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_numbers_and_unicode() {
        let v = load(
            br#"{"x":1,"x":7.0,"z":"\ud800","n":123456789012345678901234567890,"v":NaN}"#,
            4300,
        )
        .unwrap();
        assert_eq!(v.get("x"), &PyValue::from(7i128));
        assert!(dump(&v).contains("\\ud800"));
        assert!(load(b"[01]", 4300).is_err());
        assert_eq!(load(b"true", 4300).unwrap(), PyValue::from(1i128));
        assert_ne!(
            PyValue::Integer(PyInteger::parse("9007199254740993", 0).unwrap()),
            PyValue::Float(9007199254740992.0)
        );
    }
}
