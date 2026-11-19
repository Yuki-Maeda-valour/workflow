//! 文の部品: 旧版の C 風引用(`needs_quote`・`cquote`・`path_field` :429-462)とその逆(`cunquote`
//! :469-491)、起動しない道具(coreutils 9.4)が引用に使う 2 つの形(`quote`・`quoteaf`)。
//! どれも C の地域設定(旧版は `LC_ALL=C` を export する)で、バイト列のまま扱う。

/// C の地域設定の `[[:cntrl:]]`。
fn is_control(byte: u8) -> bool {
    byte < 0x20 || byte == 0x7f
}

/// `needs_quote` :429-437。
pub fn needs_quote(value: &[u8]) -> bool {
    value
        .iter()
        .any(|&c| c == b'"' || c == b'\\' || is_control(c))
}

/// `cquote` :438-460。
pub fn cquote(value: &[u8]) -> Vec<u8> {
    let mut out = vec![b'"'];
    for &c in value {
        match c {
            b'"' => out.extend_from_slice(b"\\\""),
            b'\\' => out.extend_from_slice(b"\\\\"),
            7 => out.extend_from_slice(b"\\a"),
            8 => out.extend_from_slice(b"\\b"),
            9 => out.extend_from_slice(b"\\t"),
            10 => out.extend_from_slice(b"\\n"),
            11 => out.extend_from_slice(b"\\v"),
            12 => out.extend_from_slice(b"\\f"),
            13 => out.extend_from_slice(b"\\r"),
            c if is_control(c) => out.extend_from_slice(format!("\\{c:03o}").as_bytes()),
            c => out.push(c),
        }
    }
    out.push(b'"');
    out
}

/// `path_field` :462。
pub fn path_field(value: &[u8]) -> Vec<u8> {
    if needs_quote(value) {
        cquote(value)
    } else {
        value.to_vec()
    }
}

/// `printf -v ch "\\$oct"` が stderr へ出す診断(「printf: 」より後ろ)と終了値。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PrintfNote {
    pub text: Vec<u8>,
    pub failed: bool,
}

/// `cunquote` の 1 回の `printf -v ch "\\$oct"`(:485)。`oct` は `\` の後ろの 1〜3 バイト(先頭は 0〜7)。
/// 結果の値(bash の変数に入る値。NUL より後ろは落ちる)と、診断を返す。診断は 1 回の printf で
/// 最大 1 つ(誤りのときは、そこまでの値を変数に入れて終わる)。
pub fn printf_escape(oct: &[u8]) -> (Vec<u8>, Option<PrintfNote>) {
    let mut out = Vec::new();
    let mut note = None;
    let mut i = 0usize;
    // 先頭は 8 進の逃がし(最大 3 桁)。
    let mut value = 0u32;
    let mut digits = 0;
    while digits < 3 {
        match oct.get(i) {
            Some(&c) if (b'0'..=b'7').contains(&c) => {
                value = value * 8 + u32::from(c - b'0');
                i += 1;
                digits += 1;
            }
            _ => break,
        }
    }
    out.push((value & 0xff) as u8);
    while let Some(&c) = oct.get(i) {
        i += 1;
        match c {
            b'\\' => match oct.get(i) {
                None => out.push(b'\\'),
                Some(&d) => {
                    i += 1;
                    match d {
                        b'0'..=b'7' => {
                            // 残りは高々 1 文字なので、8 進は 1 桁。
                            out.push(d - b'0');
                        }
                        b'a' => out.push(7),
                        b'b' => out.push(8),
                        b'e' | b'E' => out.push(27),
                        b'f' => out.push(12),
                        b'n' => out.push(10),
                        b'r' => out.push(13),
                        b't' => out.push(9),
                        b'v' => out.push(11),
                        b'\\' | b'\'' | b'"' | b'?' => out.push(d),
                        b'x' | b'u' | b'U' => {
                            out.push(b'\\');
                            out.push(d);
                            let kind: &[u8] = if d == b'x' { b"hex" } else { b"unicode" };
                            let mut text = b"missing ".to_vec();
                            text.extend_from_slice(kind);
                            text.extend_from_slice(b" digit for \\");
                            text.push(d);
                            text.push(b'\n');
                            note = Some(PrintfNote {
                                text,
                                failed: false,
                            });
                        }
                        b'%' => {
                            // `\%` は `\` を出し、`%` を書式の始まりとして読む(後ろが無いので誤り)。
                            out.push(b'\\');
                            return finish(out, Some(missing_format(b"%")));
                        }
                        other => {
                            out.push(b'\\');
                            out.push(other);
                        }
                    }
                }
            },
            b'%' => {
                let spec = oct.get(i - 1..).unwrap_or_default();
                let Some(&conversion) = oct.get(i) else {
                    return finish(out, Some(missing_format(spec)));
                };
                i += 1;
                match conversion {
                    // 幅・印・長さの修飾の後ろが無い。
                    b'#'
                    | b'\''
                    | b'-'
                    | b'+'
                    | b' '
                    | b'0'..=b'9'
                    | b'*'
                    | b'.'
                    | b'h'
                    | b'j'
                    | b'l'
                    | b'L'
                    | b't'
                    | b'z' => {
                        return finish(out, Some(missing_format(spec)));
                    }
                    b'd' | b'i' | b'o' | b'u' | b'x' | b'X' | b'g' | b'G' => out.push(b'0'),
                    b'e' => out.extend_from_slice(b"0.000000e+00"),
                    b'E' => out.extend_from_slice(b"0.000000E+00"),
                    b'f' | b'F' => out.extend_from_slice(b"0.000000"),
                    b'a' => out.extend_from_slice(b"0x0p+0"),
                    b'A' => out.extend_from_slice(b"0X0P+0"),
                    // 引数の無い %c は NUL を出す(変数はそこで切れる)。
                    b'c' => out.push(0),
                    b's' | b'b' | b'n' => {}
                    b'q' | b'Q' => out.extend_from_slice(b"''"),
                    b'%' => out.push(b'%'),
                    b'(' => {
                        out.extend_from_slice(b"%(");
                        note = Some(PrintfNote {
                            text: b"warning: `c': invalid time format specification\n".to_vec(),
                            failed: false,
                        });
                    }
                    other => {
                        let mut text = b"`".to_vec();
                        text.push(other);
                        text.extend_from_slice(b"': invalid format character\n");
                        return finish(out, Some(PrintfNote { text, failed: true }));
                    }
                }
            }
            other => out.push(other),
        }
    }
    finish(out, note)
}

fn missing_format(spec: &[u8]) -> PrintfNote {
    let mut text = b"`".to_vec();
    text.extend_from_slice(spec);
    text.extend_from_slice(b"': missing format character\n");
    PrintfNote { text, failed: true }
}

/// bash の変数は NUL で終わる文字列なので、最初の NUL より後ろを落とす。
fn finish(mut out: Vec<u8>, note: Option<PrintfNote>) -> (Vec<u8>, Option<PrintfNote>) {
    if let Some(end) = out.iter().position(|b| *b == 0) {
        out.truncate(end);
    }
    (out, note)
}

/// `cunquote` :469-491 の 1 回分。値と、`printf -v` の診断(出た順)を返す。誤り(終了値 1)の
/// `printf -v` があれば、そこまでの値と診断を返し、`failed` を立てる(errexit が働く所では、呼び出し側が
/// ERR の罠へ進める)。働かない所(`if` の中)では、bash は続けるので、呼び出し側は残りを続けて読む
/// `cunquote_all` を使う。
#[derive(Debug, PartialEq, Eq)]
pub struct Unquoted {
    pub value: Vec<u8>,
    pub notes: Vec<PrintfNote>,
}

/// errexit が働かない所の `cunquote`(誤りの後も続ける)。
pub fn cunquote_all(quoted: &[u8]) -> Unquoted {
    cunquote(quoted, false)
}

/// errexit が働く所の `cunquote`。最初の誤り(終了値 1 の printf)で止める。
pub fn cunquote_errexit(quoted: &[u8]) -> Unquoted {
    cunquote(quoted, true)
}

fn cunquote(quoted: &[u8], stop_on_error: bool) -> Unquoted {
    // 前後の `"` を 1 つずつだけ外す(`${s#\"}`・`${s%\"}`)。
    let mut s = quoted;
    if let Some(rest) = s.strip_prefix(b"\"") {
        s = rest;
    }
    if let Some(rest) = s.strip_suffix(b"\"") {
        s = rest;
    }
    let mut out = Vec::new();
    let mut notes = Vec::new();
    let mut i = 0usize;
    while let Some(&c) = s.get(i) {
        if c != b'\\' {
            out.push(c);
            i += 1;
            continue;
        }
        let next = s.get(i + 1).copied();
        match next {
            Some(b'a') => out.push(7),
            Some(b'b') => out.push(8),
            Some(b't') => out.push(9),
            Some(b'n') => out.push(10),
            Some(b'v') => out.push(11),
            Some(b'f') => out.push(12),
            Some(b'r') => out.push(13),
            Some(b'0'..=b'7') => {
                let end = (i + 4).min(s.len());
                let oct = s.get(i + 1..end).unwrap_or_default();
                let (value, note) = printf_escape(oct);
                // 誤りでも、そこまでの値が変数に入る(PRETURN が束ねる)。
                out.extend_from_slice(&value);
                if let Some(note) = note {
                    let failed = note.failed;
                    notes.push(note);
                    if failed && stop_on_error {
                        return Unquoted { value: out, notes };
                    }
                }
                i += 4;
                continue;
            }
            // `${s:i+1:1}` が空(末尾の `\`)なら何も足さない。
            None => {}
            Some(other) => out.push(other),
        }
        i += 2;
    }
    Unquoted { value: out, notes }
}

/// coreutils 9.4 の `quote`(gnulib の locale_quoting_style。C の地域設定では `'…'`)。
pub fn quote(value: &[u8]) -> Vec<u8> {
    let mut out = vec![b'\''];
    for &c in value {
        match c {
            7 => out.extend_from_slice(b"\\a"),
            8 => out.extend_from_slice(b"\\b"),
            12 => out.extend_from_slice(b"\\f"),
            10 => out.extend_from_slice(b"\\n"),
            13 => out.extend_from_slice(b"\\r"),
            9 => out.extend_from_slice(b"\\t"),
            11 => out.extend_from_slice(b"\\v"),
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\'' => out.extend_from_slice(b"\\'"),
            0x20..=0x7e => out.push(c),
            c => out.extend_from_slice(format!("\\{c:03o}").as_bytes()),
        }
    }
    out.push(b'\'');
    out
}

/// 印字できる、引用の形を変えない文字か(gnulib の `c_and_shell_quote_compat`)。
fn compat(byte: u8, index: usize, whole: &[u8]) -> bool {
    match byte {
        b'%' | b'+' | b',' | b'-' | b'.' | b'/' | b':' | b']' | b'_' => true,
        b'0'..=b'9' | b'A'..=b'Z' | b'a'..=b'z' => true,
        b' ' | b'\'' => true,
        b'#' | b'~' => index == 0,
        b'{' | b'}' => whole.len() == 1,
        _ => false,
    }
}

/// coreutils 9.4 の `quoteaf`(gnulib の shell_escape_always_quoting_style、C の地域設定)。
/// `'` を含むと、gnulib は 1 回目を数えるだけにして 2 回目で書く。2 回目は、1 回目の終わりの
/// `$'…'` の中かどうかを引き継ぐ(基準の容器の GNU ls で測った)。
pub fn quoteaf(value: &[u8]) -> Vec<u8> {
    let first = shell_pass(value, false);
    if !first.has_single {
        return first.out;
    }
    if first.all_compat {
        // `'` を含み、ほかに引用の形を変える文字が無ければ、C の形 `"…"` にする。
        let mut out = vec![b'"'];
        for &c in value {
            if c == b'"' || c == b'\\' {
                out.push(b'\\');
            }
            out.push(c);
        }
        out.push(b'"');
        return out;
    }
    shell_pass(value, first.pending).out
}

struct Pass {
    out: Vec<u8>,
    pending: bool,
    has_single: bool,
    all_compat: bool,
}

fn shell_pass(value: &[u8], pending_at_start: bool) -> Pass {
    let mut out = vec![b'\''];
    // `$'…'` の中にいるか。
    let mut pending = pending_at_start;
    let mut has_single = false;
    let mut all_compat = true;
    for (index, &c) in value.iter().enumerate() {
        if !compat(c, index, value) {
            all_compat = false;
        }
        let escape: Option<Vec<u8>> = match c {
            7 => Some(b"a".to_vec()),
            8 => Some(b"b".to_vec()),
            12 => Some(b"f".to_vec()),
            10 => Some(b"n".to_vec()),
            13 => Some(b"r".to_vec()),
            9 => Some(b"t".to_vec()),
            11 => Some(b"v".to_vec()),
            0x20..=0x7e => None,
            c => Some(format!("{c:03o}").into_bytes()),
        };
        match escape {
            Some(text) => {
                if !pending {
                    out.extend_from_slice(b"'$'");
                    pending = true;
                }
                out.push(b'\\');
                out.extend_from_slice(&text);
            }
            None if c == b'\'' => {
                // `'\''`。`$'…'` の中なら最初の `'` がそれを閉じる。
                has_single = true;
                out.extend_from_slice(b"'\\''");
                pending = false;
            }
            None => {
                if pending {
                    out.extend_from_slice(b"''");
                    pending = false;
                }
                out.push(c);
            }
        }
    }
    out.push(b'\'');
    Pass {
        out,
        pending,
        has_single,
        all_compat,
    }
}
