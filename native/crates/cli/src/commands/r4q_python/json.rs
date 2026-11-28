//! Python 3.10.11 の `json.loads(bytes)`(`Lib/json/__init__.py`・`Lib/json/decoder.py`・`Modules/_json.c` の
//! C の scanner)と `json.dumps`(C の encoder)の写し。値は Python の値の形(`PyValue`)で持つ。
//!
//! Python と同じ結果を出す範囲:
//! - [`loads`]: 任意のバイト列。文字コードの判定(`json.detect_encoding`)と `surrogatepass` の復号、C の scanner の
//!   受理と値(順序付きの辞書。重複した鍵は後の値が最初の位置に入る)、`NaN`・`Infinity`・`-Infinity`、
//!   大きな整数と整数の桁の上限、誤りの型と文と位置。入れ子の上限は、Python の呼出しの深さで決まるので、
//!   呼出元が `recursion_room` で渡す。記憶域の不足は扱わない。
//! - [`dumps`]: 既定の引数(`ensure_ascii=True`・`allow_nan=True`・区切り `', '`・`': '`・`indent=None`)と
//!   `sort_keys`。値は None・bool・int・float・str・list と、鍵が str の dict。int の桁の上限の `ValueError` と、
//!   bytes の値の `TypeError` は Python と同じ文を返す。
//!   - 辞書の鍵は str だけを受け付ける(旧版が dumps に渡す記録の鍵は str だけ)。str でない鍵は、Python と同じ
//!     結果(int・float・bool・None の鍵の文字列化や、`sort_keys` で型が混ざったときの `TypeError`)を出さず、
//!     呼出元の誤り [`DumpError::NonStringKey`] を返す。
//!   - 入れ子の深さで起きる `RecursionError` は再現しない(旧版が dumps に渡すのは入れ子の無い記録だけ)。

use super::codec;
use super::number;
use super::value::{self, PyValue};

/// `json.loads` が出す例外(型の名前と `str(exc)`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JsonError {
    /// `JSONDecodeError`・`UnicodeDecodeError`・`ValueError`(整数の桁の上限)・`RecursionError`。
    pub type_name: &'static str,
    pub message: String,
}

impl JsonError {
    /// `ValueError` の子か(`JSONDecodeError`・`UnicodeDecodeError`・`ValueError`)。`RecursionError` は
    /// `RuntimeError` の子。
    pub fn is_value_error(&self) -> bool {
        self.type_name != "RecursionError"
    }
}

/// `JSONDecodeError(msg, doc, pos)` の `str`。
fn decode_error(message: &str, doc: &[u32], pos: usize) -> JsonError {
    let newline = u32::from(b'\n');
    let before = &doc[..pos.min(doc.len())];
    let line = before.iter().filter(|&&code| code == newline).count() + 1;
    let column = match before.iter().rposition(|&code| code == newline) {
        Some(at) => pos - at,
        None => pos + 1,
    };
    JsonError {
        type_name: "JSONDecodeError",
        message: format!("{message}: line {line} column {column} (char {pos})"),
    }
}

/// 開いている配列か辞書(辞書は、次に入れる値の鍵を持つ)。
enum Container {
    Array(Vec<PyValue>),
    Object(Vec<(PyValue, PyValue)>, Vec<u32>),
}

/// 値の始まりを読んだ結果。
enum Step {
    Done(PyValue, usize),
    Open(Container, usize),
}

enum Scan {
    /// `StopIteration(idx)`(`Expecting value` になる)。
    Stop(usize),
    /// ほかの例外。
    Raise(JsonError),
}

fn whitespace(code: u32) -> bool {
    matches!(code, 0x20 | 0x09 | 0x0a | 0x0d)
}

struct Scanner<'a> {
    doc: &'a [u32],
    max_str_digits: usize,
    /// 入れ子の深さの余り(`Py_EnterRecursiveCall` の上限までの残り)。
    room: usize,
    depth: usize,
}

impl Scanner<'_> {
    fn at(&self, index: usize) -> u32 {
        self.doc[index]
    }

    fn error(&self, message: &str, pos: usize) -> Scan {
        Scan::Raise(decode_error(message, self.doc, pos))
    }

    /// `scanstring_unicode`(`end` は開きの引用符の次。strict)。値と、閉じの引用符の次の位置。
    fn scanstring(&self, mut end: usize) -> Result<(Vec<u32>, usize), Scan> {
        let doc = self.doc;
        let len = doc.len();
        let begin = end - 1;
        let mut out: Vec<u32> = Vec::new();
        loop {
            let mut next = end;
            let mut c = 0;
            while next < len {
                c = doc[next];
                if c == u32::from(b'"') || c == u32::from(b'\\') {
                    break;
                }
                if c <= 0x1f {
                    return Err(self.error("Invalid control character at", next));
                }
                next += 1;
            }
            // 引用符か `\` で止まらずに終わりへ着いたら、閉じていない。
            if next >= len {
                return Err(self.error("Unterminated string starting at", begin));
            }
            if !(c == u32::from(b'"') || c == u32::from(b'\\')) {
                return Err(self.error("Unterminated string starting at", begin));
            }
            out.extend_from_slice(&doc[end..next]);
            next += 1;
            if c == u32::from(b'"') {
                return Ok((out, next));
            }
            if next == len {
                return Err(self.error("Unterminated string starting at", begin));
            }
            let escape = doc[next];
            if escape != u32::from(b'u') {
                end = next + 1;
                let decoded = match char::from_u32(escape) {
                    Some('"') => u32::from(b'"'),
                    Some('\\') => u32::from(b'\\'),
                    Some('/') => u32::from(b'/'),
                    Some('b') => 0x08,
                    Some('f') => 0x0c,
                    Some('n') => 0x0a,
                    Some('r') => 0x0d,
                    Some('t') => 0x09,
                    _ => return Err(self.error("Invalid \\escape", end - 2)),
                };
                out.push(decoded);
            } else {
                next += 1;
                end = next + 4;
                if end >= len {
                    return Err(self.error("Invalid \\uXXXX escape", next - 1));
                }
                let mut code = 0u32;
                while next < end {
                    let digit = char::from_u32(doc[next]).and_then(|c| c.to_digit(16));
                    match digit {
                        Some(value) if doc[next] < 0x80 => code = (code << 4) | value,
                        _ => return Err(self.error("Invalid \\uXXXX escape", end - 5)),
                    }
                    next += 1;
                }
                if (0xd800..=0xdbff).contains(&code) && end + 6 < len {
                    let backslash = doc[next] == u32::from(b'\\');
                    let letter = backslash && doc[next + 1] == u32::from(b'u');
                    if letter {
                        next += 2;
                        end += 6;
                        let mut low = 0u32;
                        while next < end {
                            let digit = char::from_u32(doc[next]).and_then(|c| c.to_digit(16));
                            match digit {
                                Some(value) if doc[next] < 0x80 => low = (low << 4) | value,
                                _ => return Err(self.error("Invalid \\uXXXX escape", end - 5)),
                            }
                            next += 1;
                        }
                        if (0xdc00..=0xdfff).contains(&low) {
                            code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
                        } else {
                            end -= 6;
                        }
                    }
                }
                out.push(code);
            }
        }
    }

    fn enter(&mut self, what: &str) -> Result<(), Scan> {
        self.depth += 1;
        if self.depth > self.room {
            self.depth -= 1;
            return Err(Scan::Raise(JsonError {
                type_name: "RecursionError",
                message: format!(
                    "maximum recursion depth exceeded while decoding a JSON {what} from a unicode string"
                ),
            }));
        }
        Ok(())
    }

    fn skip(&self, mut idx: usize) -> usize {
        while idx < self.doc.len() && whitespace(self.at(idx)) {
            idx += 1;
        }
        idx
    }

    fn is(&self, idx: usize, c: u8) -> bool {
        idx < self.doc.len() && self.at(idx) == u32::from(c)
    }

    /// 辞書の鍵と `:` を読む(`_parse_object_unicode` のループの頭)。値の始まりの位置を返す。
    fn key(&self, idx: usize) -> Result<(Vec<u32>, usize), Scan> {
        if !self.is(idx, b'"') {
            return Err(self.error("Expecting property name enclosed in double quotes", idx));
        }
        let (key, next) = self.scanstring(idx + 1)?;
        let idx = self.skip(next);
        if !self.is(idx, b':') {
            return Err(self.error("Expecting ':' delimiter", idx));
        }
        Ok((key, self.skip(idx + 1)))
    }

    fn matches(&self, idx: usize, word: &str) -> bool {
        let word: Vec<u32> = word.chars().map(u32::from).collect();
        idx + word.len() - 1 < self.doc.len() && self.doc[idx..idx + word.len()] == word[..]
    }

    /// `_match_number_unicode`。
    fn number(&self, start: usize) -> Result<(PyValue, usize), Scan> {
        let doc = self.doc;
        let end_idx = doc.len() - 1;
        let digit = |index: usize| (u32::from(b'0')..=u32::from(b'9')).contains(&doc[index]);
        let mut idx = start;
        if doc[idx] == u32::from(b'-') {
            idx += 1;
            if idx > end_idx {
                return Err(Scan::Stop(start));
            }
        }
        if (u32::from(b'1')..=u32::from(b'9')).contains(&doc[idx]) {
            idx += 1;
            while idx <= end_idx && digit(idx) {
                idx += 1;
            }
        } else if doc[idx] == u32::from(b'0') {
            idx += 1;
        } else {
            return Err(Scan::Stop(start));
        }
        let mut is_float = false;
        if idx < end_idx && doc[idx] == u32::from(b'.') && digit(idx + 1) {
            is_float = true;
            idx += 2;
            while idx <= end_idx && digit(idx) {
                idx += 1;
            }
        }
        if idx < end_idx && (doc[idx] == u32::from(b'e') || doc[idx] == u32::from(b'E')) {
            let e_start = idx;
            idx += 1;
            if idx < end_idx && (doc[idx] == u32::from(b'-') || doc[idx] == u32::from(b'+')) {
                idx += 1;
            }
            while idx <= end_idx && digit(idx) {
                idx += 1;
            }
            if digit(idx - 1) {
                is_float = true;
            } else {
                idx = e_start;
            }
        }
        let text: Vec<u8> = doc[start..idx].iter().map(|&code| code as u8).collect();
        let raise = |message: String| {
            Scan::Raise(JsonError {
                type_name: "ValueError",
                message,
            })
        };
        let value = if is_float {
            PyValue::Float(number::float_from_bytes(&text).map_err(raise)?)
        } else {
            PyValue::Int(number::int_from_bytes(&text, self.max_str_digits).map_err(raise)?)
        };
        Ok((value, idx))
    }

    /// `scan_once_unicode` の 1 つの値の始まり。値が読み終われば `Done`、配列・辞書を開いたら `Open`
    /// (中身は呼出元が続けて読む。C の再帰を明示の積み上げで行い、深い入れ子でも Rust の stack を使わない)。
    fn start(&mut self, idx: usize) -> Result<Step, Scan> {
        let len = self.doc.len();
        if idx >= len {
            return Err(Scan::Stop(idx));
        }
        let done = |value: PyValue, next: usize| Ok(Step::Done(value, next));
        match char::from_u32(self.at(idx)) {
            Some('"') => {
                let (text, next) = self.scanstring(idx + 1)?;
                done(PyValue::Str(text), next)
            }
            Some('{') => {
                self.enter("object")?;
                let idx = self.skip(idx + 1);
                if self.is(idx, b'}') {
                    self.depth -= 1;
                    return done(PyValue::Dict(Vec::new()), idx + 1);
                }
                let (key, value_at) = self.key(idx)?;
                Ok(Step::Open(Container::Object(Vec::new(), key), value_at))
            }
            Some('[') => {
                self.enter("array")?;
                let idx = self.skip(idx + 1);
                if self.is(idx, b']') {
                    self.depth -= 1;
                    return done(PyValue::List(Vec::new()), idx + 1);
                }
                Ok(Step::Open(Container::Array(Vec::new()), idx))
            }
            Some('n') if idx + 3 < len && self.matches(idx, "null") => done(PyValue::None, idx + 4),
            Some('t') if idx + 3 < len && self.matches(idx, "true") => {
                done(PyValue::Bool(true), idx + 4)
            }
            Some('f') if idx + 4 < len && self.matches(idx, "false") => {
                done(PyValue::Bool(false), idx + 5)
            }
            Some('N') if idx + 2 < len && self.matches(idx, "NaN") => {
                done(PyValue::Float(f64::NAN), idx + 3)
            }
            Some('I') if idx + 7 < len && self.matches(idx, "Infinity") => {
                done(PyValue::Float(f64::INFINITY), idx + 8)
            }
            Some('-') if idx + 8 < len && self.matches(idx, "-Infinity") => {
                done(PyValue::Float(f64::NEG_INFINITY), idx + 9)
            }
            _ => {
                let (value, next) = self.number(idx)?;
                done(value, next)
            }
        }
    }

    /// `scan_once_unicode`(値 1 つと、その次の位置)。
    fn scan_once(&mut self, idx: usize) -> Result<(PyValue, usize), Scan> {
        let mut stack: Vec<Container> = Vec::new();
        let mut at = idx;
        loop {
            let (mut value, mut next) = match self.start(at)? {
                Step::Done(value, next) => (value, next),
                Step::Open(container, value_at) => {
                    stack.push(container);
                    at = value_at;
                    continue;
                }
            };
            // 読み終えた値を、開いている配列・辞書へ入れる。閉じたら、その配列・辞書を値として続ける。
            loop {
                let Some(container) = stack.last_mut() else {
                    return Ok((value, next));
                };
                let idx = self.skip(next);
                let close = match container {
                    Container::Array(items) => {
                        items.push(value);
                        b']'
                    }
                    Container::Object(pairs, key) => {
                        value::dict_set(pairs, PyValue::Str(std::mem::take(key)), value);
                        b'}'
                    }
                };
                if self.is(idx, close) {
                    self.depth -= 1;
                    value = match stack.pop().expect("開いている入れ物") {
                        Container::Array(items) => PyValue::List(items),
                        Container::Object(pairs, _) => PyValue::Dict(pairs),
                    };
                    next = idx + 1;
                    continue;
                }
                if !self.is(idx, b',') {
                    return Err(self.error("Expecting ',' delimiter", idx));
                }
                let idx = self.skip(idx + 1);
                at = match stack.last_mut().expect("開いている入れ物") {
                    Container::Array(_) => idx,
                    Container::Object(_, key) => {
                        let (name, value_at) = self.key(idx)?;
                        *key = name;
                        value_at
                    }
                };
                break;
            }
        }
    }
}

/// `json.loads(raw)`(raw は bytes)。
///
/// - `max_str_digits` は `sys.get_int_max_str_digits()`(0 は上限なし)。
/// - `recursion_room` は、C の scanner を呼んだ時点で `Py_EnterRecursiveCall` が上限までに許す入れ子の数
///   (再帰の上限から、呼出元の Python のフレームの数などを引いた値)。これより深い配列・辞書は
///   `RecursionError`。
pub fn loads(
    raw: &[u8],
    max_str_digits: usize,
    recursion_room: usize,
) -> Result<PyValue, JsonError> {
    let encoding = codec::detect_encoding(raw);
    let doc = codec::decode_surrogatepass(raw, encoding).map_err(|error| JsonError {
        type_name: "UnicodeDecodeError",
        message: error.message(),
    })?;
    let mut scanner = Scanner {
        doc: &doc,
        max_str_digits,
        room: recursion_room,
        depth: 0,
    };
    let start = scanner.skip(0);
    let (value, end) = match scanner.scan_once(start) {
        Ok(found) => found,
        Err(Scan::Stop(at)) => return Err(decode_error("Expecting value", &doc, at)),
        Err(Scan::Raise(error)) => return Err(error),
    };
    let end = scanner.skip(end);
    if end != doc.len() {
        return Err(decode_error("Extra data", &doc, end));
    }
    Ok(value)
}

/// `json.dumps` の誤り。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DumpError {
    /// Python と同じ例外(`ValueError`: int の桁の上限、`TypeError`: bytes の値)。
    Python {
        type_name: &'static str,
        message: String,
    },
    /// 呼出元の誤り: 辞書の鍵が str でない(この写しは str の鍵だけを扱う)。
    NonStringKey,
}

fn ensure_ascii(codes: &[u32], out: &mut String) {
    out.push('"');
    for &code in codes {
        match code {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x20..=0x7e => out.push(char::from(code as u8)),
            0x10000.. => {
                let value = code - 0x10000;
                let high = 0xd800 | ((value >> 10) & 0x3ff);
                let low = 0xdc00 | (value & 0x3ff);
                out.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
            }
            _ => out.push_str(&format!("\\u{code:04x}")),
        }
    }
    out.push('"');
}

fn int_text(digits: &str, max_str_digits: usize) -> Result<String, DumpError> {
    if max_str_digits > 0 && digits.trim_start_matches('-').len() > max_str_digits {
        return Err(DumpError::Python {
            type_name: "ValueError",
            message: format!(
                "Exceeds the limit ({max_str_digits}) for integer string conversion; \
                 use sys.set_int_max_str_digits() to increase the limit"
            ),
        });
    }
    Ok(digits.to_owned())
}

fn float_text(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_owned()
    } else if value.is_infinite() {
        if value > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        number::float_repr(value)
    }
}

fn encode(
    value: &PyValue,
    sort_keys: bool,
    max_str_digits: usize,
    out: &mut String,
) -> Result<(), DumpError> {
    match value {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(true) => out.push_str("true"),
        PyValue::Bool(false) => out.push_str("false"),
        PyValue::Int(digits) => out.push_str(&int_text(digits, max_str_digits)?),
        PyValue::Float(number) => out.push_str(&float_text(*number)),
        PyValue::Str(codes) => ensure_ascii(codes, out),
        PyValue::List(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                encode(item, sort_keys, max_str_digits, out)?;
            }
            out.push(']');
        }
        PyValue::Dict(pairs) => {
            let mut order: Vec<(&[u32], &PyValue)> = Vec::with_capacity(pairs.len());
            for (key, item) in pairs {
                match key {
                    PyValue::Str(codes) => order.push((codes, item)),
                    _ => return Err(DumpError::NonStringKey),
                }
            }
            if sort_keys {
                order.sort_by_key(|(key, _)| *key);
            }
            out.push('{');
            for (index, (key, item)) in order.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                ensure_ascii(key, out);
                out.push_str(": ");
                encode(item, sort_keys, max_str_digits, out)?;
            }
            out.push('}');
        }
        PyValue::Bytes(_) => {
            return Err(DumpError::Python {
                type_name: "TypeError",
                message: "Object of type bytes is not JSON serializable".to_owned(),
            });
        }
    }
    Ok(())
}

/// `json.dumps(value, sort_keys=sort_keys)`(既定の区切り `', '`・`': '`、`ensure_ascii=True`)。
/// 扱う値の範囲はモジュールの説明のとおり(辞書の鍵は str だけ)。
pub fn dumps(value: &PyValue, sort_keys: bool, max_str_digits: usize) -> Result<String, DumpError> {
    let mut out = String::new();
    encode(value, sort_keys, max_str_digits, &mut out)?;
    Ok(out)
}
