//! `json.loads(bytes)` の文字コードの判定(`json.detect_encoding`)と、`bytes.decode(<判定>, 'surrogatepass')`。
//! 本家 3.10.11 の `Objects/stringlib/codecs.h`(`utf8_decode`・`utf16_decode`)・`Objects/unicodeobject.c`
//! (`PyUnicode_DecodeUTF16Stateful`・`PyUnicode_DecodeUTF32Stateful`)・`Python/codecs.c`
//! (`PyCodec_SurrogatePassErrors`)・`Lib/encodings/utf_8_sig.py` の写し。

/// `UnicodeDecodeError`。`object` は復号に渡したバイト列(utf-8-sig は BOM を除いた残り)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecodeError {
    pub encoding: &'static str,
    pub object: Vec<u8>,
    pub start: usize,
    pub end: usize,
    pub reason: &'static str,
}

impl DecodeError {
    /// `str(exc)`。
    pub fn message(&self) -> String {
        if self.start < self.object.len() && self.end == self.start + 1 {
            format!(
                "'{}' codec can't decode byte 0x{:02x} in position {}: {}",
                self.encoding, self.object[self.start], self.start, self.reason
            )
        } else {
            format!(
                "'{}' codec can't decode bytes in position {}-{}: {}",
                self.encoding,
                self.start,
                self.end.wrapping_sub(1),
                self.reason
            )
        }
    }
}

/// `json.detect_encoding(b)`。
pub fn detect_encoding(raw: &[u8]) -> &'static str {
    if raw.starts_with(&[0x00, 0x00, 0xfe, 0xff]) || raw.starts_with(&[0xff, 0xfe, 0x00, 0x00]) {
        return "utf-32";
    }
    if raw.starts_with(&[0xfe, 0xff]) || raw.starts_with(&[0xff, 0xfe]) {
        return "utf-16";
    }
    if raw.starts_with(&[0xef, 0xbb, 0xbf]) {
        return "utf-8-sig";
    }
    if raw.len() >= 4 {
        if raw[0] == 0 {
            return if raw[1] != 0 {
                "utf-16-be"
            } else {
                "utf-32-be"
            };
        }
        if raw[1] == 0 {
            return if raw[2] != 0 || raw[3] != 0 {
                "utf-16-le"
            } else {
                "utf-32-le"
            };
        }
    } else if raw.len() == 2 {
        if raw[0] == 0 {
            return "utf-16-be";
        }
        if raw[1] == 0 {
            return "utf-16-le";
        }
    }
    "utf-8"
}

fn is_surrogate(code: u32) -> bool {
    (0xd800..=0xdfff).contains(&code)
}

fn continuation(byte: u8) -> bool {
    (0x80..0xc0).contains(&byte)
}

/// `raw.decode(encoding, 'surrogatepass')`(`detect_encoding` が返す名前だけ)。
pub fn decode_surrogatepass(raw: &[u8], encoding: &str) -> Result<Vec<u32>, DecodeError> {
    match encoding {
        "utf-8" => utf8(raw, true),
        "utf-8-sig" => utf8(raw.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(raw), true),
        "utf-16" => utf16(raw, None),
        "utf-16-le" => utf16(raw, Some(false)),
        "utf-16-be" => utf16(raw, Some(true)),
        "utf-32" => utf32(raw, None),
        "utf-32-le" => utf32(raw, Some(false)),
        "utf-32-be" => utf32(raw, Some(true)),
        other => panic!("扱わない文字コード: {other}"),
    }
}

/// `raw.decode('utf-8')`(strict。旧版の `Path.read_text()` の復号)。
pub fn decode_utf8_strict(raw: &[u8]) -> Result<Vec<u32>, DecodeError> {
    utf8(raw, false)
}

/// UTF-8(`surrogatepass` が真なら surrogatepass、偽なら strict)。
fn utf8(raw: &[u8], surrogatepass: bool) -> Result<Vec<u32>, DecodeError> {
    let n = raw.len();
    let mut out = Vec::with_capacity(n);
    let mut pos = 0;
    while pos < n {
        let b0 = raw[pos];
        if b0 < 0x80 {
            out.push(u32::from(b0));
            pos += 1;
            continue;
        }
        let at = |offset: usize| raw[pos + offset];
        let left = n - pos;
        // (理由, 終わり)。理由が None なら、長さ `length` の文字を読めた。
        let (failure, length): (Option<(&'static str, usize)>, usize) = if b0 < 0xc2 {
            (Some(("invalid start byte", pos + 1)), 0)
        } else if b0 < 0xe0 {
            if left < 2 {
                (Some(("unexpected end of data", n)), 0)
            } else if !continuation(at(1)) {
                (Some(("invalid continuation byte", pos + 1)), 0)
            } else {
                (None, 2)
            }
        } else if b0 < 0xf0 {
            if left < 3 {
                if left < 2 {
                    (Some(("unexpected end of data", n)), 0)
                } else {
                    let b1 = at(1);
                    if !continuation(b1) || (if b1 < 0xa0 { b0 == 0xe0 } else { b0 == 0xed }) {
                        (Some(("invalid continuation byte", pos + 1)), 0)
                    } else {
                        (Some(("unexpected end of data", n)), 0)
                    }
                }
            } else {
                let (b1, b2) = (at(1), at(2));
                if !continuation(b1) || (b0 == 0xe0 && b1 < 0xa0) || (b0 == 0xed && b1 >= 0xa0) {
                    (Some(("invalid continuation byte", pos + 1)), 0)
                } else if !continuation(b2) {
                    (Some(("invalid continuation byte", pos + 2)), 0)
                } else {
                    (None, 3)
                }
            }
        } else if b0 < 0xf5 {
            if left < 4 {
                if left < 2 {
                    (Some(("unexpected end of data", n)), 0)
                } else {
                    let b1 = at(1);
                    if !continuation(b1) || (if b1 < 0x90 { b0 == 0xf0 } else { b0 == 0xf4 }) {
                        (Some(("invalid continuation byte", pos + 1)), 0)
                    } else if left < 3 {
                        (Some(("unexpected end of data", n)), 0)
                    } else if !continuation(at(2)) {
                        (Some(("invalid continuation byte", pos + 2)), 0)
                    } else {
                        (Some(("unexpected end of data", n)), 0)
                    }
                }
            } else {
                let (b1, b2, b3) = (at(1), at(2), at(3));
                if !continuation(b1) || (b0 == 0xf0 && b1 < 0x90) || (b0 == 0xf4 && b1 >= 0x90) {
                    (Some(("invalid continuation byte", pos + 1)), 0)
                } else if !continuation(b2) {
                    (Some(("invalid continuation byte", pos + 2)), 0)
                } else if !continuation(b3) {
                    (Some(("invalid continuation byte", pos + 3)), 0)
                } else {
                    (None, 4)
                }
            }
        } else {
            (Some(("invalid start byte", pos + 1)), 0)
        };
        match failure {
            None => {
                let code = match length {
                    2 => (u32::from(b0 & 0x1f) << 6) | u32::from(at(1) & 0x3f),
                    3 => {
                        (u32::from(b0 & 0x0f) << 12)
                            | (u32::from(at(1) & 0x3f) << 6)
                            | u32::from(at(2) & 0x3f)
                    }
                    _ => {
                        (u32::from(b0 & 0x07) << 18)
                            | (u32::from(at(1) & 0x3f) << 12)
                            | (u32::from(at(2) & 0x3f) << 6)
                            | u32::from(at(3) & 0x3f)
                    }
                };
                out.push(code);
                pos += length;
            }
            Some((reason, end)) => {
                // surrogatepass: 3 バイトの surrogate の符号なら受け取って続ける。
                if surrogatepass
                    && left >= 3
                    && (b0 & 0xf0) == 0xe0
                    && continuation(at(1))
                    && continuation(at(2))
                {
                    let code = (u32::from(b0 & 0x0f) << 12)
                        | (u32::from(at(1) & 0x3f) << 6)
                        | u32::from(at(2) & 0x3f);
                    if is_surrogate(code) {
                        out.push(code);
                        pos += 3;
                        continue;
                    }
                }
                return Err(DecodeError {
                    encoding: "utf-8",
                    object: raw.to_vec(),
                    start: pos,
                    end,
                    reason,
                });
            }
        }
    }
    Ok(out)
}

/// UTF-16(surrogatepass)。`big` が None なら BOM を見る(無ければ little)。
fn utf16(raw: &[u8], big: Option<bool>) -> Result<Vec<u32>, DecodeError> {
    let n = raw.len();
    let mut pos = 0;
    let big = match big {
        Some(big) => big,
        None => {
            if n >= 2 && raw[0] == 0xff && raw[1] == 0xfe {
                pos = 2;
                false
            } else if n >= 2 && raw[0] == 0xfe && raw[1] == 0xff {
                pos = 2;
                true
            } else {
                false
            }
        }
    };
    let encoding = if big { "utf-16-be" } else { "utf-16-le" };
    let unit = |at: usize| {
        if big {
            (u32::from(raw[at]) << 8) | u32::from(raw[at + 1])
        } else {
            (u32::from(raw[at + 1]) << 8) | u32::from(raw[at])
        }
    };
    let mut out = Vec::with_capacity(n / 2);
    loop {
        let (reason, start, end) = if n - pos >= 2 {
            let first = unit(pos);
            pos += 2;
            if !is_surrogate(first) {
                out.push(first);
                continue;
            }
            if first >= 0xdc00 {
                ("illegal encoding", pos - 2, pos)
            } else if n - pos < 2 {
                ("unexpected end of data", pos - 2, n)
            } else {
                let second = unit(pos);
                pos += 2;
                if !(0xdc00..=0xdfff).contains(&second) {
                    ("illegal UTF-16 surrogate", pos - 4, pos - 2)
                } else {
                    out.push(0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00));
                    continue;
                }
            }
        } else if pos == n {
            return Ok(out);
        } else {
            ("truncated data", pos, n)
        };
        if n - start >= 2 && is_surrogate(unit(start)) {
            out.push(unit(start));
            pos = start + 2;
            continue;
        }
        return Err(DecodeError {
            encoding,
            object: raw.to_vec(),
            start,
            end,
            reason,
        });
    }
}

/// UTF-32(surrogatepass)。`big` が None なら BOM を見る(無ければ little)。
fn utf32(raw: &[u8], big: Option<bool>) -> Result<Vec<u32>, DecodeError> {
    let n = raw.len();
    let mut pos = 0;
    let big = match big {
        Some(big) => big,
        None => {
            if n >= 4 && raw[..4] == [0xff, 0xfe, 0x00, 0x00] {
                pos = 4;
                false
            } else if n >= 4 && raw[..4] == [0x00, 0x00, 0xfe, 0xff] {
                pos = 4;
                true
            } else {
                false
            }
        }
    };
    let encoding = if big { "utf-32-be" } else { "utf-32-le" };
    let unit = |at: usize| {
        let bytes = [raw[at], raw[at + 1], raw[at + 2], raw[at + 3]];
        if big {
            u32::from_be_bytes(bytes)
        } else {
            u32::from_le_bytes(bytes)
        }
    };
    let mut out = Vec::with_capacity(n / 4);
    loop {
        let (reason, start, end) = if n - pos >= 4 {
            let code = unit(pos);
            if is_surrogate(code) {
                (
                    "code point in surrogate code point range(0xd800, 0xe000)",
                    pos,
                    pos + 4,
                )
            } else if code < 0x110000 {
                out.push(code);
                pos += 4;
                continue;
            } else {
                ("code point not in range(0x110000)", pos, pos + 4)
            }
        } else if pos == n {
            return Ok(out);
        } else {
            ("truncated data", pos, n)
        };
        if n - start >= 4 && is_surrogate(unit(start)) {
            out.push(unit(start));
            pos = start + 4;
            continue;
        }
        return Err(DecodeError {
            encoding,
            object: raw.to_vec(),
            start,
            end,
            reason,
        });
    }
}
