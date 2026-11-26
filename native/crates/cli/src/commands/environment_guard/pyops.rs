//! 旧版の Python のコードが値に対して行う操作(反復・添字・スライス・`in`)と、その失敗の例外。
//! 保持値(JSON から読んだ値)の形が違うときも、旧版と同じ型名・文面の例外にする。
use super::error::PyException;
use super::pystr::cps;
use super::pyvalue::{PyDict, PyValue};
use dev_workflow_core::loop_state::CodepointString;
use sha2::{Digest, Sha256};

pub type Cps = Vec<u32>;

/// 文字番号の列から `str` の値を作る。
pub fn text(value: Cps) -> PyValue {
    PyValue::Str(CodepointString::try_from_codepoints(value).expect("文字番号は範囲内"))
}

pub fn text_of(value: &str) -> PyValue {
    PyValue::from(value)
}

pub fn as_text(value: &PyValue) -> Option<&[u32]> {
    value.as_str().map(CodepointString::as_codepoints)
}

pub fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

/// `{...}` を作る。キーは文字列だけ。
pub fn dict(items: Vec<(&str, PyValue)>) -> PyDict {
    let mut result = PyDict::new();
    for (key, value) in items {
        result.insert_str(key, value);
    }
    result
}

/// SHA-256 の小文字の 16 進(`hashlib.sha256(raw).hexdigest()`)。
pub fn sha_hex(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}

pub fn not_subscriptable(value: &PyValue) -> PyException {
    PyException::type_error(&format!(
        "'{}' object is not subscriptable",
        value.type_name()
    ))
}

pub fn not_iterable(value: &PyValue) -> PyException {
    PyException::type_error(&format!("'{}' object is not iterable", value.type_name()))
}

/// `for x in value` で取り出される値。list は要素、dict はキー、str は 1 文字、bytes は整数。
pub fn iterate(value: &PyValue) -> Result<Vec<PyValue>, PyException> {
    match value {
        PyValue::List(items) => Ok(items.clone()),
        PyValue::Dict(dict) => Ok(dict.keys().cloned().collect()),
        PyValue::Str(s) => Ok(s.as_codepoints().iter().map(|&c| text(vec![c])).collect()),
        PyValue::Bytes(b) => Ok(b.iter().map(|&x| PyValue::from(i64::from(x))).collect()),
        other => Err(not_iterable(other)),
    }
}

/// `value[:count]`。
pub fn slice_to(value: &PyValue, count: usize) -> Result<PyValue, PyException> {
    match value {
        PyValue::List(items) => Ok(PyValue::List(items[..items.len().min(count)].to_vec())),
        PyValue::Str(s) => Ok(text(
            s.as_codepoints()[..s.as_codepoints().len().min(count)].to_vec(),
        )),
        PyValue::Bytes(b) => Ok(PyValue::Bytes(b[..b.len().min(count)].to_vec())),
        PyValue::Dict(_) => Err(PyException::type_error("unhashable type: 'slice'")),
        other => Err(not_subscriptable(other)),
    }
}

/// `value[0]`。
pub fn index_zero(value: &PyValue) -> Result<PyValue, PyException> {
    match value {
        PyValue::List(items) => items
            .first()
            .cloned()
            .ok_or_else(|| PyException::new("IndexError", "list index out of range")),
        PyValue::Str(s) => s
            .as_codepoints()
            .first()
            .map(|&c| text(vec![c]))
            .ok_or_else(|| PyException::new("IndexError", "string index out of range")),
        PyValue::Bytes(b) => b
            .first()
            .map(|&x| PyValue::from(i64::from(x)))
            .ok_or_else(|| PyException::new("IndexError", "index out of range")),
        PyValue::Dict(d) => match d.get(&PyValue::from(0))? {
            Some(found) => Ok(found.clone()),
            None => Err(PyException::new("KeyError", "0")),
        },
        other => Err(not_subscriptable(other)),
    }
}

/// `value.get(key)`(`value` が dict でなければ `AttributeError`)。
pub fn get_attr_dict(value: &PyValue) -> Result<&PyDict, PyException> {
    value.as_dict().ok_or_else(|| {
        PyException::new(
            "AttributeError",
            &format!("'{}' object has no attribute 'get'", value.type_name()),
        )
    })
}

/// `x in frozenset_of_strings`。dict・list は hash できず `TypeError`。
pub fn in_str_set(value: &PyValue, set: &[&str]) -> Result<bool, PyException> {
    match value {
        PyValue::List(_) | PyValue::Dict(_) => Err(PyException::type_error(&format!(
            "unhashable type: '{}'",
            value.type_name()
        ))),
        PyValue::Str(s) => Ok(set.iter().any(|item| cps(item) == s.as_codepoints())),
        _ => Ok(false),
    }
}

pub fn starts_with(text: &[u32], prefix: &str) -> bool {
    let prefix = cps(prefix);
    text.len() >= prefix.len() && text[..prefix.len()] == prefix[..]
}

pub fn ends_with(text: &[u32], suffix: &str) -> bool {
    let suffix = cps(suffix);
    text.len() >= suffix.len() && text[text.len() - suffix.len()..] == suffix[..]
}

pub fn concat(parts: &[&[u32]]) -> Cps {
    parts.concat()
}

pub fn lit(value: &str) -> Cps {
    cps(value)
}
