//! 旧Pythonの`json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
//! allow_nan=False)`と同じ正準JSON。確認値の入力と、結果の1行に使う。

use super::text::PyText;
use sha2::{Digest, Sha256};

/// JSONの値。文字列は旧Pythonの`str`(コードポイントの並び)で持つ。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    Null,
    Bool(bool),
    /// 10進の整数の字面(`str(int)`と同じ。先頭の`-`は負の数)。
    Int(String),
    Str(PyText),
    Array(Vec<Value>),
    /// 挿入の順のまま持ち、書くときに名前の順(コードポイントの順)へ並べる。
    Object(Vec<(PyText, Value)>),
}

impl Value {
    pub fn int(value: impl std::fmt::Display) -> Self {
        Self::Int(value.to_string())
    }

    pub fn str(text: &str) -> Self {
        Self::Str(super::text::from_str(text))
    }
}

fn string(out: &mut String, text: &[u32]) {
    out.push('"');
    for &code in text {
        match code {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x20..=0x7e => out.push(char::from_u32(code).unwrap()),
            0..=0xffff => out.push_str(&format!("\\u{code:04x}")),
            _ => {
                let pair = code - 0x10000;
                out.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (pair >> 10),
                    0xdc00 + (pair & 0x3ff)
                ));
            }
        }
    }
    out.push('"');
}

fn write(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Int(digits) => out.push_str(digits),
        Value::Str(text) => string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write(out, item);
            }
            out.push(']');
        }
        Value::Object(entries) => {
            let mut sorted: Vec<&(PyText, Value)> = entries.iter().collect();
            sorted.sort_by(|left, right| left.0.cmp(&right.0));
            out.push('{');
            for (index, (key, item)) in sorted.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                string(out, key);
                out.push(':');
                write(out, item);
            }
            out.push('}');
        }
    }
}

/// 正準JSONの文字列(ASCIIだけ)。
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write(&mut out, value);
    out
}

/// 正準JSONのUTF-8のSHA-256(小文字16進)。
pub fn sha256(value: &Value) -> String {
    format!("{:x}", Sha256::digest(dumps(value).as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::super::text::{decode, from_str};
    use super::*;

    #[test]
    fn dumps_matches_python_for_escapes_sorting_and_numbers() {
        let value = Value::Object(vec![
            (from_str("z"), Value::int(1)),
            (from_str("a"), Value::Null),
            (from_str("é"), Value::Bool(false)),
            (from_str("A"), Value::int("-18446744073709551616")),
            (
                from_str("list"),
                Value::Array(vec![
                    Value::Str(decode(b"\xff\"\\\n\r\t\x08\x0c\x01\x7f")),
                    Value::str("😀é"),
                    Value::Bool(true),
                    Value::Object(vec![]),
                    Value::Array(vec![]),
                ]),
            ),
            (vec![0xdcff], Value::str("")),
        ]);
        let script = "import json\nv={'z':1,'a':None,'\\u00e9':False,'A':-18446744073709551616,'list':['\\udcff\"\\\\\\n\\r\\t\\b\\f\\x01\\x7f','\\U0001f600\\u00e9',True,{},[]],'\\udcff':''}\nprint(json.dumps(v,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False),end='')";
        let output = std::process::Command::new("python3")
            .args(["-I", "-c", script])
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert_eq!(dumps(&value), String::from_utf8(output.stdout).unwrap());
        assert_eq!(
            sha256(&Value::Object(vec![(from_str("a"), Value::int(1))])),
            "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
        );
    }
}
