//! 診断の厳密な JSON・base64・確認用の値・応答の符号化。旧版 `diagnostic-runtime.py` の
//! `b64`・`unb64`・`encoded`・`digest`・`unique`・`decode`・`keys`・`fingerprint`・`metadata`(:29-70)と
//! `Runtime.response_bytes`(:338-343)と同じ結果にする。
//!
//! 読み取り(`decode`)は、本家 3.10.11 の `json.loads`(C の scanner)と同じ手順で読み、Python の再帰の深さを
//! 呼出しごとに数えて、旧版と同じ所で `RecursionError`(ここでは [`DecodeError::Recursion`])になる。
//! 数える呼出しの並びは、本家 3.10.11 の `Lib/json/__init__.py`・`Lib/json/decoder.py`・`Modules/_json.c`・
//! `Objects/exceptions.c` と、旧版の `diagnostic-runtime.py:21-27`・`:49-61` で確かめた。
//! 書き出し(`encoded`・`encoded_unicode`・`response_bytes`)は入れ子の深さを数えない
//! (呼出元が渡すのは浅い値だけで、上限には届かない)。
use super::budget::{Budget, Failure, Kind};
use dev_workflow_core::loop_state::CodepointString;
use dev_workflow_platform::diagnostic::Stat;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

/// Python の再帰の上限(`sys.getrecursionlimit()` の既定。旧版は `-I` で起動されるので変わらない)。
pub const RECURSION_LIMIT: usize = 1000;
/// 整数の 10 進の桁の上限(`sys.get_int_max_str_digits()` の既定。旧版は `-I` で起動されるので変わらない)。
pub const MAX_STR_DIGITS: usize = 4300;

fn schema() -> Failure {
    Failure::new("schema", 2)
}

// ---------------------------------------------------------------- 値

/// 任意の大きさの整数。10 進の正規の字面(先頭に 0 の無い数字と、負なら `-`。0 は `0`)で持つ。
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Int(String);

impl Int {
    /// 正規の字面だけを受ける(`-0`・先頭の 0・空・数字でない文字は受けない)。
    pub fn parse(text: &str) -> Option<Int> {
        let digits = text.strip_prefix('-').unwrap_or(text);
        let canonical = !digits.is_empty()
            && digits.bytes().all(|b| b.is_ascii_digit())
            && (digits == "0" || !digits.starts_with('0'))
            && !(text.starts_with('-') && digits == "0");
        canonical.then(|| Int(text.to_owned()))
    }

    /// 符号と数字の列から作る(先頭の 0 は落とし、`-0` は 0 にする)。`digits` は ASCII の数字だけ。
    fn from_digits(negative: bool, digits: &str) -> Int {
        let trimmed = digits.trim_start_matches('0');
        if trimmed.is_empty() {
            Int("0".to_owned())
        } else if negative {
            Int(format!("-{trimmed}"))
        } else {
            Int(trimmed.to_owned())
        }
    }

    /// 10 進の字面。
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// 符号を除いた 10 進の桁の数。
    pub fn digit_count(&self) -> usize {
        self.0.trim_start_matches('-').len()
    }

    /// `i64` に収まるときだけ取り出す。
    pub fn to_i64(&self) -> Option<i64> {
        self.0.parse().ok()
    }
}

impl From<i64> for Int {
    fn from(value: i64) -> Self {
        Int(value.to_string())
    }
}
impl From<u64> for Int {
    fn from(value: u64) -> Self {
        Int(value.to_string())
    }
}
impl From<i128> for Int {
    fn from(value: i128) -> Self {
        Int(value.to_string())
    }
}

/// 入れた順を保つ鍵と値の列(Python の dict)。同じ鍵を入れると、位置を保って値を置き換える。
/// 鍵で引く手間は、索引を持つので平均で定数。等しさは、並びも含めて比べる。
#[derive(Clone, Debug, Default)]
pub struct Dict {
    entries: Vec<(CodepointString, Value)>,
    index: HashMap<CodepointString, usize>,
}

impl PartialEq for Dict {
    fn eq(&self, other: &Self) -> bool {
        self.entries == other.entries
    }
}

impl Dict {
    pub fn new() -> Self {
        Self::default()
    }

    /// 入れる。前の値があれば、位置を保って置き換え、前の値を返す。
    pub fn insert(&mut self, key: CodepointString, value: Value) -> Option<Value> {
        if let Some(&position) = self.index.get(&key) {
            return Some(std::mem::replace(&mut self.entries[position].1, value));
        }
        self.index.insert(key.clone(), self.entries.len());
        self.entries.push((key, value));
        None
    }

    pub fn insert_str(&mut self, key: &str, value: Value) -> Option<Value> {
        self.insert(CodepointString::from(key), value)
    }

    pub fn get(&self, key: &CodepointString) -> Option<&Value> {
        self.index
            .get(key)
            .map(|&position| &self.entries[position].1)
    }

    pub fn get_str(&self, key: &str) -> Option<&Value> {
        self.get(&CodepointString::from(key))
    }

    pub fn contains_key(&self, key: &CodepointString) -> bool {
        self.index.contains_key(key)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// 入れた順の鍵と値。
    pub fn iter(&self) -> impl Iterator<Item = (&CodepointString, &Value)> {
        self.entries.iter().map(|(key, value)| (key, value))
    }

    pub fn keys(&self) -> impl Iterator<Item = &CodepointString> {
        self.entries.iter().map(|(key, _)| key)
    }

    /// 鍵の文字の番号の順に並べた組(`sorted(dct.items())`。鍵は重ならないので値は比べない)。
    fn sorted(&self) -> Vec<(&CodepointString, &Value)> {
        let mut items: Vec<_> = self.iter().collect();
        items.sort_by(|a, b| a.0.as_codepoints().cmp(b.0.as_codepoints()));
        items
    }
}

/// JSON の値。整数と真偽値は別の型である。
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(Int),
    Float(f64),
    Str(CodepointString),
    List(Vec<Value>),
    Dict(Dict),
}

impl Value {
    pub fn str(text: &str) -> Value {
        Value::Str(CodepointString::from(text))
    }
}

// ---------------------------------------------------------------- 読み取り

/// `decode` の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecodeError {
    /// 旧版の `fail('schema', 2)`(終了値 2)。
    Schema,
    /// 旧版の `RecursionError`(`decode` は受けずに外へ出す。入口では `internal`)。
    Recursion,
}

impl DecodeError {
    /// `Schema` を理由と終了値にする(`Recursion` は `Failure` ではない)。
    pub fn failure(self) -> Option<Failure> {
        match self {
            DecodeError::Schema => Some(schema()),
            DecodeError::Recursion => None,
        }
    }
}

/// 呼出し 1 つの深さの確かめ(`Py_EnterRecursiveCall`)。深さが上限を超えれば `RecursionError`。
fn enter(depth: usize) -> Result<(), DecodeError> {
    if depth > RECURSION_LIMIT {
        Err(DecodeError::Recursion)
    } else {
        Ok(())
    }
}

/// `fail(reason, code)` を深さ `frame` のフレームから呼ぶ: `fail` のフレーム・`Failure(...)` の型の呼出し・
/// `__init__` のフレーム・`super()` と `.__init__(reason)` の呼出し。
fn fail_chain(frame: usize) -> Result<(), DecodeError> {
    enter(frame + 1)?;
    enter(frame + 2)?;
    enter(frame + 3)?;
    enter(frame + 4)
}

/// `JSONDecodeError(msg, doc, pos)` を深さ `depth` から作る: 型の呼出し・`__init__` のフレーム・
/// その中の `doc.count`・`doc.rfind`・`ValueError.__init__` の呼出し。
fn json_error(depth: usize) -> Result<(), DecodeError> {
    enter(depth + 1)?;
    enter(depth + 2)?;
    enter(depth + 3)
}

/// scanner の中の失敗。
enum ScanFailure {
    /// `StopIteration(idx)`(`raw_decode` が「Expecting value」の `JSONDecodeError` にする)。
    Stop,
    /// `ValueError`(C の `raise_errmsg`・`scanstring` が作った `JSONDecodeError` と、整数の桁の超え)。
    /// 作るときの深さは確かめ済み。
    Value,
    /// `unique` か `parse_constant` から出た `Failure('schema', 2)`(`decode` の `except` は受けない)。
    Failure,
    Recursion,
}

impl From<DecodeError> for ScanFailure {
    fn from(error: DecodeError) -> Self {
        match error {
            DecodeError::Recursion => ScanFailure::Recursion,
            DecodeError::Schema => ScanFailure::Failure,
        }
    }
}

/// `decode(raw)`。`caller_depth` は、`decode` を呼ぶ Python のフレームの深さ(そのフレームを含む)。
///
/// 旧版の順に処理する: `raw.decode('utf-8')` → `json.loads(…, object_pairs_hook=unique, parse_constant=…)`。
/// 失敗は `Schema`(旧版の `schema`)か `Recursion`(旧版の `RecursionError`)。
pub fn decode(raw: &[u8], caller_depth: usize) -> Result<Value, DecodeError> {
    let frame = caller_depth + 1; // decode のフレーム
    enter(frame)?;
    // raw.decode('utf-8')(C のメソッドの呼出し)。失敗なら UnicodeDecodeError の作成と strict の処理の呼出し。
    enter(frame + 1)?;
    let Ok(text) = std::str::from_utf8(raw) else {
        enter(frame + 2)?;
        fail_chain(frame)?;
        return Err(DecodeError::Schema);
    };
    let text: Vec<u32> = text.chars().map(u32::from).collect();
    let loads = frame + 1; // json.loads のフレーム
    enter(loads)?;
    enter(loads + 1)?; // isinstance(s, str)
    enter(loads + 1)?; // s.startswith('\ufeff')
    if text.first() == Some(&0xfeff) {
        json_error(loads)?;
        fail_chain(frame)?;
        return Err(DecodeError::Schema);
    }
    // cls(**kw): 型の呼出し・JSONDecoder.__init__ のフレーム・make_scanner の型の呼出し。
    enter(loads + 1)?;
    enter(loads + 2)?;
    enter(loads + 3)?;
    let decoder = loads + 1; // JSONDecoder.decode のフレーム
    enter(decoder)?;
    enter(decoder + 1)?; // _w(s, 0)
    enter(decoder + 1)?; // .end()
    let raw_decode = decoder + 1;
    enter(raw_decode)?;
    enter(raw_decode + 1)?; // self.scan_once(s, idx)
    let scanner = Scanner {
        text: &text,
        base: raw_decode + 1,
    };
    let start = scanner.skip(0);
    let (value, end) = match scanner.scan(start) {
        Ok(found) => found,
        Err(ScanFailure::Stop) => {
            json_error(raw_decode)?;
            fail_chain(frame)?;
            return Err(DecodeError::Schema);
        }
        Err(ScanFailure::Value) => {
            fail_chain(frame)?;
            return Err(DecodeError::Schema);
        }
        Err(ScanFailure::Failure) => return Err(DecodeError::Schema),
        Err(ScanFailure::Recursion) => return Err(DecodeError::Recursion),
    };
    enter(decoder + 1)?; // _w(s, end)
    enter(decoder + 1)?; // .end()
    enter(decoder + 1)?; // len(s)
    if scanner.skip(end) != text.len() {
        json_error(decoder)?;
        fail_chain(frame)?;
        return Err(DecodeError::Schema);
    }
    Ok(value)
}

/// 入れ子の途中の値(明示の列。Rust の再帰を使わない)。
enum Open {
    List(Vec<Value>),
    Dict(Vec<(CodepointString, Value)>, CodepointString),
}

struct Scanner<'a> {
    text: &'a [u32],
    /// scanner を呼んだ時点の深さ(`scanner_call` の型の呼出しを含む)。
    base: usize,
}

fn is_space(c: u32) -> bool {
    matches!(c, 0x20 | 0x09 | 0x0a | 0x0d)
}

fn is_digit(c: u32) -> bool {
    (0x30..=0x39).contains(&c)
}

impl Scanner<'_> {
    fn at(&self, idx: usize) -> Option<u32> {
        self.text.get(idx).copied()
    }

    fn skip(&self, mut idx: usize) -> usize {
        while self.at(idx).is_some_and(is_space) {
            idx += 1;
        }
        idx
    }

    fn word(&self, idx: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(i, c)| self.at(idx + i) == Some(u32::from(c)))
    }

    /// C の `raise_errmsg`(深さ `depth` から `JSONDecodeError` を作る)。
    fn c_error(&self, depth: usize) -> ScanFailure {
        match json_error(depth) {
            Ok(()) => ScanFailure::Value,
            Err(_) => ScanFailure::Recursion,
        }
    }

    /// `scan_once_unicode` を、入れ子を明示の列にして読む。
    fn scan(&self, start: usize) -> Result<(Value, usize), ScanFailure> {
        let mut stack: Vec<Open> = Vec::new();
        let mut idx = start;
        'value: loop {
            // 深さ: scanner の呼出し + 開いている入れ子の数。
            let depth = self.base + stack.len();
            let mut value = match self.at(idx) {
                None => return Err(ScanFailure::Stop),
                Some(0x22) => {
                    let (text, next) = self.scan_string(idx + 1, depth)?;
                    idx = next;
                    Value::Str(text)
                }
                Some(0x7b) => {
                    enter(depth + 1)?;
                    idx = self.skip(idx + 1);
                    if self.at(idx) == Some(0x7d) {
                        idx += 1;
                        self.finish_dict(Vec::new(), depth + 1)?
                    } else {
                        let (key, next) = self.key(idx, depth + 1)?;
                        idx = next;
                        stack.push(Open::Dict(Vec::new(), key));
                        continue 'value;
                    }
                }
                Some(0x5b) => {
                    enter(depth + 1)?;
                    idx = self.skip(idx + 1);
                    if self.at(idx) == Some(0x5d) {
                        idx += 1;
                        Value::List(Vec::new())
                    } else {
                        stack.push(Open::List(Vec::new()));
                        continue 'value;
                    }
                }
                Some(0x6e) if self.word(idx, "null") => {
                    idx += 4;
                    Value::Null
                }
                Some(0x74) if self.word(idx, "true") => {
                    idx += 4;
                    Value::Bool(true)
                }
                Some(0x66) if self.word(idx, "false") => {
                    idx += 5;
                    Value::Bool(false)
                }
                Some(0x4e) if self.word(idx, "NaN") => return Err(self.constant(depth)),
                Some(0x49) if self.word(idx, "Infinity") => return Err(self.constant(depth)),
                Some(0x2d) if self.word(idx, "-Infinity") => return Err(self.constant(depth)),
                Some(_) => {
                    let (number, next) = self.number(idx)?;
                    idx = next;
                    number
                }
            };
            // 読めた値を、開いている入れ子へ渡す。
            loop {
                let depth = self.base + stack.len();
                match stack.last_mut() {
                    None => return Ok((value, idx)),
                    Some(Open::List(items)) => {
                        items.push(value);
                        idx = self.skip(idx);
                        if self.at(idx) == Some(0x5d) {
                            idx += 1;
                            let Some(Open::List(items)) = stack.pop() else {
                                unreachable!()
                            };
                            value = Value::List(items);
                            continue;
                        }
                        if self.at(idx) != Some(0x2c) {
                            return Err(self.c_error(depth));
                        }
                        idx = self.skip(idx + 1);
                        continue 'value;
                    }
                    Some(Open::Dict(pairs, key)) => {
                        pairs.push((std::mem::replace(key, CodepointString::from("")), value));
                        idx = self.skip(idx);
                        if self.at(idx) == Some(0x7d) {
                            idx += 1;
                            let Some(Open::Dict(pairs, _)) = stack.pop() else {
                                unreachable!()
                            };
                            value = self.finish_dict(pairs, depth)?;
                            continue;
                        }
                        if self.at(idx) != Some(0x2c) {
                            return Err(self.c_error(depth));
                        }
                        idx = self.skip(idx + 1);
                        let (next_key, next) = self.key(idx, depth)?;
                        *key = next_key;
                        idx = next;
                        continue 'value;
                    }
                }
            }
        }
    }

    /// 辞書の鍵と `:` を読み、値の始まりの位置を返す。`depth` は辞書の中の深さ。
    fn key(&self, idx: usize, depth: usize) -> Result<(CodepointString, usize), ScanFailure> {
        if self.at(idx) != Some(0x22) {
            return Err(self.c_error(depth));
        }
        let (key, next) = self.scan_string(idx + 1, depth)?;
        let idx = self.skip(next);
        if self.at(idx) != Some(0x3a) {
            return Err(self.c_error(depth));
        }
        Ok((key, self.skip(idx + 1)))
    }

    /// `object_pairs_hook`(旧版の `unique`)を深さ `depth`(辞書の中)から呼ぶ。鍵が重なれば `fail('schema', 2)`。
    fn finish_dict(
        &self,
        pairs: Vec<(CodepointString, Value)>,
        depth: usize,
    ) -> Result<Value, ScanFailure> {
        let frame = depth + 1;
        enter(frame)?;
        let mut dict = Dict::new();
        for (key, value) in pairs {
            if dict.contains_key(&key) {
                fail_chain(frame)?;
                return Err(ScanFailure::Failure);
            }
            dict.insert(key, value);
        }
        Ok(Value::Dict(dict))
    }

    /// `NaN`・`Infinity`・`-Infinity`: `parse_constant` の lambda のフレームから `fail('schema', 2)` を呼ぶ。
    fn constant(&self, depth: usize) -> ScanFailure {
        match enter(depth + 1).and_then(|()| fail_chain(depth + 1)) {
            Ok(()) => ScanFailure::Failure,
            Err(_) => ScanFailure::Recursion,
        }
    }

    /// `_match_number_unicode`(`parse_float` と `parse_int` は既定の型)。
    fn number(&self, start: usize) -> Result<(Value, usize), ScanFailure> {
        let text = self.text;
        let end_idx = text.len() - 1;
        let mut idx = start;
        if text[idx] == 0x2d {
            idx += 1;
            if idx > end_idx {
                return Err(ScanFailure::Stop);
            }
        }
        if (0x31..=0x39).contains(&text[idx]) {
            idx += 1;
            while idx <= end_idx && is_digit(text[idx]) {
                idx += 1;
            }
        } else if text[idx] == 0x30 {
            idx += 1;
        } else {
            return Err(ScanFailure::Stop);
        }
        let mut is_float = false;
        if idx < end_idx && text[idx] == 0x2e && is_digit(text[idx + 1]) {
            is_float = true;
            idx += 2;
            while idx <= end_idx && is_digit(text[idx]) {
                idx += 1;
            }
        }
        if idx < end_idx && matches!(text[idx], 0x65 | 0x45) {
            let exponent = idx;
            idx += 1;
            if idx < end_idx && matches!(text[idx], 0x2d | 0x2b) {
                idx += 1;
            }
            while idx <= end_idx && is_digit(text[idx]) {
                idx += 1;
            }
            if is_digit(text[idx - 1]) {
                is_float = true;
            } else {
                idx = exponent;
            }
        }
        let literal: String = text[start..idx]
            .iter()
            .map(|&c| char::from_u32(c).unwrap_or('?'))
            .collect();
        if is_float {
            // PyFloat_FromString: 最も近い 64 ビットの 2 進の小数(1e400 は無限大、-0.0 は負の 0)。
            let value: f64 = literal.parse().map_err(|_| ScanFailure::Value)?;
            return Ok((Value::Float(value), idx));
        }
        // PyLong_FromString: 桁(`-` を除く)が上限を超えれば ValueError(C の中で作り、深さを使わない)。
        let (negative, digits) = match literal.strip_prefix('-') {
            Some(digits) => (true, digits),
            None => (false, literal.as_str()),
        };
        if digits.len() > MAX_STR_DIGITS {
            return Err(ScanFailure::Value);
        }
        Ok((Value::Int(Int::from_digits(negative, digits)), idx))
    }

    fn hex(&self, digit: u32, depth: usize) -> Result<u32, ScanFailure> {
        match digit {
            0x30..=0x39 => Ok(digit - 0x30),
            0x61..=0x66 => Ok(digit - 0x61 + 10),
            0x41..=0x46 => Ok(digit - 0x41 + 10),
            _ => Err(self.c_error(depth)),
        }
    }

    /// `scanstring_unicode(pystr, end, strict=True)`。`depth` は呼ぶ側の深さ(誤りは C の `raise_errmsg`)。
    fn scan_string(
        &self,
        begin_after_quote: usize,
        depth: usize,
    ) -> Result<(CodepointString, usize), ScanFailure> {
        let text = self.text;
        let len = text.len();
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
                    return Err(self.c_error(depth));
                }
                next += 1;
            }
            let Some(c) = found else {
                return Err(self.c_error(depth));
            };
            out.extend_from_slice(&text[end..next]);
            next += 1;
            if c == 0x22 {
                end = next;
                break;
            }
            if next == len {
                return Err(self.c_error(depth));
            }
            let c = text[next];
            if c != 0x75 {
                end = next + 1;
                let mapped = match c {
                    0x22 | 0x5c | 0x2f => c,
                    0x62 => 0x08,
                    0x66 => 0x0c,
                    0x6e => 0x0a,
                    0x72 => 0x0d,
                    0x74 => 0x09,
                    _ => return Err(self.c_error(depth)),
                };
                out.push(mapped);
            } else {
                next += 1;
                end = next + 4;
                if end >= len {
                    return Err(self.c_error(depth));
                }
                let mut value = 0u32;
                for &digit in &text[next..end] {
                    value = (value << 4) | self.hex(digit, depth)?;
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
                        low = (low << 4) | self.hex(digit, depth)?;
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
        let string = CodepointString::try_from_codepoints(out).map_err(|_| ScanFailure::Value)?;
        Ok((string, end))
    }
}

// ---------------------------------------------------------------- 書き出し

/// 書き出しの失敗(旧版の `ValueError`。無限大と NaN・桁の超え・UTF-8 にできない孤立したサロゲート)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EncodeError {
    Value,
}

/// `response_bytes` の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResponseError {
    /// 予算の失敗(`interrupted`・`time-limit`・`json-limit`)。
    Budget(Failure),
    /// 値の誤り(旧版の `ValueError`)。
    Value,
}

/// 最短で元に戻る 10 進の数字(指数の形)。本家の `repr`(最短の桁の中で、元の値に最も近いもの)と同じにする。
///
/// 1. Rust の `{:e}` で最短の字面を作り、その桁の数を P とする。`{:e}` は、ちょうど 2 の累乗の小数で
///    元に戻る幅が下側だけ半分になることも扱うので、P は正しい。
/// 2. P 桁で最も近い 10 進(`{:.(P-1)e}`。ちょうど中間は偶数へ丸める)を作る。
/// 3. 2 が元の値に戻れば 2 を、戻らなければ 1 を使う。同じ桁の候補が 2 つ並ぶ中間の値では、`{:e}` は上の側を
///    選ぶが、本家は偶数の側を選ぶ(2 が戻る)。2 の累乗で 2 が下側の半分の幅の外に出るときは 1 が正しい。
fn shortest_scientific(value: f64) -> String {
    let shortest = format!("{value:e}");
    let mantissa = shortest
        .split_once('e')
        .map_or(shortest.as_str(), |(m, _)| m);
    let precision = mantissa.bytes().filter(u8::is_ascii_digit).count().max(1) - 1;
    let rounded = format!("{value:.precision$e}");
    if rounded.parse::<f64>() == Ok(value) {
        rounded
    } else {
        shortest
    }
}

/// `float.__repr__`: 最短で元に戻る 10 進。指数は 10 の 16 乗以上か 10 の −4 乗未満で使う。
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    let sci = shortest_scientific(value.abs());
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let sign = if value.is_sign_negative() { "-" } else { "" };
    let body = if (-4..16).contains(&exponent) {
        if exponent >= 0 {
            let split = exponent as usize + 1;
            if digits.len() > split {
                format!("{}.{}", &digits[..split], &digits[split..])
            } else {
                format!("{digits}{}.0", "0".repeat(split - digits.len()))
            }
        } else {
            format!("0.{}{digits}", "0".repeat((-exponent - 1) as usize))
        }
    } else {
        let (head, rest) = digits.split_at(1);
        let mantissa = if rest.is_empty() {
            head.to_owned()
        } else {
            format!("{head}.{rest}")
        };
        let sign_e = if exponent < 0 { '-' } else { '+' };
        format!("{mantissa}e{sign_e}{:02}", exponent.abs())
    };
    format!("{sign}{body}")
}

fn int_text(value: &Int) -> Result<&str, EncodeError> {
    if value.digit_count() > MAX_STR_DIGITS {
        Err(EncodeError::Value)
    } else {
        Ok(value.as_str())
    }
}

/// `allow_nan=False` の `floatstr`。
fn float_strict(value: f64) -> Result<String, EncodeError> {
    if value.is_finite() {
        Ok(float_repr(value))
    } else {
        Err(EncodeError::Value)
    }
}

/// `allow_nan=True` の `floatstr`(`Infinity`・`-Infinity`・`NaN`)。
fn float_lenient(value: f64) -> String {
    if value.is_nan() {
        "NaN".to_owned()
    } else if value.is_infinite() {
        if value > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        float_repr(value)
    }
}

fn hex4(out: &mut String, unit: u32) {
    out.push_str(&format!("\\u{unit:04x}"));
}

/// `encode_basestring_ascii`(C の `ascii_escape_unicode`)。
fn ascii_string(text: &CodepointString, out: &mut String) {
    out.push('"');
    for &c in text.as_codepoints() {
        match c {
            0x5c => out.push_str("\\\\"),
            0x22 => out.push_str("\\\""),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x20..=0x7e => out.push(char::from(c as u8)),
            0x10000.. => {
                let pair = c - 0x10000;
                hex4(out, 0xd800 | (pair >> 10));
                hex4(out, 0xdc00 | (pair & 0x3ff));
            }
            _ => hex4(out, c),
        }
    }
    out.push('"');
}

/// `encode_basestring`(C の `escape_unicode`)の後の `.encode('utf-8')`。孤立したサロゲートは書けない。
fn unicode_string(text: &CodepointString, out: &mut String) -> Result<(), EncodeError> {
    out.push('"');
    for &c in text.as_codepoints() {
        match c {
            0x5c => out.push_str("\\\\"),
            0x22 => out.push_str("\\\""),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x00..=0x1f => hex4(out, c),
            _ => out.push(char::from_u32(c).ok_or(EncodeError::Value)?),
        }
    }
    out.push('"');
    Ok(())
}

/// `json.dumps(value, ensure_ascii, sort_keys=True, separators=(',', ':'), allow_nan)` の文字列。
fn dump(value: &Value, ascii: bool, out: &mut String) -> Result<(), EncodeError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(int) => out.push_str(int_text(int)?),
        Value::Float(float) => {
            if ascii {
                out.push_str(&float_strict(*float)?);
            } else {
                out.push_str(&float_lenient(*float));
            }
        }
        Value::Str(text) => {
            if ascii {
                ascii_string(text, out);
            } else {
                unicode_string(text, out)?;
            }
        }
        Value::List(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                dump(item, ascii, out)?;
            }
            out.push(']');
        }
        Value::Dict(dict) => {
            out.push('{');
            for (index, (key, item)) in dict.sorted().into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                if ascii {
                    ascii_string(key, out);
                } else {
                    unicode_string(key, out)?;
                }
                out.push(':');
                dump(item, ascii, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// `encoded(value)`: `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
/// allow_nan=False).encode('utf-8')`。無限大・NaN・10 進の桁が 4300 を超える整数は `Value`。
pub fn encoded(value: &Value) -> Result<Vec<u8>, EncodeError> {
    let mut out = String::new();
    dump(value, true, &mut out)?;
    Ok(out.into_bytes())
}

/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')`
/// (`diagnostic-plan.py:22` の `effective`)。無限大と NaN は `Infinity`・`-Infinity`・`NaN` と書く。
/// 孤立したサロゲートと、10 進の桁が 4300 を超える整数は `Value`。
pub fn encoded_unicode(value: &Value) -> Result<Vec<u8>, EncodeError> {
    let mut out = String::new();
    dump(value, false, &mut out)?;
    Ok(out.into_bytes())
}

/// 文字列・数・真偽値・null の区切りの字面(入れ子は `None`)。
fn scalar(value: &Value) -> Option<Result<String, EncodeError>> {
    Some(match value {
        Value::Str(text) => {
            let mut out = String::new();
            ascii_string(text, &mut out);
            Ok(out)
        }
        Value::Null => Ok("null".to_owned()),
        Value::Bool(true) => Ok("true".to_owned()),
        Value::Bool(false) => Ok("false".to_owned()),
        Value::Int(int) => int_text(int).map(str::to_owned),
        Value::Float(float) => float_strict(*float),
        Value::List(_) | Value::Dict(_) => return None,
    })
}

/// 区切り 1 つを予算の `json` に足して書く。
fn emit(chunk: &str, budget: &mut Budget, out: &mut Vec<u8>) -> Result<(), ResponseError> {
    budget
        .charge(Kind::Json, chunk.len() as u64)
        .map_err(ResponseError::Budget)?;
    out.extend_from_slice(chunk.as_bytes());
    Ok(())
}

fn value_error(_: EncodeError) -> ResponseError {
    ResponseError::Value
}

/// `_make_iterencode` の `_iterencode`(区切りは Python で書いた符号化と同じ)。
fn iterencode(value: &Value, budget: &mut Budget, out: &mut Vec<u8>) -> Result<(), ResponseError> {
    match value {
        Value::List(items) => iterencode_list(items, budget, out),
        Value::Dict(dict) => iterencode_dict(dict, budget, out),
        other => {
            let text = scalar(other)
                .expect("文字列・数・真偽値・null")
                .map_err(value_error)?;
            emit(&text, budget, out)
        }
    }
}

fn iterencode_list(
    items: &[Value],
    budget: &mut Budget,
    out: &mut Vec<u8>,
) -> Result<(), ResponseError> {
    if items.is_empty() {
        return emit("[]", budget, out);
    }
    let mut buf = "[";
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            buf = ",";
        }
        match scalar(item) {
            Some(text) => {
                let text = text.map_err(value_error)?;
                emit(&format!("{buf}{text}"), budget, out)?;
            }
            None => {
                emit(buf, budget, out)?;
                iterencode(item, budget, out)?;
            }
        }
    }
    emit("]", budget, out)
}

fn iterencode_dict(
    dict: &Dict,
    budget: &mut Budget,
    out: &mut Vec<u8>,
) -> Result<(), ResponseError> {
    if dict.is_empty() {
        return emit("{}", budget, out);
    }
    emit("{", budget, out)?;
    for (index, (key, item)) in dict.sorted().into_iter().enumerate() {
        if index > 0 {
            emit(",", budget, out)?;
        }
        let mut text = String::new();
        ascii_string(key, &mut text);
        emit(&text, budget, out)?;
        emit(":", budget, out)?;
        match scalar(item) {
            Some(text) => emit(&text.map_err(value_error)?, budget, out)?,
            None => iterencode(item, budget, out)?,
        }
    }
    emit("}", budget, out)
}

/// `Runtime.response_bytes(value)`: `JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':'),
/// allow_nan=False).iterencode(value)` の区切りごとに UTF-8 のバイト数を予算の `json` に足し、最後に改行の
/// 1 を足して、全体と改行を返す。途中で失敗したら、部分の結果を返さない。
pub fn response_bytes(value: &Value, budget: &mut Budget) -> Result<Vec<u8>, ResponseError> {
    let mut out = Vec::new();
    iterencode(value, budget, &mut out)?;
    budget
        .charge(Kind::Json, 1)
        .map_err(ResponseError::Budget)?;
    out.push(b'\n');
    Ok(out)
}

// ---------------------------------------------------------------- base64・確認用の値・鍵・6 欄

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// `b64(value)`: 標準の base64(`+` と `/`・`=` の埋め)。
pub fn b64(value: &[u8]) -> String {
    let mut out = String::with_capacity(value.len().div_ceil(3) * 4);
    for chunk in value.chunks(3) {
        let bytes = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let word = (u32::from(bytes[0]) << 16) | (u32::from(bytes[1]) << 8) | u32::from(bytes[2]);
        for position in 0..4 {
            if position <= chunk.len() {
                let index = (word >> (18 - 6 * position)) & 0x3f;
                out.push(char::from(ALPHABET[index as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// 正規の base64 だけを復号する(長さが 4 の倍数・字母の外の文字が無い・埋めの位置が正しい・余りのビットが 0)。
fn canonical_base64(text: &[u32]) -> Option<Vec<u8>> {
    if !text.len().is_multiple_of(4) {
        return None;
    }
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let groups = text.len() / 4;
    for (group, chunk) in text.chunks(4).enumerate() {
        let last = group + 1 == groups;
        let mut values = [0u32; 4];
        let mut padding = 0;
        for (position, &c) in chunk.iter().enumerate() {
            if c == u32::from(b'=') {
                // 埋めは最後の組の後ろの 1〜2 文字だけ。
                if !last || position < 2 {
                    return None;
                }
                padding += 1;
                continue;
            }
            if padding > 0 {
                return None;
            }
            let index = ALPHABET.iter().position(|&a| u32::from(a) == c)?;
            values[position] = index as u32;
        }
        let word = (values[0] << 18) | (values[1] << 12) | (values[2] << 6) | values[3];
        let bytes = [(word >> 16) as u8, (word >> 8) as u8, word as u8];
        let kept = 3 - padding;
        // 余りのビット(埋めで捨てる部分)が 0 でなければ、正規の形でない。
        if bytes[kept..].iter().any(|&b| b != 0) {
            return None;
        }
        out.extend_from_slice(&bytes[..kept]);
    }
    Some(out)
}

/// `unb64(value)`: 値が文字列で、正規の base64(`b64` で戻したとき同じ文字列)で、復号に NUL が無いときだけ
/// bytes を返す。ほかは `schema`(2)。旧版の 3 つの検査(ASCII でない・字母と埋めの形・正規の形)を、
/// 「正規の形か」の 1 つで判定する(正規の形なら旧版の 3 つを全部通り、そうでなければどれかで落ちる)。
pub fn unb64(value: &Value) -> Result<Vec<u8>, Failure> {
    let Value::Str(text) = value else {
        return Err(schema());
    };
    let raw = canonical_base64(text.as_codepoints()).ok_or_else(schema)?;
    if raw.contains(&0) {
        return Err(schema());
    }
    Ok(raw)
}

/// `digest(value)`: sha256 の小文字の 16 進 64 桁。
pub fn digest(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Python の `str.isspace()`(`str.split()` の区切り)。
fn is_python_space(c: char) -> bool {
    matches!(
        u32::from(c),
        0x09..=0x0d
            | 0x1c..=0x20
            | 0x85
            | 0xa0
            | 0x1680
            | 0x2000..=0x200a
            | 0x2028
            | 0x2029
            | 0x202f
            | 0x205f
            | 0x3000
    )
}

/// `keys(value, names)`: 辞書でないか、鍵の集合が `names.split()` の集合と違えば `schema`(2)。並びは見ない。
pub fn keys(value: &Value, names: &str) -> Result<(), Failure> {
    let Value::Dict(dict) = value else {
        return Err(schema());
    };
    let wanted: std::collections::HashSet<Vec<u32>> = names
        .split(is_python_space)
        .filter(|name| !name.is_empty())
        .map(|name| name.chars().map(u32::from).collect())
        .collect();
    let found: std::collections::HashSet<Vec<u32>> = dict
        .keys()
        .map(|key| key.as_codepoints().to_vec())
        .collect();
    if found == wanted {
        Ok(())
    } else {
        Err(schema())
    }
}

/// `fingerprint(s)`: 状態の 6 欄(dev・ino・mode・size・mtime_ns・ctime_ns)。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Fingerprint {
    pub dev: i64,
    pub ino: u64,
    pub mode: u32,
    pub size: i64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

pub fn fingerprint(stat: &Stat) -> Fingerprint {
    Fingerprint {
        dev: stat.dev,
        ino: stat.ino,
        mode: stat.mode,
        size: stat.size,
        mtime_ns: stat.mtime_ns,
        ctime_ns: stat.ctime_ns,
    }
}

/// `metadata(s)`: 6 欄と `type: 'regular'` の辞書(旧版と同じ入れた順)。
pub fn metadata(stat: &Stat) -> Value {
    let print = fingerprint(stat);
    let mut dict = Dict::new();
    dict.insert_str("dev", Value::Int(Int::from(print.dev)));
    dict.insert_str("ino", Value::Int(Int::from(print.ino)));
    dict.insert_str("mode", Value::Int(Int::from(i64::from(print.mode))));
    dict.insert_str("size", Value::Int(Int::from(print.size)));
    dict.insert_str("mtime_ns", Value::Int(Int::from(print.mtime_ns)));
    dict.insert_str("ctime_ns", Value::Int(Int::from(print.ctime_ns)));
    dict.insert_str("type", Value::str("regular"));
    Value::Dict(dict)
}

#[cfg(test)]
mod tests {
    use super::super::budget::BudgetOp;
    use super::*;

    const DEPTH: usize = 5;

    fn cps(values: &[u32]) -> CodepointString {
        CodepointString::try_from_codepoints(values.to_vec()).unwrap()
    }

    fn ok(raw: &[u8]) -> Value {
        decode(raw, DEPTH).unwrap_or_else(|error| panic!("{raw:?}: {error:?}"))
    }

    fn int(text: &str) -> Value {
        Value::Int(Int::parse(text).unwrap())
    }

    fn fresh() -> Budget {
        Budget::for_test(300.0, Box::new(|| 0.0), Kind::ALL.map(Kind::default_limit))
    }

    #[test]
    fn decode_reads_every_type_and_keeps_the_order() {
        let value =
            ok(b" {\"b\": [1, -0, 2.5, -0.0, 1e400, true, false, null], \"a\": \"x\\u00e9\"} ");
        let Value::Dict(dict) = &value else {
            panic!("{value:?}")
        };
        assert_eq!(
            dict.keys()
                .map(|k| k.as_codepoints().to_vec())
                .collect::<Vec<_>>(),
            vec![vec![0x62], vec![0x61]]
        );
        let Some(Value::List(items)) = dict.get_str("b") else {
            panic!()
        };
        assert_eq!(items[0], int("1"));
        assert_eq!(items[1], int("0"));
        assert_eq!(items[2], Value::Float(2.5));
        assert!(matches!(items[3], Value::Float(f) if f == 0.0 && f.is_sign_negative()));
        assert!(matches!(items[4], Value::Float(f) if f.is_infinite() && f > 0.0));
        assert_eq!(
            items[5..],
            [Value::Bool(true), Value::Bool(false), Value::Null]
        );
        assert_eq!(dict.get_str("a"), Some(&Value::Str(cps(&[0x78, 0xe9]))));
        assert_eq!(ok(b"-1e400"), Value::Float(f64::NEG_INFINITY));
        assert_eq!(ok(b"1e-400"), Value::Float(0.0));
    }

    #[test]
    fn decode_keeps_lone_surrogates_and_joins_pairs() {
        assert_eq!(ok(b"\"\\ud800\""), Value::Str(cps(&[0xd800])));
        assert_eq!(
            ok(b"\"\\udc00\\ud800\""),
            Value::Str(cps(&[0xdc00, 0xd800]))
        );
        assert_eq!(ok(b"\"\\ud83d\\ude00\""), Value::Str(cps(&[0x1f600])));
        assert_eq!(ok(b"\"\\ud83d\\u0041\""), Value::Str(cps(&[0xd83d, 0x41])));
        // 組の後ろに閉じる引用符が無いと、組にしない(`end + 6 < len`)。
        assert_eq!(decode(b"\"\\ud83d\\ude00", DEPTH), Err(DecodeError::Schema));
        assert_eq!(ok(b"\"\\uABcd\""), Value::Str(cps(&[0xabcd])));
    }

    #[test]
    fn decode_rejects_what_the_legacy_rejects() {
        for raw in [
            &b"\xef\xbb\xbf0"[..],
            b"0 x",
            b"\"\x01\"",
            b"\"\\q\"",
            b"\"\\u12\"",
            b"\"abc",
            b"NaN",
            b"[Infinity]",
            b"{\"a\": -Infinity}",
            b"{\"a\":1,\"a\":2}",
            b"{\"a\":1,\"\\u0061\":2}",
            b"[{\"x\":{\"a\":1,\"a\":2}}]",
            b"",
            b"  ",
            b"\xff",
            b"\xed\xa0\x80",
            b"\xc0\x80",
            b"[1,]",
            b"{\"a\":1,}",
            b"01",
            b"-",
            b"[",
            b"{",
        ] {
            assert_eq!(decode(raw, DEPTH), Err(DecodeError::Schema), "{raw:?}");
        }
        // BOM は文字列の先頭だけを見る(後ろの U+FEFF は値の外の余分なデータ)。
        assert_eq!(
            decode("0\u{feff}".as_bytes(), DEPTH),
            Err(DecodeError::Schema)
        );
        assert_eq!(
            decode("\"\u{feff}\"".as_bytes(), DEPTH),
            Ok(Value::Str(cps(&[0xfeff])))
        );
    }

    #[test]
    fn digit_limit_is_4300_without_the_sign() {
        let limit = "7".repeat(4300);
        assert_eq!(ok(limit.as_bytes()), int(&limit));
        assert_eq!(
            ok(format!("-{limit}").as_bytes()),
            int(&format!("-{limit}"))
        );
        let over = "7".repeat(4301);
        assert_eq!(decode(over.as_bytes(), DEPTH), Err(DecodeError::Schema));
        assert_eq!(
            decode(format!("[-{over}]").as_bytes(), DEPTH),
            Err(DecodeError::Schema)
        );
        assert_eq!(encoded(&int(&limit)).unwrap(), limit.as_bytes());
        let big = Value::Int(Int::parse(&over).unwrap());
        assert_eq!(encoded(&big), Err(EncodeError::Value));
        assert_eq!(encoded_unicode(&big), Err(EncodeError::Value));
    }

    #[test]
    fn recursion_boundaries_match_the_measured_legacy_values() {
        // 本家 3.10.11 で、フレームの深さ 3 から呼んで測った値(配列 992・辞書 991・入れ子の中の NaN 987)。
        let nest = |open: &str, inner: &str, close: &str, n: usize| {
            format!("{}{inner}{}", open.repeat(n), close.repeat(n)).into_bytes()
        };
        for (open, inner, close, limit) in [
            ("[", "", "]", 992),
            ("{\"a\":", "{}", "}", 990),
            ("[", "NaN", "]", 987),
        ] {
            assert_ne!(
                decode(&nest(open, inner, close, limit), 3),
                Err(DecodeError::Recursion),
                "{open} {limit}"
            );
            assert_eq!(
                decode(&nest(open, inner, close, limit + 1), 3),
                Err(DecodeError::Recursion),
                "{open} {}",
                limit + 1
            );
        }
        // 入れ子に依らない道: 最初に RecursionError になる呼出しの深さ。
        for (raw, first) in [
            (&b"\xff"[..], 996),
            ("\u{feff}0".as_bytes(), 996),
            (b"0", 996),
            (b"0 x", 995),
            (b"x", 994),
            (b"\"\x01\"", 993),
            (b"NaN", 991),
            (b"{\"a\":1,\"a\":2}", 990),
        ] {
            assert_ne!(
                decode(raw, first - 1),
                Err(DecodeError::Recursion),
                "{raw:?}"
            );
            assert_eq!(decode(raw, first), Err(DecodeError::Recursion), "{raw:?}");
        }
    }

    #[test]
    fn a_thousand_levels_fit_in_the_default_test_thread() {
        // 明示の列で読むので、深い入れ子でも Rust の再帰を使わない(落とすときの Drop だけが再帰する)。
        let raw = format!("{}{}", "[".repeat(990), "]".repeat(990));
        let value = decode(raw.as_bytes(), 1).unwrap();
        let mut depth = 0;
        let mut current = &value;
        while let Value::List(items) = current {
            depth += 1;
            match items.first() {
                Some(next) => current = next,
                None => break,
            }
        }
        assert_eq!(depth, 990);
        let deep = format!("{}1{}", "[".repeat(100_000), "]".repeat(100_000));
        assert_eq!(decode(deep.as_bytes(), 1), Err(DecodeError::Recursion));
    }

    #[test]
    fn encoded_matches_the_c_encoder_text() {
        let mut dict = Dict::new();
        dict.insert(cps(&[0x10000]), int("1"));
        dict.insert(cps(&[0xe000]), int("2"));
        dict.insert_str(
            "b",
            Value::List(vec![Value::Null, Value::Float(1.5), Value::Bool(true)]),
        );
        dict.insert_str(
            "a",
            Value::Str(cps(&[0x7f, 0x22, 0x5c, 0x0a, 0x01, 0xdcff, 0x1f600, 0xe9])),
        );
        let value = Value::Dict(dict);
        assert_eq!(
            String::from_utf8(encoded(&value).unwrap()).unwrap(),
            "{\"a\":\"\\u007f\\\"\\\\\\n\\u0001\\udcff\\ud83d\\ude00\\u00e9\",\"b\":[null,1.5,true],\"\\ue000\":2,\"\\ud800\\udc00\":1}"
        );
        for (float, text) in [
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (5e-324, "5e-324"),
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (100000.0, "100000.0"),
            (f64::MAX, "1.7976931348623157e+308"),
            (0.1, "0.1"),
            // 同じ桁の候補が 2 つ並ぶ中間(本家は偶数の側)。
            // 899096400445237.25 と .75 はちょうど表せる(字面を短くすると別の値に見えるので足し算で書く)。
            (899_096_400_445_237.0 + 0.25, "899096400445237.2"),
            (899_096_400_445_237.0 + 0.75, "899096400445237.8"),
        ] {
            assert_eq!(
                encoded(&Value::Float(float)).unwrap(),
                text.as_bytes(),
                "{float}"
            );
        }
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(encoded(&Value::Float(bad)), Err(EncodeError::Value));
        }
    }

    /// 2 の `exponent` 乗(−1074〜1023)を、ビットの並びから正確に作る。
    fn pow2(exponent: i32) -> f64 {
        if exponent >= -1022 {
            f64::from_bits(((exponent + 1023) as u64) << 52)
        } else {
            f64::from_bits(1u64 << (exponent + 1074))
        }
    }

    /// ちょうど 2 の累乗の小数は、元に戻る幅が下側だけ半分になる(本家 3.10.11 の `repr` の値)。
    #[test]
    fn powers_of_two_use_the_shortest_text_that_round_trips() {
        for (exponent, text) in [
            (-24, "5.960464477539063e-08"),
            (-44, "5.684341886080802e-14"),
            (89, "6.189700196426902e+26"),
            (-1074, "5e-324"),
            (-1022, "2.2250738585072014e-308"),
            (1023, "8.98846567431158e+307"),
            (0, "1.0"),
            (53, "9007199254740992.0"),
        ] {
            let value = pow2(exponent);
            assert_eq!(float_repr(value), text, "2^{exponent}");
            assert_eq!(float_repr(-value), format!("-{text}"), "-2^{exponent}");
        }
        // 上限ちょうどの `json` の量は、旧版と同じ字面の長さで決まる(1 バイト多いと json-limit になる)。
        let value = Value::Float(pow2(-24));
        let mut limits = Kind::ALL.map(Kind::default_limit);
        limits[Kind::Json as usize] = "5.960464477539063e-08".len() as u64 + 1;
        let mut budget = Budget::for_test(300.0, Box::new(|| 0.0), limits);
        assert_eq!(
            response_bytes(&value, &mut budget),
            Ok(b"5.960464477539063e-08\n".to_vec())
        );
    }

    #[test]
    fn encoded_unicode_writes_utf8_and_refuses_lone_surrogates() {
        let value = Value::List(vec![
            Value::Str(cps(&[0x7f, 0xe9, 0x1f600, 0x0a, 0x1f, 0x22])),
            Value::Float(f64::INFINITY),
            Value::Float(f64::NEG_INFINITY),
            Value::Float(f64::NAN),
        ]);
        assert_eq!(
            String::from_utf8(encoded_unicode(&value).unwrap()).unwrap(),
            "[\"\u{7f}é\u{1f600}\\n\\u001f\\\"\",Infinity,-Infinity,NaN]"
        );
        assert_eq!(
            encoded_unicode(&Value::Str(cps(&[0xd800]))),
            Err(EncodeError::Value)
        );
    }

    #[test]
    fn response_bytes_charges_each_chunk_and_the_newline() {
        let value = ok(b"{\"b\":[1,\"a\",[2],{}],\"a\":{\"c\":null}}");
        let mut budget = fresh();
        let bytes = response_bytes(&value, &mut budget).unwrap();
        let mut expected = encoded(&value).unwrap();
        expected.push(b'\n');
        assert_eq!(bytes, expected);
        assert_eq!(budget.used(Kind::Json), bytes.len() as u64);
        // 区切り: { "a" : { "c" : null } , "b" : [1 ,"a" , [2 ] , {} ] } と改行の 1(本家 3.10.11 の iterencode と同じ)。
        budget.set_test_log(true);
        response_bytes(&value, &mut budget).unwrap();
        let charges: Vec<u64> = budget
            .take_test_log()
            .into_iter()
            .filter_map(|op| match op {
                BudgetOp::Charge(_, count) => Some(count),
                _ => None,
            })
            .collect();
        assert_eq!(
            charges,
            vec![
                1, 3, 1, 1, 3, 1, 4, 1, 1, 3, 1, 2, 4, 1, 2, 1, 1, 2, 1, 1, 1
            ]
        );
        // 値の誤りは、その区切りを作る時点で返す(前の区切りは足した後)。
        let mut budget = fresh();
        let bad = Value::List(vec![int("1"), Value::Float(f64::INFINITY)]);
        assert_eq!(response_bytes(&bad, &mut budget), Err(ResponseError::Value));
        assert_eq!(budget.used(Kind::Json), 2);
        // 予算の失敗は部分の結果を返さない。
        let mut limits = Kind::ALL.map(Kind::default_limit);
        limits[Kind::Json as usize] = 3;
        let mut budget = Budget::for_test(300.0, Box::new(|| 0.0), limits);
        assert_eq!(
            response_bytes(&int("12"), &mut budget),
            Ok(b"12\n".to_vec())
        );
        let mut budget = Budget::for_test(300.0, Box::new(|| 0.0), limits);
        assert_eq!(
            response_bytes(&int("123"), &mut budget),
            Err(ResponseError::Budget(Failure::new("json-limit", 20)))
        );
    }

    /// 旧版の検査 `test_json_limit_stops_before_a_complete_response_is_returned`(:163)と同じ性質。
    #[test]
    fn json_limit_stops_before_a_complete_response_is_returned() {
        let mut limits = Kind::ALL.map(Kind::default_limit);
        limits[Kind::Json as usize] = 8;
        let mut budget = Budget::for_test(300.0, Box::new(|| 0.0), limits);
        let mut dict = Dict::new();
        dict.insert_str("value", Value::str("too long"));
        assert_eq!(
            response_bytes(&Value::Dict(dict), &mut budget),
            Err(ResponseError::Budget(Failure::new("json-limit", 20)))
        );
    }

    #[test]
    fn base64_is_canonical_and_refuses_nul() {
        for (raw, text) in [
            (&b""[..], ""),
            (b"f", "Zg=="),
            (b"fo", "Zm8="),
            (b"foo", "Zm9v"),
            (b"\xfb\xff", "+/8="),
        ] {
            assert_eq!(b64(raw), text);
            assert_eq!(unb64(&Value::str(text)), Ok(raw.to_vec()));
        }
        for bad in [
            "Zg", "Zg=", "Zh==", "Zm9=", "Z===", "=Zg=", "Zg==Zg==", "Zm 9v", "Zm9v\n", "Zm9vé",
            "-_8=", "AA==",
        ] {
            assert_eq!(unb64(&Value::str(bad)), Err(schema()), "{bad}");
        }
        for value in [
            Value::Null,
            Value::Bool(true),
            int("1"),
            Value::List(vec![]),
            Value::Dict(Dict::new()),
        ] {
            assert_eq!(unb64(&value), Err(schema()));
        }
        assert_eq!(
            digest(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn keys_compare_sets_split_like_python() {
        let value = ok(b"{\"a\":1,\"b\":2}");
        assert_eq!(keys(&value, "b a"), Ok(()));
        assert_eq!(keys(&value, "  a\t\nb \u{1c}\u{3000} "), Ok(()));
        assert_eq!(keys(&value, "a b a"), Ok(()));
        assert_eq!(keys(&value, "a"), Err(schema()));
        assert_eq!(keys(&value, "a b c"), Err(schema()));
        assert_eq!(keys(&Value::List(vec![]), ""), Err(schema()));
        assert_eq!(keys(&ok(b"{}"), " "), Ok(()));
    }

    #[test]
    fn dict_replaces_in_place() {
        let mut dict = Dict::new();
        assert_eq!(dict.insert_str("a", int("1")), None);
        dict.insert_str("b", int("2"));
        assert_eq!(dict.insert_str("a", int("3")), Some(int("1")));
        assert_eq!(
            dict.iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<Vec<_>>(),
            vec![
                (CodepointString::from("a"), int("3")),
                (CodepointString::from("b"), int("2"))
            ]
        );
        assert_eq!(Int::parse("-0"), None);
        assert_eq!(Int::parse("01"), None);
        assert_eq!(Int::parse("0").unwrap().to_i64(), Some(0));
        assert_eq!(Int::from(i128::MAX).to_i64(), None);
    }

    #[test]
    fn metadata_has_six_fields_and_the_type() {
        let stat = Stat {
            dev: -5,
            ino: u64::MAX,
            mode: 0o100644,
            size: 3,
            uid: 1000,
            mtime_ns: 13_569_465_600_000_000_000,
            ctime_ns: 1,
        };
        assert_eq!(
            String::from_utf8(encoded(&metadata(&stat)).unwrap()).unwrap(),
            "{\"ctime_ns\":1,\"dev\":-5,\"ino\":18446744073709551615,\"mode\":33188,\"mtime_ns\":13569465600000000000,\"size\":3,\"type\":\"regular\"}"
        );
        assert_eq!(fingerprint(&stat).mtime_ns, 13_569_465_600_000_000_000);
    }
}
