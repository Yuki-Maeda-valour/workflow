//! Unicode 13.0.0 の NFC(Python 3.10 の `unicodedata.normalize("NFC", s)` と同じ結果)。
//!
//! 表は `unicode/generate_nfc.py` が生成した固定データ。孤立 surrogate は結合クラス 0 で
//! 分解も合成もされず、そのまま残る。製品の実行時に Python は呼ばない。

use super::chars::PyStr;

mod tables {
    include!("unicode/nfc13.rs");
}

pub const UNICODE_VERSION: &str = tables::NFC_UNICODE_VERSION;
pub const TABLE_SHA256: &str = tables::NFC_TABLE_SHA256;

const S_BASE: u32 = 0xac00;
const L_BASE: u32 = 0x1100;
const V_BASE: u32 = 0x1161;
const T_BASE: u32 = 0x11a7;
const L_COUNT: u32 = 19;
const V_COUNT: u32 = 21;
const T_COUNT: u32 = 28;
const N_COUNT: u32 = V_COUNT * T_COUNT;
const S_COUNT: u32 = L_COUNT * N_COUNT;

fn combining_class(code: u32) -> u8 {
    let at = tables::COMBINING_CLASS.partition_point(|&(start, _, _)| start <= code);
    match at.checked_sub(1).map(|i| tables::COMBINING_CLASS[i]) {
        Some((_, end, class)) if code <= end => class,
        _ => 0,
    }
}

fn decompose_into(code: u32, out: &mut Vec<u32>) {
    if (S_BASE..S_BASE + S_COUNT).contains(&code) {
        let s_index = code - S_BASE;
        out.push(L_BASE + s_index / N_COUNT);
        out.push(V_BASE + (s_index % N_COUNT) / T_COUNT);
        let t = T_BASE + s_index % T_COUNT;
        if t != T_BASE {
            out.push(t);
        }
        return;
    }
    match tables::DECOMPOSITION.binary_search_by_key(&code, |&(c, _)| c) {
        Ok(at) => {
            for &part in tables::DECOMPOSITION[at].1 {
                decompose_into(part, out);
            }
        }
        Err(_) => out.push(code),
    }
}

fn compose_pair(first: u32, second: u32) -> Option<u32> {
    if (L_BASE..L_BASE + L_COUNT).contains(&first) && (V_BASE..V_BASE + V_COUNT).contains(&second) {
        return Some(S_BASE + ((first - L_BASE) * V_COUNT + (second - V_BASE)) * T_COUNT);
    }
    if (S_BASE..S_BASE + S_COUNT).contains(&first)
        && (first - S_BASE).is_multiple_of(T_COUNT)
        && (T_BASE + 1..T_BASE + T_COUNT).contains(&second)
    {
        return Some(first + (second - T_BASE));
    }
    tables::COMPOSITION
        .binary_search_by(|&(a, b, _)| (a, b).cmp(&(first, second)))
        .ok()
        .map(|at| tables::COMPOSITION[at].2)
}

pub fn nfc(text: &[u32]) -> PyStr {
    // 1. 完全な正準分解
    let mut decomposed = Vec::with_capacity(text.len());
    for &code in text {
        decompose_into(code, &mut decomposed);
    }
    // 2. 結合クラスの正準順序(0 を越えない・安定)
    let mut at = 0;
    while at < decomposed.len() {
        if combining_class(decomposed[at]) == 0 {
            at += 1;
            continue;
        }
        let start = at;
        while at < decomposed.len() && combining_class(decomposed[at]) != 0 {
            at += 1;
        }
        decomposed[start..at].sort_by_key(|&c| combining_class(c));
    }
    // 3. 正準合成
    let mut out: Vec<u32> = Vec::with_capacity(decomposed.len());
    let mut starter: Option<usize> = None;
    let mut last_class: i32 = -1;
    for &code in &decomposed {
        let class = i32::from(combining_class(code));
        if let Some(index) = starter {
            // 直前の文字との間に、同じ以下の結合クラスの文字が無い(ブロックされていない)とき合成する。
            let blocked = last_class != -1 && (last_class == 0 || last_class >= class);
            if !blocked && let Some(composite) = compose_pair(out[index], code) {
                out[index] = composite;
                continue;
            }
        }
        out.push(code);
        if class == 0 {
            starter = Some(out.len() - 1);
            last_class = -1;
        } else {
            last_class = class;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cps(text: &str) -> PyStr {
        text.chars().map(u32::from).collect()
    }

    #[test]
    fn composes_decomposes_and_orders() {
        assert_eq!(nfc(&cps("e\u{301}")), cps("\u{e9}"));
        assert_eq!(nfc(&cps("\u{212b}")), cps("\u{c5}"));
        assert_eq!(nfc(&cps("a\u{323}\u{307}")), cps("\u{1ea1}\u{307}"));
        assert_eq!(nfc(&cps("a\u{307}\u{323}")), cps("\u{1ea1}\u{307}"));
        assert_eq!(nfc(&cps("\u{1100}\u{1161}\u{11a8}")), cps("\u{ac01}"));
        assert_eq!(nfc(&cps("\u{ac00}\u{11a8}")), cps("\u{ac01}"));
        assert_eq!(nfc(&[0xd800, 0x301]), vec![0xd800, 0x301]);
        assert_eq!(UNICODE_VERSION, "13.0.0");
    }
}
