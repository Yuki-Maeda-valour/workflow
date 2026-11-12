#![forbid(unsafe_code)]
//! R4Q(#494)の単体検査。
//! - Python の再現の純粋な部品(`commands/r4q_python`)を、本家 CPython 3.10.11 の実測の固定期待
//!   (`native/fixtures/r4q/units.json`。作り方は同じフォルダの `gen_units.py`)と照らす。
//! - 監督の OS の部品(platform の `r4q_process`)を、実際のプロセスで確かめる。プロセス全体に効く設定
//!   (信号の処理・mask・subreaper・SIGCHLD の無視)を変える検査は、このテストの実行物を子として起動し直し、
//!   その中だけで行う(`in_child`。R4R の `restore_taskmd.rs` と同じ形)。
//! - 監督と親(`loop_supervisor`・`check_process`)を、OS の操作と時計を差し替えた世界で動かし、旧版を
//!   mock して作った固定期待(`native/fixtures/r4q/injected.json`。作り方は `gen_injected.py`)と、
//!   ケースの ID ごとに操作の呼出しの並びと結果を照らす(`injected`)。
//! - 一時フォルダは cargo の `CARGO_TARGET_TMPDIR`(ビルド先の中)の下に作り、検査の終わりに消す。
#[path = "../src/commands/check_process/mod.rs"]
// R4V と比較器が使う関数と、実行物の入口(`run`)のうち、この検査で照らさないものがある。
#[allow(dead_code, unused_imports)]
mod check_process;

use check_process::loop_supervisor::r4q_python;

use r4q_python::argparse::{self, Outcome};
use r4q_python::exception::{self, Filename, OsError};
use r4q_python::legacy::{self, InSupervise, Site, uncaught};
use r4q_python::number::{self, PyNumber};
use r4q_python::oserror::{self, Os};
use r4q_python::value::{self, PyValue};
use r4q_python::{codec, json};
use serde_json::Value;
use sha2::{Digest, Sha256};

const UNITS: &str = include_str!("../../../fixtures/r4q/units.json");

fn units() -> Value {
    serde_json::from_str(UNITS).expect("固定期待の JSON を読めない")
}

fn list(value: &Value) -> &Vec<Value> {
    value.as_array().expect("配列でない")
}

fn int(value: &Value) -> i64 {
    value.as_i64().expect("整数でない")
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&text[at..at + 2], 16).expect("16 進でない"))
        .collect()
}

fn hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// 固定期待の str(文字列か `{"hex": UTF-8(surrogatepass)}`)の符号位置。
fn text(value: &Value) -> Vec<u32> {
    match value {
        Value::String(text) => text.chars().map(u32::from).collect(),
        Value::Object(map) => {
            codec::decode_surrogatepass(&unhex(map["hex"].as_str().unwrap()), "utf-8")
                .expect("surrogatepass の UTF-8")
        }
        other => panic!("str の形が違う: {other}"),
    }
}

fn show(codes: &[u32]) -> String {
    value::text(codes)
}

/// 固定期待の値の形(`{"none": null}`・`{"int": …}` など)。
fn py_value(value: &Value) -> PyValue {
    let (kind, item) = value
        .as_object()
        .and_then(|map| map.iter().next())
        .expect("値の形が違う");
    match kind.as_str() {
        "none" => PyValue::None,
        "bool" => PyValue::Bool(item.as_bool().unwrap()),
        "int" => PyValue::Int(item.as_str().unwrap().to_owned()),
        "float" => {
            let repr = item.as_str().unwrap();
            PyValue::Float(match repr {
                "nan" => f64::NAN,
                "inf" => f64::INFINITY,
                "-inf" => f64::NEG_INFINITY,
                _ => repr.parse().unwrap(),
            })
        }
        "str" => PyValue::Str(text(item)),
        "bytes" => PyValue::Bytes(unhex(item.as_str().unwrap())),
        "list" => PyValue::List(list(item).iter().map(py_value).collect()),
        "dict" => PyValue::Dict(
            list(item)
                .iter()
                .map(|pair| (py_value(&pair[0]), py_value(&pair[1])))
                .collect(),
        ),
        other => panic!("知らない値の形: {other}"),
    }
}

/// 固定期待の値の形へ戻す(比べるため)。
fn value_form(value: &PyValue) -> Value {
    let str_form = |codes: &[u32]| match char_string(codes) {
        Some(text) => Value::String(text),
        None => serde_json::json!({"hex": hex(&value::encode_surrogatepass(codes))}),
    };
    match value {
        PyValue::None => serde_json::json!({"none": null}),
        PyValue::Bool(flag) => serde_json::json!({"bool": flag}),
        PyValue::Int(digits) => serde_json::json!({"int": digits}),
        PyValue::Float(number) => serde_json::json!({"float": number::float_repr(*number)}),
        PyValue::Str(codes) => serde_json::json!({"str": str_form(codes)}),
        PyValue::Bytes(raw) => serde_json::json!({"bytes": hex(raw)}),
        PyValue::List(items) => {
            serde_json::json!({"list": items.iter().map(value_form).collect::<Vec<_>>()})
        }
        PyValue::Dict(pairs) => serde_json::json!({"dict": pairs
            .iter()
            .map(|(key, item)| serde_json::json!([value_form(key), value_form(item)]))
            .collect::<Vec<_>>()}),
    }
}

fn char_string(codes: &[u32]) -> Option<String> {
    codes.iter().map(|&code| char::from_u32(code)).collect()
}

/// 生成器の `value_text`(深い入れ子の値の正規の字面)。
fn value_text(value: &PyValue, out: &mut String) {
    match value {
        PyValue::None => out.push('N'),
        PyValue::Bool(true) => out.push('T'),
        PyValue::Bool(false) => out.push('F'),
        PyValue::Int(digits) => out.push_str(&format!("I{digits};")),
        PyValue::Float(number) => out.push_str(&format!("D{};", number::float_repr(*number))),
        PyValue::Str(codes) => {
            out.push_str(&format!("S{};", hex(&value::encode_surrogatepass(codes))))
        }
        PyValue::Bytes(raw) => out.push_str(&format!("B{};", hex(raw))),
        PyValue::List(items) => {
            out.push_str(&format!("L{}:", items.len()));
            for item in items {
                value_text(item, out);
            }
        }
        PyValue::Dict(pairs) => {
            out.push_str(&format!("M{}:", pairs.len()));
            for (key, item) in pairs {
                value_text(key, out);
                value_text(item, out);
            }
        }
    }
}

/// 生成器の `value_text` を読む(dumps に渡す値)。
fn parse_value_text(text: &str) -> PyValue {
    fn take_until<'a>(rest: &mut &'a str, end: char) -> &'a str {
        let at = rest.find(end).expect("終わりの印");
        let head = &rest[..at];
        *rest = &rest[at + 1..];
        head
    }
    fn parse(rest: &mut &str) -> PyValue {
        let tag = rest.chars().next().expect("値がある");
        *rest = &rest[1..];
        match tag {
            'N' => PyValue::None,
            'T' => PyValue::Bool(true),
            'F' => PyValue::Bool(false),
            'I' => PyValue::Int(take_until(rest, ';').to_owned()),
            'D' => {
                let repr = take_until(rest, ';');
                PyValue::Float(match repr {
                    "nan" => f64::NAN,
                    "inf" => f64::INFINITY,
                    "-inf" => f64::NEG_INFINITY,
                    _ => repr.parse().unwrap(),
                })
            }
            'S' => PyValue::Str(
                codec::decode_surrogatepass(&unhex(take_until(rest, ';')), "utf-8").unwrap(),
            ),
            'B' => PyValue::Bytes(unhex(take_until(rest, ';'))),
            'L' => {
                let count: usize = take_until(rest, ':').parse().unwrap();
                PyValue::List((0..count).map(|_| parse(rest)).collect())
            }
            'M' => {
                let count: usize = take_until(rest, ':').parse().unwrap();
                PyValue::Dict(
                    (0..count)
                        .map(|_| {
                            let key = parse(rest);
                            (key, parse(rest))
                        })
                        .collect(),
                )
            }
            other => panic!("知らない印: {other}"),
        }
    }
    let mut rest = text;
    let value = parse(&mut rest);
    assert!(rest.is_empty());
    value
}

fn sha256_hex(raw: &[u8]) -> String {
    hex(&Sha256::digest(raw))
}

// ---- 固定期待の出どころ ----

#[test]
fn fixture_matches_upstream_python_and_current_legacy_scripts() {
    let units = units();
    assert_eq!(units["python"]["version"], "3.10.11");
    let scripts = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/dev-workflow/skills/ship-task/scripts");
    for name in ["check-process.py", "loop-supervisor.py"] {
        let raw = std::fs::read(scripts.join(name)).expect("旧版を読めない");
        assert_eq!(
            sha256_hex(&raw),
            units["python"]["legacy_sha256"][name].as_str().unwrap(),
            "旧版 {name} が固定期待を作った時と違う(gen_units.py で作り直す)"
        );
    }
    assert_eq!(
        units["docstrings"]["check-process.py"].as_str().unwrap(),
        legacy::CHECK_PROCESS_DOC
    );
    assert_eq!(
        units["docstrings"]["loop-supervisor.py"].as_str().unwrap(),
        legacy::LOOP_SUPERVISOR_DOC
    );
}

#[test]
fn unicode_space_and_decimal_tables() {
    let units = units();
    let spaces: Vec<u32> = list(&units["unicode"]["space_above_ascii"])
        .iter()
        .map(|code| int(code) as u32)
        .collect();
    let ours: Vec<u32> = (127..0x110000)
        .filter(|&code| number::unicode_space_above_ascii(code))
        .collect();
    assert_eq!(ours, spaces);
    let mut expected = vec![None; 0x110000];
    for pair in list(&units["unicode"]["decimal"]) {
        expected[int(&pair[0]) as usize] = Some(int(&pair[1]) as u8);
    }
    for (code, want) in expected.iter().enumerate() {
        assert_eq!(r4q_python::decimal(code as u32), *want, "U+{code:04X}");
    }
    let ascii: Vec<u32> = list(&units["unicode"]["ascii_isspace"])
        .iter()
        .map(|code| int(code) as u32)
        .collect();
    assert_eq!(ascii, [9, 10, 11, 12, 13, 28, 29, 30, 31, 32]);
}

// ---- int()・float()・float の repr・数の和 ----

#[test]
fn int_from_str_and_bytes_match_python() {
    let units = units();
    let cases = list(&units["number"]["int"]);
    assert!(cases.len() >= 2000);
    for case in cases {
        let input = text(&case["input"]);
        let digits = int(&case["max_digits"]) as usize;
        let ours = number::int_from_str(&input, digits);
        match (&case["result"]["int"], &case["result"]["error"]) {
            (Value::String(want), _) => {
                assert_eq!(ours.as_deref(), Ok(want.as_str()), "{}", show(&input))
            }
            (_, error) => assert_eq!(ours, Err(show(&text(error))), "{}", show(&input)),
        }
    }
    let cases = list(&units["number"]["int_bytes"]);
    assert!(cases.len() >= 1000);
    for case in cases {
        let input = unhex(case["input"].as_str().unwrap());
        let digits = int(&case["max_digits"]) as usize;
        let ours = number::int_from_bytes(&input, digits);
        match (&case["result"]["int"], &case["result"]["error"]) {
            (Value::String(want), _) => assert_eq!(ours.as_deref(), Ok(want.as_str()), "{input:?}"),
            (_, error) => assert_eq!(ours, Err(show(&text(error))), "{input:?}"),
        }
    }
}

#[test]
fn float_from_str_and_repr_match_python() {
    let units = units();
    let cases = list(&units["number"]["float"]);
    assert!(cases.len() >= 2000);
    for case in cases {
        let input = text(&case["input"]);
        let ours = number::float_from_str(&input);
        if let Some(error) = case.get("error") {
            assert_eq!(
                ours.map(|v| v.to_bits()),
                Err(show(&text(error))),
                "{}",
                show(&input)
            );
        } else {
            let value = ours.unwrap_or_else(|error| panic!("{}: {error}", show(&input)));
            assert_eq!(
                number::float_repr(value),
                case["result"]["repr"].as_str().unwrap()
            );
            if !value.is_nan() {
                assert_eq!(
                    hex(&value.to_be_bytes()),
                    case["result"]["bits"].as_str().unwrap()
                );
            }
        }
    }
    let cases = list(&units["number"]["float_repr"]);
    assert!(cases.len() >= 4000);
    for case in cases {
        let bits = unhex(case["bits"].as_str().unwrap());
        let value = f64::from_be_bytes(bits.try_into().unwrap());
        assert_eq!(
            number::float_repr(value),
            case["repr"].as_str().unwrap(),
            "{:?}",
            case["bits"]
        );
    }
}

fn number_from(value: &PyValue) -> PyNumber {
    match value {
        PyValue::Int(digits) => PyNumber::Int(digits.clone()),
        PyValue::Float(number) => PyNumber::Float(*number),
        other => panic!("数でない: {other:?}"),
    }
}

#[test]
fn python_numbers_add_isfinite_and_str() {
    let units = units();
    let cases = list(&units["number"]["sum"]);
    assert_eq!(cases.len(), 400);
    for case in cases {
        let left = number_from(&py_value(&case["left"]));
        let right = number_from(&py_value(&case["right"]));
        let label = format!("{left:?} + {right:?}");
        match left.add(&right) {
            Ok(sum) => {
                let want = py_value(&case["sum"]);
                assert_eq!(
                    value_form(&number_value(&sum)),
                    value_form(&want),
                    "{label}"
                );
            }
            Err(_) => assert_eq!(
                show(&text(&case["sum"]["error"])),
                number::Overflow::MESSAGE,
                "{label}"
            ),
        }
        match left.is_finite() {
            Ok(finite) => assert_eq!(
                case["left_isfinite"]["bool"].as_bool(),
                Some(finite),
                "{label}"
            ),
            Err(_) => assert_eq!(
                show(&text(&case["left_isfinite"]["error"])),
                number::Overflow::MESSAGE
            ),
        }
        assert_eq!(left.to_str(), show(&text(&case["left_str"])));
    }
    assert_eq!(
        PyNumber::Int("-3".into()).compare_zero(),
        Some(std::cmp::Ordering::Less)
    );
    assert_eq!(PyNumber::Float(f64::NAN).compare_zero(), None);
    assert_eq!(
        PyNumber::Float(-0.0).compare_zero(),
        Some(std::cmp::Ordering::Equal)
    );
}

fn number_value(number: &PyNumber) -> PyValue {
    match number {
        PyNumber::Int(digits) => PyValue::Int(digits.clone()),
        PyNumber::Float(value) => PyValue::Float(*value),
    }
}

// ---- json ----

#[test]
fn json_loads_bytes_matches_python() {
    let units = units();
    let room = int(&units["json"]["recursion_room"]) as usize;
    assert!(room > 900, "再帰の余りが小さすぎる: {room}");
    let cases = list(&units["json"]["loads"]);
    assert!(cases.len() >= 2000);
    for case in cases {
        let input = unhex(case["input"].as_str().unwrap());
        let digits = int(&case["max_digits"]) as usize;
        let ours = json::loads(&input, digits, room);
        let label = String::from_utf8_lossy(&input[..input.len().min(80)]).into_owned();
        match (&case["result"]["value"], &case["result"]["error"]) {
            (Value::String(want), _) => {
                let value = ours.unwrap_or_else(|error| panic!("{label}: {error:?}"));
                let mut got = String::new();
                value_text(&value, &mut got);
                assert_eq!(&got, want, "{label}");
            }
            (_, error) => {
                let error_value = ours
                    .err()
                    .unwrap_or_else(|| panic!("{label}: 誤りにならない"));
                assert_eq!(
                    error_value.type_name,
                    error["type"].as_str().unwrap(),
                    "{label}"
                );
                assert_eq!(
                    error_value.message,
                    show(&text(&error["message"])),
                    "{label}"
                );
                assert_eq!(
                    error_value.is_value_error(),
                    error_value.type_name != "RecursionError"
                );
            }
        }
    }
    for case in list(&units["json"]["detect_encoding"]) {
        let input = unhex(case["input"].as_str().unwrap());
        assert_eq!(
            codec::detect_encoding(&input),
            case["encoding"].as_str().unwrap()
        );
    }
}

#[test]
fn json_dumps_matches_python() {
    let units = units();
    let cases = list(&units["json"]["dumps"]);
    assert!(cases.len() >= 1000);
    for case in cases {
        let value = parse_value_text(case["value"].as_str().unwrap());
        let digits = case
            .get("max_digits")
            .map_or(4300, |value| int(value) as usize);
        for (name, sort_keys) in [("default", false), ("sort_keys", true)] {
            let ours = json::dumps(&value, sort_keys, digits);
            match &case[name] {
                Value::Object(map) if map.contains_key("error") => {
                    let error = ours.expect_err("誤りになる");
                    assert_eq!(
                        error,
                        json::DumpError::Python {
                            type_name: match map["error"]["type"].as_str().unwrap() {
                                "ValueError" => "ValueError",
                                "TypeError" => "TypeError",
                                other => panic!("知らない誤り: {other}"),
                            },
                            message: show(&text(&map["error"]["message"])),
                        }
                    );
                }
                want => assert_eq!(
                    ours.map(|t| t.chars().map(u32::from).collect::<Vec<_>>()),
                    Ok(text(want))
                ),
            }
        }
    }
}

#[test]
fn json_dumps_accepts_only_string_keys() {
    // 旧版が dumps に渡す記録の鍵は str だけ。str でない鍵は呼出元の誤りとして返す(Python の文字列化はしない)。
    for key in [
        PyValue::int(1),
        PyValue::None,
        PyValue::Bool(true),
        PyValue::Float(0.5),
    ] {
        let nested = PyValue::List(vec![PyValue::Dict(vec![(key.clone(), PyValue::None)])]);
        for sort_keys in [false, true] {
            assert_eq!(
                json::dumps(&nested, sort_keys, 4300),
                Err(json::DumpError::NonStringKey)
            );
        }
    }
    let record = PyValue::Dict(vec![
        (PyValue::str("returncode"), PyValue::int(3)),
        (PyValue::str("clean"), PyValue::Bool(true)),
    ]);
    assert_eq!(
        json::dumps(&record, true, 4300).as_deref(),
        Ok("{\"clean\": true, \"returncode\": 3}")
    );
}

// ---- argparse ----

fn parser_of(name: &str) -> &'static argparse::Parser {
    match name {
        "check-process.py" => &argparse::CHECK_PROCESS,
        "loop-supervisor.py" => &argparse::LOOP_SUPERVISOR,
        other => panic!("知らない parser: {other}"),
    }
}

fn doc_of(name: &str) -> &'static str {
    match name {
        "check-process.py" => legacy::CHECK_PROCESS_DOC,
        _ => legacy::LOOP_SUPERVISOR_DOC,
    }
}

#[test]
fn argparse_usage_and_help_for_each_width() {
    let units = units();
    let formats = list(&units["argparse"]["formats"]);
    assert_eq!(formats.len(), 288);
    for case in formats {
        let name = case["parser"].as_str().unwrap();
        let parser = parser_of(name);
        let width = int(&case["columns"]) - 2;
        assert_eq!(
            argparse::format_usage(parser, width),
            case["usage"].as_str().unwrap(),
            "{name} {width}"
        );
        assert_eq!(
            argparse::format_help(parser, width, Some(doc_of(name))),
            case["help"].as_str().unwrap(),
            "{name} {width}"
        );
        assert_eq!(
            argparse::format_help(parser, width, None),
            case["help_without_description"].as_str().unwrap(),
            "{name} {width}"
        );
    }
}

#[test]
fn argparse_parse_matches_legacy_parsers() {
    let units = units();
    let cases = list(&units["argparse"]["cases"]);
    assert!(cases.len() >= 3000);
    let formats = list(&units["argparse"]["formats"]);
    let format_of = |name: &str, columns: i64, key: &str| -> String {
        formats
            .iter()
            .find(|entry| entry["parser"] == name && int(&entry["columns"]) == columns)
            .unwrap_or_else(|| panic!("幅 {columns} の固定期待が無い"))[key]
            .as_str()
            .unwrap()
            .to_owned()
    };
    let (mut parsed, mut helps, mut errors) = (0, 0, 0);
    for case in cases {
        let name = case["parser"].as_str().unwrap();
        let parser = parser_of(name);
        let columns = int(&case["columns"]);
        let description = case["description"].as_bool().unwrap();
        let args: Vec<Vec<u32>> = list(&case["argv"]).iter().map(text).collect();
        let label = format!(
            "{name} {:?} columns={columns}",
            args.iter().map(|arg| show(arg)).collect::<Vec<_>>()
        );
        let context = argparse::Context {
            width: columns - 2,
            description: description.then(|| doc_of(name)),
            max_str_digits: int(&case["max_digits"]) as usize,
        };
        let outcome = &case["outcome"];
        match argparse::parse(parser, &args, &context) {
            Outcome::Parsed(namespace) => {
                parsed += 1;
                let want = list(&outcome["parsed"]);
                assert_eq!(namespace.len(), want.len(), "{label}");
                for ((dest, value), expected) in namespace.iter().zip(want) {
                    assert_eq!(*dest, expected[0].as_str().unwrap(), "{label}");
                    assert_eq!(value_form(value), expected[1], "{label}: {dest}");
                }
            }
            Outcome::Help(output) => {
                helps += 1;
                assert_eq!(outcome["help"], true, "{label}");
                let key = if description {
                    "help"
                } else {
                    "help_without_description"
                };
                assert_eq!(show(&output), format_of(name, columns, key), "{label}");
            }
            Outcome::Error(error) => {
                errors += 1;
                let output = error.text();
                // usage の write と `<prog>: error:` の write は別(書く途中の失敗の呼出履歴が分かれる)。
                assert_eq!(
                    show(&error.usage),
                    format_of(name, columns, "usage"),
                    "{label}"
                );
                let message = outcome
                    .get("error")
                    .unwrap_or_else(|| panic!("{label}: 誤りでない {outcome}"));
                let mut want: Vec<u32> = format_of(name, columns, "usage")
                    .chars()
                    .map(u32::from)
                    .collect();
                want.extend(format!("{name}: error: ").chars().map(u32::from));
                want.extend(text(message));
                want.push(u32::from(b'\n'));
                assert_eq!(show(&output), show(&want), "{label}");
                assert_eq!(output, want, "{label}");
            }
        }
    }
    assert!(
        parsed > 100 && helps > 100 && errors > 2000,
        "{parsed} {helps} {errors}"
    );
}

#[test]
fn terminal_columns_matches_shutil() {
    let units = units();
    for case in list(&units["terminal"]) {
        let columns = match &case["columns"] {
            Value::Null => None,
            other => Some(text(other)),
        };
        let terminal = case["terminal"].as_u64().map(|value| value as u16);
        let digits = int(&case["max_digits"]) as usize;
        assert_eq!(
            argparse::terminal_columns(columns.as_deref(), terminal, digits),
            int(&case["result"]),
            "{case}"
        );
    }
}

// ---- 例外の文 ----

fn filename_of(value: &Value) -> Option<Filename> {
    match py_value(value) {
        PyValue::None => None,
        PyValue::Str(codes) => Some(Filename::Str(codes)),
        PyValue::Bytes(raw) => Some(Filename::Bytes(raw)),
        PyValue::Int(digits) => Some(Filename::Int(digits.parse().unwrap())),
        other => panic!("ファイル名の形が違う: {other:?}"),
    }
}

#[test]
fn oserror_messages_types_and_linux_strerror() {
    let units = units();
    let cases = list(&units["exception"]["oserror"]);
    assert!(cases.len() >= 300);
    for case in cases {
        let error = OsError {
            errno: int(&case["errno"]) as i32,
            strerror: show(&text(&case["strerror"])),
            filename: filename_of(&case["filename"]),
            filename2: filename_of(&case["filename2"]),
        };
        assert_eq!(error.message(), show(&text(&case["message"])), "{case}");
    }
    for case in list(&units["exception"]["oserror_type"]) {
        assert_eq!(
            oserror::exception_type_on(Os::Linux, int(&case["errno"]) as i32),
            case["type"].as_str().unwrap(),
            "{case}"
        );
    }
    let strerror = list(&units["exception"]["strerror_linux"]);
    assert_eq!(strerror.len(), 146);
    for case in strerror {
        assert_eq!(
            exception::strerror_on(Os::Linux, int(&case["errno"]) as i32),
            case["text"].as_str().unwrap(),
            "{case}"
        );
    }
    if cfg!(target_os = "linux") {
        assert_eq!(exception::strerror(2), "No such file or directory");
        assert_eq!(OsError::os(2, None).type_name(), "FileNotFoundError");
    }
}

#[test]
fn mac_strerror_table_from_apple_libc() {
    // 表の中身は Apple の公開の Libc(gen/errlst.c)から写した。Mac の CI の比較器(単位 3)が、errno 1〜106 の全部と
    // 107 を旧版の os.strerror と照らす。ここでは表の形と、表に無い番号の文の形を確かめる。
    assert_eq!(exception::MAC_STRERROR.len(), 106);
    assert_eq!(exception::mac_strerror(1), "Operation not permitted");
    assert_eq!(
        exception::mac_strerror(35),
        "Resource temporarily unavailable"
    );
    assert_eq!(
        exception::mac_strerror(62),
        "Too many levels of symbolic links"
    );
    assert_eq!(
        exception::mac_strerror(106),
        "Interface output queue is full"
    );
    assert_eq!(exception::mac_strerror(107), "Unknown error: 107");
    assert_eq!(exception::mac_strerror(-1), "Unknown error: -1");
    assert_eq!(
        exception::strerror_on(Os::Macos, 2),
        "No such file or directory"
    );
    assert!(exception::MAC_STRERROR.iter().all(|text| !text.is_empty()));
    assert_eq!(exception::mac_strerror(0), "Undefined error: 0");
    if cfg!(target_os = "macos") {
        assert_eq!(exception::strerror(13), "Permission denied");
    }
}

#[test]
fn timeout_expired_pathlib_key_error_and_encoders() {
    let units = units();
    for case in list(&units["exception"]["timeout_expired"]) {
        let cmd: Vec<Vec<u32>> = list(&case["cmd"]).iter().map(text).collect();
        let timeout = number_from(&py_value(&case["timeout"]));
        let grace = number_from(&py_value(&case["grace"]));
        let total = number_from(&py_value(&case["total"]));
        if total != PyNumber::Int("2".into()) || grace != PyNumber::Int("0".into()) {
            let ours = timeout
                .add(&grace)
                .and_then(|sum| sum.add(&PyNumber::Int("7".into())))
                .unwrap();
            assert_eq!(ours.to_str(), total.to_str(), "{case}");
        }
        assert_eq!(
            exception::timeout_expired_message(&cmd, &total),
            show(&text(&case["message"])),
            "{case}"
        );
    }
    for case in list(&units["exception"]["pathlib"]) {
        assert_eq!(
            hex(&exception::path_child(
                &unhex(case["directory"].as_str().unwrap()),
                &unhex(case["name"].as_str().unwrap())
            )),
            case["result"].as_str().unwrap()
        );
    }
    for case in list(&units["exception"]["key_error"]) {
        assert_eq!(
            exception::key_error_message(&py_value(&case["key"])),
            show(&text(&case["message"]))
        );
    }
    for case in list(&units["exception"]["backslashreplace"]) {
        assert_eq!(
            hex(&value::encode_backslashreplace(&text(&case["input"]))),
            case["result"].as_str().unwrap()
        );
    }
    let surrogate = [u32::from(b'a'), 0xdcff, 0xd800];
    assert_eq!(
        value::encode_surrogateescape(&surrogate[..2]),
        Some(vec![b'a', 0xff])
    );
    assert_eq!(value::encode_surrogateescape(&surrogate), None);
    assert_eq!(r4q_python::repr(b"x\xff'"), "\"x\\udcff'\"");
}

// ---- 旧版の呼出履歴 ----

#[test]
fn legacy_tracebacks_match_real_runs() {
    let units = units();
    let cases = list(&units["traceback"]);
    assert_eq!(cases.len(), 18);
    for case in cases {
        let id = case["id"].as_str().unwrap();
        let inner = match case["inner"].as_str() {
            None => None,
            Some("Returncode") => Some(InSupervise::Returncode),
            Some("IdentityBefore") => Some(InSupervise::IdentityBefore),
            Some("IdentityAfter") => Some(InSupervise::IdentityAfter),
            Some(other) => panic!("知らない場所: {other}"),
        };
        let site = match (case["site"].as_str().unwrap(), inner) {
            ("CheckProcessAnchor", None) => Site::CheckProcessAnchor,
            ("CheckProcessDeadline", None) => Site::CheckProcessDeadline,
            ("CheckProcessOpen", None) => Site::CheckProcessOpen,
            ("CheckProcessSupervise", Some(inner)) => Site::CheckProcessSupervise(inner),
            ("LoopSupervisorDeadline", None) => Site::LoopSupervisorDeadline,
            ("LoopSupervisorOpen", None) => Site::LoopSupervisorOpen,
            ("LoopSupervisorSupervise", Some(inner)) => Site::LoopSupervisorSupervise(inner),
            other => panic!("知らない場所: {other:?}"),
        };
        let exception = match id {
            "cp-anchor-none" => legacy::exception("TypeError", uncaught::NONE_AS_INTEGER),
            "cp-anchor-list" => legacy::exception("TypeError", uncaught::LIST_AS_INTEGER),
            "cp-anchor-overflow" | "cp-anchor-overflow-negative" | "cp-anchor-overflow-huge" => {
                legacy::exception("OverflowError", uncaught::C_INT_OVERFLOW)
            }
            "cp-deadline-timeout-list" | "cp-deadline-grace-list" => {
                legacy::exception("TypeError", uncaught::LIST_AS_REAL)
            }
            "cp-open-none" => legacy::exception("TypeError", uncaught::OPEN_NONE),
            "cp-open-list" | "ls-open-list" => legacy::exception("TypeError", uncaught::OPEN_LIST),
            "ls-deadline-timeout-list" => legacy::exception("TypeError", uncaught::LIST_LE_INT),
            "ls-deadline-grace-list" => legacy::exception("TypeError", uncaught::LIST_LT_INT),
            "ls-keyerror" | "cp-keyerror" => legacy::exception("KeyError", "@PID@"),
            _ if id.contains("-index-") => {
                legacy::exception("IndexError", uncaught::INDEX_OUT_OF_RANGE)
            }
            other => panic!("知らないケース: {other}"),
        };
        assert_eq!(
            legacy::render(site, exception),
            case["stderr"].as_str().unwrap(),
            "{id}"
        );
    }
    // KeyError の値は、品質コマンドの pid の repr。
    assert_eq!(exception::key_error_message(&PyValue::int(4321)), "4321");
}

/// argparse を通る書き出しの失敗(help・usage・error の経路と、stdout・stderr の失敗・None)を、本家
/// 3.10.11 で旧版の main を流した実測(書いた流れと文、終了値か呼出履歴の文)と照らす。
#[test]
fn argparse_write_failures_match_legacy() {
    use legacy::{Stream, WriteStep};
    let units = units();
    let cases = list(&units["argparse_writes"]);
    assert_eq!(cases.len(), 26);
    let mut chained = 0;
    for case in cases {
        let id = case["id"].as_str().unwrap();
        let (parser, script, doc) = match case["script"].as_str().unwrap() {
            "check-process.py" => (
                &argparse::CHECK_PROCESS,
                legacy::Script::CheckProcess,
                legacy::CHECK_PROCESS_DOC,
            ),
            "loop-supervisor.py" => (
                &argparse::LOOP_SUPERVISOR,
                legacy::Script::LoopSupervisor,
                legacy::LOOP_SUPERVISOR_DOC,
            ),
            other => panic!("知らない旧スクリプト: {other}"),
        };
        let args: Vec<Vec<u32>> = list(&case["args"]).iter().map(text).collect();
        let context = argparse::Context {
            width: 78,
            description: Some(doc),
            max_str_digits: 4300,
        };
        // 流れの状態: "ok"(書ける)・"none"(None)・"fail<n>"(n 回目の write が OSError)。
        let mode = |stream: Stream| match stream {
            Stream::Stdout => case["stdout"].as_str().unwrap(),
            Stream::Stderr => case["stderr"].as_str().unwrap(),
        };
        let mut counts = [0usize; 2];
        let mut writes: Vec<Value> = Vec::new();
        let mut write =
            |streams: &[Stream], body: &[u32]| -> Result<(), r4q_python::traceback::Exception> {
                let Some(&stream) = streams.iter().find(|&&stream| mode(stream) != "none") else {
                    return Err(legacy::exception(
                        "AttributeError",
                        "'NoneType' object has no attribute 'write'",
                    ));
                };
                let slot = stream as usize;
                counts[slot] += 1;
                if mode(stream) == format!("fail{}", counts[slot]) {
                    return Err(legacy::exception(
                        "OSError",
                        "[Errno 28] No space left on device",
                    ));
                }
                let name = if stream == Stream::Stdout {
                    "stdout"
                } else {
                    "stderr"
                };
                writes.push(serde_json::json!([name, show(body)]));
                Ok(())
            };
        let raised = |chain: Vec<r4q_python::traceback::Traceback>| -> Value {
            let text: String = r4q_python::traceback::print_exception(&chain)
                .into_iter()
                .map(|piece| piece.text)
                .collect();
            serde_json::json!({ "raised": text })
        };
        let end = match argparse::parse(parser, &args, &context) {
            Outcome::Help(body) => match write(legacy::HELP_STREAMS, &body) {
                Ok(()) => serde_json::json!({ "exit": 0 }),
                Err(error) => raised(legacy::argparse_chain(script, None, WriteStep::Help, error)),
            },
            Outcome::Error(error) => match write(legacy::USAGE_STREAMS, &error.usage) {
                Err(exception) => raised(legacy::argparse_chain(
                    script,
                    Some(&error.site),
                    WriteStep::Usage,
                    exception,
                )),
                Ok(()) => match write(legacy::MESSAGE_STREAMS, &error.message) {
                    Err(exception) => raised(legacy::argparse_chain(
                        script,
                        Some(&error.site),
                        WriteStep::Message,
                        exception,
                    )),
                    Ok(()) => serde_json::json!({ "exit": 2 }),
                },
            },
            Outcome::Parsed(_) => panic!("{id}: 書き出しの経路でない"),
        };
        if end["raised"]
            .as_str()
            .is_some_and(|text| text.contains("During handling"))
        {
            chained += 1;
        }
        assert_eq!(writes, case["writes"].as_array().unwrap().clone(), "{id}");
        assert_eq!(
            end,
            case["end"],
            "{id}: {}",
            end["raised"].as_str().unwrap_or_default()
        );
    }
    assert_eq!(chained, 11, "ArgumentError を捕まえた経路の数");
}

/// 壊れた children の pid を `os.pidfd_open` に渡すときの変換(本家 3.10.11 の本物の `os.pidfd_open` の実測)。
#[test]
fn pidfd_pid_conversion_matches_python() {
    use check_process::loop_supervisor::supervise::pidfd_pid;
    let units = units();
    let cases = list(&units["pidfd_pid"]);
    assert_eq!(cases.len(), 11);
    for case in cases {
        let pid = case["pid"].as_str().unwrap();
        match case["overflow"].as_str() {
            Some(message) => assert_eq!(pidfd_pid(pid), Err(message), "{pid}"),
            None => assert_eq!(pidfd_pid(pid), Ok(pid.parse::<i32>().unwrap()), "{pid}"),
        }
    }
}

// ---- 監督の OS の部品(platform の r4q_process) ----

mod os_parts {
    use dev_workflow_platform::r4q_process::{
        self as os, Disposition, GroupSignal, Handler, PidWait, Reaped, Spawn, SpawnError,
        Termination,
    };
    use std::ffi::OsString;
    use std::os::fd::{AsFd, AsRawFd, BorrowedFd};
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::time::{Duration, Instant};

    const CHILD: &str = "R4Q_UNITS_CHILD";

    /// 子のテスト実行物の中なら true。親なら同じ名前の検査だけを子で流し、成功を確かめる。
    fn in_child(name: &str) -> bool {
        if std::env::var(CHILD).as_deref() == Ok(name) {
            return true;
        }
        let binary = std::env::current_exe().expect("テストの実行物");
        let output = Command::new(&binary)
            .env(CHILD, name)
            .args([name, "--exact", "--nocapture", "--test-threads=1"])
            .output()
            .expect("子のテストを起動できない");
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            output.status.success() && stdout.contains("CHILD-OK"),
            "{name}: {stdout}\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
        false
    }

    /// cargo の一時フォルダの下の、この検査だけのフォルダ。終わりに消す。
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let path = Path::new(env!("CARGO_TARGET_TMPDIR"))
                .join(format!("r4q-units-{name}-{}", std::process::id()));
            if path.exists() {
                std::fs::remove_dir_all(&path).unwrap();
            }
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn path(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn bytes(path: &Path) -> Vec<u8> {
        path.as_os_str().as_bytes().to_vec()
    }

    struct Request<'a> {
        argv: Vec<Vec<u8>>,
        path: Option<Vec<u8>>,
        env: Option<Vec<(Vec<u8>, Vec<u8>)>>,
        cwd: Option<Vec<u8>>,
        stdin: Option<BorrowedFd<'a>>,
        stdout: Option<BorrowedFd<'a>>,
        stderr: Option<BorrowedFd<'a>>,
        pass: Vec<BorrowedFd<'a>>,
        session: bool,
    }

    impl<'a> Request<'a> {
        fn new(argv: &[&[u8]]) -> Self {
            Self {
                argv: argv.iter().map(|arg| arg.to_vec()).collect(),
                path: None,
                env: None,
                cwd: None,
                stdin: None,
                stdout: None,
                stderr: None,
                pass: Vec::new(),
                session: false,
            }
        }

        fn spawn(&self) -> Result<i32, SpawnError> {
            let path = match (&self.env, &self.path) {
                (_, Some(path)) => Some(path.clone()),
                (Some(env), None) => env
                    .iter()
                    .rev()
                    .find(|(key, _)| key == b"PATH")
                    .map(|(_, value)| value.clone()),
                (None, None) => std::env::var_os("PATH").map(OsString::into_vec),
            };
            let candidates = os::exec_candidates(&self.argv[0], path.as_deref());
            let executables: Vec<&[u8]> = candidates.iter().map(Vec::as_slice).collect();
            let argv: Vec<&[u8]> = self.argv.iter().map(Vec::as_slice).collect();
            let env: Option<Vec<(&[u8], &[u8])>> = self.env.as_ref().map(|env| {
                env.iter()
                    .map(|(key, value)| (key.as_slice(), value.as_slice()))
                    .collect()
            });
            os::spawn(&Spawn {
                argv: &argv,
                executables: &executables,
                env: env.as_deref(),
                cwd: self.cwd.as_deref(),
                stdin: self.stdin,
                stdout: self.stdout,
                stderr: self.stderr,
                pass_fds: &self.pass,
                new_session: self.session,
            })
        }
    }

    fn wait(pid: i32) -> Termination {
        match os::retry_eintr(|| os::wait_pid(pid, true)).unwrap() {
            PidWait::Exited(status) => status,
            other => panic!("回収できない: {other:?}"),
        }
    }

    /// 標準出力をファイルへ向けて起動し、回収して、終わり方と出力を返す。
    fn run_to_file(request: &Request<'_>, out: &Path) -> (Termination, Vec<u8>) {
        let file = os::open_output(&bytes(out)).unwrap();
        let request = Request {
            argv: request.argv.clone(),
            path: request.path.clone(),
            env: request.env.clone(),
            cwd: request.cwd.clone(),
            stdin: request.stdin,
            stdout: Some(file.as_fd()),
            stderr: request.stderr,
            pass: request.pass.clone(),
            session: request.session,
        };
        let pid = request
            .spawn()
            .unwrap_or_else(|error| panic!("起動できない: {error:?}"));
        let status = wait(pid);
        drop(request);
        drop(file);
        (status, std::fs::read(out).unwrap())
    }

    #[test]
    fn exec_candidates_follow_get_exec_path_and_posixpath_join() {
        assert_eq!(
            os::exec_candidates(b"./x", Some(b"/bin")),
            vec![b"./x".to_vec()]
        );
        assert_eq!(
            os::exec_candidates(b"/bin/sh", None),
            vec![b"/bin/sh".to_vec()]
        );
        assert_eq!(
            os::exec_candidates(b"ls", None),
            vec![b"/bin/ls".to_vec(), b"/usr/bin/ls".to_vec()]
        );
        assert_eq!(os::exec_candidates(b"ls", Some(b"")), vec![b"ls".to_vec()]);
        assert_eq!(
            os::exec_candidates(b"t", Some(b"a::b/:/")),
            vec![
                b"a/t".to_vec(),
                b"t".to_vec(),
                b"b/t".to_vec(),
                b"/t".to_vec()
            ]
        );
        assert_eq!(os::exec_candidates(b"", Some(b"d")), vec![b"d/".to_vec()]);
        assert!(os::selectable(0) && os::selectable(1023));
        assert!(!os::selectable(1024) && !os::selectable(-1));
        assert_eq!(os::SELECT_LIMIT, 1024);
        assert_eq!(
            Termination::Exited(3).status(),
            Some(dev_workflow_platform::process::Status::Exited(3))
        );
        assert_eq!(
            Termination::Signaled(9).status(),
            Some(dev_workflow_platform::process::Status::Signaled(9))
        );
        assert_eq!(Termination::Unknown(0x7f).status(), None);
    }

    #[test]
    fn signal_handlers_record_first_latest_and_nothing_without_restart() {
        if !in_child("os_parts::signal_handlers_record_first_latest_and_nothing_without_restart") {
            return;
        }
        let mut saved = Vec::new();
        for number in [os::SIGTERM, os::SIGINT, os::SIGHUP] {
            saved.push(os::set_handler(number, Handler::RecordFirst).unwrap());
            let info = os::handler_info(number).unwrap();
            assert_eq!(info.disposition, Disposition::Caught);
            assert!(!info.restart && info.onstack && info.empty_mask);
        }
        os::clear_first_signal();
        assert_eq!(os::first_signal(), 0);
        os::signal_current_thread(os::SIGTERM).unwrap();
        os::signal_current_thread(os::SIGINT).unwrap();
        assert_eq!(os::first_signal(), os::SIGTERM);
        os::clear_first_signal();
        os::signal_current_thread(os::SIGHUP).unwrap();
        assert_eq!(os::first_signal(), os::SIGHUP);
        // 前の処理へ戻す(既定の処理へ)。
        for previous in saved.iter().rev() {
            os::restore_handler(previous).unwrap();
        }
        for number in [os::SIGTERM, os::SIGINT, os::SIGHUP] {
            assert_eq!(os::disposition(number).unwrap(), Disposition::Default);
        }
        // 後の信号で上書きする処理。
        for number in [os::SIGTERM, os::SIGINT, os::SIGHUP] {
            os::set_handler(number, Handler::RecordLatest).unwrap();
            assert!(!os::handler_info(number).unwrap().restart);
        }
        os::signal_current_thread(os::SIGHUP).unwrap();
        os::signal_current_thread(os::SIGINT).unwrap();
        assert_eq!(os::latest_signal(), os::SIGINT);
        os::set_latest_signal(os::SIGTERM);
        assert_eq!(os::latest_signal(), os::SIGTERM);
        // 何もしない処理(TERM でも終わらない)。
        let previous = os::set_handler(os::SIGTERM, Handler::Nothing).unwrap();
        assert_eq!(previous.number(), os::SIGTERM);
        os::signal_current_thread(os::SIGTERM).unwrap();
        assert_eq!(os::latest_signal(), os::SIGTERM);
        // 前の処理(RecordLatest)へ戻す。
        os::restore_handler(&previous).unwrap();
        os::set_latest_signal(0);
        os::signal_current_thread(os::SIGTERM).unwrap();
        assert_eq!(os::latest_signal(), os::SIGTERM);
        // 子のテストの標準出力はパイプ(端末でない)。
        assert_eq!(os::stdout_terminal_columns(), None);
        // SIGPIPE と SIGXFSZ の無視。
        os::ignore_pipe_and_file_size_signals().unwrap();
        assert_eq!(os::disposition(os::SIGPIPE).unwrap(), Disposition::Ignore);
        assert_eq!(os::disposition(os::SIGXFSZ).unwrap(), Disposition::Ignore);
        // SA_RESTART が無いので、止まった read は EINTR で戻る。pause は処理が走ると戻る。
        // 止まる呼出しは起動したスレッドの中で行い、主のスレッドがその JoinHandle を借りて信号を送る
        // (信号が呼出しより先に届いたときに備えて、終わるまで送り直す)。
        os::set_handler(os::SIGUSR1, Handler::Nothing).unwrap();
        let (reader, writer) = os::pipe().unwrap();
        let reading = std::thread::spawn(move || {
            let mut buffer = [0u8; 4];
            os::read(reader.as_fd(), &mut buffer).map_err(|error| error.raw_os_error())
        });
        interrupt_until_finished(&reading);
        assert_eq!(reading.join().unwrap(), Err(Some(os::errno::EINTR)));
        let pausing = std::thread::spawn(|| {
            let started = Instant::now();
            os::pause();
            started.elapsed()
        });
        interrupt_until_finished(&pausing);
        pausing.join().unwrap();
        // retry_eintr は EINTR を繰り返す。
        let mut calls = 0;
        let result = os::retry_eintr(|| {
            calls += 1;
            if calls < 3 {
                Err(std::io::Error::from_raw_os_error(os::errno::EINTR))
            } else {
                Ok(calls)
            }
        });
        assert_eq!(result.unwrap(), 3);
        drop(writer);
        println!("CHILD-OK");
    }

    /// `handle` のスレッドが終わるまで、20 ms ごとに SIGUSR1 を送る(10 秒まで)。
    fn interrupt_until_finished<T>(handle: &std::thread::JoinHandle<T>) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !handle.is_finished() {
            assert!(Instant::now() < deadline, "止まった呼出しが信号で戻らない");
            // 確かめと送る間に終わったスレッドは ESRCH になりうる(join まで pthread_t は有効)。
            match os::signal_thread(handle, os::SIGUSR1) {
                Err(error) if error.raw_os_error() != Some(os::errno::ESRCH) => {
                    panic!("送れない: {error}")
                }
                _ => {}
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn wait_results_distinguish_running_exited_and_echild() {
        if !in_child("os_parts::wait_results_distinguish_running_exited_and_echild") {
            return;
        }
        assert_eq!(os::wait_any_nohang().unwrap(), Reaped::NoChildren);
        // 自分の子でない pid は ECHILD。
        assert_eq!(os::wait_pid(1, false).unwrap(), PidWait::NoChild);
        let sleeper = Request::new(&[b"/bin/sh", b"-c", b"exec sleep 30"])
            .spawn()
            .unwrap();
        assert_eq!(os::wait_pid(sleeper, false).unwrap(), PidWait::Running);
        assert_eq!(os::wait_any_nohang().unwrap(), Reaped::NotYet);
        os::kill(sleeper, os::SIGKILL).unwrap();
        let reaped = loop {
            match os::wait_any_nohang().unwrap() {
                Reaped::NotYet => std::thread::sleep(Duration::from_millis(5)),
                other => break other,
            }
        };
        assert_eq!(
            reaped,
            Reaped::Child {
                pid: sleeper,
                status: Termination::Signaled(9)
            }
        );
        assert_eq!(os::wait_any_nohang().unwrap(), Reaped::NoChildren);
        let pid = Request::new(&[b"/bin/sh", b"-c", b"exit 300"])
            .spawn()
            .unwrap();
        assert_eq!(wait(pid), Termination::Exited(44));
        let pid = Request::new(&[b"/bin/sh", b"-c", b"kill -SEGV $$"])
            .spawn()
            .unwrap();
        assert_eq!(wait(pid), Termination::Signaled(11));
        // SIGCHLD を無視した起動: Linux では子が自動で回収され、wait は ECHILD(旧版の Popen は returncode 0)。
        // Mac の自動の回収は Mac の CI の比較器(単位 3)で確かめる。
        os::set_handler(os::SIGCHLD, Handler::Ignore).unwrap();
        let pid = Request::new(&[b"/bin/sh", b"-c", b"exit 3"])
            .spawn()
            .unwrap();
        let waited = os::retry_eintr(|| os::wait_pid(pid, true)).unwrap();
        if cfg!(target_os = "linux") {
            assert_eq!(waited, PidWait::NoChild);
        }
        // 起動の失敗も panic せずに返る(std の Command と違う)。
        let error = Request::new(&[b"/nonexistent/r4q"]).spawn().unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOENT,
                    before_exec: false
                }
            ),
            "{error:?}"
        );
        os::set_handler(os::SIGCHLD, Handler::Default).unwrap();
        println!("CHILD-OK");
    }

    #[test]
    fn killpg_results_are_typed() {
        if !in_child("os_parts::killpg_results_are_typed") {
            return;
        }
        let request = Request {
            session: true,
            ..Request::new(&[b"/bin/sh", b"-c", b"exec sleep 30"])
        };
        let leader = request.spawn().unwrap();
        assert!(matches!(os::killpg(leader, 0), GroupSignal::Sent));
        let permission = std::io::Error::from_raw_os_error(os::errno::EPERM);
        assert!(matches!(
            GroupSignal::from_result(Err(permission)),
            GroupSignal::NotPermitted
        ));
        let missing = std::io::Error::from_raw_os_error(os::errno::ESRCH);
        assert!(matches!(
            GroupSignal::from_result(Err(missing)),
            GroupSignal::NoProcess
        ));
        assert!(matches!(os::killpg(leader, 99999), GroupSignal::Failed(_)));
        assert!(matches!(os::killpg(leader, os::SIGKILL), GroupSignal::Sent));
        assert_eq!(wait(leader), Termination::Signaled(9));
        // 回収の後は ESRCH(Mac では回収の直後に一時的な EPERM があるので、旧版と同じく 5 秒まで待つ)。
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            match os::killpg(leader, 0) {
                GroupSignal::NoProcess => break,
                GroupSignal::NotPermitted
                    if cfg!(target_os = "macos") && Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(20));
                }
                other => panic!("群が残る: {other:?}"),
            }
        }
        println!("CHILD-OK");
    }

    /// 段階 5 へ渡す /proc の読取り(platform)と解釈(純粋な関数)。
    #[cfg(target_os = "linux")]
    #[test]
    fn proc_stat_and_children_are_read_and_parsed() {
        use super::check_process::loop_supervisor::supervise::{
            IdentityError, parse_children, parse_identity,
        };
        let me = os::getpid().to_string();
        let raw = os::read_proc_stat(&me).unwrap();
        let (ppid, starttime) = parse_identity(&raw, 4300).unwrap();
        assert_eq!(ppid, os::getppid().to_string());
        assert!(starttime.parse::<u64>().unwrap() > 0);
        let children = os::read_own_children(os::getpid()).unwrap();
        let text: Vec<u32> = children.iter().map(|&byte| u32::from(byte)).collect();
        assert!(parse_children(&text, 4300).is_ok());
        let missing = os::read_proc_stat("2147483647").unwrap_err();
        assert!(missing.has_filename(), "開く段の誤りはファイル名付き");
        assert_eq!(missing.errno(), os::errno::ENOENT);
        assert_eq!(os::proc_stat_path("12"), "/proc/12/stat");
        assert_eq!(os::own_children_path(12), "/proc/self/task/12/children");
        // 解釈の純粋な関数(旧版の str.split・rindex(b')')・fields[1]・fields[19])。
        let spaced: Vec<u32> = "\u{3000}12\t34\x1c".chars().map(u32::from).collect();
        assert_eq!(parse_children(&spaced, 4300).unwrap(), vec!["12", "34"]);
        assert!(parse_children(&['x' as u32], 4300).is_err());
        assert!(matches!(
            parse_identity(b"1 cmd S 2", 4300),
            Err(IdentityError::Raise(_))
        ));
        assert_eq!(
            parse_identity(b"1 (a) b) S 2", 4300),
            Err(IdentityError::Index)
        );
    }

    #[test]
    fn pipe_select_and_files() {
        let scratch = Scratch::new("files");
        let (reader, writer) = os::pipe().unwrap();
        assert!(!os::select_readable(reader.as_fd(), Some(Duration::ZERO)).unwrap());
        assert!(!os::select_readable(reader.as_fd(), Some(Duration::from_millis(20))).unwrap());
        assert_eq!(os::write(writer.as_fd(), b"ab").unwrap(), 2);
        assert!(os::select_readable(reader.as_fd(), None).unwrap());
        let mut buffer = [0u8; 8];
        assert_eq!(os::read(reader.as_fd(), &mut buffer).unwrap(), 2);
        os::close(writer).unwrap();
        // 書き口が全部閉じると、終わりも「読める」。同じ実行物の別のテストのスレッドが fork した子は、
        // exec(CLOEXEC で閉じる)までの間だけ書き口の写しを持つので、期限を付けて待つ。
        assert!(os::select_readable(reader.as_fd(), Some(Duration::from_secs(20))).unwrap());
        assert_eq!(os::read(reader.as_fd(), &mut buffer).unwrap(), 0);
        // 記録のファイル: 排他で作り、0600(umask を引く)、リンクを辿らない。
        let record = scratch.path("recovery.json");
        let fd = os::create_record(&bytes(&record)).unwrap();
        assert_eq!(os::write(fd.as_fd(), b"{}\n").unwrap(), 3);
        os::fsync(fd.as_fd()).unwrap();
        os::close(fd).unwrap();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&record).unwrap().permissions().mode() & 0o777 & !0o600,
            0
        );
        let again = os::create_record(&bytes(&record)).unwrap_err();
        assert_eq!(again.raw_os_error(), Some(os::errno::EEXIST));
        let link = scratch.path("link.json");
        std::os::unix::fs::symlink(scratch.path("target.json"), &link).unwrap();
        assert_eq!(
            os::create_record(&bytes(&link)).unwrap_err().raw_os_error(),
            Some(os::errno::EEXIST)
        );
        assert!(!scratch.path("target.json").exists(), "リンク先を作らない");
        std::fs::write(scratch.path("target.json"), b"x").unwrap();
        assert_eq!(
            os::open_record(&bytes(&link)).unwrap_err().raw_os_error(),
            Some(os::errno::ELOOP)
        );
        let fd = os::open_record(&bytes(&record)).unwrap();
        let info = os::fstat(fd.as_fd()).unwrap();
        assert!(info.regular && info.size == 3);
        let mut raw = [0u8; 4097];
        assert_eq!(os::read(fd.as_fd(), &mut raw).unwrap(), 3);
        let directory = os::open_record(&bytes(&scratch.0)).unwrap();
        assert!(!os::fstat(directory.as_fd()).unwrap().regular);
        // 出力のファイル: 切り詰めて作り直し、リンクを辿る。
        let output = os::open_output(&bytes(&link)).unwrap();
        assert_eq!(os::write(output.as_fd(), b"out").unwrap(), 3);
        assert_eq!(os::seek_start(output.as_fd()).unwrap(), 0);
        assert_eq!(std::fs::read(scratch.path("target.json")).unwrap(), b"out");
        let null = os::open_dev_null().unwrap();
        assert_eq!(os::write(null.as_fd(), b"x").unwrap(), 1);
        os::set_inheritable(null.as_fd(), true).unwrap();
        os::set_inheritable(null.as_fd(), false).unwrap();
        // anchor の継いだ fd: 開いている番号だけを借りる。負の番号と閉じた番号は EBADF。
        let inherited = os::inherited_fd(null.as_raw_fd()).unwrap();
        assert_eq!(os::write(inherited, b"y").unwrap(), 1);
        // 閉じた番号は、別のテストのスレッドがすぐ使い直しうるので、開けない番号(i32::MAX)で確かめる。
        for bad in [-1, -5, i32::MAX] {
            assert_eq!(
                os::inherited_fd(bad).unwrap_err().raw_os_error(),
                Some(os::errno::EBADF)
            );
        }
    }

    #[test]
    fn spawn_rejects_values_before_fork_and_reports_exec_errors() {
        let scratch = Scratch::new("exec");
        let error = Request {
            env: Some(vec![(b"A=B".to_vec(), b"1".to_vec())]),
            ..Request::new(&[b"/bin/true"])
        }
        .spawn()
        .unwrap_err();
        assert!(matches!(error, SpawnError::IllegalEnvironmentName));
        for request in [
            Request::new(&[b"/bin/tr\0ue"]),
            Request::new(&[b"/bin/true", b"a\0b"]),
            Request {
                env: Some(vec![(b"A".to_vec(), b"x\0".to_vec())]),
                ..Request::new(&[b"/bin/true"])
            },
            Request {
                cwd: Some(b"/\0".to_vec()),
                ..Request::new(&[b"/bin/true"])
            },
        ] {
            assert!(matches!(
                request.spawn().unwrap_err(),
                SpawnError::EmbeddedNull
            ));
        }
        // 作業フォルダへ移れない(exec の前の失敗)。
        let error = Request {
            cwd: Some(bytes(&scratch.path("missing"))),
            ..Request::new(&[b"/bin/true"])
        }
        .spawn()
        .unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOENT,
                    before_exec: true
                }
            ),
            "{error:?}"
        );
        // シバンの無い実行可能ファイルは ENOEXEC(sh に切り替えない)。
        let bare = scratch.path("bare");
        std::fs::write(&bare, b"exit 0\n").unwrap();
        set_mode(&bare, 0o755);
        let error = Request::new(&[&bytes(&bare)]).spawn().unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOEXEC,
                    before_exec: false
                }
            ),
            "{error:?}"
        );
        // PATH の探し方: EACCES の後も探し、見つからなければ最初の ENOENT・ENOTDIR でない誤り。
        let empty = scratch.path("empty");
        let denied = scratch.path("denied");
        let good = scratch.path("good");
        for directory in [&empty, &denied, &good] {
            std::fs::create_dir(directory).unwrap();
        }
        std::fs::write(denied.join("tool"), b"#!/bin/sh\nexit 9\n").unwrap();
        set_mode(&denied.join("tool"), 0o644);
        std::fs::write(good.join("tool"), b"#!/bin/sh\nexit 7\n").unwrap();
        set_mode(&good.join("tool"), 0o755);
        let joined = |dirs: &[&Path]| {
            dirs.iter()
                .map(|dir| bytes(dir))
                .collect::<Vec<_>>()
                .join(&b':')
        };
        let pid = Request {
            path: Some(joined(&[&empty, &denied, &good])),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap();
        assert_eq!(wait(pid), Termination::Exited(7));
        let error = Request {
            path: Some(joined(&[&empty, &denied])),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::EACCES,
                    before_exec: false
                }
            ),
            "{error:?}"
        );
        let error = Request {
            path: Some(joined(&[&empty])),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOENT,
                    ..
                }
            ),
            "{error:?}"
        );
        // 全部が ENOENT・ENOTDIR なら、最後の候補の誤り。
        let file = scratch.path("plain");
        std::fs::write(&file, b"").unwrap();
        let error = Request {
            path: Some(joined(&[&empty, &file])),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOTDIR,
                    ..
                }
            ),
            "{error:?}"
        );
        let error = Request {
            path: Some(joined(&[&file, &empty])),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap_err();
        assert!(
            matches!(
                error,
                SpawnError::Child {
                    errno: os::errno::ENOENT,
                    ..
                }
            ),
            "{error:?}"
        );
        // EACCES の後に ENOENT・ENOTDIR が続いても、報告するのは最初の EACCES(最後の候補の誤りではない)。
        for tail in [&empty, &file] {
            let error = Request {
                path: Some(joined(&[&denied, tail])),
                ..Request::new(&[b"tool"])
            }
            .spawn()
            .unwrap_err();
            assert!(
                matches!(
                    error,
                    SpawnError::Child {
                        errno: os::errno::EACCES,
                        before_exec: false
                    }
                ),
                "{}: {error:?}",
                tail.display()
            );
        }
        // env を渡したときは、その PATH で探す。
        let pid = Request {
            env: Some(vec![(b"PATH".to_vec(), bytes(&good))]),
            ..Request::new(&[b"tool"])
        }
        .spawn()
        .unwrap();
        assert_eq!(wait(pid), Termination::Exited(7));
    }

    fn set_mode(path: &Path, mode: u32) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    }

    #[test]
    fn spawn_passes_env_in_order_cwd_and_standard_fds() {
        let scratch = Scratch::new("env");
        let env = vec![
            (b"Z".to_vec(), b"1".to_vec()),
            (b"A".to_vec(), b"2".to_vec()),
            (b"PATH".to_vec(), b"/usr/bin:/bin".to_vec()),
            (b"M".to_vec(), b"\xff".to_vec()),
        ];
        let request = Request {
            env: Some(env),
            ..Request::new(&[b"/usr/bin/env"])
        };
        let (status, output) = run_to_file(&request, &scratch.path("env.txt"));
        assert_eq!(status, Termination::Exited(0));
        assert_eq!(output, b"Z=1\nA=2\nPATH=/usr/bin:/bin\nM=\xff\n");
        // env を渡さないときは、起動した側の環境(libc の environ)をそのまま(並びも)継ぐ。
        // 旧版の Popen(env=None)も execv で environ を継ぐ。cargo が Mac で足す
        // DYLD_FALLBACK_LIBRARY_PATH も、そのまま子に届く(macOS 15 の CI で /usr/bin/env が出力した)。
        // vars_os は environ を前から読む(`=` の無い項目だけを飛ばす。テストの環境には無い)。
        let request = Request::new(&[b"/usr/bin/env"]);
        let (_, output) = run_to_file(&request, &scratch.path("inherit.txt"));
        let mut expected = Vec::new();
        for (key, value) in std::env::vars_os() {
            expected.extend_from_slice(key.as_bytes());
            expected.push(b'=');
            expected.extend_from_slice(value.as_bytes());
            expected.push(b'\n');
        }
        assert_eq!(output, expected);
        // 作業フォルダ。
        let request = Request {
            cwd: Some(bytes(&scratch.0)),
            ..Request::new(&[b"/bin/pwd"])
        };
        let (_, output) = run_to_file(&request, &scratch.path("pwd.txt"));
        assert_eq!(
            output,
            [
                bytes(&std::fs::canonicalize(&scratch.0).unwrap()),
                b"\n".to_vec()
            ]
            .concat()
        );
        // 0〜2 の向け先(stdin は /dev/null、stdout と stderr はファイル)。
        let null = os::open_dev_null().unwrap();
        let errors = os::open_output(&bytes(&scratch.path("stderr.txt"))).unwrap();
        let request = Request {
            stdin: Some(null.as_fd()),
            stderr: Some(errors.as_fd()),
            ..Request::new(&[b"/bin/sh", b"-c", b"cat; printf err >&2; printf out"])
        };
        let (status, output) = run_to_file(&request, &scratch.path("stdout.txt"));
        assert_eq!(status, Termination::Exited(0));
        assert_eq!(output, b"out");
        assert_eq!(std::fs::read(scratch.path("stderr.txt")).unwrap(), b"err");
    }

    #[cfg(target_os = "linux")]
    fn status_field(text: &str, name: &str) -> u64 {
        let line = text
            .lines()
            .find_map(|line| line.strip_prefix(&format!("{name}:")))
            .unwrap_or_else(|| panic!("{name} が無い"));
        u64::from_str_radix(line.trim(), 16).unwrap()
    }

    #[cfg(target_os = "linux")]
    fn bit(number: i32) -> u64 {
        1 << (number - 1)
    }

    /// `/proc/<pid>/stat` の (pid, pgrp, session)。
    #[cfg(target_os = "linux")]
    fn ids(stat: &[u8]) -> (i64, i64, i64) {
        let text = String::from_utf8_lossy(stat);
        let pid: i64 = text.split_whitespace().next().unwrap().parse().unwrap();
        let rest = &text[text.rfind(')').unwrap() + 2..];
        let fields: Vec<&str> = rest.split_whitespace().collect();
        (pid, fields[2].parse().unwrap(), fields[3].parse().unwrap())
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn spawn_keeps_mask_and_ignored_signals_resets_pipe_and_closes_fds() {
        if !in_child("os_parts::spawn_keeps_mask_and_ignored_signals_resets_pipe_and_closes_fds") {
            return;
        }
        let scratch = Scratch::new("signals");
        os::set_handler(os::SIGUSR2, Handler::Ignore).unwrap();
        os::set_handler(os::SIGQUIT, Handler::Ignore).unwrap();
        os::ignore_pipe_and_file_size_signals().unwrap();
        os::set_handler(os::SIGTERM, Handler::RecordFirst).unwrap();
        os::set_handler(os::SIGHUP, Handler::Nothing).unwrap();
        let before = os::block_signals(&[os::SIGUSR1]).unwrap();
        assert!(!before.contains(os::SIGUSR1));
        let request = Request::new(&[b"/bin/cat", b"/proc/self/status"]);
        let (status, output) = run_to_file(&request, &scratch.path("status.txt"));
        assert_eq!(status, Termination::Exited(0));
        let text = String::from_utf8(output).unwrap();
        let blocked = status_field(&text, "SigBlk");
        let ignored = status_field(&text, "SigIgn");
        let caught = status_field(&text, "SigCgt");
        assert_ne!(
            blocked & bit(os::SIGUSR1),
            0,
            "起動した側の mask を受け継ぐ"
        );
        assert_eq!(
            blocked & !bit(os::SIGUSR1),
            0,
            "fork の間に止めた信号は戻す"
        );
        assert_ne!(ignored & bit(os::SIGUSR2), 0, "無視の設定は残る");
        assert_ne!(ignored & bit(os::SIGQUIT), 0);
        for number in [os::SIGPIPE, os::SIGXFSZ, os::SIGTERM, os::SIGHUP] {
            assert_eq!(ignored & bit(number), 0, "{number} は既定");
            assert_eq!(caught & bit(number), 0, "{number} は既定");
        }
        os::set_mask(&before).unwrap();
        // 0〜2 のほかの fd を閉じる。渡した fd だけを同じ番号で継がせる。setsid は求めたときだけ。
        let passed = std::fs::File::open("/dev/null").unwrap();
        let leaked = std::fs::File::open("/dev/null").unwrap();
        os::set_inheritable(leaked.as_fd(), true).unwrap();
        let script = b"ls /proc/$$/fd; cat /proc/$$/stat".to_vec();
        let request = Request {
            pass: vec![passed.as_fd()],
            ..Request::new(&[b"/bin/sh", b"-c", &script])
        };
        let (_, output) = run_to_file(&request, &scratch.path("fds.txt"));
        let text = String::from_utf8(output).unwrap();
        let mut lines: Vec<&str> = text.lines().collect();
        let stat = lines.pop().unwrap().as_bytes().to_vec();
        let mut fds: Vec<i32> = lines.iter().map(|line| line.parse().unwrap()).collect();
        fds.sort_unstable();
        assert_eq!(fds, vec![0, 1, 2, passed.as_raw_fd()]);
        let own = std::fs::read("/proc/self/stat").unwrap();
        let (_, own_group, own_session) = ids(&own);
        let (child, group, session) = ids(&stat);
        assert_ne!(child, group);
        assert_eq!((group, session), (own_group, own_session), "setsid しない");
        let request = Request {
            session: true,
            ..Request::new(&[b"/bin/cat", b"/proc/self/stat"])
        };
        let (_, stat) = run_to_file(&request, &scratch.path("session.txt"));
        let (child, group, session) = ids(&stat);
        assert_eq!(
            (group, session),
            (child, child),
            "setsid で新しいセッション"
        );
        println!("CHILD-OK");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn select_rejects_fd_at_or_above_fd_setsize() {
        if !in_child("os_parts::select_rejects_fd_at_or_above_fd_setsize") {
            return;
        }
        let mut held: Vec<std::os::fd::OwnedFd> = Vec::new();
        let high = loop {
            let fd = os::open_dev_null().unwrap();
            if fd.as_raw_fd() >= 1024 {
                break fd;
            }
            held.push(fd);
        };
        assert!(matches!(
            os::select_readable(high.as_fd(), Some(Duration::ZERO)),
            Err(os::SelectError::OutOfRange)
        ));
        let low = held.first().unwrap();
        assert!(os::select_readable(low.as_fd(), Some(Duration::ZERO)).unwrap());
        println!("CHILD-OK");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn subreaper_adopts_orphans_and_pidfd_signals() {
        if !in_child("os_parts::subreaper_adopts_orphans_and_pidfd_signals") {
            return;
        }
        let parent = os::getppid();
        assert_eq!(parent as u32, std::os::unix::process::parent_id());
        assert_eq!(os::getpid() as u32, std::process::id());
        os::set_child_subreaper().unwrap();
        os::set_parent_death_signal(os::SIGTERM).unwrap();
        let pidfd = os::pidfd_open(os::getpid()).unwrap();
        os::pidfd_send_signal(pidfd.as_fd(), 0).unwrap();
        drop(pidfd);
        // 孫(親が先に終わる)は subreaper の自分へ移り、waitpid(-1) で回収できる。
        let pid = Request::new(&[b"/bin/sh", b"-c", b"(sleep 0.3; exit 5) & exit 3"])
            .spawn()
            .unwrap();
        let mut statuses = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            match os::wait_any_nohang().unwrap() {
                Reaped::Child {
                    pid: reaped,
                    status,
                } => statuses.push((reaped == pid, status)),
                Reaped::NotYet => {
                    assert!(Instant::now() < deadline, "孫を回収できない");
                    std::thread::sleep(Duration::from_millis(10));
                }
                Reaped::NoChildren => break,
            }
        }
        statuses.sort_by_key(|(direct, _)| !*direct);
        assert_eq!(
            statuses,
            vec![
                (true, Termination::Exited(3)),
                (false, Termination::Exited(5))
            ]
        );
        // pidfd で信号を送る。回収した後の pid は開けない(ESRCH)。
        let sleeper = Request::new(&[b"/bin/sh", b"-c", b"exec sleep 30"])
            .spawn()
            .unwrap();
        let pidfd = os::pidfd_open(sleeper).unwrap();
        os::pidfd_send_signal(pidfd.as_fd(), os::SIGKILL).unwrap();
        assert_eq!(wait(sleeper), Termination::Signaled(9));
        let error = os::pidfd_send_signal(pidfd.as_fd(), 0).unwrap_err();
        assert_eq!(error.raw_os_error(), Some(os::errno::ESRCH));
        assert_eq!(
            os::pidfd_open(sleeper).unwrap_err().raw_os_error(),
            Some(os::errno::ESRCH)
        );
        println!("CHILD-OK");
    }

    /// 親の終了通知の受け手(テストの実行物を孫として起動したもの)。親のスレッドが終わると SIGUSR1 を受け、
    /// 印のファイルに `got` を書く。
    #[cfg(target_os = "linux")]
    #[test]
    fn parent_death_signal_target() {
        let Ok(marker) = std::env::var("R4Q_UNITS_PDEATH_MARKER") else {
            return;
        };
        os::set_handler(os::SIGUSR1, Handler::RecordLatest).unwrap();
        os::set_latest_signal(0);
        os::set_parent_death_signal(os::SIGUSR1).unwrap();
        std::fs::write(&marker, b"ready").unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        while os::latest_signal() != os::SIGUSR1 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        if os::latest_signal() == os::SIGUSR1 {
            std::fs::write(&marker, b"got").unwrap();
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn parent_death_signal_reaches_child_when_parent_thread_ends() {
        if std::env::var(CHILD).is_err() {
            // 親: 中間の子を起動し、中間が終わった後に孫が印を `got` にするのを待つ。
            let scratch = Scratch::new("pdeath");
            let marker = scratch.path("marker");
            let binary = std::env::current_exe().unwrap();
            let name = "os_parts::parent_death_signal_reaches_child_when_parent_thread_ends";
            let status = Command::new(&binary)
                .env(CHILD, name)
                .env("R4Q_UNITS_PDEATH_DIR", &scratch.0)
                .args([name, "--exact", "--nocapture", "--test-threads=1"])
                .output()
                .unwrap();
            assert!(
                status.status.success()
                    && String::from_utf8_lossy(&status.stdout).contains("CHILD-OK"),
                "{}",
                String::from_utf8_lossy(&status.stderr)
            );
            let deadline = Instant::now() + Duration::from_secs(20);
            loop {
                if std::fs::read(&marker).ok().as_deref() == Some(b"got") {
                    break;
                }
                assert!(Instant::now() < deadline, "親の終了通知が届かない");
                std::thread::sleep(Duration::from_millis(20));
            }
            return;
        }
        // 中間: 孫を起動し、孫が準備できたら終わる(このスレッドが終わると孫へ SIGUSR1 が届く)。
        let directory = PathBuf::from(std::env::var_os("R4Q_UNITS_PDEATH_DIR").unwrap());
        let marker = directory.join("marker");
        let binary = std::env::current_exe().unwrap();
        let env: Vec<(Vec<u8>, Vec<u8>)> = vec![
            (b"R4Q_UNITS_PDEATH_MARKER".to_vec(), bytes(&marker)),
            (b"PATH".to_vec(), b"/usr/bin:/bin".to_vec()),
        ];
        let null = os::open_dev_null().unwrap();
        let request = Request {
            env: Some(env),
            stdout: Some(null.as_fd()),
            stderr: Some(null.as_fd()),
            ..Request::new(&[
                &bytes(&binary),
                b"os_parts::parent_death_signal_target",
                b"--exact",
                b"--test-threads=1",
            ])
        };
        request.spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        while std::fs::read(&marker).ok().as_deref() != Some(b"ready") {
            assert!(Instant::now() < deadline, "孫が準備できない");
            std::thread::sleep(Duration::from_millis(10));
        }
        println!("CHILD-OK");
    }
}

/// 監督と親の差し替えの比較(設計の「比べ方の決まり」の差し替えの列)。旧版を mock して作った固定期待と、
/// 同じ応答を操作の単位の trait([`Ops`])で与えた Rust の呼出しの並びと結果を、ケースの ID ごとに照らす。
mod injected {
    use super::check_process::cli::{self as cp_cli, CheckProcessResult};
    use super::check_process::loop_supervisor::cli::{self as ls_cli, MainResult};
    use super::check_process::loop_supervisor::ops::{
        Class, FileInfo, Handler, MonitorLaunch, OpenFlags, Ops, OsFailure, OsResult, Platform,
        PyErr, Saved, SelectFailure, WaitFailure,
    };
    use super::check_process::run_command::{
        CALLER_FRAMES, Method, Monitor, PYTHON_RECURSION_LIMIT, Request, RunError, RunRequest,
        enter_stop, exit_stop, notice_room, read_result_room, run_with,
    };
    use super::r4q_python::argparse::Context;
    use super::r4q_python::exception::Filename;
    use super::r4q_python::legacy;
    use super::r4q_python::number::PyNumber;
    use super::r4q_python::value::PyValue;
    use super::{hex, unhex};
    use serde_json::{Value, json};
    use std::collections::{HashMap, HashSet, VecDeque};
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    use std::path::PathBuf;

    const INJECTED: &str = include_str!("../../../fixtures/r4q/injected.json");
    /// 固定期待のケースの数(足したら、ここも直す)。
    const COUNT: usize = 197;
    /// どの機械でも必ず流す ID。
    const FIXED: [&str; 2] = [
        "inj-posix-anchor-killed-after-notice-linux",
        "inj-posix-anchor-killed-after-notice-macos",
    ];
    const LIMIT: usize = 20000;
    const ENOENT: i32 = 2;
    const EPERM: i32 = 1;
    const ESRCH: i32 = 3;
    const SIGINT: i32 = 2;

    /// 否定の検査で当てる、わざと誤った振る舞い(誤った操作の並びや応答の扱い)。
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    enum Mutation {
        None,
        /// killpg の TERM・KILL の EPERM を無視する。
        IgnoreEperm,
        /// 群の確かめ(signal 0)の EPERM を不在とする。
        EpermAsAbsent,
        /// pidfd ではなく pid で送る。
        SendByPid,
        /// pidfd を開いた後の starttime を照らさない(前に読んだ値を使う)。
        SkipStarttime,
        /// 最初の killpg の前に anchor を poll で回収する。
        ReapAnchorEarly,
        /// pidfd が無いと POSIX の方式へ切り替える(pidfd の無い Linux を POSIX の OS に見せる)。
        FallbackToPosix,
    }

    /// 差し替えた OS の世界(`gen_injected.py` の `World` と同じ規則)。
    struct Fake {
        queues: HashMap<String, VecDeque<Value>>,
        defaults: HashMap<String, Value>,
        calls: Vec<Value>,
        clock: f64,
        platform: Platform,
        has_pidfd: bool,
        pid: i32,
        ppid: i32,
        handlers: HashMap<i32, Handler>,
        latest: i32,
        first: i32,
        saved: usize,
        next_fd: i32,
        output_fds: HashSet<i32>,
        pidfds: HashMap<i32, i32>,
        last_stat: HashMap<String, Vec<u8>>,
        reaped_early: bool,
        /// 既定の SIGINT の処理の印(KeyboardInterrupt)。
        keyboard: bool,
        /// 呼出しが上限を超えた(誤った振る舞いの終わらない待ち。時計を先へ飛ばして期限で抜けさせる)。
        exhausted: bool,
        mutation: Mutation,
    }

    fn chars(text: &str) -> Vec<u32> {
        text.chars().map(u32::from).collect()
    }

    fn string(codes: &[u32]) -> String {
        codes
            .iter()
            .map(|&code| char::from_u32(code).expect("surrogate を含まない"))
            .collect()
    }

    fn utf8(raw: &[u8]) -> String {
        String::from_utf8(raw.to_vec()).expect("固定期待の入力は UTF-8")
    }

    impl Fake {
        fn new(world: &Value, mutation: Mutation) -> Self {
            let mut queues = HashMap::new();
            if let Some(responses) = world.get("responses").and_then(Value::as_object) {
                for (name, items) in responses {
                    let items: VecDeque<Value> =
                        items.as_array().unwrap().iter().cloned().collect();
                    queues.insert(name.clone(), items);
                }
            }
            let mut defaults = HashMap::new();
            if let Some(items) = world.get("defaults").and_then(Value::as_object) {
                for (name, item) in items {
                    defaults.insert(name.clone(), item.clone());
                }
            }
            let platform = match world
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("linux")
            {
                "linux" => Platform::Linux,
                "darwin" => Platform::Darwin,
                other => panic!("知らない OS: {other}"),
            };
            let number = |name: &str, default: i64| {
                world.get(name).and_then(Value::as_i64).unwrap_or(default) as i32
            };
            Self {
                queues,
                defaults,
                calls: Vec::new(),
                clock: 1000.0,
                platform,
                has_pidfd: world
                    .get("has_pidfd")
                    .and_then(Value::as_bool)
                    .unwrap_or(true),
                pid: number("pid", 100),
                ppid: number("ppid", 50),
                handlers: HashMap::new(),
                latest: 0,
                first: 0,
                saved: 0,
                next_fd: 10,
                output_fds: HashSet::new(),
                pidfds: HashMap::new(),
                last_stat: HashMap::new(),
                reaped_early: false,
                keyboard: false,
                exhausted: false,
                mutation,
            }
        }

        fn record(&mut self, call: Value) {
            if self.calls.len() >= LIMIT {
                self.exhausted = true;
                return;
            }
            self.calls.push(call);
        }

        fn take(&mut self, name: &str) -> Option<Value> {
            if let Some(queue) = self.queues.get_mut(name)
                && let Some(item) = queue.pop_front()
            {
                return Some(item);
            }
            self.defaults.get(name).cloned()
        }

        /// 記録して応答を取り、時計を進める。
        fn step(&mut self, name: &str, call: Value) -> Option<Value> {
            self.record(call);
            let response = self.take(name);
            self.advance(&response);
            response
        }

        fn advance(&mut self, response: &Option<Value>) {
            if let Some(seconds) = response
                .as_ref()
                .and_then(|r| r.get("advance"))
                .and_then(Value::as_f64)
            {
                self.clock += seconds;
            }
        }

        /// 応答の "signal": 置いた処理を呼ぶ。
        fn deliver(&mut self, response: &Option<Value>) {
            let Some(number) = response
                .as_ref()
                .and_then(|r| r.get("signal"))
                .and_then(Value::as_i64)
            else {
                return;
            };
            let number = number as i32;
            match self.handlers.get(&number) {
                Some(Handler::Interrupted) => self.latest = number,
                Some(Handler::Receive) => {
                    if self.first == 0 {
                        self.first = number;
                    }
                }
                Some(Handler::Nothing) => {}
                // 置き換える前の SIGINT は Python の既定の処理(KeyboardInterrupt。区切りで見る)。
                None if number == SIGINT => self.keyboard = true,
                other => panic!("信号 {number} の処理が置かれていない: {other:?}"),
            }
        }

        fn failure(response: &Option<Value>) -> Option<(OsFailure, Option<String>)> {
            let error = response.as_ref()?.get("err")?;
            Some((
                OsFailure {
                    errno: error["errno"].as_i64().unwrap() as i32,
                    text: error["text"].as_str().unwrap().to_owned(),
                },
                error
                    .get("filename")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            ))
        }

        /// `/proc` の読取りの誤り(開く段はファイル名付き、"at": "read" の読む段は付かない)。
        fn proc_failure(response: &Option<Value>, path: &str) -> Option<PyErr> {
            let (failure, _) = Self::failure(response)?;
            let at_read =
                response.as_ref()?["err"].get("at").and_then(Value::as_str) == Some("read");
            let filename = (!at_read).then(|| Filename::from_raw(path.as_bytes()));
            Some(PyErr::os(&failure, filename))
        }

        fn ok(response: &Option<Value>) -> Option<&Value> {
            response.as_ref()?.get("ok")
        }

        fn fd(&mut self) -> i32 {
            self.next_fd += 1;
            self.next_fd
        }

        /// 値を返すだけの操作(誤りは OsFailure)。
        fn simple<T>(
            &mut self,
            name: &str,
            call: Value,
            read: impl FnOnce(&Value) -> T,
            default: impl FnOnce(&mut Self) -> T,
        ) -> OsResult<T> {
            let response = self.step(name, call);
            if let Some((failure, _)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(failure);
            }
            let value = match Self::ok(&response).filter(|value| !value.is_null()) {
                Some(value) => read(value),
                None => default(self),
            };
            self.deliver(&response);
            Ok(value)
        }

        fn unused(&self) -> Vec<String> {
            let mut names: Vec<String> = self
                .queues
                .iter()
                .filter(|(_, items)| !items.is_empty())
                .map(|(name, items)| format!("{name}×{}", items.len()))
                .collect();
            names.sort();
            names
        }
    }

    fn handler_name(handler: Handler) -> Value {
        match handler {
            Handler::Interrupted => json!("interrupted"),
            Handler::Receive => json!("receive"),
            Handler::Nothing => json!("nothing"),
            Handler::Previous(Saved(number)) => json!(["previous", number]),
        }
    }

    impl Ops for Fake {
        fn monotonic(&mut self) -> f64 {
            let response = self.step("monotonic", json!(["monotonic"]));
            if let Some(value) = Self::ok(&response).and_then(Value::as_f64) {
                self.clock = value;
            }
            if self.exhausted {
                self.clock = 1e18;
            }
            let value = self.clock;
            self.deliver(&response);
            value
        }

        fn sleep(&mut self, seconds: f64) -> Result<(), PyErr> {
            let response = self.step("sleep", json!(["sleep", seconds]));
            self.clock += seconds;
            self.deliver(&response);
            Ok(())
        }

        fn getpid(&mut self) -> i32 {
            self.simple(
                "getpid",
                json!(["getpid"]),
                |v| v.as_i64().unwrap() as i32,
                |f| f.pid,
            )
            .unwrap()
        }

        fn getppid(&mut self) -> i32 {
            self.simple(
                "getppid",
                json!(["getppid"]),
                |v| v.as_i64().unwrap() as i32,
                |f| f.ppid,
            )
            .unwrap()
        }

        fn prctl(&mut self, option: i32, arg: i32) -> i32 {
            self.simple(
                "prctl",
                json!(["prctl", option, arg]),
                |v| v.as_i64().unwrap() as i32,
                |_| 0,
            )
            .unwrap()
        }

        fn get_errno(&mut self) -> i32 {
            self.simple(
                "get_errno",
                json!(["get_errno"]),
                |v| v.as_i64().unwrap() as i32,
                |_| 0,
            )
            .unwrap()
        }

        fn set_handler(&mut self, number: i32, handler: Handler) -> Saved {
            self.record(json!(["set_handler", number, handler_name(handler)]));
            self.handlers.insert(number, handler);
            self.saved += 1;
            let response = self.take("set_handler");
            self.advance(&response);
            self.deliver(&response);
            Saved(self.saved)
        }

        fn latest_signal(&self) -> i32 {
            self.latest
        }

        fn set_latest_signal(&mut self, number: i32) {
            self.latest = number;
        }

        fn first_signal(&self) -> i32 {
            self.first
        }

        fn clear_first_signal(&mut self) {
            self.first = 0;
        }

        fn spawn(&mut self, argv: &[Vec<u32>]) -> Result<i32, PyErr> {
            let argv: Vec<String> = argv.iter().map(|arg| string(arg)).collect();
            let response = self.step("spawn", json!(["spawn", argv]));
            if let Some((failure, filename)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(PyErr::os(
                    &failure,
                    filename.map(|name| Filename::Str(chars(&name))),
                ));
            }
            let pid = Self::ok(&response).and_then(Value::as_i64).unwrap_or(200) as i32;
            self.deliver(&response);
            Ok(pid)
        }

        fn waitpid_any(&mut self) -> OsResult<(i32, i32)> {
            self.simple(
                "waitpid_any",
                json!(["waitpid_any"]),
                |v| (v[0].as_i64().unwrap() as i32, v[1].as_i64().unwrap() as i32),
                |_| (0, 0),
            )
        }

        fn read_children(&mut self, pid: i32) -> Result<Vec<u32>, PyErr> {
            let text = format!("/proc/self/task/{pid}/children");
            let response = self.step("read_children", json!(["read_children", text]));
            if let Some(error) = Self::proc_failure(&response, &text) {
                self.deliver(&response);
                return Err(error);
            }
            let value = Self::ok(&response).map_or_else(Vec::new, |v| chars(v.as_str().unwrap()));
            self.deliver(&response);
            Ok(value)
        }

        fn read_stat(&mut self, pid: &str) -> Result<Vec<u8>, PyErr> {
            let text = format!("/proc/{pid}/stat");
            let after_pidfd = self
                .calls
                .last()
                .is_some_and(|call| call[0] == "pidfd_open");
            let response = self.step("read_stat", json!(["read_stat", text]));
            let response = response.or_else(|| {
                Some(json!({"err": {"errno": ENOENT, "text": "No such file or directory"}}))
            });
            if let Some(error) = Self::proc_failure(&response, &text) {
                self.deliver(&response);
                return Err(error);
            }
            let mut raw = unhex(Self::ok(&response).unwrap().as_str().unwrap());
            if self.mutation == Mutation::SkipStarttime
                && after_pidfd
                && let Some(before) = self.last_stat.get(&text)
            {
                raw = before.clone();
            }
            self.last_stat.insert(text, raw.clone());
            self.deliver(&response);
            Ok(raw)
        }

        fn pidfd_open(&mut self, pid: i32) -> OsResult<i32> {
            let fd = self.simple(
                "pidfd_open",
                json!(["pidfd_open", pid]),
                |v| v.as_i64().unwrap() as i32,
                Fake::fd,
            )?;
            self.pidfds.insert(fd, pid);
            Ok(fd)
        }

        fn pidfd_send_signal(&mut self, fd: i32, number: i32) -> OsResult<()> {
            let call = if self.mutation == Mutation::SendByPid {
                json!(["kill", self.pidfds.get(&fd).copied().unwrap_or(-1), number])
            } else {
                json!(["pidfd_send_signal", fd, number])
            };
            self.record(call);
            let response = self.take("pidfd_send_signal");
            self.advance(&response);
            if let Some((failure, _)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(failure);
            }
            self.deliver(&response);
            Ok(())
        }

        fn close(&mut self, fd: i32) -> OsResult<()> {
            self.simple("close", json!(["close", fd]), |_| (), |_| ())
        }

        fn spawn_monitor(&mut self, launch: &MonitorLaunch) -> Result<i32, PyErr> {
            let argv: Vec<String> = launch.argv.iter().map(|arg| utf8(arg)).collect();
            let env = launch.env.as_ref().map(|pairs| {
                pairs
                    .iter()
                    .map(|(key, value)| json!([utf8(key), utf8(value)]))
                    .collect::<Vec<_>>()
            });
            let call = json!(["spawn_monitor", {
                "argv": argv,
                "cwd": launch.cwd.as_ref().map(|cwd| utf8(cwd)),
                "env": env,
                "stdout": launch.stdout,
                "stderr": launch.stderr,
                "pass_fds": launch.pass_fds,
                "new_session": launch.new_session,
            }]);
            let response = self.step("spawn_monitor", call);
            if let Some((failure, filename)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(PyErr::os(
                    &failure,
                    filename.map(|name| Filename::Str(chars(&name))),
                ));
            }
            let pid = Self::ok(&response).and_then(Value::as_i64).unwrap_or(300) as i32;
            self.deliver(&response);
            Ok(pid)
        }

        fn poll_monitor(&mut self, pid: i32) -> Option<i32> {
            let response = self.step("poll_monitor", json!(["poll_monitor", pid]));
            let value = Self::ok(&response)
                .and_then(Value::as_i64)
                .map(|code| code as i32);
            self.deliver(&response);
            value
        }

        fn wait_monitor(&mut self, pid: i32, timeout: Option<f64>) -> Result<i32, WaitFailure> {
            let response = self.step("wait_monitor", json!(["wait_monitor", pid, timeout]));
            if response
                .as_ref()
                .and_then(|r| r.get("timeout"))
                .and_then(Value::as_bool)
                == Some(true)
            {
                self.deliver(&response);
                return Err(WaitFailure::Timeout);
            }
            if let Some((failure, _)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(WaitFailure::Os(failure));
            }
            let code = Self::ok(&response).and_then(Value::as_i64).unwrap_or(0) as i32;
            self.deliver(&response);
            Ok(code)
        }

        fn token_hex(&mut self) -> Result<String, PyErr> {
            let response = self.step("token_hex", json!(["token_hex"]));
            if let Some(raised) = response.as_ref().and_then(|r| r.get("raise")) {
                self.deliver(&response);
                let message = raised[1].as_str().unwrap().to_owned();
                return Err(match raised[0].as_str().unwrap() {
                    "NotImplementedError" => {
                        PyErr::new(Class::Runtime("NotImplementedError"), message)
                    }
                    "RuntimeError" => PyErr::runtime(message),
                    other => panic!("知らない例外: {other}"),
                });
            }
            if let Some((failure, _)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(PyErr::os(&failure, None));
            }
            let token = Self::ok(&response)
                .and_then(Value::as_str)
                .map_or_else(|| "0123456789abcdef".repeat(4), str::to_owned);
            self.deliver(&response);
            Ok(token)
        }

        fn keyboard_interrupt(&mut self) -> bool {
            std::mem::take(&mut self.keyboard)
        }

        fn pipe(&mut self) -> OsResult<(i32, i32)> {
            self.simple(
                "pipe",
                json!(["pipe"]),
                |v| (v[0].as_i64().unwrap() as i32, v[1].as_i64().unwrap() as i32),
                |_| (20, 21),
            )
        }

        fn select(&mut self, fd: i32, timeout: f64) -> Result<bool, SelectFailure> {
            self.record(json!(["select", fd, timeout]));
            if fd >= 1024 {
                return Err(SelectFailure::OutOfRange);
            }
            let response = self.take("select");
            self.advance(&response);
            if let Some((failure, _)) = Self::failure(&response) {
                self.deliver(&response);
                return Err(SelectFailure::Os(failure));
            }
            let ready = Self::ok(&response)
                .and_then(Value::as_bool)
                .unwrap_or(false);
            self.deliver(&response);
            Ok(ready)
        }

        fn read(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>> {
            self.simple(
                "read",
                json!(["read", fd, size]),
                |v| unhex(v.as_str().unwrap()),
                |_| Vec::new(),
            )
        }

        fn killpg(&mut self, pgid: i32, number: i32) -> OsResult<()> {
            if self.mutation == Mutation::ReapAnchorEarly && !self.reaped_early {
                self.reaped_early = true;
                self.record(json!(["poll_monitor", pgid]));
            }
            match self.simple("killpg", json!(["killpg", pgid, number]), |_| (), |_| ()) {
                Err(failure)
                    if failure.errno == EPERM
                        && number != 0
                        && self.mutation == Mutation::IgnoreEperm =>
                {
                    Ok(())
                }
                Err(failure)
                    if failure.errno == EPERM
                        && number == 0
                        && self.mutation == Mutation::EpermAsAbsent =>
                {
                    Err(OsFailure {
                        errno: ESRCH,
                        text: "No such process".to_owned(),
                    })
                }
                other => other,
            }
        }

        fn open(&mut self, path: &[u8], flags: OpenFlags) -> OsResult<i32> {
            let mode = match flags {
                OpenFlags::Create => json!(0o600),
                OpenFlags::Read => Value::Null,
            };
            let call = json!(["open", utf8(path), flags.names(), mode]);
            self.simple("open", call, |v| v.as_i64().unwrap() as i32, Fake::fd)
        }

        fn fstat(&mut self, fd: i32) -> OsResult<FileInfo> {
            self.simple(
                "fstat",
                json!(["fstat", fd]),
                |v| FileInfo {
                    regular: v["regular"].as_bool().unwrap(),
                    size: v["size"].as_i64().unwrap(),
                },
                |_| FileInfo {
                    regular: true,
                    size: 0,
                },
            )
        }

        fn write(&mut self, fd: i32, data: &[u8]) -> OsResult<usize> {
            let length = data.len();
            self.simple(
                "write",
                json!(["write", fd, hex(data)]),
                |v| v.as_u64().unwrap() as usize,
                |_| length,
            )
        }

        fn fsync(&mut self, fd: i32) -> OsResult<()> {
            self.simple("fsync", json!(["fsync", fd]), |_| (), |_| ())
        }

        fn platform(&mut self) -> Platform {
            if self.mutation == Mutation::FallbackToPosix && !self.has_pidfd {
                return Platform::Darwin;
            }
            self.platform
        }

        fn has_pidfd(&mut self) -> bool {
            self.has_pidfd
        }

        fn open_output(&mut self, path: &[u8]) -> OsResult<i32> {
            let fd = self.simple(
                "open_output",
                json!(["open_output", utf8(path)]),
                |v| v.as_i64().unwrap() as i32,
                Fake::fd,
            )?;
            self.output_fds.insert(fd);
            Ok(fd)
        }

        fn close_output(&mut self, fd: i32) -> OsResult<()> {
            self.simple("close_output", json!(["close_output", fd]), |_| (), |_| ())
        }

        fn fstat_output(&mut self, fd: i32) -> OsResult<i64> {
            assert!(
                self.output_fds.contains(&fd),
                "出力のファイルでない fd {fd}"
            );
            self.simple(
                "fstat_output",
                json!(["fstat_output", fd]),
                |v| v.as_i64().unwrap(),
                |_| 0,
            )
        }

        fn rewind_output(&mut self, fd: i32) -> OsResult<()> {
            self.simple(
                "rewind_output",
                json!(["rewind_output", fd]),
                |_| (),
                |_| (),
            )
        }

        fn read_output(&mut self, fd: i32, size: usize) -> OsResult<Vec<u8>> {
            self.simple(
                "read_output",
                json!(["read_output", fd, size]),
                |v| unhex(v.as_str().unwrap()),
                |_| Vec::new(),
            )
        }
    }

    fn main_result(result: MainResult) -> Value {
        match result {
            MainResult::Exit(code) => json!({"exit": code, "stderr": ""}),
            MainResult::KeyboardInterrupt => json!({"keyboard_interrupt": true, "stderr": ""}),
            MainResult::Message { text, code } => {
                json!({"exit": code, "stderr": format!("{}\n", string(&text))})
            }
            MainResult::Uncaught {
                site,
                type_name,
                message,
            } => {
                // 旧版の main から下の呼出履歴(`<module>` の行は mock の中では出ない)。
                let frames: Vec<Value> = site.frames()[1..]
                    .iter()
                    .map(|frame| json!([frame.file, frame.line, frame.name, frame.source]))
                    .collect();
                json!({"uncaught": [type_name, message], "frames": frames, "stderr": ""})
            }
            other => panic!("差し替えのケースで argparse の結果は出ない: {other:?}"),
        }
    }

    fn form(value: &PyValue) -> Value {
        match value {
            PyValue::None => Value::Null,
            PyValue::Bool(flag) => json!(flag),
            PyValue::Int(digits) => json!(digits.parse::<i64>().unwrap()),
            PyValue::Str(codes) => json!(string(codes)),
            PyValue::Bytes(raw) => json!({"bytes": hex(raw)}),
            other => panic!("結果に知らない値: {other:?}"),
        }
    }

    fn number(value: &Value) -> PyNumber {
        match (value.get("int"), value.get("float")) {
            (Some(digits), _) => PyNumber::Int(digits.as_str().unwrap().to_owned()),
            (_, Some(text)) => PyNumber::Float(text.as_str().unwrap().parse().unwrap()),
            _ => panic!("数でない: {value}"),
        }
    }

    fn args(given: &Value) -> Vec<Vec<u32>> {
        given["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|arg| chars(arg.as_str().unwrap()))
            .collect()
    }

    fn os(text: &str) -> OsString {
        OsString::from_vec(text.as_bytes().to_vec())
    }

    fn run_rc(fake: &mut Fake, given: &Value) -> Value {
        let argv: Vec<OsString> = given["argv"]
            .as_array()
            .unwrap()
            .iter()
            .map(|arg| os(arg.as_str().unwrap()))
            .collect();
        let cwd = given["cwd"].as_str().map(PathBuf::from);
        let env: Option<Vec<(OsString, OsString)>> = given["env"].as_array().map(|pairs| {
            pairs
                .iter()
                .map(|pair| (os(pair[0].as_str().unwrap()), os(pair[1].as_str().unwrap())))
                .collect()
        });
        let directory = PathBuf::from(given["directory"].as_str().unwrap());
        let method = match given["method"].as_str() {
            Some("linux") => Some(Method::Linux),
            Some("posix") => Some(Method::Posix),
            None => None,
            Some(other) => panic!("知らない方式: {other}"),
        };
        let monitor = Monitor {
            program: os("python3"),
            prefix: vec![os("-B"), os("check-process.py")],
        };
        let request = RunRequest {
            argv: &argv,
            cwd: cwd.as_deref(),
            env: env.as_deref(),
            timeout: number(&given["timeout"]),
            directory: &directory,
            grace: number(&given["grace"]),
            method,
            keep: given["keep"].as_u64().unwrap() as usize,
            monitor: &monitor,
            int_max_str_digits: given["int_max_str_digits"].as_u64().unwrap() as usize,
            caller_frames: given["caller_frames"].as_u64().unwrap() as usize,
            recursion_limit: PYTHON_RECURSION_LIMIT,
        };
        let previous = enter_stop(fake);
        let outcome = run_with(fake, &Request::from_request(&request));
        exit_stop(fake, &previous);
        match outcome {
            Ok(pairs) => {
                json!({"result": pairs.iter().map(|(key, value)| json!([form(key), form(value)])).collect::<Vec<_>>()})
            }
            Err(RunError::Recovery(message)) => json!({"error": ["RecoveryError", message]}),
            Err(RunError::Interrupted(message)) => json!({"error": ["Interrupted", message]}),
            Err(RunError::Unwrapped { kind, message }) => json!({"error": [kind, message]}),
        }
    }

    /// 1 つのケースを流し、呼出しの並びと結果と使わなかった応答を返す。
    fn run_case(case: &Value, mutation: Mutation) -> (Vec<Value>, Value, Vec<String>) {
        let mut fake = Fake::new(&case["world"], mutation);
        let given = &case["input"];
        let result = match case["kind"].as_str().unwrap() {
            "ls" => {
                let context = Context {
                    width: 78,
                    description: Some(legacy::LOOP_SUPERVISOR_DOC),
                    max_str_digits: 4300,
                };
                main_result(ls_cli::main_logic(&mut fake, &args(given), &context))
            }
            "cp" => {
                let context = Context {
                    width: 78,
                    description: Some(legacy::CHECK_PROCESS_DOC),
                    max_str_digits: 4300,
                };
                match cp_cli::main_logic(&mut fake, &args(given), &context) {
                    CheckProcessResult::Done(result) => main_result(result),
                    CheckProcessResult::Anchor(_) => panic!("差し替えのケースに anchor は無い"),
                }
            }
            "rc" => run_rc(&mut fake, given),
            other => panic!("知らない種類: {other}"),
        };
        let unused = fake.unused();
        (fake.calls, result, unused)
    }

    fn fixture() -> Value {
        serde_json::from_str(INJECTED).expect("差し替えの固定期待の JSON を読めない")
    }

    /// 固定期待と食い違うケースの ID と、最初に違った所。
    fn mismatches(mutation: Mutation) -> Vec<(String, String)> {
        let fixture = fixture();
        let mut found = Vec::new();
        for case in fixture["cases"].as_array().unwrap() {
            let id = case["id"].as_str().unwrap().to_owned();
            let (calls, result, unused) = run_case(case, mutation);
            let expected = &case["expect"];
            let expected_calls = expected["calls"].as_array().unwrap();
            let detail = if let Some(index) = (0..calls.len().max(expected_calls.len()))
                .find(|&index| calls.get(index) != expected_calls.get(index))
            {
                format!(
                    "呼出し {index}: 期待 {} / 実際 {}",
                    expected_calls
                        .get(index)
                        .map_or("なし".to_owned(), Value::to_string),
                    calls.get(index).map_or("なし".to_owned(), Value::to_string)
                )
            } else if result != expected["result"] {
                format!("結果: 期待 {} / 実際 {result}", expected["result"])
            } else if !unused.is_empty() {
                format!("使わなかった応答: {unused:?}")
            } else if calls.len() >= LIMIT {
                format!("呼出しが上限 {LIMIT} に達した")
            } else {
                continue;
            };
            found.push((id, detail));
        }
        found
    }

    /// json.loads の入れ子の上限は、呼出元のフレームの数で決まる(既定は検査用の親の 2)。固定期待を作った
    /// 親は 4(`<module>`・`main`・`build`・`run_rc`)で、上限ちょうどと 1 つ深い記録・通知のケースがある。
    #[test]
    fn json_nesting_room_follows_caller_frames() {
        assert_eq!(read_result_room(PYTHON_RECURSION_LIMIT, CALLER_FRAMES), 991);
        assert_eq!(notice_room(PYTHON_RECURSION_LIMIT, CALLER_FRAMES), 992);
        let fixture = fixture();
        let mut nested = 0;
        for case in fixture["cases"].as_array().unwrap() {
            if case["kind"] != "rc" {
                continue;
            }
            let frames = case["input"]["caller_frames"].as_u64().unwrap() as usize;
            assert_eq!(frames, 4, "{}", case["id"]);
            let id = case["id"].as_str().unwrap();
            if id.contains("nested") {
                nested += 1;
                let room = if id.contains("record") {
                    read_result_room(PYTHON_RECURSION_LIMIT, frames)
                } else {
                    notice_room(PYTHON_RECURSION_LIMIT, frames)
                };
                let reads = case["world"]["responses"]["read"].as_array().unwrap();
                let raw = unhex(reads[0]["ok"].as_str().unwrap());
                let depth = raw.iter().filter(|&&byte| byte == b'[').count();
                assert_eq!(depth, room + usize::from(id.ends_with("over-room")), "{id}");
            }
        }
        assert_eq!(nested, 4);
    }

    #[test]
    fn fixture_has_fixed_count_ids_and_current_legacy() {
        let fixture = fixture();
        let cases = fixture["cases"].as_array().unwrap();
        assert_eq!(cases.len(), COUNT);
        assert_eq!(fixture["count"].as_u64(), Some(COUNT as u64));
        let ids: HashSet<&str> = cases
            .iter()
            .map(|case| case["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids.len(), COUNT, "ID が重なる");
        for id in FIXED {
            assert!(ids.contains(id), "{id} が無い");
        }
        assert!(ids.iter().all(|id| id.starts_with("inj-")));
        let units = super::units();
        assert_eq!(
            fixture["python"]["legacy_sha256"],
            units["python"]["legacy_sha256"]
        );
        assert_eq!(fixture["python"]["version"], "3.10.11");
    }

    #[test]
    fn calls_and_results_match_mocked_legacy() {
        let found = mismatches(Mutation::None);
        assert!(
            found.is_empty(),
            "{} 件が食い違う:\n{}",
            found.len(),
            found
                .iter()
                .map(|(id, detail)| format!("{id}: {detail}"))
                .collect::<Vec<_>>()
                .join("\n")
        );
    }

    /// わざと誤った振る舞いを当てると、決まったケースが必ず食い違う(否定の検査)。
    #[test]
    fn broken_behaviours_always_mismatch() {
        let expected: [(Mutation, &[&str]); 6] = [
            (
                Mutation::IgnoreEperm,
                &[
                    "inj-rc-posix-term-eperm",
                    "inj-rc-posix-kill-eperm",
                    "inj-posix-anchor-killed-after-notice-macos",
                ],
            ),
            (
                Mutation::EpermAsAbsent,
                &[
                    "inj-rc-posix-eperm-continues",
                    "inj-rc-posix-eperm-then-esrch",
                ],
            ),
            (
                Mutation::SendByPid,
                &[
                    "inj-ls-timeout-term-resend",
                    "inj-ls-grace-zero-kill-first",
                    "inj-rc-linux-stop-forwarded",
                    "inj-ls-check-ok",
                ],
            ),
            (
                Mutation::SkipStarttime,
                &[
                    "inj-ls-starttime-changed",
                    "inj-ls-starttime-changed-close-ebadf",
                ],
            ),
            (
                Mutation::ReapAnchorEarly,
                &[
                    "inj-rc-posix-exit3",
                    "inj-posix-anchor-killed-after-notice-linux",
                    "inj-posix-anchor-killed-after-notice-macos",
                ],
            ),
            (
                Mutation::FallbackToPosix,
                &["inj-rc-method-default-linux-without-pidfd"],
            ),
        ];
        for (mutation, ids) in expected {
            let found: HashSet<String> =
                mismatches(mutation).into_iter().map(|(id, _)| id).collect();
            for id in ids {
                assert!(found.contains(*id), "{mutation:?} で {id} が食い違わない");
            }
        }
    }

    /// 固定期待の応答を 1 つ壊すと、照らし合わせが食い違う(固定期待を読んでいることの確かめ)。
    #[test]
    fn altered_expectation_is_detected() {
        let fixture = fixture();
        let case = fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["id"] == "inj-ls-exit3")
            .unwrap();
        let (calls, result, _) = run_case(case, Mutation::None);
        assert_eq!(&calls, case["expect"]["calls"].as_array().unwrap());
        let mut changed = case["world"].clone();
        changed["responses"]["waitpid_any"][0]["ok"][1] = json!(4 << 8);
        let altered = json!({"kind": case["kind"], "input": case["input"], "world": changed});
        let (_, other, _) = run_case(&altered, Mutation::None);
        assert_eq!(result, case["expect"]["result"]);
        assert_eq!(
            other, result,
            "記録の returncode は結果の欄ではなく write の呼出しに出る"
        );
        let (other_calls, _, _) = run_case(&altered, Mutation::None);
        assert_ne!(&other_calls, case["expect"]["calls"].as_array().unwrap());
    }
}
