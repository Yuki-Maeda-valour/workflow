//! テスト用の補助。固定期待(`native/ci/s2e_fixtures/`)の読み込み。
use serde_json::Value;

/// `native/ci/s2e_fixtures/<名前>` を JSON として読む。
macro_rules! fixture {
    ($name:literal) => {
        serde_json::from_str::<serde_json::Value>(include_str!(concat!(
            "../../../../../ci/s2e_fixtures/",
            $name
        )))
        .expect(concat!($name, " が JSON でない"))
    };
}
pub(crate) use fixture;

/// 文字番号の配列。
pub fn cps_of(value: &Value) -> Vec<u32> {
    value
        .as_array()
        .expect("配列")
        .iter()
        .map(|v| u32::try_from(v.as_u64().expect("数")).expect("範囲内"))
        .collect()
}

use super::error::PyException;
use super::pystr::to_string;
use super::pyvalue::{PyValue, float_repr};
use serde_json::json;

/// 固定期待の「型の印つき」の形。bool・int・float・str・list・dict・none を区別する。
pub fn to_typed(value: &PyValue) -> Value {
    match value {
        PyValue::None => json!({"t": "none"}),
        PyValue::Bool(b) => json!({"t": "bool", "v": b}),
        PyValue::Int(i) => json!({"t": "int", "v": i.to_decimal(0).unwrap()}),
        PyValue::Float(f) => json!({"t": "float", "v": float_repr(*f)}),
        PyValue::Str(s) => json!({"t": "str", "v": s.as_codepoints()}),
        PyValue::Bytes(b) => json!({"t": "bytes", "v": b}),
        PyValue::List(items) => {
            json!({"t": "list", "v": items.iter().map(to_typed).collect::<Vec<_>>()})
        }
        PyValue::Dict(dict) => json!({
            "t": "dict",
            "v": dict.iter().map(|(k, v)| json!([to_typed(k), to_typed(v)])).collect::<Vec<_>>()
        }),
    }
}

/// 固定期待の `{"error": {"type", "msg"}}` と比べる形。
pub fn error_of(error: &PyException) -> Value {
    json!({"type": error.type_name, "msg": to_string(error.message.as_codepoints())})
}

pub fn from_hex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).unwrap())
        .collect()
}

/// 入れ子が 1000 段近い入力を、debug ビルドでも溢れない大きさのスタックで流す。
pub fn with_big_stack(test: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(256 * 1024 * 1024)
        .spawn(test)
        .expect("スレッドを作れる")
        .join()
        .unwrap_or_else(|panic| std::panic::resume_unwind(panic));
}

/// 固定期待の `namespace`(`vars(args)` の型の印つきの形)。
pub fn namespace_json(parsed: &super::args::Parsed) -> Value {
    use super::args::{ArgValue, NsValue};
    let typed_arg = |value: &ArgValue| match value {
        ArgValue::Str(s) => json!({"t": "str", "v": s.as_codepoints()}),
        ArgValue::Int(i) => json!({"t": "int", "v": i.to_decimal(0).unwrap()}),
        ArgValue::EmptyList => json!({"t": "list", "v": []}),
    };
    let mut map = serde_json::Map::new();
    for (dest, value) in parsed.namespace.entries() {
        let typed = match value {
            NsValue::None => json!({"t": "none"}),
            NsValue::Value(v) => typed_arg(v),
            NsValue::Many(items) => {
                json!({"t": "list", "v": items.iter().map(typed_arg).collect::<Vec<_>>()})
            }
            NsValue::Words(words) => {
                json!({"t": "list", "v": words.iter().map(|w| json!({"t": "str", "v": w.as_codepoints()})).collect::<Vec<_>>()})
            }
            NsValue::Name(name) => {
                json!({"t": "str", "v": name.chars().map(u32::from).collect::<Vec<_>>()})
            }
        };
        map.insert((*dest).to_owned(), typed);
    }
    Value::Object(map)
}

/// EAGAIN(非 block の出力先が満杯)の番号。Linux は 11、Mac は 35。固定期待の `[Errno 11]` は Linux で作ったので、
/// 比べるときに OS の値へ置き換える。
#[cfg(target_os = "macos")]
pub const EAGAIN: i32 = 35;
#[cfg(not(target_os = "macos"))]
pub const EAGAIN: i32 = 11;

/// 固定期待の文面のうち、OS で番号が変わる EAGAIN の `[Errno 11]` を、この OS の番号にする。
pub fn for_this_os(expected: &str) -> String {
    expected.replace(
        "[Errno 11] write could not",
        &format!("[Errno {EAGAIN}] write could not"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn for_this_os_only_changes_the_eagain_number() {
        let text = "BlockingIOError: [Errno 11] write could not complete without blocking\n";
        let expect = format!(
            "BlockingIOError: [Errno {EAGAIN}] write could not complete without blocking\n"
        );
        assert_eq!(for_this_os(text), expect);
        #[cfg(not(target_os = "macos"))]
        assert_eq!(for_this_os(text), text, "Linux では何も変えない");
        for other in [
            "OSError: [Errno 28] No space left on device",
            "[Errno 11] 別の文面",
            "[Errno 0] write could not complete without blocking",
        ] {
            assert_eq!(for_this_os(other), other);
        }
    }
}
