//! Python の値の形(順序付きの辞書・大きな整数・surrogate を含む文字列・bytes)と、repr・文字列の符号化。
//! 文字列は Unicode の符号位置の列で持つ(lone surrogate も 1 つの符号位置)。

use super::number;

/// Python の値。`Int` は正規化した 10 進の字面(`-` と、先頭に 0 の無い数字。0 は `0`)。
/// `Dict` は入れた順の `(鍵, 値)` の並び(鍵は重ならない)。
#[derive(Clone, Debug)]
pub enum PyValue {
    None,
    Bool(bool),
    Int(String),
    Float(f64),
    Str(Vec<u32>),
    Bytes(Vec<u8>),
    List(Vec<PyValue>),
    Dict(Vec<(PyValue, PyValue)>),
}

impl PyValue {
    /// UTF-8 の文字列から作る。
    pub fn str(text: &str) -> Self {
        PyValue::Str(text.chars().map(u32::from).collect())
    }

    /// 整数から作る。
    pub fn int(value: i64) -> Self {
        PyValue::Int(value.to_string())
    }

    /// `type(value).__name__`。
    pub fn type_name(&self) -> &'static str {
        match self {
            PyValue::None => "NoneType",
            PyValue::Bool(_) => "bool",
            PyValue::Int(_) => "int",
            PyValue::Float(_) => "float",
            PyValue::Str(_) => "str",
            PyValue::Bytes(_) => "bytes",
            PyValue::List(_) => "list",
            PyValue::Dict(_) => "dict",
        }
    }

    /// `repr(value)`。
    pub fn repr(&self) -> Vec<u32> {
        let mut out = Vec::new();
        self.write_repr(&mut out);
        out
    }

    fn write_repr(&self, out: &mut Vec<u32>) {
        let push = |out: &mut Vec<u32>, text: &str| out.extend(text.chars().map(u32::from));
        match self {
            PyValue::None => push(out, "None"),
            PyValue::Bool(true) => push(out, "True"),
            PyValue::Bool(false) => push(out, "False"),
            PyValue::Int(digits) => push(out, digits),
            PyValue::Float(value) => push(out, &number::float_repr(*value)),
            PyValue::Str(codes) => out.extend(str_repr(codes)),
            PyValue::Bytes(raw) => out.extend(bytes_repr(raw)),
            PyValue::List(items) => {
                push(out, "[");
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        push(out, ", ");
                    }
                    item.write_repr(out);
                }
                push(out, "]");
            }
            PyValue::Dict(pairs) => {
                push(out, "{");
                for (index, (key, value)) in pairs.iter().enumerate() {
                    if index > 0 {
                        push(out, ", ");
                    }
                    key.write_repr(out);
                    push(out, ": ");
                    value.write_repr(out);
                }
                push(out, "}");
            }
        }
    }

    /// 数としての値(bool は 0 か 1)。数でなければ None。
    fn numeric(&self) -> Option<Numeric<'_>> {
        match self {
            PyValue::Bool(value) => Some(Numeric::Int(if *value { "1" } else { "0" })),
            PyValue::Int(digits) => Some(Numeric::Int(digits)),
            PyValue::Float(value) => Some(Numeric::Float(*value)),
            _ => None,
        }
    }

    /// Python の `==`(辞書の鍵の同一視に使う範囲。数は bool・int・float をまたいで値で比べる)。
    pub fn py_eq(&self, other: &PyValue) -> bool {
        if let (Some(left), Some(right)) = (self.numeric(), other.numeric()) {
            return left.equals(&right);
        }
        match (self, other) {
            (PyValue::None, PyValue::None) => true,
            (PyValue::Str(left), PyValue::Str(right)) => left == right,
            (PyValue::Bytes(left), PyValue::Bytes(right)) => left == right,
            (PyValue::List(left), PyValue::List(right)) => {
                left.len() == right.len() && left.iter().zip(right).all(|(a, b)| a.py_eq(b))
            }
            (PyValue::Dict(left), PyValue::Dict(right)) => {
                left.len() == right.len()
                    && left.iter().all(|(key, value)| {
                        right.iter().any(|(other_key, other_value)| {
                            key.py_eq(other_key) && value.py_eq(other_value)
                        })
                    })
            }
            _ => false,
        }
    }
}

enum Numeric<'a> {
    Int(&'a str),
    Float(f64),
}

impl Numeric<'_> {
    fn equals(&self, other: &Numeric<'_>) -> bool {
        match (self, other) {
            (Numeric::Int(left), Numeric::Int(right)) => left == right,
            (Numeric::Float(left), Numeric::Float(right)) => left == right,
            (Numeric::Int(digits), Numeric::Float(value))
            | (Numeric::Float(value), Numeric::Int(digits)) => {
                value.is_finite()
                    && value.fract() == 0.0
                    && number::exact_integer_text(*value).as_deref() == Some(*digits)
            }
        }
    }
}

/// 辞書に入れる(`d[key] = value`)。同じ鍵があれば、最初の位置の値を置き換える。
pub fn dict_set(pairs: &mut Vec<(PyValue, PyValue)>, key: PyValue, value: PyValue) {
    match pairs.iter_mut().find(|(existing, _)| existing.py_eq(&key)) {
        Some(entry) => entry.1 = value,
        None => pairs.push((key, value)),
    }
}

/// 辞書から引く(`d.get(key)`)。
pub fn dict_get<'a>(pairs: &'a [(PyValue, PyValue)], key: &PyValue) -> Option<&'a PyValue> {
    pairs
        .iter()
        .find(|(existing, _)| existing.py_eq(key))
        .map(|(_, value)| value)
}

fn push_ascii(out: &mut Vec<u32>, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

/// Python 3.10 の `repr(str)`(`unicode_repr`)。引用符は内容から選ぶ。
pub fn str_repr(codes: &[u32]) -> Vec<u32> {
    let single = u32::from(b'\'');
    let double = u32::from(b'"');
    let quote = if codes.contains(&single) && !codes.contains(&double) {
        double
    } else {
        single
    };
    let mut out = vec![quote];
    for &code in codes {
        match code {
            _ if code == quote || code == u32::from(b'\\') => {
                out.push(u32::from(b'\\'));
                out.push(code);
            }
            0x09 => push_ascii(&mut out, "\\t"),
            0x0a => push_ascii(&mut out, "\\n"),
            0x0d => push_ascii(&mut out, "\\r"),
            _ if code < 0x20 || code == 0x7f => push_ascii(&mut out, &format!("\\x{code:02x}")),
            _ if code < 0x7f => out.push(code),
            _ if super::printable(code) => out.push(code),
            _ if code <= 0xff => push_ascii(&mut out, &format!("\\x{code:02x}")),
            _ if code <= 0xffff => push_ascii(&mut out, &format!("\\u{code:04x}")),
            _ => push_ascii(&mut out, &format!("\\U{code:08x}")),
        }
    }
    out.push(quote);
    out
}

/// Python 3.10 の `repr(bytes)`。
pub fn bytes_repr(raw: &[u8]) -> Vec<u32> {
    let quote = if raw.contains(&b'\'') && !raw.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::from("b");
    out.push(char::from(quote));
    for &byte in raw {
        match byte {
            _ if byte == quote || byte == b'\\' => {
                out.push('\\');
                out.push(char::from(byte));
            }
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            _ if !(0x20..0x7f).contains(&byte) => out.push_str(&format!("\\x{byte:02x}")),
            _ => out.push(char::from(byte)),
        }
    }
    out.push(char::from(quote));
    out.chars().map(u32::from).collect()
}

/// 符号位置の列を Rust の文字列にする(repr の結果のように surrogate を含まない列に使う)。
/// surrogate は U+FFFD にする。
pub fn text(codes: &[u32]) -> String {
    codes
        .iter()
        .map(|&code| char::from_u32(code).unwrap_or('\u{fffd}'))
        .collect()
}

fn push_utf8(out: &mut Vec<u8>, code: u32) {
    if code < 0x80 {
        out.push(code as u8);
    } else if code < 0x800 {
        out.push(0xc0 | (code >> 6) as u8);
        out.push(0x80 | (code & 0x3f) as u8);
    } else if code < 0x10000 {
        out.push(0xe0 | (code >> 12) as u8);
        out.push(0x80 | ((code >> 6) & 0x3f) as u8);
        out.push(0x80 | (code & 0x3f) as u8);
    } else {
        out.push(0xf0 | (code >> 18) as u8);
        out.push(0x80 | ((code >> 12) & 0x3f) as u8);
        out.push(0x80 | ((code >> 6) & 0x3f) as u8);
        out.push(0x80 | (code & 0x3f) as u8);
    }
}

fn is_surrogate(code: u32) -> bool {
    (0xd800..=0xdfff).contains(&code)
}

/// UTF-8(surrogatepass。lone surrogate も 3 バイトで書く)。
pub fn encode_surrogatepass(codes: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &code in codes {
        push_utf8(&mut out, code);
    }
    out
}

/// UTF-8(標準エラーの backslashreplace。surrogate は `\udXXX`)。
pub fn encode_backslashreplace(codes: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &code in codes {
        if is_surrogate(code) {
            out.extend_from_slice(format!("\\u{code:04x}").as_bytes());
        } else {
            push_utf8(&mut out, code);
        }
    }
    out
}

/// UTF-8(surrogateescape。U+DC80〜U+DCFF は元のバイト)。ほかの surrogate は書けない(None)。
pub fn encode_surrogateescape(codes: &[u32]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    for &code in codes {
        if (0xdc80..=0xdcff).contains(&code) {
            out.push((code - 0xdc00) as u8);
        } else if is_surrogate(code) {
            return None;
        } else {
            push_utf8(&mut out, code);
        }
    }
    Some(out)
}
