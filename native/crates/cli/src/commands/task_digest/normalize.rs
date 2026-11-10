//! 旧 task-digest.py の行単位の正規化。Markdown 一般の構文は解釈しない。
use sha2::{Digest, Sha256};

pub const INDENT_ERROR: &str =
    "対応外の字下げのコードフェンスがある(半角スペースは0〜3個、タブは不可)";
pub const UNCLOSED_ERROR: &str = "閉じていないコードフェンスがある";

/// Python 3.10 の Unicode 正規表現の \s。LF 以外の改行も行内では残す。
pub fn whitespace(c: char) -> bool {
    matches!(c, '\u{9}'..='\u{d}' | '\u{1c}'..='\u{20}' | '\u{85}' | '\u{a0}' | '\u{1680}'
        | '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}')
}

fn marker(line: &str) -> Option<(u8, usize)> {
    let bytes = line.as_bytes();
    let &first = bytes.first()?;
    if first != b'`' && first != b'~' {
        return None;
    }
    let count = bytes.iter().take_while(|&&c| c == first).count();
    (count >= 3).then_some((first, count))
}

fn fence_flags(lines: &[&str]) -> Result<Vec<bool>, String> {
    let mut closing = None;
    let mut flags = Vec::with_capacity(lines.len());
    for line in lines {
        let spaces = line.as_bytes().iter().take_while(|&&c| c == b' ').count();
        let candidate = (spaces <= 3).then(|| marker(&line[spaces..])).flatten();
        if let Some((symbol, length)) = closing {
            flags.push(true);
            if let Some((found, count)) = candidate
                && found == symbol
                && count >= length
                && line[spaces + count..]
                    .bytes()
                    .all(|c| c == b' ' || c == b'\t')
            {
                closing = None;
            }
        } else {
            if candidate.is_none() {
                let indented = line.trim_start_matches([' ', '\t']);
                if indented.len() < line.len() && marker(indented).is_some() {
                    return Err(INDENT_ERROR.to_owned());
                }
            }
            flags.push(candidate.is_some());
            closing = candidate;
        }
    }
    if closing.is_some() {
        return Err(UNCLOSED_ERROR.to_owned());
    }
    Ok(flags)
}

pub fn body(text: &str) -> Result<String, String> {
    let text = text
        .strip_prefix('\u{feff}')
        .unwrap_or(text)
        .replace("\r\n", "\n")
        .replace('\r', "\n");
    let lines: Vec<_> = text
        .split('\n')
        .map(|line| line.trim_end_matches([' ', '\t']))
        .collect();
    let fenced = fence_flags(&lines)?;
    let headings: Vec<_> = lines
        .iter()
        .enumerate()
        .filter_map(|(i, line)| (!fenced[i] && line.starts_with("## ")).then_some(i))
        .collect();
    let records: Vec<_> = headings
        .iter()
        .copied()
        .filter(|&i| lines[i] == "## 追加修正記録")
        .collect();
    if records.len() != 1 {
        return Err(format!(
            "コードフェンスの外の `## 追加修正記録` の見出し行が {} 個ある(ちょうど 1 個が必要)",
            records.len()
        ));
    }
    let start = records[0];
    let end = headings
        .iter()
        .copied()
        .find(|&i| i > start)
        .unwrap_or(lines.len());
    let first = headings[0];
    let mut kept = Vec::new();
    for (i, line) in lines.into_iter().enumerate() {
        if (start..end).contains(&i) {
            continue;
        }
        if fenced[i] {
            kept.push(line.to_owned());
            continue;
        }
        if i < first
            && [
                "> **ステータス**:",
                "> **基準コミット**:",
                "> **無人実行**:",
            ]
            .iter()
            .any(|prefix| line.starts_with(prefix))
        {
            continue;
        }
        if line.is_empty() {
            continue;
        }
        let content = line.trim_start_matches(whitespace);
        if content.starts_with("- [x]") || content.starts_with("- [X]") {
            kept.push(format!(
                "{}- [ ]{}",
                &line[..line.len() - content.len()],
                &content[5..]
            ));
        } else {
            kept.push(line.to_owned());
        }
    }
    Ok(kept.join("\n"))
}

pub fn digest(normalized: &str) -> String {
    let hash = Sha256::digest(normalized.as_bytes());
    format!(
        "sha256:{}",
        hash[..8]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    )
}
