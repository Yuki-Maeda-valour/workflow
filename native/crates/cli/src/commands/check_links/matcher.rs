//! 旧版の 2 種類の照合を、一般的な正規表現を使わずに同じ後戻りで再現する。
//! - リンク: `\[[^\]]*\]\(([^)\s]+)\)`
//! - コード中のパス: `` `([A-Za-z0-9_.@/-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})` ``
//!
//! どちらも `re.finditer` と同じく、左から重ならずに拾い、合わなければ次の文字から探す。
//! 位置はバイトの番号で返す(開始の字は ASCII なので、文字の境目に当たる)。

/// Python 3.10(Unicode 13.0.0)の `\s` と `str.isspace` の字(`Py_UNICODE_ISSPACE`)。
const SPACES: [(u32, u32); 10] = [
    (0x09, 0x0d),
    (0x1c, 0x20),
    (0x85, 0x85),
    (0xa0, 0xa0),
    (0x1680, 0x1680),
    (0x2000, 0x200a),
    (0x2028, 0x2029),
    (0x202f, 0x202f),
    (0x205f, 0x205f),
    (0x3000, 0x3000),
];

/// 拾った文字列と、一致の始まり(`[` かバッククォートの位置)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Found<'a> {
    pub start: usize,
    pub group: &'a str,
}

/// Python の `\s`。
pub fn is_space(c: char) -> bool {
    let code = u32::from(c);
    SPACES
        .iter()
        .any(|&(low, high)| (low..=high).contains(&code))
}

/// `text.count("\n", 0, start) + 1`。
pub fn line_number(text: &str, start: usize) -> usize {
    text.as_bytes()[..start]
        .iter()
        .filter(|&&byte| byte == b'\n')
        .count()
        + 1
}

/// リンクの照合。`[` の後の最初の `]`、直後の `(`、空白と `)` 以外の 1 字以上、`)` の並び。
pub fn links(text: &str) -> Vec<Found<'_>> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    // 同じ `]` を指す `[` が続くとき、`]` の位置と括弧の中の結果を使い回す(結果は同じ)。
    let mut close: Option<Option<usize>> = None;
    let mut group: Option<(usize, Option<usize>)> = None;
    let mut position = 0;
    while position < bytes.len() {
        if bytes[position] != b'[' {
            position += 1;
            continue;
        }
        let first_close = match close {
            Some(Some(index)) if index > position => Some(index),
            Some(None) => None,
            _ => {
                let index = bytes[position + 1..]
                    .iter()
                    .position(|&byte| byte == b']')
                    .map(|offset| position + 1 + offset);
                close = Some(index);
                index
            }
        };
        let Some(bracket) = first_close else {
            // `]` が後ろに無ければ、どの `[` からも一致しない。
            break;
        };
        let end = if bytes.get(bracket + 1) == Some(&b'(') {
            match group {
                Some((at, end)) if at == bracket => end,
                _ => {
                    let end = group_end(text, bracket + 2);
                    group = Some((bracket, end));
                    end
                }
            }
        } else {
            None
        };
        match end {
            Some(end) => {
                found.push(Found {
                    start: position,
                    group: &text[bracket + 2..end],
                });
                position = end + 1;
            }
            None => position += 1,
        }
    }
    found
}

/// `([^)\s]+)\)` の終わり(`)` の位置)。1 字以上で、続く字が `)` のときだけ。
fn group_end(text: &str, from: usize) -> Option<usize> {
    let mut end = from;
    for c in text[from..].chars() {
        if c == ')' || is_space(c) {
            break;
        }
        end += c.len_utf8();
    }
    (end > from && text.as_bytes().get(end) == Some(&b')')).then_some(end)
}

fn path_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'@' | b'/' | b'-')
}

/// コード中のパスの照合。バッククォートの後の字の並びが、閉じのバッククォートまで正規表現の全体に合うか。
pub fn code_paths(text: &str) -> Vec<Found<'_>> {
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut position = 0;
    while position < bytes.len() {
        if bytes[position] != b'`' {
            position += 1;
            continue;
        }
        let from = position + 1;
        let end = from
            + bytes[from..]
                .iter()
                .take_while(|&&byte| path_byte(byte))
                .count();
        if bytes.get(end) == Some(&b'`') && code_path_shape(&bytes[from..end]) {
            found.push(Found {
                start: position,
                group: &text[from..end],
            });
            position = end + 1;
        } else {
            // 合わなければ次の字から探す。閉じのバッククォートが次の開きになる(旧版の取り違え)。
            position += 1;
        }
    }
    found
}

/// 字の並び(すべて `[A-Za-z0-9_.@/-]`)が `P/Q.R` に分けられるか。
/// R は末尾の英数字の並び全体(1〜10 字)、その前が `.`、Q は最後の `/` の後(1 字以上・`@` なし)、P は 1 字以上。
fn code_path_shape(run: &[u8]) -> bool {
    let extension = run
        .iter()
        .rev()
        .take_while(|byte| byte.is_ascii_alphanumeric())
        .count();
    if !(1..=10).contains(&extension) || run.len() < extension + 1 {
        return false;
    }
    let dot = run.len() - extension - 1;
    if run[dot] != b'.' {
        return false;
    }
    let head = &run[..dot];
    let Some(slash) = head.iter().rposition(|&byte| byte == b'/') else {
        return false;
    };
    let last = &head[slash + 1..];
    slash > 0 && !last.is_empty() && !last.contains(&b'@')
}

/// 旧版が飛ばすリンクの接頭辞(大文字小文字を区別する)。
const SKIPPED_LINKS: [&str; 5] = ["http://", "https://", "mailto:", "#", "tel:"];
/// 旧版が飛ばすコード中のパスの接頭辞。
const SKIPPED_CODES: [&str; 4] = ["http", "npm", "pnpm", "yarn"];

/// リンクの拾った文字列から確かめる対象を作る。飛ばす接頭辞なら None。
/// そうでなければ `strip_link`(`#` より前を取り、前後の `\s` を削る)。空なら呼出元が飛ばす。
pub fn link_target(raw: &str) -> Option<&str> {
    if SKIPPED_LINKS.iter().any(|prefix| raw.starts_with(prefix)) {
        return None;
    }
    let before = raw.split('#').next().unwrap_or(raw);
    Some(before.trim_matches(is_space))
}

/// コード中のパスを飛ばすか。
pub fn code_skipped(target: &str) -> bool {
    SKIPPED_CODES
        .iter()
        .any(|prefix| target.starts_with(prefix))
}
