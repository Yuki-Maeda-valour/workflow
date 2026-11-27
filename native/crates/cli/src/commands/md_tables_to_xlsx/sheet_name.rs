//! シート名(旧 `unique_sheet_name` と、openpyxl 3.1.5 の `avoid_duplicate_name`・`title` の設定)。
//!
//! 2 段で重複を避ける。
//! 1. 旧版の `unique_sheet_name`: `\/*?[]:` を空白へ、前後の空白を除き(空なら `Sheet`)、31 文字で
//!    切る。大文字小文字を区別して、使った名前と重なれば `_2`・`_3`… を足す(名前の前を切る)。
//! 2. openpyxl の `avoid_duplicate_name`(`openpyxl/workbook/child.py:15-34`): 既存のシート名と
//!    `str.lower()` で一致すれば、既存の名前を `,` でつないだ 1 本の文字列から
//!    `(?P<title>{re.escape(value)})(?P<count>\d*),?` を `re.I` で `findall` し、拾った数の
//!    最大値 + 1 を足す。
//!
//! 足した結果が 32 文字以上なら、`child.py:99` の `UserWarning` を実行中の最初の 1 回だけ出す
//! (警告の既定の扱い。出す所は呼び出す側)。

use super::markdown::{sre_ignorecase_eq, strip};
use super::pyio::PyStr;
use super::r4b_unicode;
use super::unicode13;
use std::collections::HashSet;

/// Excel のシート名の長さの上限(旧版の切り方と、openpyxl の警告の境目)。
pub const MAX_TITLE: usize = 31;

/// openpyxl 3.1.5 の `child.py:99` の警告の文(`warnings.formatwarning` の形。ファイル名は固定名)。
pub const TITLE_WARNING: &str = "openpyxl/workbook/child.py:99: UserWarning: Title is more than 31 characters. Some applications may not be able to read the file\n  warnings.warn(\"Title is more than 31 characters. Some applications may not be able to read the file\")\n";

/// 旧 `unique_sheet_name` で使わない文字(`[\\/*?\[\]:]`)。
const FORBIDDEN: [char; 7] = ['\\', '/', '*', '?', '[', ']', ':'];
const CAPITAL_SIGMA: u32 = 0x3a3;
const SMALL_SIGMA: u32 = 0x3c3;
const FINAL_SIGMA: u32 = 0x3c2;
const COMMA: u32 = 0x2c;

/// 旧 `unique_sheet_name(name, used)`。返す名前を `used` に足す。
pub fn unique_sheet_name(hint: &PyStr, used: &mut HashSet<PyStr>) -> PyStr {
    let replaced = hint
        .as_code_points()
        .iter()
        .map(|&code| {
            if FORBIDDEN.iter().any(|&c| u32::from(c) == code) {
                u32::from(' ')
            } else {
                code
            }
        })
        .collect::<Vec<_>>();
    let stripped = strip(&replaced);
    let name = if stripped.is_empty() {
        PyStr::from("Sheet").into_code_points()
    } else {
        stripped.to_vec()
    };
    let prefix = |length: usize| name[..length.min(name.len())].to_vec();
    let mut candidate = PyStr::from_code_points(prefix(MAX_TITLE));
    let mut number: u64 = 2;
    while used.contains(&candidate) {
        let suffix = format!("_{number}");
        // `name[: 31 - len(suffix)]`(番号は 30 桁に届かないので、負にはならない)。
        let mut points = prefix(MAX_TITLE.saturating_sub(suffix.len()));
        points.extend(suffix.chars().map(u32::from));
        candidate = PyStr::from_code_points(points);
        number += 1;
    }
    used.insert(candidate.clone());
    candidate
}

/// `str.lower()`(CPython 3.10.11・Unicode 13.0.0)。完全な小文字化と、語末のシグマ
/// (`handle_capital_sigma`)。サロゲートは変わらない。
pub fn python_lower(text: &[u32]) -> Vec<u32> {
    let mut lower = Vec::with_capacity(text.len());
    for (index, &code) in text.iter().enumerate() {
        if code == CAPITAL_SIGMA {
            lower.push(if is_final_sigma(text, index) {
                FINAL_SIGMA
            } else {
                SMALL_SIGMA
            });
        } else {
            unicode13::push_full_lower(code, &mut lower);
        }
    }
    lower
}

/// `\p{cased}\p{case-ignorable}*Σ!(\p{case-ignorable}*\p{cased})` の文脈か。
fn is_final_sigma(text: &[u32], index: usize) -> bool {
    let before = text[..index]
        .iter()
        .rev()
        .find(|&&code| !unicode13::case_ignorable(code));
    if !before.is_some_and(|&code| unicode13::cased(code)) {
        return false;
    }
    let after = text[index + 1..]
        .iter()
        .find(|&&code| !unicode13::case_ignorable(code));
    after.is_none_or(|&code| !unicode13::cased(code))
}

/// openpyxl 3.1.5 の `avoid_duplicate_name(names, value)`。
pub fn avoid_duplicate_name(names: &[PyStr], value: &PyStr) -> PyStr {
    let wanted = python_lower(value.as_code_points());
    if !names
        .iter()
        .any(|name| python_lower(name.as_code_points()) == wanted)
    {
        return value.clone();
    }
    let mut joined = Vec::new();
    for (index, name) in names.iter().enumerate() {
        if index > 0 {
            joined.push(COMMA);
        }
        joined.extend_from_slice(name.as_code_points());
    }
    let Some(counts) = find_counts(&joined, value.as_code_points()) else {
        return value.clone();
    };
    // `highest = max(counts)`(無ければ 0)、`value + str(highest + 1)`。
    let highest = counts
        .into_iter()
        .max_by(|left, right| compare_decimal(left, right))
        .unwrap_or_default();
    let mut result = value.clone();
    result.push_str(&increment_decimal(&highest));
    result
}

/// `re.compile(f"(?P<title>{re.escape(value)})(?P<count>\\d*),?", re.I).findall(joined)` の
/// 一致があれば、各一致の `count` のうち空でないものを、ASCII の 10 進の数字の列(先頭の 0 を除く)で
/// 返す(`int(idx) for … if idx.isdigit()`)。一致が無ければ `None`。
fn find_counts(joined: &[u32], value: &[u32]) -> Option<Vec<Vec<u8>>> {
    let mut matched = false;
    let mut counts = Vec::new();
    let mut position = 0;
    while position + value.len() <= joined.len() {
        let title_matches = value
            .iter()
            .zip(&joined[position..])
            .all(|(&pattern, &code)| sre_ignorecase_eq(pattern, code));
        if !title_matches {
            position += 1;
            continue;
        }
        matched = true;
        let mut end = position + value.len();
        let mut digits = Vec::new();
        while let Some(digit) = joined.get(end).and_then(|&code| r4b_unicode::decimal(code)) {
            digits.push(b'0' + digit);
            end += 1;
        }
        if joined.get(end) == Some(&COMMA) {
            end += 1;
        }
        if !digits.is_empty() {
            let first = digits
                .iter()
                .position(|&digit| digit != b'0')
                .unwrap_or(digits.len());
            counts.push(digits[first..].to_vec());
        }
        // 題名は 1 文字以上なので、一致は空でない。
        position = end;
    }
    matched.then_some(counts)
}

/// 先頭の 0 の無い 10 進の数字の列(空は 0)の大小。
fn compare_decimal(left: &[u8], right: &[u8]) -> std::cmp::Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}

/// 先頭の 0 の無い 10 進の数字の列(空は 0)に 1 を足した文字列。
fn increment_decimal(digits: &[u8]) -> String {
    let mut result = digits.to_vec();
    let mut index = result.len();
    loop {
        if index == 0 {
            result.insert(0, b'1');
            break;
        }
        index -= 1;
        if result[index] == b'9' {
            result[index] = b'0';
        } else {
            result[index] += 1;
            break;
        }
    }
    String::from_utf8(result).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn py(text: &str) -> PyStr {
        PyStr::from(text)
    }

    fn lower(text: &str) -> String {
        python_lower(&text.chars().map(u32::from).collect::<Vec<_>>())
            .into_iter()
            .map(|code| char::from_u32(code).unwrap())
            .collect()
    }

    /// 旧版の 2 段を、表の名前の順に当てる(`wb.create_sheet(unique_sheet_name(hint, used))`)。
    fn titles(hints: &[&str]) -> Vec<String> {
        let mut used = HashSet::new();
        let mut names: Vec<PyStr> = Vec::new();
        for hint in hints {
            let candidate = unique_sheet_name(&py(hint), &mut used);
            let title = avoid_duplicate_name(&names, &candidate);
            names.push(title);
        }
        names.iter().map(PyStr::to_string_lossy).collect()
    }

    #[test]
    fn unique_names_follow_the_legacy_rule() {
        assert_eq!(
            titles(&["v a\\b/c*d?e[f]g:h", "v [x]", "v :y:"]),
            ["v a b c d e f g h", "v  x", "v  y"]
        );
        assert_eq!(titles(&["  ", "[]"]), ["Sheet", "Sheet_2"]);
        let long = "x".repeat(40);
        let mut used = HashSet::new();
        assert_eq!(unique_sheet_name(&py(&long), &mut used).len(), 31);
        let second = unique_sheet_name(&py(&long), &mut used);
        assert_eq!(second.to_string_lossy(), format!("{}_2", "x".repeat(29)));
        let many = (0..12).map(|_| "Report 同じ").collect::<Vec<_>>();
        let names = titles(&many);
        assert_eq!(names[0], "Report 同じ");
        assert_eq!(names[11], "Report 同じ_12");
    }

    #[test]
    fn openpyxl_adds_the_next_number_case_insensitively() {
        assert_eq!(titles(&["v a", "v a5", "v A"]), ["v a", "v a5", "v A6"]);
        assert_eq!(titles(&["v a", "v A", "v a1"]), ["v a", "v A1", "v a11"]);
        assert_eq!(
            titles(&["Report Summary", "Report SUMMARY", "Report summary"]),
            ["Report Summary", "Report SUMMARY1", "Report summary2"]
        );
        // 別のシート名の途中の数字も拾う(つないだ文字列のどこにでも一致する)。`,` の後の数字は、
        // `,?` が先に `,` を取るので拾わない。
        assert_eq!(titles(&["x", "yx2", "X"]), ["x", "yx2", "X3"]);
        assert_eq!(titles(&["x", "x,2", "X"]), ["x", "x,2", "X1"]);
        // Unicode の 10 進数字も数として拾う(٣ は 3、５ は 5)。
        assert_eq!(titles(&["a", "a٣", "A"]), ["a", "a٣", "A4"]);
        assert_eq!(titles(&["b", "b５", "B"]), ["b", "b５", "B6"]);
        // 10 進数字でない数字(² など)は拾わない。
        assert_eq!(titles(&["c", "c²", "C"]), ["c", "c²", "C1"]);
        // 先頭の 0 は数として読む。
        assert_eq!(titles(&["d", "d007", "D"]), ["d", "d007", "D8"]);
        // 大きな数も桁を落とさない。
        assert_eq!(
            titles(&["e", "e99999999999999999999999999", "E"]),
            [
                "e",
                "e99999999999999999999999999",
                "E100000000000000000000000000"
            ]
        );
    }

    #[test]
    fn turkish_i_and_sigma_use_python_lower() {
        assert_eq!(lower("İ"), "i\u{307}");
        assert_eq!(lower("I"), "i");
        assert_eq!(lower("ı"), "ı");
        assert_eq!(lower("ΣΑΣ"), "σας");
        assert_eq!(lower("Σ"), "σ");
        assert_eq!(lower("ΑΣ."), "ας.");
        assert_eq!(lower("ΑΣ'Α"), "ασ'α");
        assert_eq!(lower("ǅ"), "ǆ");
        assert_eq!(lower("ẞ"), "ß");
        assert_eq!(lower("\u{2c2f}"), "\u{2c2f}");
        // İ の完全な小文字化は 2 文字なので、i とは一致しない(番号を足さない)。
        assert_eq!(titles(&["v i", "v İ"]), ["v i", "v İ"]);
        // ı の小文字は ı(i と別)。re.I では i・I・ı・İ が同じと見なされる。
        assert_eq!(titles(&["v I", "v i"]), ["v I", "v i1"]);
        assert_eq!(titles(&["v jİ", "v Jİ"]), ["v jİ", "v Jİ1"]);
        // σας と ΣΑΣ は str.lower() で一致する。
        assert_eq!(titles(&["v ΣΑΣ", "v σας"]), ["v ΣΑΣ", "v σας1"]);
        // Σ だけは σ、ς だけは ς なので一致しない。
        assert_eq!(titles(&["v σ", "v ς"]), ["v σ", "v ς"]);
        // ſ は s と小文字化が違うが、re.I では同じ。
        assert_eq!(titles(&["v s", "v S", "v ſ"]), ["v s", "v S1", "v ſ"]);
        // K(U+212A)の小文字化は k。
        assert_eq!(titles(&["v k", "v \u{212a}"]), ["v k", "v \u{212a}1"]);
    }

    #[test]
    fn full_lower_match_without_a_regex_match_keeps_the_name() {
        // 「i + U+0307」と「İ」は str.lower() で一致するが、re の照合は長さが違うので一致しない。
        let names = [py("v İ")];
        let value = py("v i\u{307}");
        assert_eq!(avoid_duplicate_name(&names, &value), value);
    }

    #[test]
    fn decimals_compare_and_increment() {
        assert_eq!(increment_decimal(b""), "1");
        assert_eq!(increment_decimal(b"9"), "10");
        assert_eq!(increment_decimal(b"199"), "200");
        assert_eq!(compare_decimal(b"10", b"9"), std::cmp::Ordering::Greater);
        assert_eq!(compare_decimal(b"", b"1"), std::cmp::Ordering::Less);
    }
}
