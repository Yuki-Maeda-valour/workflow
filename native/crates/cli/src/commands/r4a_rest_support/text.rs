//! R4A の 5 本で共有する本文の前処理。フェンスと状態名の定義は既存の
//! `task_digest` / `resolve_task_dir` のものを使い、ここには複製しない。

use super::chars::{self, PyStr};

/// BOM を 1 個だけ除き、CRLF と CR を LF に揃えて `\n` で分ける(`str.split("\n")` と同じ)。
pub fn script_lines(text: &[u32]) -> Vec<PyStr> {
    let text = if text.first() == Some(&0xfeff) {
        &text[1..]
    } else {
        text
    };
    let mut unified: PyStr = Vec::with_capacity(text.len());
    let mut at = 0;
    while at < text.len() {
        if text[at] == 13 {
            unified.push(10);
            if text.get(at + 1) == Some(&10) {
                at += 1;
            }
        } else {
            unified.push(text[at]);
        }
        at += 1;
    }
    unified.split(|&c| c == 10).map(<[u32]>::to_vec).collect()
}

/// `line.rstrip(" \t")`。
pub fn rstrip_space_tab(line: &[u32]) -> PyStr {
    let mut end = line.len();
    while end > 0 && (line[end - 1] == 32 || line[end - 1] == 9) {
        end -= 1;
    }
    line[..end].to_vec()
}

pub fn starts_with(line: &[u32], prefix: &str) -> bool {
    let mut it = line.iter();
    prefix.chars().all(|c| it.next() == Some(&u32::from(c)))
}

pub fn eq_str(line: &[u32], text: &str) -> bool {
    line.len() == text.chars().count() && starts_with(line, text)
}

/// 行を `&str` へ(本文は厳密 UTF-8 か置換で復号済みなので孤立 surrogate を含まない)。
pub fn to_string(line: &[u32]) -> String {
    line.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

/// UTF-8 の厳密な復号の失敗(`UnicodeDecodeError` の表示に使う)。
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DecodeError {
    pub start: usize,
    pub end: usize,
    pub byte: u8,
    pub reason: &'static str,
}

impl DecodeError {
    /// `str(UnicodeDecodeError)`。
    pub fn message(&self) -> String {
        if self.end - self.start == 1 {
            format!(
                "'utf-8' codec can't decode byte 0x{:02x} in position {}: {}",
                self.byte, self.start, self.reason
            )
        } else {
            format!(
                "'utf-8' codec can't decode bytes in position {}-{}: {}",
                self.start,
                self.end - 1,
                self.reason
            )
        }
    }
}

/// `bytes.decode("utf-8")`(strict)。
pub fn decode_strict(bytes: &[u8]) -> Result<PyStr, DecodeError> {
    if let Some(text) = chars::from_utf8_strict(bytes) {
        return Ok(text);
    }
    let mut position = 0;
    let mut chunks = bytes.utf8_chunks().peekable();
    while let Some(chunk) = chunks.next() {
        position += chunk.valid().len();
        let invalid = chunk.invalid();
        if invalid.is_empty() {
            continue;
        }
        let first = invalid[0];
        let last_chunk = chunks.peek().is_none();
        let (end, reason) = if invalid.len() == 1 && !matches!(first, 0xc2..=0xf4) {
            (position + 1, "invalid start byte")
        } else if last_chunk && position + invalid.len() == bytes.len() {
            (bytes.len(), "unexpected end of data")
        } else {
            (position + invalid.len(), "invalid continuation byte")
        };
        return Err(DecodeError {
            start: position,
            end,
            byte: first,
            reason,
        });
    }
    unreachable!("a non-UTF-8 input has an invalid chunk")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_are_unified_and_only_one_bom_is_removed() {
        let text = chars::from_str("\u{feff}\u{feff}a\r\nb\rc\n");
        let lines = script_lines(&text);
        assert_eq!(lines.len(), 4);
        assert_eq!(lines[0], chars::from_str("\u{feff}a"));
        assert_eq!(lines[3], Vec::<u32>::new());
    }

    #[test]
    fn decode_errors_use_python_messages() {
        let message = |bytes: &[u8]| decode_strict(bytes).unwrap_err().message();
        assert_eq!(
            message(b"ab\xff"),
            "'utf-8' codec can't decode byte 0xff in position 2: invalid start byte"
        );
        assert_eq!(
            message(b"\xe2\x82"),
            "'utf-8' codec can't decode bytes in position 0-1: unexpected end of data"
        );
        assert_eq!(
            message(b"\xe2\x82A"),
            "'utf-8' codec can't decode bytes in position 0-1: invalid continuation byte"
        );
        assert_eq!(
            message(b"\xe2A"),
            "'utf-8' codec can't decode byte 0xe2 in position 0: invalid continuation byte"
        );
        assert_eq!(
            message(b"\xe2"),
            "'utf-8' codec can't decode byte 0xe2 in position 0: unexpected end of data"
        );
        assert_eq!(
            message(b"\xed\xa0\x80"),
            "'utf-8' codec can't decode byte 0xed in position 0: invalid continuation byte"
        );
        assert_eq!(
            message(b"\xc0\x80"),
            "'utf-8' codec can't decode byte 0xc0 in position 0: invalid start byte"
        );
    }
}
