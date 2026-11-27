//! 保護記録の bytes の形式の検査(R4D-3)。旧版 `diagnostic-state.py` と同じ順で処理する。
//!
//! 移すのは `cfield`(:12-34)・`lines`(:36-38)・`integer`(:40-44)・`State.manifest`(:105-117)・
//! `State.__init__` の記録の読み取り(:67-101)・`State.origins` の形式の部分(:118-138)・`State.ancestors`(:194-198)である。
//! ファイルの読み書きも Git の起動もしない。保護記録と実物の結び付け(:47-66・:102-104・:139-193)は R4D-3b(#544)が受け持つ。
//! bytes は bytes のまま扱い、UTF-8 として解釈しない。
use super::budget::{Budget, Failure};
use std::collections::HashSet;

/// 失敗の種類。旧版の `Failure`(理由と終了値)・`ValueError`・`UnicodeError`・`OSError` に当たる。
/// 入口で終了値と文に直すのは R4D-5 の仕事で、ここでは種類ごとに区別して返すだけにする。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Failure(Failure),
    /// 旧版の `ValueError`(整数の桁の上限)。
    Value,
    /// 旧版の `UnicodeError`(文字コードの変換の失敗)。
    Unicode,
    /// 旧版の `OSError`。値は errno。R4D-3 の関数は自分では作らず、R4D-3b の trait の実装が返す。
    Os(i32),
}

impl From<Failure> for Error {
    fn from(failure: Failure) -> Self {
        Error::Failure(failure)
    }
}

const SCHEMA: Failure = Failure::new("state-schema", 22);
const STATE_BINDING: Failure = Failure::new("state-binding", 22);
const OLD_STATE: Failure = Failure::new("old-state", 22);
const UNSUPPORTED_FORMAT: Failure = Failure::new("unsupported-format", 23);
const CONTEXT_BINDING: Failure = Failure::new("context-binding", 22);

/// Python 3.10.11 の `int()` が 10 進の文字列を整数にするときの桁の上限(先頭の 0 を数え、`-` を数えない)。
pub const INT_MAX_STR_DIGITS: usize = 4300;
/// context の第 12 欄の上限(旧版の `16*R.MIB`)。
const FIELD_12_MAX: i64 = 16 * 1024 * 1024;

const TAB: u8 = b'\t';

fn schema<T>() -> Result<T, Error> {
    Err(SCHEMA.into())
}

/// 小文字 16 進で、ちょうど `width` 桁か。
pub(crate) fn is_lower_hex(raw: &[u8], width: usize) -> bool {
    raw.len() == width && raw.iter().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
}

fn split_tab(row: &[u8]) -> Vec<&[u8]> {
    row.split(|&c| c == TAB).collect()
}

/// C 形式の引用を読む(旧版の `cfield`)。先頭が `"` でなければ、NUL・LF・TAB を含まない限りそのまま返す。
pub fn cfield(raw: &[u8]) -> Result<Vec<u8>, Failure> {
    if raw.first() != Some(&b'"') {
        if raw.iter().any(|&c| c == 0 || c == b'\n' || c == TAB) {
            return Err(SCHEMA);
        }
        return Ok(raw.to_vec());
    }
    if raw.len() < 2 || raw.last() != Some(&b'"') {
        return Err(SCHEMA);
    }
    let end = raw.len() - 1;
    let mut out = Vec::with_capacity(end);
    let mut i = 1;
    while i < end {
        let mut c = raw[i];
        i += 1;
        if c == b'"' {
            return Err(SCHEMA);
        }
        if c == b'\\' {
            if i >= end {
                return Err(SCHEMA);
            }
            c = raw[i];
            i += 1;
            c = match c {
                b'a' => 7,
                b'b' => 8,
                b't' => 9,
                b'n' => 10,
                b'v' => 11,
                b'f' => 12,
                b'r' => 13,
                b'"' => 34,
                b'\\' => 92,
                b'0'..=b'7' => {
                    // 旧版は raw[i:i+2] を取る。閉じの `"` を含みうるので、3 桁がそろわなければ失敗になる。
                    let digits = &raw[i..(i + 2).min(raw.len())];
                    if digits.len() != 2 || digits.iter().any(|d| !(b'0'..=b'7').contains(d)) {
                        return Err(SCHEMA);
                    }
                    let value = u32::from(c - b'0') * 64
                        + u32::from(digits[0] - b'0') * 8
                        + u32::from(digits[1] - b'0');
                    i += 2;
                    match u8::try_from(value) {
                        Ok(value) => value,
                        Err(_) => return Err(SCHEMA),
                    }
                }
                _ => return Err(SCHEMA),
            };
        }
        if c == 0 {
            return Err(SCHEMA);
        }
        out.push(c);
    }
    Ok(out)
}

/// 記録を LF で分ける(旧版の `lines`)。空でなく末尾が LF でなければ失敗。最後の空の要素は除く。
pub fn lines(raw: &[u8]) -> Result<Vec<&[u8]>, Failure> {
    if !raw.is_empty() && raw.last() != Some(&b'\n') {
        return Err(SCHEMA);
    }
    let mut rows: Vec<&[u8]> = raw.split(|&c| c == b'\n').collect();
    rows.pop();
    Ok(rows)
}

/// 10 進の整数を読む(旧版の `integer`)。形が `-?[0-9]+` でなければ `state-schema`、
/// 桁の上限を超えれば `Error::Value`、範囲の外なら `state-schema`。
pub fn integer(raw: &[u8], minimum: i64, maximum: i64) -> Result<i64, Error> {
    let (negative, digits) = match raw.split_first() {
        Some((b'-', rest)) => (true, rest),
        _ => (false, raw),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return schema();
    }
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(Error::Value);
    }
    let start = digits
        .iter()
        .position(|&c| c != b'0')
        .unwrap_or(digits.len());
    let significant = &digits[start..];
    // i128 に収まる桁(38 桁)を超える値は、i64 の範囲のどの上限よりも外にある。
    if significant.len() > 38 {
        return schema();
    }
    let magnitude = significant
        .iter()
        .fold(0_i128, |value, &c| value * 10 + i128::from(c - b'0'));
    let value = if negative { -magnitude } else { magnitude };
    if value < i128::from(minimum) || value > i128::from(maximum) {
        return schema();
    }
    Ok(value as i64)
}

/// `manifest.tsv` の形式を確かめる(旧版の `State.manifest`)。各行の先に予算を確かめる。
pub fn check_manifest(raw: &[u8], budget: &mut Budget) -> Result<(), Error> {
    let rows = lines(raw)?;
    if rows.is_empty() {
        return schema();
    }
    for (n, row) in rows.iter().enumerate() {
        budget.check()?;
        let f = split_tab(row);
        if f.len() != 4
            || (n == 0 && f[0] != b"T")
            || (n > 0 && !matches!(f[0], b"f" | b"l" | b"o" | b"u"))
        {
            return schema();
        }
        let (kind, mode, value, path) = (f[0], f[1], f[2], f[3]);
        let octal_mode =
            (3..=4).contains(&mode.len()) && mode.iter().all(|c| (b'0'..=b'7').contains(c));
        if !(octal_mode || mode == b"-") {
            return schema();
        }
        if matches!(kind, b"T" | b"f") && !is_lower_hex(value, 64) {
            return schema();
        }
        if kind == b"l" {
            cfield(value)?;
        }
        if matches!(kind, b"o" | b"u") && value != b"-" {
            return schema();
        }
        let path = cfield(path)?;
        if path.is_empty() || (n == 0) != path.starts_with(b"/") {
            return schema();
        }
    }
    Ok(())
}

/// context の hash の形式。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HashFormat {
    Sha1,
    Sha256,
}

impl HashFormat {
    pub fn name(self) -> &'static str {
        match self {
            HashFormat::Sha1 => "sha1",
            HashFormat::Sha256 => "sha256",
        }
    }

    /// object の名前の 16 進の桁数(40・64)。
    pub fn width(self) -> usize {
        match self {
            HashFormat::Sha1 => 40,
            HashFormat::Sha256 => 64,
        }
    }
}

/// context の参照の保存形式。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backend {
    Files,
    Reftable,
}

impl Backend {
    pub fn name(self) -> &'static str {
        match self {
            Backend::Files => "files",
            Backend::Reftable => "reftable",
        }
    }
}

/// `config-contexts.txt` の `context` の 1 行(旧版の `self.contexts` の 1 項目)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Context {
    pub id: usize,
    pub parent: i64,
    pub depth: i64,
    pub active: bool,
    pub wt: Vec<u8>,
    pub gd: Vec<u8>,
    pub common: Vec<u8>,
    pub format: HashFormat,
    pub backend: Backend,
    /// 持っている object の名前(第 11 欄が `-` なら無し)。
    pub held: Option<String>,
}

/// 記録の読み取りの結果(旧版の `self.meta` の `cwd`・`toplevel` と `self.contexts`)。`root` の行は確かめるだけで持たない。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Records {
    pub cwd: Option<Vec<u8>>,
    pub toplevel: Option<Vec<u8>>,
    pub contexts: Vec<Context>,
}

impl Records {
    /// `n` から親をたどり、0 番までの番号を根から順に返す(旧版の `State.ancestors`)。
    /// `n` が context の数以上なら `None`(旧版は `IndexError`。旧版の呼出元は `context-binding` の確かめの後の番号だけを渡す)。
    /// 親の連なりが閉じている手作りの値(`parse_records` は作らない)でも止まるよう、context の数より多くはたどらない。
    pub fn ancestors(&self, n: usize) -> Option<Vec<usize>> {
        let mut result = Vec::new();
        let mut current = i64::try_from(n).ok()?;
        while current >= 0 {
            let index = usize::try_from(current).ok()?;
            let context = self.contexts.get(index)?;
            if result.len() >= self.contexts.len() {
                return None;
            }
            result.push(index);
            current = context.parent;
        }
        result.reverse();
        Some(result)
    }
}

fn hash_format(raw: &[u8]) -> Option<HashFormat> {
    match raw {
        b"sha1" => Some(HashFormat::Sha1),
        b"sha256" => Some(HashFormat::Sha256),
        _ => None,
    }
}

fn backend(raw: &[u8]) -> Option<Backend> {
    match raw {
        b"files" => Some(Backend::Files),
        b"reftable" => Some(Backend::Reftable),
        _ => None,
    }
}

/// `4-meta.txt` と `config-contexts.txt` を読む(旧版の `State.__init__` の :67-101)。予算は使わない(旧版もこの部分では使わない)。
///
/// `cwd` は作業場所の実パス(呼出元が求める)。`context_id` は、渡された番号が 0 以上で `u64` に収まるときだけ `Some`。
pub fn parse_records(
    meta: &[u8],
    contexts: &[u8],
    cwd: &[u8],
    context_id: Option<u64>,
) -> Result<Records, Error> {
    let mut records = Records {
        cwd: None,
        toplevel: None,
        contexts: Vec::new(),
    };
    for line in lines(meta)? {
        let Some(tab) = line.iter().position(|&c| c == TAB) else {
            return schema();
        };
        let (key, value) = (&line[..tab], &line[tab + 1..]);
        let slot = match key {
            b"cwd" => &mut records.cwd,
            b"toplevel" => &mut records.toplevel,
            _ => continue,
        };
        if slot.is_some() {
            return schema();
        }
        *slot = Some(cfield(value)?);
    }
    if records.cwd.as_deref() != Some(cwd) || !cwd.starts_with(b"/") {
        return Err(STATE_BINDING.into());
    }
    let rows = lines(contexts)?;
    let Some((first, rows)) = rows.split_first() else {
        return Err(OLD_STATE.into());
    };
    if *first != b"context-plan-v1" {
        return Err(OLD_STATE.into());
    }
    let mut root_maxima = Vec::new();
    for row in rows {
        let f = split_tab(row);
        if f[0] == b"context" && f.len() == 15 {
            let n = integer(f[1], 0, 1999)?;
            let parent = integer(f[2], -1, 1999)?;
            let depth = integer(f[3], 0, 40)?;
            let active = integer(f[4], 0, 1)?;
            let count = records.contexts.len() as i64;
            if n != count || (n == 0 && parent != -1) || (n > 0 && !(0 <= parent && parent < n)) {
                return schema();
            }
            let expected = if parent < 0 {
                0
            } else {
                records.contexts[parent as usize].depth + 1
            };
            if depth != expected {
                return schema();
            }
            let wt = cfield(f[5])?;
            let gd = cfield(f[6])?;
            let common = cfield(f[7])?;
            let (Some(format), Some(backend)) = (hash_format(f[8]), backend(f[9])) else {
                return Err(UNSUPPORTED_FORMAT.into());
            };
            if !wt.starts_with(b"/")
                || [&gd, &common]
                    .iter()
                    .any(|p| p.as_slice() != b"-" && !p.starts_with(b"/"))
            {
                return schema();
            }
            if f[10] != b"-" && !is_lower_hex(f[10], format.width()) {
                return schema();
            }
            integer(f[11], 0, FIELD_12_MAX)?;
            if !matches!(f[12], b"none" | b"file" | b"blob" | b"unmerged") {
                return schema();
            }
            let held = if f[10] == b"-" {
                None
            } else {
                Some(f[10].iter().map(|&c| char::from(c)).collect())
            };
            records.contexts.push(Context {
                id: n as usize,
                parent,
                depth,
                active: active == 1,
                wt,
                gd,
                common,
                format,
                backend,
                held,
            });
        } else if f[0] == b"root" && f.len() == 3 {
            let pair: Vec<&[u8]> = f[1].split(|&c| c == b':').collect();
            if pair.len() != 2 {
                return schema();
            }
            let left = integer(pair[0], 0, 1999)?;
            let right = integer(pair[1], 0, 1999)?;
            root_maxima.push(left.max(right));
            let path = cfield(f[2])?;
            if !path.starts_with(b"/") {
                return schema();
            }
        } else {
            return schema();
        }
    }
    let count = records.contexts.len() as i64;
    if records.contexts.is_empty()
        || records.toplevel.as_ref() != Some(&records.contexts[0].wt)
        || root_maxima.iter().any(|&value| value >= count)
    {
        return schema();
    }
    let active = context_id
        .and_then(|id| usize::try_from(id).ok())
        .and_then(|id| records.contexts.get(id))
        .is_some_and(|context| context.active);
    if !active {
        return Err(CONTEXT_BINDING.into());
    }
    Ok(records)
}

/// `config-origins.txt` の 1 行を TAB で分けたままの欄(先頭は行の種類)。
pub type OriginRow = Vec<Vec<u8>>;

/// `candidate` の名前(`cfield` で読んだ bytes)と、その後の行(`node`・`missing`・`data`)。
pub type OriginGroup = (Vec<u8>, Vec<OriginRow>);

/// `config-origins.txt` の形式の部分の結果。`candidate` の組を出た順に持つ。`admin` は `plan-admin` の 2 つの値。
/// 旧版では関数の中の変数(`groups`・`admin`)で、戻り値にも属性にもならない。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Origins {
    pub groups: Vec<OriginGroup>,
    pub admin: (Vec<u8>, Vec<u8>),
}

/// `config-origins.txt` の形式を確かめる(旧版の `State.origins` の :119-138)。予算は使わない。
/// 旧版はこの後に `candidate` ごとに予算を確かめて実物と結び付ける(:139-181。R4D-3b)。
pub fn parse_origins(raw: &[u8]) -> Result<Origins, Error> {
    let rows = lines(raw)?;
    let Some((first, rows)) = rows.split_first() else {
        return Err(OLD_STATE.into());
    };
    if *first != b"config-origins-v3" {
        return Err(OLD_STATE.into());
    }
    let mut groups: Vec<OriginGroup> = Vec::new();
    let mut seen: HashSet<Vec<u8>> = HashSet::new();
    let mut admin = None;
    for row in rows {
        let f = split_tab(row);
        let tag = f[0];
        if tag == b"candidate" && f.len() == 2 {
            let name = cfield(f[1])?;
            if !seen.insert(name.clone()) {
                return schema();
            }
            groups.push((name, Vec::new()));
        } else if matches!(tag, b"node" | b"missing" | b"data") {
            let Some((_, rows)) = groups.last_mut() else {
                return schema();
            };
            let wanted = if tag == b"node" { 5 } else { 2 };
            if f.len() != wanted {
                return schema();
            }
            rows.push(f.iter().map(|field| field.to_vec()).collect());
        } else if tag == b"plan-admin" && f.len() == 3 {
            if admin.is_some() {
                return schema();
            }
            let first = cfield(f[1])?;
            let second = cfield(f[2])?;
            admin = Some((first, second));
        } else if tag == b"plan-root" && f.len() == 2 {
            cfield(f[1])?;
        } else if tag == b"env" && f.len() == 4 {
        } else {
            return schema();
        }
    }
    let Some(admin) = admin else {
        return Err(OLD_STATE.into());
    };
    Ok(Origins { groups, admin })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    fn s(reason: &'static str, code: u8) -> Error {
        Error::Failure(Failure::new(reason, code))
    }

    fn counting_budget() -> (Budget, Rc<Cell<u64>>) {
        let calls = Rc::new(Cell::new(0_u64));
        let counter = Rc::clone(&calls);
        let clock = Box::new(move || {
            counter.set(counter.get() + 1);
            0.0
        });
        let limits = super::super::budget::Kind::ALL.map(super::super::budget::Kind::default_limit);
        (Budget::for_test(300.0, clock, limits), calls)
    }

    #[test]
    fn cfield_reads_plain_and_quoted_values() {
        assert_eq!(cfield(b"").unwrap(), b"");
        assert_eq!(cfield(b"a\"b").unwrap(), b"a\"b");
        assert_eq!(cfield(b"\xff\x01").unwrap(), b"\xff\x01");
        for bad in [&b"a\0b"[..], b"a\nb", b"a\tb"] {
            assert_eq!(cfield(bad), Err(SCHEMA));
        }
        assert_eq!(cfield(b"\"\"").unwrap(), b"");
        assert_eq!(
            cfield(b"\"\\a\\b\\t\\n\\v\\f\\r\\\"\\\\\"").unwrap(),
            [7, 8, 9, 10, 11, 12, 13, 34, 92]
        );
        assert_eq!(cfield(b"\"\\101\\377\\001\"").unwrap(), [65, 255, 1]);
        assert_eq!(cfield(b"\"a\tb\"").unwrap(), b"a\tb");
    }

    #[test]
    fn cfield_rejects_every_broken_escape() {
        for bad in [
            &b"\""[..],
            b"\"a",
            b"\"\\400\"",
            b"\"\\777\"",
            b"\"\\000\"",
            b"\"\\8\"",
            b"\"\\18\"",
            b"\"\\\"",
            b"\"a\\\"",
            b"\"\\1\"",
            b"\"\\12\"",
            b"\"a\"b\"",
            b"\"\\x\"",
            b"\"\0\"",
        ] {
            assert_eq!(cfield(bad), Err(SCHEMA), "{bad:?}");
        }
    }

    #[test]
    fn lines_needs_a_final_lf() {
        assert_eq!(lines(b"").unwrap(), Vec::<&[u8]>::new());
        assert_eq!(lines(b"\n").unwrap(), vec![&b""[..]]);
        assert_eq!(lines(b"a\nb\n").unwrap(), vec![&b"a"[..], b"b"]);
        assert_eq!(lines(b"a\n\n").unwrap(), vec![&b"a"[..], b""]);
        assert_eq!(lines(b"a"), Err(SCHEMA));
        assert_eq!(lines(b"a\nb"), Err(SCHEMA));
    }

    #[test]
    fn integer_matches_the_digit_limit_and_range() {
        assert_eq!(integer(b"0", 0, 1), Ok(0));
        assert_eq!(integer(b"-0", 0, 1), Ok(0));
        assert_eq!(integer(b"-1", -1, 1), Ok(-1));
        assert_eq!(integer(b"007", 0, 7), Ok(7));
        assert_eq!(integer(b"8", 0, 7), schema());
        assert_eq!(integer(b"-2", -1, 7), schema());
        for bad in [
            &b""[..],
            b"-",
            b"+1",
            b" 1",
            b"1 ",
            b"--1",
            b"1_0",
            b"\xef\xbc\x91",
        ] {
            assert_eq!(integer(bad, 0, 2000), schema(), "{bad:?}");
        }
        let zeros = |count: usize| vec![b'0'; count];
        let mut v = zeros(4299);
        v.push(b'1');
        assert_eq!(integer(&v, 0, 1), Ok(1));
        v.insert(0, b'0');
        assert_eq!(integer(&v, 0, 1), Err(Error::Value));
        let mut negative = vec![b'-'];
        negative.extend(zeros(4300));
        assert_eq!(integer(&negative, 0, 1), Ok(0));
        negative.push(b'0');
        assert_eq!(integer(&negative, 0, 1), Err(Error::Value));
        let mut big = vec![b'1'];
        big.extend(zeros(4299));
        assert_eq!(integer(&big, 0, i64::MAX), schema());
        assert_eq!(integer(b"9223372036854775807", 0, i64::MAX), Ok(i64::MAX));
        assert_eq!(integer(b"9223372036854775808", 0, i64::MAX), schema());
        assert_eq!(integer(b"-9223372036854775809", i64::MIN, 0), schema());
        // 桁の上限は範囲より先、形は桁の上限より先。
        let mut long = vec![b'+'];
        long.extend(zeros(5000));
        assert_eq!(integer(&long, 0, 1), schema());
    }

    fn manifest(rows: &[&[u8]]) -> Vec<u8> {
        let mut raw = Vec::new();
        for row in rows {
            raw.extend_from_slice(row);
            raw.push(b'\n');
        }
        raw
    }

    const HEX: &[u8] = b"51c3935c02ad666c06c0b0961d432233cf1fac1923b223a688f8d549c930899c";

    #[test]
    fn manifest_accepts_the_take_form_and_checks_each_line_first() {
        let t = [&b"T\t664\t"[..], HEX, b"\t/repo/task/t.md"].concat();
        let f = [&b"f\t0644\t"[..], HEX, b"\ttask/t.md"].concat();
        let good = manifest(&[
            &t,
            b"l\t777\tseed.txt\tlink",
            &f,
            b"o\t-\t-\t\"sub\\tdir\"",
            b"u\t-\t-\tx",
        ]);
        let (mut budget, calls) = counting_budget();
        assert_eq!(check_manifest(&good, &mut budget), Ok(()));
        assert_eq!(calls.get() - 1, 5);
        let (mut budget, calls) = counting_budget();
        assert_eq!(check_manifest(b"", &mut budget), schema());
        assert_eq!(
            check_manifest(&good[..good.len() - 1], &mut budget),
            schema()
        );
        assert_eq!(calls.get() - 1, 0);
        let (mut budget, _) = counting_budget();
        budget.request_stop();
        assert_eq!(
            check_manifest(&good, &mut budget),
            Err(s("interrupted", 20))
        );
    }

    #[test]
    fn manifest_rejects_each_field() {
        let t = [&b"T\t664\t"[..], HEX, b"\t/t"].concat();
        let hex63 = &HEX[..63];
        let cases: Vec<Vec<u8>> = vec![
            manifest(&[b"T\t664\tx"]),
            manifest(&[&[&b"f\t664\t"[..], HEX, b"\t/t"].concat()]),
            manifest(&[&t, &[&b"T\t664\t"[..], HEX, b"\tx"].concat()]),
            manifest(&[&t, b"x\t664\t-\tx"]),
            manifest(&[&[&b"T\t64\t"[..], HEX, b"\t/t"].concat()]),
            manifest(&[&[&b"T\t06444\t"[..], HEX, b"\t/t"].concat()]),
            manifest(&[&[&b"T\t068\t"[..], HEX, b"\t/t"].concat()]),
            manifest(&[&[&b"T\t664\t"[..], hex63, b"\t/t"].concat()]),
            manifest(&[&t, b"l\t777\t\"x\tl"]),
            manifest(&[&t, b"o\t-\tx\tl"]),
            manifest(&[&t, b"u\t-\t\tl"]),
            manifest(&[&t, b"o\t-\t-\t\"x"]),
            manifest(&[&[&b"T\t664\t"[..], HEX, b"\t"].concat()]),
            manifest(&[&[&b"T\t664\t"[..], HEX, b"\tt"].concat()]),
            manifest(&[&t, b"o\t-\t-\t/abs"]),
            manifest(&[&t, b"o\t-\t-\t\"\""]),
        ];
        for raw in cases {
            let (mut budget, _) = counting_budget();
            assert_eq!(check_manifest(&raw, &mut budget), schema(), "{raw:?}");
        }
        let (mut budget, _) = counting_budget();
        assert_eq!(
            check_manifest(&manifest(&[&t, b"o\t-\t-\tok"]), &mut budget),
            Ok(())
        );
    }

    fn context_row(fields: &[&[u8]]) -> Vec<u8> {
        let mut row = b"context".to_vec();
        for field in fields {
            row.push(TAB);
            row.extend_from_slice(field);
        }
        row
    }

    const OID: &[u8] = b"38a62dbe66b7405333fcf620e0333e5897c6ec20";

    fn base_row(n: &[u8], parent: &[u8], depth: &[u8], wt: &[u8]) -> Vec<u8> {
        context_row(&[
            n, parent, depth, b"1", wt, b"/r/.git", b"/r/.git", b"sha1", b"files", OID, b"0",
            b"none", b"-", b"-",
        ])
    }

    fn contexts(rows: &[&[u8]]) -> Vec<u8> {
        let mut all: Vec<&[u8]> = vec![b"context-plan-v1"];
        all.extend_from_slice(rows);
        manifest(&all)
    }

    const META: &[u8] = b"toplevel\t/r\ncwd\t/r\nexclude\t.claude/reviews\n";

    #[test]
    fn records_read_contexts_and_roots() {
        let rows = [
            base_row(b"0", b"-1", b"0", b"/r"),
            base_row(b"1", b"0", b"1", b"/r/a"),
            base_row(b"2", b"0", b"1", b"/r/b"),
            base_row(b"3", b"2", b"2", b"/r/b/c"),
            b"root\t3:0\t/r/.git/config".to_vec(),
        ];
        let refs: Vec<&[u8]> = rows.iter().map(Vec::as_slice).collect();
        let records = parse_records(META, &contexts(&refs), b"/r", Some(3)).unwrap();
        assert_eq!(records.cwd.as_deref(), Some(&b"/r"[..]));
        assert_eq!(records.contexts.len(), 4);
        assert_eq!(records.contexts[3].depth, 2);
        assert_eq!(
            records.contexts[0].held.as_deref(),
            Some("38a62dbe66b7405333fcf620e0333e5897c6ec20")
        );
        assert_eq!(records.ancestors(3), Some(vec![0, 2, 3]));
        assert_eq!(records.ancestors(0), Some(vec![0]));
        assert_eq!(records.ancestors(4), None);
    }

    #[test]
    fn records_check_in_the_legacy_order() {
        let good = base_row(b"0", b"-1", b"0", b"/r");
        // cwd の不一致は、contexts の誤りより先。
        assert_eq!(
            parse_records(META, b"broken", b"/x", Some(0)),
            Err(STATE_BINDING.into())
        );
        // 作業場所の実パスが `/` で始まらない(旧版の比較器では届かない分岐)。
        assert_eq!(
            parse_records(b"toplevel\tr\ncwd\tr\n", &contexts(&[&good]), b"r", Some(0)),
            Err(STATE_BINDING.into())
        );
        assert_eq!(
            parse_records(b"cwd\t/r\ncwd\t/r\n", &contexts(&[&good]), b"/r", Some(0)),
            schema()
        );
        assert_eq!(
            parse_records(b"cwd /r\n", &contexts(&[&good]), b"/r", Some(0)),
            schema()
        );
        assert_eq!(
            parse_records(META, b"", b"/r", Some(0)),
            Err(OLD_STATE.into())
        );
        assert_eq!(
            parse_records(META, b"context-plan-v0\n", b"/r", Some(0)),
            Err(OLD_STATE.into())
        );
        // LF で終わらない記録は、形式名より先に `state-schema`。
        assert_eq!(
            parse_records(META, b"context-plan-v0", b"/r", Some(0)),
            schema()
        );
        assert_eq!(
            parse_records(META, &contexts(&[]), b"/r", Some(0)),
            schema()
        );
        let ok = |rows: &[&[u8]], id: Option<u64>| parse_records(META, &contexts(rows), b"/r", id);
        assert!(ok(&[&good], Some(0)).is_ok());
        assert_eq!(ok(&[&good], Some(1)), Err(CONTEXT_BINDING.into()));
        assert_eq!(ok(&[&good], None), Err(CONTEXT_BINDING.into()));
        let inactive = context_row(&[
            b"0", b"-1", b"0", b"0", b"/r", b"-", b"-", b"sha1", b"files", b"-", b"0", b"none",
            b"-", b"-",
        ]);
        assert_eq!(ok(&[&inactive], Some(0)), Err(CONTEXT_BINDING.into()));
        let cases: Vec<(Vec<u8>, Error)> = vec![
            (base_row(b"1", b"-1", b"0", b"/r"), SCHEMA.into()),
            (base_row(b"0", b"0", b"0", b"/r"), SCHEMA.into()),
            (base_row(b"0", b"-1", b"1", b"/r"), SCHEMA.into()),
            (base_row(b"0", b"-1", b"0", b"r"), SCHEMA.into()),
            (base_row(b"0", b"-1", b"0", b"/x"), SCHEMA.into()),
            (base_row(b"2000", b"-1", b"0", b"/r"), SCHEMA.into()),
            (base_row(b"0", b"-2", b"0", b"/r"), SCHEMA.into()),
            (base_row(b"0", b"-1", b"41", b"/r"), SCHEMA.into()),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"2", b"/r", b"-", b"-", b"sha1", b"files", b"-", b"0",
                    b"none", b"-", b"-",
                ]),
                SCHEMA.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"/r", b"-", b"-", b"md5", b"files", b"-", b"0",
                    b"none", b"-", b"-",
                ]),
                UNSUPPORTED_FORMAT.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"/r", b"-", b"-", b"sha1", b"loose", b"-", b"0",
                    b"none", b"-", b"-",
                ]),
                UNSUPPORTED_FORMAT.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"/r", b"g", b"-", b"sha1", b"files", b"-", b"0",
                    b"none", b"-", b"-",
                ]),
                SCHEMA.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"/r", b"-", b"-", b"sha256", b"files", OID, b"0",
                    b"none", b"-", b"-",
                ]),
                SCHEMA.into(),
            ),
            (
                context_row(&[
                    b"0",
                    b"-1",
                    b"0",
                    b"1",
                    b"/r",
                    b"-",
                    b"-",
                    b"sha1",
                    b"files",
                    b"-",
                    b"16777217",
                    b"none",
                    b"-",
                    b"-",
                ]),
                SCHEMA.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"/r", b"-", b"-", b"sha1", b"files", b"-", b"0",
                    b"tree", b"-", b"-",
                ]),
                SCHEMA.into(),
            ),
            (
                context_row(&[
                    b"0", b"-1", b"0", b"1", b"\"/r", b"-", b"-", b"sha1", b"files", b"-", b"0",
                    b"none", b"-", b"-",
                ]),
                SCHEMA.into(),
            ),
            (b"context\t0".to_vec(), SCHEMA.into()),
            (b"other\tx".to_vec(), SCHEMA.into()),
        ];
        for (row, expected) in cases {
            assert_eq!(
                ok(&[&row], Some(0)),
                Err(expected),
                "{:?}",
                String::from_utf8_lossy(&row)
            );
        }
        // 兄弟の context は同じ親を持てる。直前でない正しい親も通る。
        let rows = [
            good.clone(),
            base_row(b"1", b"0", b"1", b"/r/a"),
            base_row(b"2", b"1", b"2", b"/r/a/b"),
            base_row(b"3", b"0", b"1", b"/r/c"),
        ];
        let refs: Vec<&[u8]> = rows.iter().map(Vec::as_slice).collect();
        assert!(ok(&refs, Some(3)).is_ok());
        let wrong_depth = [good.clone(), base_row(b"1", b"0", b"2", b"/r/a")];
        let refs: Vec<&[u8]> = wrong_depth.iter().map(Vec::as_slice).collect();
        assert_eq!(ok(&refs, Some(0)), schema());
        for root in [
            &b"root\t0\t/x"[..],
            b"root\t0:1\t/x",
            b"root\t0:0\tx",
            b"root\t0:0:0\t/x",
            b"root\ta:0\t/x",
        ] {
            assert_eq!(ok(&[&good, root], Some(0)), schema(), "{root:?}");
        }
        let mut digits = b"0".repeat(4301);
        digits.insert(0, b'0');
        let huge = base_row(&digits, b"-1", b"0", b"/r");
        assert_eq!(ok(&[&huge], Some(0)), Err(Error::Value));
    }

    #[test]
    fn origins_read_groups_and_admin() {
        let raw = b"config-origins-v3\nenv\tHOME\tset\tx\nplan-admin\t/a\t\"/b\\n\"\nplan-root\t/c\ncandidate\t/d\nnode\td\t1:2\t-\t/\ndata\tabc\ncandidate\t/e\nmissing\t/e\n";
        let origins = parse_origins(raw).unwrap();
        assert_eq!(origins.admin, (b"/a".to_vec(), b"/b\n".to_vec()));
        assert_eq!(origins.groups.len(), 2);
        assert_eq!(origins.groups[0].0, b"/d");
        assert_eq!(
            origins.groups[0].1,
            vec![
                vec![
                    b"node".to_vec(),
                    b"d".to_vec(),
                    b"1:2".to_vec(),
                    b"-".to_vec(),
                    b"/".to_vec()
                ],
                vec![b"data".to_vec(), b"abc".to_vec()],
            ]
        );
        assert_eq!(
            origins.groups[1].1,
            vec![vec![b"missing".to_vec(), b"/e".to_vec()]]
        );
    }

    #[test]
    fn origins_reject_each_row() {
        let head = b"config-origins-v3\nplan-admin\t/a\t/b\n";
        assert_eq!(parse_origins(b""), Err(OLD_STATE.into()));
        assert_eq!(parse_origins(b"config-origins-v2\n"), Err(OLD_STATE.into()));
        assert_eq!(parse_origins(b"config-origins-v3\n"), Err(OLD_STATE.into()));
        assert_eq!(parse_origins(b"config-origins-v3"), schema());
        for extra in [
            &b"node\td\t1\t-\t/\n"[..],
            b"candidate\t/x\nnode\td\t1\t-\n",
            b"candidate\t/x\nmissing\n",
            b"candidate\t/x\ncandidate\t/x\n",
            b"candidate\t\"x\n",
            b"plan-admin\t/a\t/b\n",
            b"plan-admin\t/a\n",
            b"plan-root\t\"x\n",
            b"env\tA\tB\n",
            b"unknown\n",
            b"\n",
        ] {
            let raw = [&head[..], extra].concat();
            assert_eq!(
                parse_origins(&raw),
                schema(),
                "{:?}",
                String::from_utf8_lossy(extra)
            );
        }
    }
}
