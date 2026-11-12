//! 旧Pythonの`str`。コードポイントの並びで持ち、UTF-8で読めないバイトはU+DC80〜U+DCFFに置く。

/// 旧Pythonの`str`(コードポイントの並び)。
pub type PyText = Vec<u32>;

/// `os.fsdecode`(UTF-8と`surrogateescape`)。
pub fn decode(bytes: &[u8]) -> PyText {
    super::bytes::characters(bytes)
}

/// Rustの文字列から作る。
pub fn from_str(text: &str) -> PyText {
    text.chars().map(u32::from).collect()
}

/// `surrogateescape`で書けない文字の範囲(旧Pythonの`UnicodeEncodeError`の`start`と`end`)。
///
/// 旧PythonのUTF-8の符号化は、surrogateの続く並びを1つの範囲として誤りの処理へ渡す。
/// `surrogateescape`は並びの先頭から戻せる文字(U+DC80〜U+DCFF)を書き、最初の戻せない文字`start`から
/// 並びの終わり`end`までを例外の範囲にする。`code`は`start`の文字。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EncodeError {
    pub start: usize,
    pub end: usize,
    pub code: u32,
}

impl EncodeError {
    /// `str(UnicodeEncodeError)`(`'utf-8' codec can't encode ...: surrogates not allowed`)。
    pub fn message(&self) -> String {
        if self.end - self.start == 1 {
            format!(
                "'utf-8' codec can't encode character '\\u{:04x}' in position {}: surrogates not allowed",
                self.code, self.start
            )
        } else {
            format!(
                "'utf-8' codec can't encode characters in position {}-{}: surrogates not allowed",
                self.start,
                self.end - 1
            )
        }
    }
}

fn push_char(out: &mut Vec<u8>, code: u32) {
    let mut buffer = [0; 4];
    out.extend_from_slice(
        char::from_u32(code)
            .expect("surrogate handled by caller")
            .encode_utf8(&mut buffer)
            .as_bytes(),
    );
}

/// `str.encode('utf-8', 'surrogateescape')`。U+DC80〜U+DCFFは元のバイトへ戻し、ほかのsurrogateは失敗。
pub fn encode_surrogateescape(text: &[u32]) -> Result<Vec<u8>, EncodeError> {
    let mut out = Vec::with_capacity(text.len());
    for (index, &code) in text.iter().enumerate() {
        match code {
            0xdc80..=0xdcff => out.push((code - 0xdc00) as u8),
            0xd800..=0xdfff => {
                let end = index
                    + text[index..]
                        .iter()
                        .take_while(|code| (0xd800..=0xdfff).contains(*code))
                        .count();
                return Err(EncodeError {
                    start: index,
                    end,
                    code,
                });
            }
            _ => push_char(&mut out, code),
        }
    }
    Ok(out)
}

/// `str.encode('utf-8', 'backslashreplace')`。surrogateだけを`\udcxx`の形で書く(標準エラー)。
pub fn encode_backslashreplace(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    for &code in text {
        if (0xd800..=0xdfff).contains(&code) {
            out.extend_from_slice(format!("\\u{code:04x}").as_bytes());
        } else {
            push_char(&mut out, code);
        }
    }
    out
}

/// 旧Pythonの`repr(str)`。引用符は内容から選ぶ。印字できない文字はUnicode 13.0で決める。
pub fn repr(text: &[u32]) -> PyText {
    let single = u32::from(b'\'');
    let double = u32::from(b'"');
    let quote = if text.contains(&single) && !text.contains(&double) {
        double
    } else {
        single
    };
    let mut out = vec![quote];
    for &code in text {
        match code {
            10 => out.extend(from_str("\\n")),
            13 => out.extend(from_str("\\r")),
            9 => out.extend(from_str("\\t")),
            92 => out.extend(from_str("\\\\")),
            _ if code == quote => {
                out.push(92);
                out.push(quote);
            }
            _ if !super::unicode::printable(code) => {
                let escaped = if code <= 0xff {
                    format!("\\x{code:02x}")
                } else if code <= 0xffff {
                    format!("\\u{code:04x}")
                } else {
                    format!("\\U{code:08x}")
                };
                out.extend(from_str(&escaped));
            }
            _ => out.push(code),
        }
    }
    out.push(quote);
    out
}

/// 旧Pythonの`str.isspace()`(Unicode 13.0)。
pub fn is_space(code: u32) -> bool {
    matches!(
        code,
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

/// 旧Pythonの10進の数字の値(`unicodedata.decimal`。Unicode 13.0)。
pub fn decimal(code: u32) -> Option<u8> {
    super::unicode::decimal(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surrogateescape_round_trips_and_rejects_other_surrogates() {
        let text = decode(b"a\xff\xc3\xa9");
        assert_eq!(text, [0x61, 0xdcff, 0xe9]);
        assert_eq!(encode_surrogateescape(&text).unwrap(), b"a\xff\xc3\xa9");
        assert_eq!(
            encode_surrogateescape(&[0x61, 0xd800]),
            Err(EncodeError {
                start: 1,
                end: 2,
                code: 0xd800
            })
        );
        assert_eq!(encode_backslashreplace(&text), b"a\\udcff\xc3\xa9");
    }

    /// 書けないsurrogateの範囲と文を、本物のPythonの`str.encode('utf-8', 'surrogateescape')`と比べる。
    #[test]
    fn encode_error_range_and_message_match_python() {
        let cases: [&[u32]; 7] = [
            &[0x61, 0xd800],
            &[0xd800, 0xdfff, 0x61],
            &[0xdc80, 0xd800, 0xdcff, 0x62],
            &[0x61, 0xdc80, 0xdc81, 0xdbff, 0xdc00],
            &[0x62, 0xdcff, 0x63, 0xd801, 0xd802, 0xd803],
            &[0xdfff],
            &[0x41, 0xdc7f, 0xdc80],
        ];
        for text in cases {
            let literal: String = text.iter().map(|code| format!("\\U{code:08x}")).collect();
            let script = format!(
                "import sys\ntry:\n    '{literal}'.encode('utf-8', 'surrogateescape')\nexcept UnicodeEncodeError as e:\n    sys.stdout.write('%d %d %s' % (e.start, e.end, e))"
            );
            let output = std::process::Command::new("python3")
                .args(["-I", "-c", &script])
                .output()
                .unwrap();
            let error = encode_surrogateescape(text).unwrap_err();
            assert_eq!(
                String::from_utf8(output.stdout).unwrap(),
                format!("{} {} {}", error.start, error.end, error.message()),
                "{text:x?}"
            );
        }
    }

    #[test]
    fn repr_matches_python_quotes_and_escapes() {
        let cases: [(&[u32], &str); 5] = [
            (&from_str("a'b"), "\"a'b\""),
            (&from_str("a'b\""), "'a\\'b\"'"),
            (&[0x61, 0xdcff, 0x0a, 0x7f, 0xe9], "'a\\udcff\\n\\x7fé'"),
            (&[0x200b, 0x1f600], "'\\u200b😀'"),
            (&[0xe0001], "'\\U000e0001'"),
        ];
        for (text, expected) in cases {
            assert_eq!(repr(text), from_str(expected));
        }
    }

    #[test]
    fn whitespace_and_decimals_follow_unicode_13() {
        for code in [0x20, 0x09, 0x1c, 0x85, 0xa0, 0x3000, 0x2028] {
            assert!(is_space(code), "{code:x}");
        }
        for code in [0x200b, 0xfeff, 0x61, 0x180e] {
            assert!(!is_space(code), "{code:x}");
        }
        assert_eq!(decimal(0x0663), Some(3));
        assert_eq!(decimal(0xff15), Some(5));
        assert_eq!(decimal(0x00b2), None);
    }
}
