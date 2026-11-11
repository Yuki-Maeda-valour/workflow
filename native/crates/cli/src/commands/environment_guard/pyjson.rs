//! Python 3.10 の `json` の再現。読取りは `json.loads`(C の scanner)、書き出しは `json.dumps`(C の encoder)。
//! 呼出しの深さ(Python の再帰の深さ)は `PyCtx` で受け取り、`RecursionError` の境界を旧版と合わせる。
use super::error::PyException;
use super::pypath::PyPath;
use super::pystr::{cps, parse_int};
use super::pyvalue::{PyDict, PyInt, PyValue, float_repr};
use dev_workflow_core::loop_state::CodepointString;

/// Python の再帰の上限(`sys.getrecursionlimit()` の既定)。
pub const RECURSION_LIMIT: usize = 1000;

/// 旧版を動かしている Python の状態。呼び出す関数の外から渡す。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PyCtx {
    /// 呼び出した側のフレーム(呼び出した関数自身を含む)の再帰の深さ。
    pub depth: usize,
    /// `PYTHONINTMAXSTRDIGITS` の値(0 は無制限)。既定は 4300。
    pub max_str_digits: usize,
}

impl PyCtx {
    pub const DEFAULT_MAX_STR_DIGITS: usize = 4300;

    pub fn new(depth: usize) -> Self {
        Self {
            depth,
            max_str_digits: Self::DEFAULT_MAX_STR_DIGITS,
        }
    }
}

// ---------------------------------------------------------------- 符号化の復号

fn message_of(text: String) -> CodepointString {
    CodepointString::from(text.as_str())
}

fn decode_error(codec: &str, data: &[u8], start: usize, end: usize, reason: &str) -> PyException {
    let text = if end == start + 1 {
        format!(
            "'{codec}' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            data[start]
        )
    } else {
        format!(
            "'{codec}' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    };
    PyException::with_cps("UnicodeDecodeError", message_of(text))
}

/// `bytes.decode('utf-8', errors)`。`surrogate_pass` が真なら符号化された surrogate(3 バイト)を通す。
pub fn decode_utf8(data: &[u8], surrogate_pass: bool) -> Result<Vec<u32>, PyException> {
    let mut out = Vec::with_capacity(data.len());
    let mut s = 0;
    let len = data.len();
    let cont = |b: u8| (b & 0xc0) == 0x80;
    while s < len {
        let ch = data[s];
        if ch < 0x80 {
            out.push(u32::from(ch));
            s += 1;
            continue;
        }
        // (終わりの位置, 理由, 開始から数えた誤りの長さ)。成功は None。
        let mut failure: Option<(&str, usize)> = None;
        let mut advance = 0;
        let mut code = 0;
        if ch < 0xc2 {
            failure = Some(("invalid start byte", 1));
        } else if ch < 0xe0 {
            if len - s < 2 {
                failure = Some(("unexpected end of data", len - s));
            } else if !cont(data[s + 1]) {
                failure = Some(("invalid continuation byte", 1));
            } else {
                code = (u32::from(ch & 0x1f) << 6) | u32::from(data[s + 1] & 0x3f);
                advance = 2;
            }
        } else if ch < 0xf0 {
            if len - s < 3 {
                if len - s < 2 {
                    failure = Some(("unexpected end of data", len - s));
                } else {
                    let ch2 = data[s + 1];
                    let bad = !cont(ch2) || if ch2 < 0xa0 { ch == 0xe0 } else { ch == 0xed };
                    failure = Some(if bad {
                        ("invalid continuation byte", 1)
                    } else {
                        ("unexpected end of data", len - s)
                    });
                }
            } else {
                let (ch2, ch3) = (data[s + 1], data[s + 2]);
                if !cont(ch2) || (ch == 0xe0 && ch2 < 0xa0) || (ch == 0xed && ch2 >= 0xa0) {
                    failure = Some(("invalid continuation byte", 1));
                } else if !cont(ch3) {
                    failure = Some(("invalid continuation byte", 2));
                } else {
                    code = (u32::from(ch & 0x0f) << 12)
                        | (u32::from(ch2 & 0x3f) << 6)
                        | u32::from(ch3 & 0x3f);
                    advance = 3;
                }
            }
        } else if ch < 0xf5 {
            if len - s < 4 {
                if len - s < 2 {
                    failure = Some(("unexpected end of data", len - s));
                } else {
                    let ch2 = data[s + 1];
                    if !cont(ch2) || if ch2 < 0x90 { ch == 0xf0 } else { ch == 0xf4 } {
                        failure = Some(("invalid continuation byte", 1));
                    } else if len - s < 3 {
                        failure = Some(("unexpected end of data", len - s));
                    } else if !cont(data[s + 2]) {
                        failure = Some(("invalid continuation byte", 2));
                    } else {
                        failure = Some(("unexpected end of data", len - s));
                    }
                }
            } else {
                let (ch2, ch3, ch4) = (data[s + 1], data[s + 2], data[s + 3]);
                if !cont(ch2) || (ch == 0xf0 && ch2 < 0x90) || (ch == 0xf4 && ch2 >= 0x90) {
                    failure = Some(("invalid continuation byte", 1));
                } else if !cont(ch3) {
                    failure = Some(("invalid continuation byte", 2));
                } else if !cont(ch4) {
                    failure = Some(("invalid continuation byte", 3));
                } else {
                    code = (u32::from(ch & 0x07) << 18)
                        | (u32::from(ch2 & 0x3f) << 12)
                        | (u32::from(ch3 & 0x3f) << 6)
                        | u32::from(ch4 & 0x3f);
                    advance = 4;
                }
            }
        } else {
            failure = Some(("invalid start byte", 1));
        }
        match failure {
            None => {
                out.push(code);
                s += advance;
            }
            Some((reason, size)) => {
                if surrogate_pass
                    && len - s >= 3
                    && (data[s] & 0xf0) == 0xe0
                    && cont(data[s + 1])
                    && cont(data[s + 2])
                {
                    let code = (u32::from(data[s] & 0x0f) << 12)
                        | (u32::from(data[s + 1] & 0x3f) << 6)
                        | u32::from(data[s + 2] & 0x3f);
                    if (0xd800..=0xdfff).contains(&code) {
                        out.push(code);
                        s += 3;
                        continue;
                    }
                }
                return Err(decode_error("utf-8", data, s, s + size, reason));
            }
        }
    }
    Ok(out)
}

fn is_surrogate(code: u32) -> bool {
    (0xd800..=0xdfff).contains(&code)
}

/// `bytes.decode('utf-16', 'surrogatepass')`(`byteorder` が 0 なら BOM で決め、-1 は LE・1 は BE)。
pub fn decode_utf16(data: &[u8], byteorder: i8) -> Result<Vec<u32>, PyException> {
    let mut bo = byteorder;
    let mut q = 0;
    if bo == 0 && data.len() >= 2 {
        let bom = (u32::from(data[1]) << 8) | u32::from(data[0]);
        if bom == 0xfeff {
            q = 2;
            bo = -1;
        } else if bom == 0xfffe {
            q = 2;
            bo = 1;
        }
    }
    let codec = match bo {
        -1 => "utf-16-le",
        1 => "utf-16-be",
        _ => "utf-16",
    };
    let unit = |at: usize| -> u32 {
        if bo == 1 {
            (u32::from(data[at]) << 8) | u32::from(data[at + 1])
        } else {
            (u32::from(data[at + 1]) << 8) | u32::from(data[at])
        }
    };
    let mut out = Vec::new();
    let end = data.len();
    while q < end {
        if end - q < 2 {
            return Err(decode_error(codec, data, q, end, "truncated data"));
        }
        let ch = unit(q);
        q += 2;
        if !is_surrogate(ch) {
            out.push(ch);
            continue;
        }
        // 誤りの範囲 [start, start + width) の先頭の 1 文字だけを surrogatepass が通す。
        let (reason, start, stop) = if end - q < 2 {
            ("unexpected end of data", q - 2, end)
        } else if ch < 0xdc00 {
            let ch2 = unit(q);
            if (0xdc00..=0xdfff).contains(&ch2) {
                q += 2;
                out.push(0x10000 + ((ch - 0xd800) << 10) + (ch2 - 0xdc00));
                continue;
            }
            ("illegal UTF-16 surrogate", q - 2, q)
        } else {
            ("illegal encoding", q - 2, q)
        };
        // surrogatepass: 誤りの先頭の 2 バイトが surrogate なら通して、その直後から続ける。
        if end - start >= 2 && is_surrogate(unit(start)) {
            out.push(unit(start));
            q = start + 2;
            continue;
        }
        return Err(decode_error(
            codec,
            data,
            start,
            stop.max(start + 1),
            reason,
        ));
    }
    Ok(out)
}

/// `bytes.decode('utf-32', 'surrogatepass')`(`byteorder` の意味は utf-16 と同じ)。
pub fn decode_utf32(data: &[u8], byteorder: i8) -> Result<Vec<u32>, PyException> {
    let mut bo = byteorder;
    let mut q = 0;
    let word = |at: usize, bo: i8| -> u32 {
        let b = &data[at..at + 4];
        if bo == 1 {
            u32::from_be_bytes([b[0], b[1], b[2], b[3]])
        } else {
            u32::from_le_bytes([b[0], b[1], b[2], b[3]])
        }
    };
    if bo == 0 && data.len() >= 4 {
        let bom = word(0, -1);
        if bom == 0x0000_feff {
            bo = -1;
            q = 4;
        } else if bom == 0xfffe_0000 {
            bo = 1;
            q = 4;
        }
    }
    let codec = match bo {
        -1 => "utf-32-le",
        1 => "utf-32-be",
        _ => "utf-32",
    };
    let end = data.len();
    let mut out = Vec::new();
    while q < end {
        if end - q < 4 {
            return Err(decode_error(codec, data, q, end, "truncated data"));
        }
        let ch = word(q, bo);
        if ch > 0x10ffff {
            return Err(decode_error(
                codec,
                data,
                q,
                q + 4,
                "code point not in range(0x110000)",
            ));
        }
        if is_surrogate(ch) {
            // surrogatepass が通す。
            out.push(ch);
            q += 4;
            continue;
        }
        out.push(ch);
        q += 4;
    }
    Ok(out)
}

/// `json.detect_encoding` の結果。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Encoding {
    Utf8,
    Utf8Sig,
    Utf16,
    Utf16Le,
    Utf16Be,
    Utf32,
    Utf32Le,
    Utf32Be,
}

pub fn detect_encoding(b: &[u8]) -> Encoding {
    if b.starts_with(&[0xff, 0xfe, 0, 0]) || b.starts_with(&[0, 0, 0xfe, 0xff]) {
        return Encoding::Utf32;
    }
    if b.starts_with(&[0xff, 0xfe]) || b.starts_with(&[0xfe, 0xff]) {
        return Encoding::Utf16;
    }
    if b.starts_with(&[0xef, 0xbb, 0xbf]) {
        return Encoding::Utf8Sig;
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 {
                Encoding::Utf16Be
            } else {
                Encoding::Utf32Be
            };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                Encoding::Utf16Le
            } else {
                Encoding::Utf32Le
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return Encoding::Utf16Be;
        }
        if b[1] == 0 {
            return Encoding::Utf16Le;
        }
    }
    Encoding::Utf8
}

/// `s.decode(json.detect_encoding(s), 'surrogatepass')`。
pub fn decode_json_bytes(raw: &[u8]) -> Result<Vec<u32>, PyException> {
    match detect_encoding(raw) {
        Encoding::Utf8 => decode_utf8(raw, true),
        Encoding::Utf8Sig => decode_utf8(&raw[3..], true),
        Encoding::Utf16 => decode_utf16(raw, 0),
        Encoding::Utf16Le => decode_utf16(raw, -1),
        Encoding::Utf16Be => decode_utf16(raw, 1),
        Encoding::Utf32 => decode_utf32(raw, 0),
        Encoding::Utf32Le => decode_utf32(raw, -1),
        Encoding::Utf32Be => decode_utf32(raw, 1),
    }
}

// ---------------------------------------------------------------- 読取り

/// `json.JSONDecodeError`。位置は文字番号。
pub fn json_decode_error(message: &str, text: &[u32], pos: usize) -> PyException {
    let before = &text[..pos.min(text.len())];
    let line = before.iter().filter(|&&c| c == 0x0a).count() + 1;
    let column = match before.iter().rposition(|&c| c == 0x0a) {
        Some(index) => pos - index,
        None => pos + 1,
    };
    PyException::new(
        "JSONDecodeError",
        &format!("{message}: line {line} column {column} (char {pos})"),
    )
}

/// 解析中に呼ばれる `object_pairs_hook` と `parse_constant` の取り決め。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Hooks {
    /// 既定の decoder(重複キーは最後の値、`NaN`・`Infinity` を受ける)。
    None,
    /// `_no_duplicate_object` と、`Stop` を投げる `parse_constant`。
    Strict {
        duplicate: &'static str,
        constant: &'static str,
    },
}

enum Failure {
    /// scanner の `StopIteration(idx)`。
    Stop(usize),
    Error(PyException),
}

impl From<PyException> for Failure {
    fn from(error: PyException) -> Self {
        Failure::Error(error)
    }
}

struct Scanner<'a> {
    text: &'a [u32],
    hooks: Hooks,
    /// いまの再帰の深さ(`tstate->recursion_depth` 相当)。
    depth: usize,
    max_digits: usize,
}

fn is_json_space(c: u32) -> bool {
    matches!(c, 0x20 | 0x09 | 0x0a | 0x0d)
}

fn is_digit(c: u32) -> bool {
    (0x30..=0x39).contains(&c)
}

impl Scanner<'_> {
    fn error(&self, message: &str, pos: usize) -> Failure {
        // C から `JSONDecodeError` を作る呼出しも、再帰の深さを使う。
        if let Err(error) = json_error_calls(self.depth) {
            return Failure::Error(error);
        }
        Failure::Error(json_decode_error(message, self.text, pos))
    }

    fn enter(&mut self, what: &str) -> Result<(), Failure> {
        self.depth += 1;
        if self.depth > RECURSION_LIMIT {
            self.depth -= 1;
            return Err(Failure::Error(PyException::recursion(&format!(
                " while decoding a JSON {what} from a unicode string"
            ))));
        }
        Ok(())
    }

    fn skip_space(&self, mut idx: usize) -> usize {
        while idx < self.text.len() && is_json_space(self.text[idx]) {
            idx += 1;
        }
        idx
    }

    fn at(&self, idx: usize) -> Option<u32> {
        self.text.get(idx).copied()
    }

    fn matches(&self, idx: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(i, c)| self.at(idx + i) == Some(u32::from(c)))
    }

    fn constant(&self, value: f64) -> Result<PyValue, Failure> {
        match self.hooks {
            Hooks::None => {
                // 既定の decoder の `_CONSTANTS.__getitem__` は、C の呼出し 1 回分。
                enter_call(self.depth + 1, Call::Native)?;
                Ok(PyValue::Float(value))
            }
            Hooks::Strict { constant, .. } => {
                // lambda のフレーム、`Stop(...)` と `.throw` の呼出し、generator のフレーム。
                enter_call(self.depth + 1, Call::Frame)?;
                enter_call(self.depth + 2, Call::Native)?;
                enter_call(self.depth + 3, Call::Frame)?;
                Err(Failure::Error(PyException::stop(constant)))
            }
        }
    }

    fn scan_once(&mut self, idx: usize) -> Result<(PyValue, usize), Failure> {
        let Some(c) = self.at(idx) else {
            return Err(Failure::Stop(idx));
        };
        match c {
            0x22 => {
                let (value, next) = self.scan_string(idx + 1)?;
                Ok((PyValue::Str(value), next))
            }
            0x7b => {
                self.enter("object")?;
                let result = self.parse_object(idx + 1);
                self.depth -= 1;
                result
            }
            0x5b => {
                self.enter("array")?;
                let result = self.parse_array(idx + 1);
                self.depth -= 1;
                result
            }
            0x6e if self.matches(idx, "null") => Ok((PyValue::None, idx + 4)),
            0x74 if self.matches(idx, "true") => Ok((PyValue::Bool(true), idx + 4)),
            0x66 if self.matches(idx, "false") => Ok((PyValue::Bool(false), idx + 5)),
            0x4e if self.matches(idx, "NaN") => Ok((self.constant(f64::NAN)?, idx + 3)),
            0x49 if self.matches(idx, "Infinity") => Ok((self.constant(f64::INFINITY)?, idx + 8)),
            0x2d if self.matches(idx, "-Infinity") => {
                Ok((self.constant(f64::NEG_INFINITY)?, idx + 9))
            }
            _ => self.match_number(idx),
        }
    }

    fn match_number(&self, start: usize) -> Result<(PyValue, usize), Failure> {
        let len = self.text.len();
        let end_idx = len - 1;
        let mut idx = start;
        if self.text[idx] == 0x2d {
            idx += 1;
            if idx > end_idx {
                return Err(Failure::Stop(start));
            }
        }
        let c = self.text[idx];
        if (0x31..=0x39).contains(&c) {
            idx += 1;
            while idx <= end_idx && is_digit(self.text[idx]) {
                idx += 1;
            }
        } else if c == 0x30 {
            idx += 1;
        } else {
            return Err(Failure::Stop(start));
        }
        let mut is_float = false;
        if idx < end_idx && self.text[idx] == 0x2e && is_digit(self.text[idx + 1]) {
            is_float = true;
            idx += 2;
            while idx <= end_idx && is_digit(self.text[idx]) {
                idx += 1;
            }
        }
        if idx < end_idx && matches!(self.text[idx], 0x65 | 0x45) {
            let exponent_start = idx;
            idx += 1;
            if idx < end_idx && matches!(self.text[idx], 0x2b | 0x2d) {
                idx += 1;
            }
            while idx <= end_idx && is_digit(self.text[idx]) {
                idx += 1;
            }
            if is_digit(self.text[idx - 1]) {
                is_float = true;
            } else {
                idx = exponent_start;
            }
        }
        let number: String = self.text[start..idx]
            .iter()
            .map(|&c| char::from_u32(c).expect("ASCII"))
            .collect();
        if is_float {
            return Ok((PyValue::Float(number.parse().expect("float")), idx));
        }
        let int = parse_int(&cps(&number), self.max_digits)
            .map_err(|error| Failure::Error(error.exception(&cps(&number))))?;
        Ok((PyValue::Int(int), idx))
    }

    fn scan_string(&self, begin_after_quote: usize) -> Result<(CodepointString, usize), Failure> {
        let text = self.text;
        let len = text.len();
        let begin = begin_after_quote - 1;
        let mut end = begin_after_quote;
        let mut out: Vec<u32> = Vec::new();
        loop {
            let mut next = end;
            let mut found = None;
            while next < len {
                let d = text[next];
                if d == 0x22 || d == 0x5c {
                    found = Some(d);
                    break;
                }
                if d <= 0x1f {
                    return Err(self.error("Invalid control character at", next));
                }
                next += 1;
            }
            let Some(c) = found else {
                return Err(self.error("Unterminated string starting at", begin));
            };
            out.extend_from_slice(&text[end..next]);
            next += 1;
            if c == 0x22 {
                end = next;
                break;
            }
            if next == len {
                return Err(self.error("Unterminated string starting at", begin));
            }
            let c = text[next];
            if c != 0x75 {
                end = next + 1;
                let mapped = match c {
                    0x22 | 0x5c | 0x2f => c,
                    0x62 => 8,
                    0x66 => 12,
                    0x6e => 10,
                    0x72 => 13,
                    0x74 => 9,
                    _ => return Err(self.error("Invalid \\escape", end - 2)),
                };
                out.push(mapped);
            } else {
                next += 1;
                end = next + 4;
                if end >= len {
                    return Err(self.error("Invalid \\uXXXX escape", next - 1));
                }
                let mut value = 0u32;
                for &digit in &text[next..end] {
                    value = (value << 4) | self.hex(digit, end - 5)?;
                }
                next = end;
                if (0xd800..0xdc00).contains(&value)
                    && end + 6 < len
                    && text[next] == 0x5c
                    && text[next + 1] == 0x75
                {
                    next += 2;
                    end += 6;
                    let mut low = 0u32;
                    for &digit in &text[next..end] {
                        low = (low << 4) | self.hex(digit, end - 5)?;
                    }
                    if (0xdc00..0xe000).contains(&low) {
                        value = 0x10000 + ((value - 0xd800) << 10) + (low - 0xdc00);
                    } else {
                        end -= 6;
                    }
                }
                out.push(value);
            }
        }
        Ok((
            CodepointString::try_from_codepoints(out).expect("範囲内"),
            end,
        ))
    }

    fn hex(&self, digit: u32, position: usize) -> Result<u32, Failure> {
        match digit {
            0x30..=0x39 => Ok(digit - 0x30),
            0x61..=0x66 => Ok(digit - 0x61 + 10),
            0x41..=0x46 => Ok(digit - 0x41 + 10),
            _ => Err(self.error("Invalid \\uXXXX escape", position)),
        }
    }

    fn parse_object(&mut self, start: usize) -> Result<(PyValue, usize), Failure> {
        let mut idx = self.skip_space(start);
        let mut pairs: Vec<(CodepointString, PyValue)> = Vec::new();
        if self.at(idx) != Some(0x7d) {
            loop {
                if self.at(idx) != Some(0x22) {
                    return Err(
                        self.error("Expecting property name enclosed in double quotes", idx)
                    );
                }
                let (key, next) = self.scan_string(idx + 1)?;
                idx = self.skip_space(next);
                if self.at(idx) != Some(0x3a) {
                    return Err(self.error("Expecting ':' delimiter", idx));
                }
                idx = self.skip_space(idx + 1);
                let (value, next) = self.scan_once(idx)?;
                pairs.push((key, value));
                idx = self.skip_space(next);
                if self.at(idx) == Some(0x7d) {
                    break;
                }
                if self.at(idx) != Some(0x2c) {
                    return Err(self.error("Expecting ',' delimiter", idx));
                }
                idx = self.skip_space(idx + 1);
            }
        }
        let next = idx + 1;
        let mut dict = PyDict::new();
        match self.hooks {
            Hooks::None => {
                for (key, value) in pairs {
                    dict.insert(PyValue::Str(key), value)?;
                }
            }
            Hooks::Strict { duplicate, .. } => {
                // `_no_duplicate_object(pairs)` の呼出し(Python のフレーム 1 つ)。
                enter_call(self.depth + 1, Call::Frame)?;
                for (key, value) in pairs {
                    let key = PyValue::Str(key);
                    if dict.get(&key)?.is_some() {
                        enter_call(self.depth + 2, Call::Native)?;
                        return Err(Failure::Error(PyException::stop(duplicate)));
                    }
                    dict.insert(key, value)?;
                }
            }
        }
        Ok((PyValue::Dict(dict), next))
    }

    fn parse_array(&mut self, start: usize) -> Result<(PyValue, usize), Failure> {
        let mut idx = self.skip_space(start);
        let mut items = Vec::new();
        if self.at(idx) != Some(0x5d) {
            loop {
                let (value, next) = self.scan_once(idx)?;
                items.push(value);
                idx = self.skip_space(next);
                if self.at(idx) == Some(0x5d) {
                    break;
                }
                if self.at(idx) != Some(0x2c) {
                    return Err(self.error("Expecting ',' delimiter", idx));
                }
                idx = self.skip_space(idx + 1);
            }
        }
        if self.at(idx) != Some(0x5d) {
            return Err(self.error("Expecting value", self.text.len() - 1));
        }
        Ok((PyValue::List(items), idx + 1))
    }
}

/// `JSONDecoder.decode(text)`。`entry_depth` は scanner を呼ぶ時点の再帰の深さ。
fn decode_text(
    text: &[u32],
    hooks: Hooks,
    frame: usize,
    max_digits: usize,
) -> Result<PyValue, PyException> {
    // `frame` は `loads` のフレーム。`decode` は frame + 1、`raw_decode` は frame + 2。
    let mut scanner = Scanner {
        text,
        hooks,
        depth: frame + 3,
        max_digits,
    };
    let start = scanner.skip_space(0);
    let (value, end) = match scanner.scan_once(start) {
        Ok(found) => found,
        Err(Failure::Stop(at)) => {
            // `except StopIteration` の後、`raw_decode` のフレームで作る。
            json_error_calls(frame + 2)?;
            return Err(json_decode_error("Expecting value", text, at));
        }
        Err(Failure::Error(error)) => return Err(error),
    };
    let end = scanner.skip_space(end);
    if end != text.len() {
        json_error_calls(frame + 1)?;
        return Err(json_decode_error("Extra data", text, end));
    }
    Ok(value)
}

/// `JSONDecodeError(...)` を作る呼出し。`depth` は呼ぶ側の再帰の深さ。
/// 型の呼出し、`__init__` のフレーム、その中の `str.count` などの呼出しが、深さを使う。
fn json_error_calls(depth: usize) -> Result<(), PyException> {
    enter_call(depth + 1, Call::Native)?;
    enter_call(depth + 2, Call::Frame)?;
    enter_call(depth + 3, Call::Native)
}

/// 呼出しの深さの確認。`depth` は、その呼出しで使う再帰の深さ(上限を超えると `RecursionError`)。
/// 旧版の呼出しの種類ごとに、文面が違う。
#[derive(Clone, Copy)]
pub(crate) enum Call {
    /// Python の関数のフレーム。
    Frame,
    /// C の型の呼出し(`tp_call`)。
    Native,
    /// `isinstance(x, (型, 型))` の中の再帰。
    InstanceCheck,
    /// 比較(`PyObject_RichCompare`)の中の再帰。
    Comparison,
}

pub(crate) fn enter_call(depth: usize, call: Call) -> Result<(), PyException> {
    if depth <= RECURSION_LIMIT {
        return Ok(());
    }
    Err(PyException::recursion(match call {
        Call::Frame => "",
        Call::Native => " while calling a Python object",
        Call::InstanceCheck => " in __instancecheck__",
        Call::Comparison => " in comparison",
    }))
}

/// `json.loads` が、`decode` を経て scanner を呼ぶまでの呼出し。`frame` は `loads` のフレームの深さ。
/// `custom` は `object_pairs_hook` などの引数がある呼出し(`JSONDecoder(**kw)` を作る)。
fn decoder_chain(frame: usize, custom: bool) -> Result<(), PyException> {
    if custom {
        // `cls(**kw)`: 型の呼出し、`__init__` のフレーム、その中の `make_scanner`。
        enter_call(frame + 1, Call::Native)?;
        enter_call(frame + 2, Call::Frame)?;
        enter_call(frame + 3, Call::Native)?;
    }
    // `decode` のフレーム、その中の正規表現の呼出し、`raw_decode` のフレーム、scanner(`tp_call`)。
    enter_call(frame + 1, Call::Frame)?;
    enter_call(frame + 2, Call::Native)?;
    enter_call(frame + 2, Call::Frame)?;
    enter_call(frame + 3, Call::Native)
}

/// `json.loads(bytes)`(`ctx.depth` は `json.loads` を呼ぶフレームの深さ)。
pub fn loads_bytes(raw: &[u8], hooks: Hooks, ctx: PyCtx) -> Result<PyValue, PyException> {
    let frame = ctx.depth + 1;
    enter_call(frame, Call::Frame)?;
    // `isinstance(s, str)`、`isinstance(s, (bytes, bytearray))`、`detect_encoding`(フレームと中の呼出し)、`s.decode`。
    enter_call(frame + 1, Call::Native)?;
    enter_call(frame + 2, Call::InstanceCheck)?;
    enter_call(frame + 1, Call::Frame)?;
    enter_call(frame + 2, Call::Native)?;
    let text = decode_json_bytes(raw)?;
    decoder_chain(frame, hooks != Hooks::None)?;
    decode_text(&text, hooks, frame, ctx.max_str_digits)
}

/// `json.loads(str)`。先頭が U+FEFF なら旧版と同じ `JSONDecodeError`。
pub fn loads_str(text: &[u32], hooks: Hooks, ctx: PyCtx) -> Result<PyValue, PyException> {
    let frame = ctx.depth + 1;
    enter_call(frame, Call::Frame)?;
    // `isinstance(s, str)` と `s.startswith`。
    enter_call(frame + 1, Call::Native)?;
    if text.first() == Some(&0xfeff) {
        json_error_calls(frame)?;
        return Err(json_decode_error(
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            text,
            0,
        ));
    }
    decoder_chain(frame, hooks != Hooks::None)?;
    decode_text(text, hooks, frame, ctx.max_str_digits)
}

// ---------------------------------------------------------------- 3 種の読取り

/// 控え: `json.loads(raw)`(bytes。`caller` の深さは呼び出した関数のフレーム)。
pub fn load_state(raw: &[u8], ctx: PyCtx) -> Result<PyValue, PyException> {
    loads_bytes(raw, Hooks::None, ctx)
}

/// `strict_settings(raw)`。`ctx.depth` は `strict_settings` を呼ぶフレームの深さ。
pub fn strict_settings(raw: &[u8], ctx: PyCtx) -> Result<PyValue, PyException> {
    const HOST: &str = "host-settings";
    enter_call(ctx.depth + 1, Call::Frame)?;
    let inner = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    // `raw.decode('utf-8')`(呼出しと、失敗したときの例外の作成)。
    let decoded = enter_call(inner.depth + 1, Call::Native).and_then(|()| {
        decode_utf8(raw, false).map_err(|error| {
            enter_call(inner.depth + 2, Call::Native)
                .err()
                .unwrap_or(error)
        })
    });
    let parsed = decoded.and_then(|text| {
        loads_str(
            &text,
            Hooks::Strict {
                duplicate: HOST,
                constant: HOST,
            },
            inner,
        )
    });
    let value = match parsed {
        Ok(value) => value,
        Err(error)
            if matches!(
                error.type_name.as_str(),
                "UnicodeDecodeError" | "JSONDecodeError" | "RecursionError"
            ) =>
        {
            // `raise Stop(...) from None`: `Stop(...)` の呼出し。
            enter_call(inner.depth + 1, Call::Native)?;
            return Err(PyException::stop(HOST).without_context());
        }
        Err(error) => return Err(error),
    };
    enter_call(inner.depth + 1, Call::Native)?;
    if !matches!(value, PyValue::Dict(_)) {
        return Err(PyException::stop(HOST));
    }
    walk_settings(&value, 0, inner.depth)?;
    Ok(value)
}

/// `walk(item, depth)`。`frame_depth` は `strict_settings` のフレームの深さ。
fn walk_settings(item: &PyValue, level: usize, frame_depth: usize) -> Result<(), PyException> {
    // `walk` のフレームと、その中の `isinstance`・`Stop(...)` の呼出し。
    enter_call(frame_depth + 1 + level, Call::Frame)?;
    enter_call(frame_depth + 2 + level, Call::Native)?;
    if level > 100 {
        return Err(PyException::stop("host-settings"));
    }
    match item {
        PyValue::Dict(dict) => {
            for child in dict.values() {
                walk_settings(child, level + 1, frame_depth)?;
            }
        }
        PyValue::List(list) => {
            for child in list {
                walk_settings(child, level + 1, frame_depth)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// `plugin_inventory(raw)`。`ctx.depth` は `plugin_inventory` を呼ぶフレームの深さ。
pub fn plugin_inventory(raw: &[u8], ctx: PyCtx) -> Result<Vec<PyValue>, PyException> {
    enter_call(ctx.depth + 1, Call::Frame)?;
    let inner = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    let value = loads_bytes(
        raw,
        Hooks::Strict {
            duplicate: "host-settings",
            constant: "plugin 一覧の形式が不正",
        },
        inner,
    )?;
    // `isinstance(value, list)`・`set()`・`sorted(...)` などの呼出し。
    enter_call(inner.depth + 1, Call::Native)?;
    let PyValue::List(items) = value else {
        return Err(PyException::stop("有効 plugin 一覧が配列でない"));
    };
    let mut active: Vec<PyValue> = Vec::new();
    let mut ids: Vec<CodepointString> = Vec::new();
    for item in items {
        let PyValue::Dict(dict) = &item else {
            return Err(PyException::stop("plugin 一覧の項目が不正"));
        };
        let enabled = dict
            .get_str("enabled")
            .cloned()
            .unwrap_or(PyValue::Bool(true));
        let PyValue::Bool(enabled) = enabled else {
            return Err(PyException::stop("plugin 一覧の項目が不正"));
        };
        if !enabled {
            continue;
        }
        match dict.get_str("installPath") {
            Some(PyValue::Str(path)) if PyPath::new(path.as_codepoints()).is_absolute() => {}
            _ => {
                return Err(PyException::stop(
                    "有効 plugin の installPath を解決できない",
                ));
            }
        }
        let id = match dict.get_str("id") {
            Some(PyValue::Str(id)) if !id.as_codepoints().is_empty() && !ids.contains(id) => {
                id.clone()
            }
            _ => return Err(PyException::stop("有効 plugin の ID が一意でない")),
        };
        ids.push(id);
        active.push(item);
    }
    // sorted(key=lambda x: json.dumps(x, sort_keys=True)): `sorted`(P+1)→`list.sort`(P+2)→lambda(P+3)
    // の中で `json.dumps` を呼ぶ。
    enter_call(inner.depth + 2, Call::Native)?;
    let mut keyed = Vec::with_capacity(active.len());
    for item in active {
        enter_call(inner.depth + 3, Call::Frame)?;
        let key = dumps(
            &item,
            DumpOptions::SORT_KEY,
            PyCtx {
                depth: inner.depth + 3,
                ..inner
            },
        )?;
        keyed.push((key, item));
    }
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(keyed.into_iter().map(|(_, item)| item).collect())
}

// ---------------------------------------------------------------- 書き出し

/// `json.dumps` の引数。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DumpOptions {
    pub ensure_ascii: bool,
    pub sort_keys: bool,
    /// `separators=(',', ':')`。偽なら既定の `, ` と `: `。
    pub compact: bool,
}

impl DumpOptions {
    /// 控え・`component_policy_digest` の材料。
    pub const STATE: Self = Self {
        ensure_ascii: true,
        sort_keys: true,
        compact: true,
    };
    /// 受領。
    pub const RECEIPT: Self = Self {
        ensure_ascii: true,
        sort_keys: false,
        compact: false,
    };
    /// `host-paths`・`host-settings`。
    pub const HOST: Self = Self {
        ensure_ascii: true,
        sort_keys: false,
        compact: true,
    };
    /// `host-allowlist` の 1 行目。
    pub const ALLOWLIST: Self = Self {
        ensure_ascii: false,
        sort_keys: false,
        compact: false,
    };
    /// 一覧の並べ替えの鍵。
    pub const SORT_KEY: Self = Self {
        ensure_ascii: true,
        sort_keys: true,
        compact: false,
    };
}

struct Encoder {
    options: DumpOptions,
    depth: usize,
    max_digits: usize,
}

fn escape_string(text: &[u32], ensure_ascii: bool, out: &mut Vec<u32>) {
    out.push(0x22);
    for &c in text {
        match c {
            0x22 => out.extend(cps("\\\"")),
            0x5c => out.extend(cps("\\\\")),
            0x0a => out.extend(cps("\\n")),
            0x0d => out.extend(cps("\\r")),
            0x09 => out.extend(cps("\\t")),
            0x08 => out.extend(cps("\\b")),
            0x0c => out.extend(cps("\\f")),
            0x20..=0x7e => out.push(c),
            _ if c < 0x20 => out.extend(cps(&format!("\\u{c:04x}"))),
            _ if !ensure_ascii => out.push(c),
            0..=0xffff => out.extend(cps(&format!("\\u{c:04x}"))),
            _ => {
                let pair = c - 0x10000;
                out.extend(cps(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (pair >> 10),
                    0xdc00 + (pair & 0x3ff)
                )));
            }
        }
    }
    out.push(0x22);
}

fn float_text(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_owned()
    } else if value.is_infinite() {
        if value > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        float_repr(value)
    }
}

impl Encoder {
    fn enter(&mut self) -> Result<(), PyException> {
        self.depth += 1;
        if self.depth > RECURSION_LIMIT {
            self.depth -= 1;
            return Err(PyException::recursion(" while encoding a JSON object"));
        }
        Ok(())
    }

    fn int_text(&self, value: &PyInt) -> Result<String, PyException> {
        value.to_decimal(self.max_digits)
    }

    fn key_text(&self, key: &PyValue) -> Result<Vec<u32>, PyException> {
        Ok(match key {
            PyValue::Str(s) => s.as_codepoints().to_vec(),
            PyValue::Float(f) => cps(&float_text(*f)),
            PyValue::Bool(true) => cps("true"),
            PyValue::Bool(false) => cps("false"),
            PyValue::None => cps("null"),
            PyValue::Int(i) => cps(&self.int_text(i)?),
            other => {
                return Err(PyException::type_error(&format!(
                    "keys must be str, int, float, bool or None, not {}",
                    other.type_name()
                )));
            }
        })
    }

    fn encode(&mut self, value: &PyValue, out: &mut Vec<u32>) -> Result<(), PyException> {
        match value {
            PyValue::None => out.extend(cps("null")),
            PyValue::Bool(true) => out.extend(cps("true")),
            PyValue::Bool(false) => out.extend(cps("false")),
            PyValue::Int(i) => out.extend(cps(&self.int_text(i)?)),
            PyValue::Float(f) => out.extend(cps(&float_text(*f))),
            PyValue::Str(s) => escape_string(s.as_codepoints(), self.options.ensure_ascii, out),
            PyValue::Bytes(_) => {
                return Err(PyException::type_error(
                    "Object of type bytes is not JSON serializable",
                ));
            }
            PyValue::List(items) => {
                self.enter()?;
                if items.is_empty() {
                    out.extend(cps("[]"));
                    self.depth -= 1;
                    return Ok(());
                }
                out.push(0x5b);
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.extend(self.item_separator());
                    }
                    self.encode(item, out)?;
                }
                out.push(0x5d);
                self.depth -= 1;
            }
            PyValue::Dict(dict) => {
                self.enter()?;
                if dict.is_empty() {
                    out.extend(cps("{}"));
                    self.depth -= 1;
                    return Ok(());
                }
                out.push(0x7b);
                let mut items: Vec<(&PyValue, &PyValue)> = dict.iter().collect();
                if self.options.sort_keys {
                    if items.len() > 1 {
                        // 項目の比較(`(キー, 値)` の組の比較)が、再帰の深さを 1 使う。
                        enter_call(self.depth + 1, Call::Comparison)?;
                    }
                    sort_items(&mut items)?;
                }
                for (index, (key, item)) in items.into_iter().enumerate() {
                    if index > 0 {
                        out.extend(self.item_separator());
                    }
                    escape_string(&self.key_text(key)?, self.options.ensure_ascii, out);
                    out.extend(if self.options.compact {
                        cps(":")
                    } else {
                        cps(": ")
                    });
                    self.encode(item, out)?;
                }
                out.push(0x7d);
                self.depth -= 1;
            }
        }
        Ok(())
    }

    fn item_separator(&self) -> Vec<u32> {
        if self.options.compact {
            cps(",")
        } else {
            cps(", ")
        }
    }
}

/// `sorted(dct.items())`。キーが文字列だけ、または数だけなら Python と同じ順にする。
fn sort_items(items: &mut [(&PyValue, &PyValue)]) -> Result<(), PyException> {
    if items.iter().all(|(k, _)| k.is_str()) {
        items.sort_by(|a, b| {
            a.0.as_str()
                .map(CodepointString::as_codepoints)
                .cmp(&b.0.as_str().map(CodepointString::as_codepoints))
        });
        return Ok(());
    }
    let numeric = |v: &PyValue| matches!(v, PyValue::Bool(_) | PyValue::Int(_) | PyValue::Float(_));
    if items.iter().all(|(k, _)| numeric(k)) {
        let as_f64 = |v: &PyValue| match v {
            PyValue::Bool(b) => f64::from(u8::from(*b)),
            PyValue::Int(i) => i.to_f64().unwrap_or(f64::INFINITY),
            PyValue::Float(f) => *f,
            _ => f64::NAN,
        };
        items.sort_by(|a, b| as_f64(a.0).total_cmp(&as_f64(b.0)));
        return Ok(());
    }
    if items.iter().any(|(k, _)| matches!(k, PyValue::None)) && items.len() > 1 {
        let (a, b) = (items[1].0.type_name(), items[0].0.type_name());
        return Err(PyException::type_error(&format!(
            "'<' not supported between instances of '{a}' and '{b}'"
        )));
    }
    let a = items.get(1).map_or("str", |i| i.0.type_name());
    let b = items.first().map_or("str", |i| i.0.type_name());
    Err(PyException::type_error(&format!(
        "'<' not supported between instances of '{a}' and '{b}'"
    )))
}

/// `json.dumps(value, ...)`。`ctx.depth` は `json.dumps` を呼ぶフレームの深さ。
pub fn dumps(value: &PyValue, options: DumpOptions, ctx: PyCtx) -> Result<Vec<u32>, PyException> {
    let frame = ctx.depth + 1;
    enter_call(frame, Call::Frame)?;
    // 既定の引数だけなら `_default_encoder.encode`。ほかは `JSONEncoder(...)` を作ってから `encode`。
    if options != DumpOptions::RECEIPT {
        enter_call(frame + 1, Call::Native)?;
        enter_call(frame + 2, Call::Frame)?;
    }
    enter_call(frame + 1, Call::Frame)?;
    enter_call(frame + 2, Call::Native)?;
    // 文字列だけは `encode` の近道で、`iterencode` と encoder の呼出しを通らない。
    if !matches!(value, PyValue::Str(_)) {
        enter_call(frame + 2, Call::Frame)?;
        enter_call(frame + 3, Call::Native)?;
    }
    let mut encoder = Encoder {
        options,
        depth: frame + 3,
        max_digits: ctx.max_str_digits,
    };
    let mut out = Vec::new();
    encoder.encode(value, &mut out)?;
    Ok(out)
}

/// 控え(`dump`): `json.dumps(ensure_ascii=True, sort_keys=True, separators=(',', ':'))` と末尾の LF。
/// 旧版の `dump(value)` は Python の関数なので、`json.dumps` はその 1 段下から呼ばれる。
/// `ctx.depth` は `dump` を呼ぶフレームの深さ。
pub fn dump_state(value: &PyValue, ctx: PyCtx) -> Result<Vec<u8>, PyException> {
    // `dump` 自身のフレームの確認。
    enter_call(ctx.depth + 1, Call::Frame)?;
    let inner = PyCtx {
        depth: ctx.depth + 1,
        ..ctx
    };
    let mut text = dumps(value, DumpOptions::STATE, inner)?;
    text.push(0x0a);
    Ok(text.iter().map(|&c| c as u8).collect())
}

#[cfg(test)]
mod tests {
    use super::super::pystr::to_string;
    use super::super::testing::{cps_of, error_of, fixture, from_hex, to_typed, with_big_stack};
    use super::*;

    fn ctx() -> PyCtx {
        PyCtx::new(5)
    }

    fn dumped(value: &PyValue, options: DumpOptions) -> String {
        to_string(&dumps(value, options, ctx()).unwrap())
    }

    #[test]
    fn detect_encoding_follows_json_module() {
        for (bytes, expect) in [
            (&b"{}"[..], Encoding::Utf8),
            (&b"\xef\xbb\xbf{}"[..], Encoding::Utf8Sig),
            (&b"\xff\xfe{\x00"[..], Encoding::Utf16),
            (&b"\xfe\xff\x00{"[..], Encoding::Utf16),
            (&b"\xff\xfe\x00\x00{\x00\x00\x00"[..], Encoding::Utf32),
            (&b"\x00\x00\x00{"[..], Encoding::Utf32Be),
            (&b"\x00{\x00}"[..], Encoding::Utf16Be),
            (&b"{\x00}\x00"[..], Encoding::Utf16Le),
            (&b"{\x00\x00\x00"[..], Encoding::Utf32Le),
            (&b"\x00{"[..], Encoding::Utf16Be),
            (&b"{\x00"[..], Encoding::Utf16Le),
            (&b"{\x00}"[..], Encoding::Utf8),
        ] {
            assert_eq!(detect_encoding(bytes), expect, "{bytes:?}");
        }
    }

    #[test]
    fn dumps_covers_the_five_output_shapes() {
        let mut dict = PyDict::new();
        dict.insert_str("b", PyValue::from(1));
        dict.insert_str(
            "a",
            PyValue::List(vec![
                PyValue::None,
                PyValue::Float(1.5),
                PyValue::Bool(true),
            ]),
        );
        let surrogate_value =
            CodepointString::try_from_codepoints(vec![0xd800, 0x78, 0x2f, 0x80]).unwrap();
        dict.insert_str("é\u{1f600}\u{7f}\n\"", PyValue::Str(surrogate_value));
        let value = PyValue::Dict(dict);
        assert_eq!(
            dumped(&value, DumpOptions::STATE),
            "{\"a\":[null,1.5,true],\"b\":1,\"\\u00e9\\ud83d\\ude00\\u007f\\n\\\"\":\"\\ud800x/\\u0080\"}"
        );
        assert_eq!(
            dumped(&value, DumpOptions::RECEIPT),
            "{\"b\": 1, \"a\": [null, 1.5, true], \"\\u00e9\\ud83d\\ude00\\u007f\\n\\\"\": \"\\ud800x/\\u0080\"}"
        );
        assert_eq!(
            dumped(&value, DumpOptions::HOST),
            "{\"b\":1,\"a\":[null,1.5,true],\"\\u00e9\\ud83d\\ude00\\u007f\\n\\\"\":\"\\ud800x/\\u0080\"}"
        );
        let mut expect =
            cps("{\"b\": 1, \"a\": [null, 1.5, true], \"é\u{1f600}\u{7f}\\n\\\"\": \"");
        expect.extend([0xd800, 0x78, 0x2f, 0x80, 0x22, 0x7d]);
        assert_eq!(
            dumps(&value, DumpOptions::ALLOWLIST, ctx()).unwrap(),
            expect
        );
        assert_eq!(
            dumped(&value, DumpOptions::SORT_KEY),
            "{\"a\": [null, 1.5, true], \"b\": 1, \"\\u00e9\\ud83d\\ude00\\u007f\\n\\\"\": \"\\ud800x/\\u0080\"}"
        );
        assert_eq!(dumped(&PyValue::List(vec![]), DumpOptions::STATE), "[]");
        assert_eq!(
            dumped(&PyValue::Dict(PyDict::new()), DumpOptions::RECEIPT),
            "{}"
        );
        assert_eq!(
            dumped(&PyValue::Float(f64::INFINITY), DumpOptions::STATE),
            "Infinity"
        );
        assert_eq!(
            dumped(&PyValue::Float(f64::NEG_INFINITY), DumpOptions::STATE),
            "-Infinity"
        );
        assert_eq!(dumped(&PyValue::Float(1e16), DumpOptions::STATE), "1e+16");
        assert_eq!(
            dump_state(&PyValue::List(vec![PyValue::from(1)]), ctx()).unwrap(),
            b"[1]\n"
        );
    }

    #[test]
    fn dumps_errors_have_the_python_types() {
        let bytes = PyValue::Bytes(vec![1]);
        assert_eq!(
            dumps(&bytes, DumpOptions::STATE, ctx())
                .unwrap_err()
                .type_name,
            "TypeError"
        );
        let big = PyValue::Int(PyInt::from_decimal(false, &"9".repeat(4301)));
        assert_eq!(
            dumps(&big, DumpOptions::STATE, ctx())
                .unwrap_err()
                .type_name,
            "ValueError"
        );
        let unlimited = PyCtx {
            max_str_digits: 0,
            ..ctx()
        };
        assert!(dumps(&big, DumpOptions::STATE, unlimited).is_ok());
        let mut dict = PyDict::new();
        dict.insert(PyValue::from(1), PyValue::None).unwrap();
        dict.insert_str("a", PyValue::None);
        assert_eq!(
            dumps(&PyValue::Dict(dict), DumpOptions::STATE, ctx())
                .unwrap_err()
                .type_name,
            "TypeError"
        );
        let mut keyed = PyDict::new();
        keyed.insert(PyValue::Float(1.5), PyValue::None).unwrap();
        keyed.insert(PyValue::Bool(true), PyValue::None).unwrap();
        keyed.insert(PyValue::None, PyValue::None).unwrap_or(());
        assert!(dumps(&PyValue::Dict(keyed), DumpOptions::RECEIPT, ctx()).is_ok());
    }

    fn expect_decode(
        result: Result<Vec<u32>, PyException>,
        expect: &serde_json::Value,
        label: &str,
    ) {
        match (result, expect.get("ok")) {
            (Ok(text), Some(ok)) => assert_eq!(text, cps_of(ok), "{label}"),
            (Err(error), None) => assert_eq!(error_of(&error), expect["error"], "{label}"),
            (result, _) => panic!("{label}: {result:?} / {expect}"),
        }
    }

    #[test]
    fn decode_cases_match_python_3_10() {
        let cases = fixture!("json_decode_cases.json");
        assert!(cases.as_array().unwrap().len() > 5000);
        for case in cases.as_array().unwrap() {
            let raw = from_hex(case["raw"].as_str().unwrap());
            expect_decode(
                decode_json_bytes(&raw),
                &case["json"],
                &format!("json {raw:02x?}"),
            );
            expect_decode(
                decode_utf8(&raw, false),
                &case["utf8"],
                &format!("utf8 {raw:02x?}"),
            );
        }
    }

    fn check_reader(
        case: &serde_json::Value,
        raw: &[u8],
        ctx_for: impl Fn(usize) -> PyCtx,
        summary: bool,
    ) {
        let label = || {
            format!(
                "{} {}",
                case["raw"]
                    .as_str()
                    .map_or(String::new(), |hex| hex.chars().take(120).collect()),
                case.get("kind").map_or(String::new(), |k| format!(
                    "{k} {} {}",
                    case["k"], case["site"]
                ))
            )
        };
        let compare = |name: &str, result: Result<serde_json::Value, PyException>| {
            let expect = &case[name];
            match (result, expect.get("ok")) {
                (Ok(value), Some(ok)) => {
                    if !summary {
                        assert_eq!(&value, ok, "{name} {}", label());
                    }
                }
                (Err(error), None) => {
                    assert_eq!(error_of(&error), expect["error"], "{name} {}", label())
                }
                (result, _) => panic!("{name} {}: {result:?} / {expect}", label()),
            }
        };
        let site = |name: &str| case[name]["site"].as_u64().unwrap() as usize;
        compare(
            "state",
            load_state(raw, ctx_for(site("state"))).map(|v| to_typed(&v)),
        );
        compare(
            "strict",
            strict_settings(raw, ctx_for(site("strict"))).map(|v| to_typed(&v)),
        );
        compare(
            "inventory",
            plugin_inventory(raw, ctx_for(site("inventory")))
                .map(|items| to_typed(&PyValue::List(items))),
        );
    }

    #[test]
    fn text_cases_match_python_3_10() {
        for name in [
            fixture!("json_text_cases.json"),
            fixture!("json_encoded_cases.json"),
        ] {
            assert!(name.as_array().unwrap().len() > 100);
            for case in name.as_array().unwrap() {
                let raw = from_hex(case["raw"].as_str().unwrap());
                check_reader(case, &raw, PyCtx::new, false);
            }
        }
    }

    #[test]
    fn integer_digit_limit_cases_match_python_3_10() {
        for case in fixture!("json_limit_cases.json").as_array().unwrap() {
            let raw = from_hex(case["raw"].as_str().unwrap());
            let limit = case["limit"].as_u64().unwrap() as usize;
            check_reader(
                case,
                &raw,
                |depth| PyCtx {
                    depth,
                    max_str_digits: limit,
                },
                false,
            );
        }
    }

    #[test]
    fn recursion_boundaries_match_python_3_10_for_loads() {
        with_big_stack(loads_boundaries);
    }

    fn loads_boundaries() {
        let cases = fixture!("json_depth_cases.json");
        let loads = cases["loads"].as_array().unwrap();
        assert!(loads.len() > 300);
        for case in loads {
            let k = case["k"].as_u64().unwrap() as usize;
            let (opening, closing, leaf) = if case["kind"] == "array" {
                ("[".repeat(k), "]".repeat(k), "")
            } else {
                ("{\"a\":".repeat(k), "}".repeat(k), "1")
            };
            if case["kind"] == "inventory" {
                let text = match case["shape"].as_str().unwrap() {
                    "one" => r#"[{"id":"a","installPath":"/x"}]"#,
                    "two" => r#"[{"id":"a","installPath":"/x"},{"id":"b","installPath":"/y"}]"#,
                    "pair" => r#"[{"id":"a","installPath":"/x","z":1}]"#,
                    "nested1" => r#"[{"id":"a","installPath":"/x","z":[1]}]"#,
                    _ => r#"[{"id":"a","installPath":"/x","z":[[[1]]]}]"#,
                };
                check_reader(case, text.as_bytes(), PyCtx::new, true);
                continue;
            }
            let text = match case["shape"].as_str().unwrap() {
                "valid" => format!("{opening}{leaf}{closing}"),
                "extra" => format!("{opening}{leaf}{closing}x"),
                "bad_leaf" => format!("{opening}x{closing}"),
                "unterminated" => format!("{opening}{leaf}"),
                "nan" => format!("{opening}NaN{closing}"),
                "ninf" => format!("{opening}-Infinity{closing}"),
                "inf" => format!("{opening}Infinity{closing}"),
                _ if case["kind"] == "object" => {
                    format!("{}{{x", opening.rsplit_once("{\"a\":").unwrap().0)
                }
                _ => format!("{opening},"),
            };
            check_reader(case, text.as_bytes(), PyCtx::new, true);
        }
    }

    fn form_options(form: &str) -> DumpOptions {
        match form {
            "state" => DumpOptions::STATE,
            "receipt" => DumpOptions::RECEIPT,
            "host" => DumpOptions::HOST,
            "allowlist" => DumpOptions::ALLOWLIST,
            _ => DumpOptions::SORT_KEY,
        }
    }

    #[test]
    fn dumps_of_loaded_values_match_python_3_10() {
        // 読んだ値を、書き出し 5 種の形で書き出した出力(surrogate・大きい整数・float を含む)。
        let cases = fixture!("json_text_cases.json");
        let mut count = 0;
        for case in cases.as_array().unwrap() {
            let Some(dumps_expect) = case.get("dumps") else {
                continue;
            };
            let raw = from_hex(case["raw"].as_str().unwrap());
            let value = load_state(
                &raw,
                PyCtx::new(case["state"]["site"].as_u64().unwrap() as usize),
            )
            .unwrap();
            for (form, expect) in dumps_expect.as_object().unwrap() {
                let site = expect["site"].as_u64().unwrap() as usize;
                let result = dumps(&value, form_options(form), PyCtx::new(site));
                match (result, expect.get("ok")) {
                    (Ok(text), Some(ok)) => assert_eq!(text, cps_of(ok), "{form} {}", case["raw"]),
                    (Err(error), None) => {
                        assert_eq!(error_of(&error), expect["error"], "{form} {}", case["raw"]);
                    }
                    (result, _) => panic!("{form} {}: {:?}", case["raw"], result.map(|_| ())),
                }
                count += 1;
            }
        }
        assert!(count > 800, "{count}");
    }

    #[test]
    fn equality_of_loaded_values_matches_python_3_10() {
        let cases = fixture!("json_equality_cases.json");
        assert!(cases.as_array().unwrap().len() > 1500);
        for case in cases.as_array().unwrap() {
            let load = |name: &str| {
                load_state(&from_hex(case[name].as_str().unwrap()), PyCtx::new(20)).unwrap()
            };
            let (left, right) = (load("a"), load("b"));
            let label = format!("{} {}", case["a"], case["b"]);
            assert_eq!(
                left.py_eq(&right),
                case["eq"].as_bool().unwrap(),
                "== {label}"
            );
            assert_eq!(
                left.py_ne(&right),
                case["ne"].as_bool().unwrap(),
                "!= {label}"
            );
        }
    }

    fn shape_options(shape: &str) -> DumpOptions {
        match shape {
            "receipt" => DumpOptions::RECEIPT,
            "allowlist" => DumpOptions::ALLOWLIST,
            _ => DumpOptions::STATE,
        }
    }

    #[test]
    fn recursion_boundaries_match_python_3_10_for_dumps() {
        with_big_stack(dumps_boundaries);
    }

    fn dumps_boundaries() {
        let cases = fixture!("json_depth_cases.json");
        let dumps_cases = cases["dumps"].as_array().unwrap();
        assert!(dumps_cases.len() > 200);
        for case in dumps_cases {
            let site = case["site"].as_u64().unwrap() as usize;
            let (value, options) = if let Some(k) = case["k"].as_u64() {
                let array = case["kind"] == "array";
                let shape = case["shape"].as_str().unwrap();
                let mut value = if shape == "empty" {
                    if array {
                        PyValue::List(vec![])
                    } else {
                        PyValue::Dict(PyDict::new())
                    }
                } else {
                    PyValue::from(1)
                };
                let rounds = if shape == "empty" { k - 1 } else { k };
                let two = shape == "state2";
                for _ in 0..rounds {
                    value = match (array, two) {
                        (true, false) => PyValue::List(vec![value]),
                        (true, true) => PyValue::List(vec![value, PyValue::from(1)]),
                        (false, _) => {
                            let mut dict = PyDict::new();
                            dict.insert_str("a", value);
                            if two {
                                dict.insert_str("b", PyValue::from(1));
                            }
                            PyValue::Dict(dict)
                        }
                    };
                }
                (value, shape_options(shape))
            } else {
                let value = match case["scalar"]["t"].as_str().unwrap() {
                    "str" => PyValue::from("text"),
                    "int" => PyValue::from(1),
                    "none" => PyValue::None,
                    "list" => PyValue::List(vec![]),
                    _ => PyValue::Dict(PyDict::new()),
                };
                let _ = &value;
                (value, shape_options(case["shape"].as_str().unwrap()))
            };
            let result = dumps(
                &value,
                options,
                PyCtx::new(case["site"].as_u64().unwrap() as usize),
            );
            let _ = site;
            match (result, case.get("ok")) {
                (Ok(_), Some(_)) => {}
                (Err(error), None) => assert_eq!(error_of(&error), case["error"], "{case}"),
                (result, _) => panic!("{case}: {:?}", result.map(|_| ())),
            }
        }
    }
}
