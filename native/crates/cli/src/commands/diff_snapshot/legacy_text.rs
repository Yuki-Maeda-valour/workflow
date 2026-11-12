//! The old helper's Python startup and diagnostics. Encoding settings are fixed
//! to UTF-8 with surrogateescape (2026-11-11 22:24): PYTHONIOENCODING,
//! PYTHONUTF8, PYTHONCOERCECLOCALE and the locale are not read. Standard error
//! keeps Python's default backslashreplace for unpaired surrogates.
use super::{Failure, Result};
use dev_workflow_core::loop_state::CodepointString;
fn environment(key: &str) -> Option<Vec<u8>> {
    std::env::var_os(key).map(|v| super::bytes::raw(&v).to_vec())
}
/// Python reads its own variables only when they are set and not empty.
fn setting(key: &str) -> Option<Vec<u8>> {
    environment(key).filter(|v| !v.is_empty())
}
fn c_integer(raw: &[u8]) -> Option<i64> {
    // C strtol skips all six C-locale spaces, including vertical tab.
    let raw = &raw[raw.iter().take_while(|b| matches!(b, 9..=13 | 32)).count()..];
    if raw.is_empty() {
        return None;
    }
    let digits = raw
        .strip_prefix(b"+")
        .or_else(|| raw.strip_prefix(b"-"))
        .unwrap_or(raw);
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(raw)
        .ok()?
        .parse::<i64>()
        .ok()
        .filter(|n| i32::try_from(*n).is_ok())
}
pub fn integer_limit() -> i64 {
    setting("PYTHONINTMAXSTRDIGITS")
        .as_deref()
        .and_then(c_integer)
        .unwrap_or(4300)
}
/// CPython's `config_init_hash_seed`: "random" or C `strtoul` reaching the end
/// with a value up to 4294967295 (a negative value wraps above the range).
fn hash_seed_valid(raw: &[u8]) -> bool {
    if raw == b"random" {
        return true;
    }
    let rest = &raw[raw.iter().take_while(|b| matches!(b, 9..=13 | 32)).count()..];
    let (negative, digits) = match rest.first() {
        Some(b'+') => (false, &rest[1..]),
        Some(b'-') => (true, &rest[1..]),
        _ => (false, rest),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return false;
    }
    let significant = &digits[digits.iter().take_while(|b| **b == b'0').count()..];
    if significant.is_empty() {
        return true;
    }
    if negative || significant.len() > 10 {
        return false;
    }
    std::str::from_utf8(significant)
        .ok()
        .and_then(|t| t.parse::<u64>().ok())
        .is_some_and(|n| n <= 4_294_967_295)
}
/// Python's decoding of argv and environment bytes in UTF-8 mode.
pub(super) fn decode_argument(mut raw: &[u8]) -> CodepointString {
    let mut points = vec![];
    while !raw.is_empty() {
        match std::str::from_utf8(raw) {
            Ok(text) => {
                points.extend(text.chars().map(u32::from));
                break;
            }
            Err(error) => {
                let valid = error.valid_up_to();
                points.extend(
                    std::str::from_utf8(&raw[..valid])
                        .unwrap()
                        .chars()
                        .map(u32::from),
                );
                let count = error.error_len().unwrap_or(raw.len() - valid);
                points.extend(
                    raw[valid..valid + count]
                        .iter()
                        .map(|b| 0xdc00 + u32::from(*b)),
                );
                raw = &raw[valid + count..];
            }
        }
    }
    CodepointString::try_from_codepoints(points).expect("environment bytes decode to code points")
}
/// UTF-8 with Python's standard-error `backslashreplace` for surrogates.
pub(super) fn write_text(parts: &[CodepointString]) -> Vec<u8> {
    let mut output = Vec::new();
    for part in parts {
        for &cp in part.as_codepoints() {
            match char::from_u32(cp) {
                Some(c) => {
                    let mut buffer = [0; 4];
                    output.extend(c.encode_utf8(&mut buffer).as_bytes());
                }
                None => output.extend(format!("\\u{cp:04x}").as_bytes()),
            }
        }
    }
    output
}
pub fn diagnostic(text: &str) -> Vec<u8> {
    text.as_bytes().to_vec()
}
pub(super) fn fatal(prefix: &str, detail: &str, thread: bool) -> Failure {
    let mut message = format!("Fatal Python error: {prefix}\n{detail}\n");
    if thread {
        message.push_str(&format!(
            "Current thread 0x{} (most recent call first):\n  <no Python frame>\n",
            dev_workflow_platform::diff_snapshot::signals::legacy_thread_identifier()
        ));
    }
    message.push_str("ERROR [internal] secret profile の和集合を作れない\n");
    Failure::raw(20, message.into_bytes())
}
pub fn startup() -> Result<()> {
    if let Some(value) = setting("PYTHONHASHSEED")
        && !hash_seed_valid(&value)
    {
        return Err(fatal(
            "config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]",
            "Python runtime state: preinitialized\n",
            false,
        ));
    }
    if let Some(value) = setting("PYTHONINTMAXSTRDIGITS")
        && !c_integer(&value).is_some_and(|n| n == 0 || n >= 640)
    {
        return Err(fatal(
            "config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.",
            "Python runtime state: preinitialized\n",
            false,
        ));
    }
    if dev_workflow_platform::diff_snapshot::fs::standard_is_directory(0) {
        return Err(fatal(
            "init_sys_streams: <stdin> is a directory, cannot continue",
            "Python runtime state: core initialized\n",
            true,
        ));
    }
    Ok(())
}
pub fn unicode_failure(message: &str) -> Failure {
    // Only these three source-location fields differ from the legacy helper, as
    // approved on 2026-11-11. The stack, old lines, source text, and exception remain.
    let line =
        r#"sys.stdout.buffer.write(b"".join(item.encode("utf-8") + b"\0" for item in unique))"#;
    let trace = [
        "Traceback (most recent call last):\n",
        "  File \"secret-profiles.py\", line 287, in <module>\n",
        "    sys.exit(main())\n",
        "  File \"secret-profiles.py\", line 279, in main\n",
        "    ",
        line,
        "\n",
        "  File \"secret-profiles.py\", line 279, in <genexpr>\n",
        "    ",
        line,
        "\n",
        "UnicodeEncodeError: ",
        message,
        "\n",
    ]
    .concat();
    Failure::raw(
        20,
        [
            trace.into_bytes(),
            "ERROR [internal] secret profile の和集合を作れない\n"
                .as_bytes()
                .to_vec(),
        ]
        .concat(),
    )
}
#[cfg(test)]
mod tests {
    #[test]
    fn hash_seed_follows_strtoul() {
        for (value, valid) in [
            ("random", true),
            ("0", true),
            ("4294967295", true),
            ("4294967296", false),
            ("-0", true),
            ("-1", false),
            ("+7", true),
            (" 5", true),
            ("\t5", true),
            ("5 ", false),
            ("0x10", false),
            ("+", false),
            ("00000000004294967295", true),
        ] {
            assert_eq!(super::hash_seed_valid(value.as_bytes()), valid, "{value:?}");
        }
    }
}
