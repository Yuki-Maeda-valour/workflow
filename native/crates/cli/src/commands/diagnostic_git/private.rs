//! 参照の名前・参照のファイルの中身・参照の解決・`HEAD`・REV の object の名前(R4D-3)。
//! 旧版 `diagnostic-private.py` の `valid_ref`(:13-17)・`Private.oid`(:119-133)・`Private.refs`(:143-173)・
//! `Private.file_refs` の中身の読み取り(:177-183・:191-194)・`Private.head`(:196-212)と同じ順で処理する。
//!
//! ファイルを開いて読む所・フォルダをたどる所・2 回目からの参照の解決で前の結果を返す所・子の起動は R4D-3b(#544)が受け持つ。
//! 子の起動と参照の解決の結果は trait で受け取る。予算は呼出元のものを毎回 `&mut Budget` で受け取り、作り直さない。
use super::budget::{Budget, Failure};
use super::paths::encode_utf8;
use super::state::{Error, is_lower_hex};
use dev_workflow_core::loop_state::CodepointString;
use std::collections::{HashMap, HashSet};

const INVALID_REF: Failure = Failure::new("invalid-ref", 23);
const SYMBOLIC_REF_CYCLE: Failure = Failure::new("symbolic-ref-cycle", 23);
const INCOMPLETE_METADATA: Failure = Failure::new("incomplete-metadata", 23);
const REVISION_UNAVAILABLE: Failure = Failure::new("revision-unavailable", 23);
const GIT_FAILED: Failure = Failure::new("git-failed", 23);

/// `rev-parse` の標準出力の上限(旧版の `limit=4096`)。
pub const REV_PARSE_LIMIT: u64 = 4096;
/// 1 つの参照のファイルと `HEAD` から呼出元が読む頭のバイト数(旧版の `read(4097)`)。
pub const LOOSE_REF_HEAD: usize = 4097;

fn contains(raw: &[u8], needle: &[u8]) -> bool {
    raw.windows(needle.len()).any(|window| window == needle)
}

/// 参照の名前の文法(旧版の `valid_ref`)。
pub fn valid_ref(name: &[u8]) -> bool {
    name.starts_with(b"refs/")
        && name.len() <= 1024
        && !name.ends_with(b"/")
        && !name.ends_with(b".")
        && !contains(name, b"..")
        && !contains(name, b"@{")
        && !name
            .iter()
            .any(|&c| c < 32 || c == 127 || b" ~^:?*[\\".contains(&c))
        && name
            .split(|&c| c == b'/')
            .all(|part| !part.is_empty() && !part.starts_with(b".") && !part.ends_with(b".lock"))
}

/// 参照の値(行き着く object の名前か、指す先の参照の名前)。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RefValue {
    pub oid: Option<String>,
    pub target: Option<Vec<u8>>,
}

/// 参照の値の列。名前 → 値を入れた順に持つ。同じ名前を入れると、位置を保って値を置き換える(Python の dict と同じ)。
/// 名前から位置の索引を持ち、引く手間を平均で定数にする。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RefMap {
    index: HashMap<Vec<u8>, usize>,
    entries: Vec<(Vec<u8>, RefValue)>,
}

impl RefMap {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, name: Vec<u8>, oid: Option<String>, target: Option<Vec<u8>>) {
        let value = RefValue { oid, target };
        if let Some(&position) = self.index.get(&name) {
            self.entries[position].1 = value;
        } else {
            self.index.insert(name.clone(), self.entries.len());
            self.entries.push((name, value));
        }
    }

    pub fn get(&self, name: &[u8]) -> Option<&RefValue> {
        self.index
            .get(name)
            .map(|&position| &self.entries[position].1)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&[u8], &RefValue)> {
        self.entries
            .iter()
            .map(|(name, value)| (name.as_slice(), value))
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// `bytes.splitlines()` と同じ分け方(LF・CR・CRLF だけで分け、末尾の区切りの後に空の要素を作らない)。
fn splitlines(raw: &[u8]) -> Vec<&[u8]> {
    let mut rows = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < raw.len() {
        match raw[i] {
            b'\n' => {
                rows.push(&raw[start..i]);
                i += 1;
                start = i;
            }
            b'\r' => {
                rows.push(&raw[start..i]);
                i += if raw.get(i + 1) == Some(&b'\n') { 2 } else { 1 };
                start = i;
            }
            _ => i += 1,
        }
    }
    if start < raw.len() {
        rows.push(&raw[start..]);
    }
    rows
}

/// `bytes.strip()` と `bytes.split()` の空白(空白・TAB・LF・VT・FF・CR。0x1c〜0x1f は空白でない)。
fn is_space(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

fn strip(raw: &[u8]) -> &[u8] {
    let start = raw.iter().position(|&c| !is_space(c)).unwrap_or(raw.len());
    let end = raw
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |p| p + 1);
    &raw[start..end]
}

fn split_whitespace(raw: &[u8]) -> Vec<&[u8]> {
    raw.split(|&c| is_space(c))
        .filter(|part| !part.is_empty())
        .collect()
}

fn rstrip_lf(raw: &[u8]) -> &[u8] {
    let end = raw.iter().rposition(|&c| c != b'\n').map_or(0, |p| p + 1);
    &raw[..end]
}

fn ascii_text(raw: &[u8]) -> String {
    raw.iter().map(|&c| char::from(c)).collect()
}

/// 旧版の `read(4097)` と同じく、頭の 4097 バイトだけを見る(呼出元がそれより長く渡しても切る)。
fn loose_head(raw: &[u8]) -> &[u8] {
    &raw[..raw.len().min(LOOSE_REF_HEAD)]
}

/// 形式の幅の小文字 16 進と、あれば LF 1 つだけ(旧版の `[0-9a-f]{width}\n?` の fullmatch)。
fn oid_line(raw: &[u8], width: usize) -> Option<&[u8]> {
    let body = raw.strip_suffix(b"\n").unwrap_or(raw);
    is_lower_hex(body, width).then_some(body)
}

/// `packed-refs` の中身を読み、`into` に入れる(旧版の `file_refs` の :178-183)。各行の先に予算を確かめる。
pub fn parse_packed_refs(
    raw: &[u8],
    width: usize,
    budget: &mut Budget,
    into: &mut RefMap,
) -> Result<(), Failure> {
    for line in splitlines(raw) {
        budget.check()?;
        if line.starts_with(b"#") || line.starts_with(b"^") {
            continue;
        }
        let fields: Vec<&[u8]> = line.split(|&c| c == b' ').collect();
        if fields.len() != 2 || !is_lower_hex(fields[0], width) {
            return Err(INVALID_REF);
        }
        into.insert(fields[1].to_vec(), Some(ascii_text(fields[0])), None);
    }
    Ok(())
}

/// 1 つの参照のファイルの中身を読む(旧版の `file_refs` の :191-194)。`raw` は頭の 4097 バイトだけを見る(長ければ切る)。
/// 戻り値は (object の名前, 指す先)。
pub fn parse_loose_ref(
    raw: &[u8],
    width: usize,
) -> Result<(Option<String>, Option<Vec<u8>>), Failure> {
    let raw = loose_head(raw);
    if let Some(rest) = raw.strip_prefix(b"ref: ") {
        return Ok((None, Some(rstrip_lf(rest).to_vec())));
    }
    match oid_line(raw, width) {
        Some(body) => Ok((Some(ascii_text(body)), None)),
        None => Err(INVALID_REF),
    }
}

/// 子の起動に渡す引数。`Text` は R4D-3b の本物の実装が UTF-8 と surrogateescape で bytes にする。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GitArg {
    Bytes(Vec<u8>),
    Text(CodepointString),
}

impl GitArg {
    /// 引数の bytes(旧版の `subprocess.Popen` と同じく、`Text` は UTF-8 と surrogateescape で変換する)。
    /// U+DC80〜U+DCFF は 0x80〜0xFF の 1 バイトになり、ほかのサロゲートは `Error::Unicode`。
    pub fn to_bytes(&self) -> Result<Vec<u8>, Error> {
        match self {
            GitArg::Bytes(raw) => Ok(raw.clone()),
            GitArg::Text(text) => {
                let mut out = Vec::with_capacity(text.as_codepoints().len());
                let mut buffer = [0_u8; 4];
                for &point in text.as_codepoints() {
                    if (0xdc80..=0xdcff).contains(&point) {
                        out.push((point - 0xdc00) as u8);
                        continue;
                    }
                    let character = char::from_u32(point).ok_or(Error::Unicode)?;
                    out.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
                }
                Ok(out)
            }
        }
    }
}

/// 子の起動(git の場所と Git の安全用のオプションの並び `SAFE` は、実装が前に付ける)。
///
/// 契約(R4D-3b の本物の実装が守る): 旧版の `rt.process` と同じく、最初に `budget.check()` をし、次に `Text` の引数を
/// UTF-8 と surrogateescape で bytes にする(変換できなければ `Error::Unicode`)。起動の失敗は `Error::Os`。
/// `input` は標準入力(無しは空)、`limit` は標準出力の上限(無しは既定)。終了値の許す範囲の確かめは呼ぶ側が行う。
pub trait GitQuery {
    fn run(
        &mut self,
        budget: &mut Budget,
        args: &[GitArg],
        input: Option<&[u8]>,
        limit: Option<u64>,
    ) -> Result<(i32, Vec<u8>), Error>;
}

/// 参照の解決の 1 件(旧版の `ref_records` の 1 項目)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RefRecord {
    pub name: Vec<u8>,
    /// 行き着いた object の名前。
    pub oid: Option<String>,
    /// 指す先(シンボリック参照のとき)。
    pub target: Option<Vec<u8>>,
    /// 旧版の `resolution` が `resolved` か(行き着いた object の名前が空でない文字列)。
    pub resolved: bool,
}

/// 参照の解決の結果。記録は名前の bytes の順。`head` は取り出した `HEAD` の値(旧版の `head_value`)。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Resolution {
    pub records: Vec<RefRecord>,
    pub head: Option<RefValue>,
}

impl Resolution {
    /// 記録を前から見て、名前が同じ最初の記録の object の名前(旧版の `next((r['oid'] for r in records if …), None)`)。
    fn oid_of(&self, name: &[u8]) -> Option<String> {
        self.records
            .iter()
            .find(|record| record.name == name)
            .and_then(|record| record.oid.clone())
    }
}

/// 参照の解決の結果を受け取る。R4D-3b の本物の実装は、2 回目からは前の結果を返す(旧版の :135)。
pub trait RefSource {
    fn refs(&mut self, budget: &mut Budget) -> Result<Resolution, Error>;
}

/// `HEAD` の値(旧版の `head()` の `symbolic_target_b64` と `oid`)。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Head {
    pub target: Option<Vec<u8>>,
    pub oid: Option<String>,
}

/// REV の object の名前を求めるときに使う部品(子の起動・参照の解決・`HEAD`)。
pub trait OidSource: GitQuery + RefSource {
    fn head(&mut self, budget: &mut Budget) -> Result<Head, Error>;
}

fn private_name(name: &[u8]) -> bool {
    name.starts_with(b"refs/bisect/")
        || name.starts_with(b"refs/worktree/")
        || name.starts_with(b"refs/rewritten/")
}

/// `[A-Z][A-Z0-9_]*` に全体が合う名前(`FETCH_HEAD` など)。
fn pseudo_ref(name: &[u8]) -> bool {
    matches!(name.first(), Some(b'A'..=b'Z'))
        && name[1..]
            .iter()
            .all(|c| matches!(c, b'A'..=b'Z' | b'0'..=b'9' | b'_'))
}

/// 参照を解決する(旧版の `Private.refs` の :143-173)。`common` は共通の gitdir の値、`local` は gitdir の値、
/// `same_dir` は gitdir と共通の gitdir が同じか。各名前の先に予算を確かめ、行き着いた名前が 1 つ以上あるときだけ
/// `cat-file --batch-check` で種類を確かめる。
pub fn resolve_refs(
    common: &RefMap,
    local: &RefMap,
    same_dir: bool,
    budget: &mut Budget,
    git: &mut impl GitQuery,
) -> Result<Resolution, Error> {
    let mut values = RefMap::new();
    for (name, value) in common.iter().filter(|(name, _)| !private_name(name)) {
        values.insert(name.to_vec(), value.oid.clone(), value.target.clone());
    }
    if same_dir {
        for (name, value) in common.iter() {
            values.insert(name.to_vec(), value.oid.clone(), value.target.clone());
        }
    } else {
        for (name, value) in local
            .iter()
            .filter(|(name, _)| private_name(name) || *name == b"HEAD")
        {
            values.insert(name.to_vec(), value.oid.clone(), value.target.clone());
        }
    }
    let head = values.get(b"HEAD").cloned();
    let entries: Vec<(Vec<u8>, RefValue)> = values
        .entries
        .into_iter()
        .filter(|(name, _)| name != b"HEAD" && !pseudo_ref(name))
        .collect();
    let position: HashMap<&[u8], usize> = entries
        .iter()
        .enumerate()
        .map(|(index, (name, _))| (name.as_slice(), index))
        .collect();
    for (name, value) in &entries {
        if !valid_ref(name)
            || value
                .target
                .as_deref()
                .is_some_and(|target| !valid_ref(target))
        {
            return Err(INVALID_REF.into());
        }
    }
    // 位置ごとの解決の結果。外側の None は「まだ解決していない」、内側は行き着いた object の名前。
    let mut resolved: Vec<Option<Option<String>>> = vec![None; entries.len()];
    for start in 0..entries.len() {
        budget.check()?;
        let mut chain = Vec::new();
        let mut visited = HashSet::new();
        let mut current: &[u8] = &entries[start].0;
        let value = loop {
            let Some(&index) = position.get(current) else {
                break None;
            };
            if let Some(done) = &resolved[index] {
                break done.clone();
            }
            if !visited.insert(index) {
                return Err(SYMBOLIC_REF_CYCLE.into());
            }
            chain.push(index);
            let entry = &entries[index].1;
            match &entry.target {
                None => {
                    resolved[index] = Some(entry.oid.clone());
                    break entry.oid.clone();
                }
                Some(target) => current = target,
            }
        };
        for &index in chain.iter().rev() {
            resolved[index] = Some(value.clone());
        }
    }
    let mut ids: Vec<&str> = resolved
        .iter()
        .filter_map(|value| value.as_ref().and_then(|oid| oid.as_deref()))
        .collect();
    ids.sort_unstable();
    ids.dedup();
    if !ids.is_empty() {
        let mut input = ids.join("\n").into_bytes();
        input.push(b'\n');
        let args = [
            GitArg::Bytes(b"cat-file".to_vec()),
            GitArg::Bytes(b"--batch-check=%(objectname) %(objecttype)".to_vec()),
        ];
        let (rc, checked) = git.run(budget, &args, Some(&input), None)?;
        let rows = splitlines(&checked);
        if rc != 0 || rows.len() != ids.len() {
            return Err(INCOMPLETE_METADATA.into());
        }
        for (oid, row) in ids.iter().zip(rows) {
            let fields = split_whitespace(row);
            if fields.len() != 2
                || fields[0] != oid.as_bytes()
                || !matches!(fields[1], b"blob" | b"tree" | b"commit" | b"tag")
            {
                return Err(INCOMPLETE_METADATA.into());
            }
        }
    }
    let mut order: Vec<usize> = (0..entries.len()).collect();
    order.sort_unstable_by(|&left, &right| entries[left].0.cmp(&entries[right].0));
    let records = order
        .into_iter()
        .map(|index| {
            let oid = resolved[index].clone().flatten();
            RefRecord {
                name: entries[index].0.clone(),
                resolved: oid.as_deref().is_some_and(|text| !text.is_empty()),
                oid,
                target: entries[index].1.target.clone(),
            }
        })
        .collect();
    Ok(Resolution { records, head })
}

/// files の `HEAD`(旧版の `Private.head` の :205-212)。`raw` は頭の 4097 バイトだけを見る(長ければ切る)。
/// `ref: ` の指す先が `valid_ref` を通ったときだけ参照の解決を行う。object の名前の `HEAD` では行わない。
pub fn head_from_file(
    raw: &[u8],
    width: usize,
    budget: &mut Budget,
    refs: &mut impl RefSource,
) -> Result<Head, Error> {
    let raw = loose_head(raw);
    if let Some(rest) = raw.strip_prefix(b"ref: ") {
        let target = rstrip_lf(rest).to_vec();
        if !valid_ref(&target) {
            return Err(INVALID_REF.into());
        }
        let oid = refs.refs(budget)?.oid_of(&target);
        return Ok(Head {
            target: Some(target),
            oid,
        });
    }
    match oid_line(raw, width) {
        Some(body) => Ok(Head {
            target: None,
            oid: Some(ascii_text(body)),
        }),
        None => Err(INVALID_REF.into()),
    }
}

/// reftable の `HEAD`(旧版の `Private.head` の :198-204)。呼出元が先に参照の解決を行う(旧版の :198 と同じ順)。
/// 指す先が無ければ、取り出した `HEAD` の object の名前をそのまま使う(幅も確かめず、解決の結果も引かない)。
pub fn head_from_reftable(resolution: &Resolution) -> Result<Head, Failure> {
    let Some(head) = &resolution.head else {
        return Err(INCOMPLETE_METADATA);
    };
    match &head.target {
        Some(target) => {
            if !valid_ref(target) {
                return Err(INVALID_REF);
            }
            Ok(Head {
                target: Some(target.clone()),
                oid: resolution.oid_of(target),
            })
        }
        None => Ok(Head {
            target: None,
            oid: head.oid.clone(),
        }),
    }
}

fn is_text(value: &CodepointString, text: &str) -> bool {
    value
        .as_codepoints()
        .iter()
        .copied()
        .eq(text.chars().map(u32::from))
}

fn starts_with_text(value: &CodepointString, text: &str) -> bool {
    let points = value.as_codepoints();
    let prefix: Vec<u32> = text.chars().map(u32::from).collect();
    points.starts_with(&prefix)
}

/// REV の object の名前を求める(旧版の `Private.oid` の :119-133)。`HEAD` は `head` の object の名前、
/// `refs/` で始まる値は参照の解決の結果の名前で引き、ほかはそのまま `rev-parse --verify` へ渡す。
pub fn oid(
    value: &CodepointString,
    unborn: bool,
    width: usize,
    budget: &mut Budget,
    source: &mut impl OidSource,
) -> Result<Option<String>, Error> {
    let original_head = is_text(value, "HEAD");
    let replaced = if original_head {
        source
            .head(budget)?
            .oid
            .map(|oid| CodepointString::from(oid.as_str()))
    } else if starts_with_text(value, "refs/") {
        let resolution = source.refs(budget)?;
        // 旧版は最初の記録と比べる所で初めて value.encode() を行う(記録が 0 件なら変換しない)。
        if resolution.records.is_empty() {
            None
        } else {
            let name = encode_utf8(value)?;
            resolution
                .oid_of(&name)
                .map(|oid| CodepointString::from(oid.as_str()))
        }
    } else {
        Some(value.clone())
    };
    let Some(current) = replaced else {
        if unborn && original_head {
            return Ok(None);
        }
        return Err(REVISION_UNAVAILABLE.into());
    };
    let mut points = current.as_codepoints().to_vec();
    points.extend("^{commit}".chars().map(u32::from));
    // 元の値も足す文字も U+10FFFF 以下なので失敗しない(失敗の口は型の都合で残す)。
    let Ok(argument) = CodepointString::try_from_codepoints(points) else {
        return Err(Error::Unicode);
    };
    let args = [
        GitArg::Bytes(b"rev-parse".to_vec()),
        GitArg::Bytes(b"--verify".to_vec()),
        GitArg::Bytes(b"--quiet".to_vec()),
        GitArg::Bytes(b"--end-of-options".to_vec()),
        GitArg::Text(argument),
    ];
    let (rc, raw) = source.run(budget, &args, None, Some(REV_PARSE_LIMIT))?;
    if !matches!(rc, 0 | 1 | 128) {
        return Err(GIT_FAILED.into());
    }
    if rc != 0 {
        // 旧版の :129 を字面どおりに写す(置き換えた後の値が `HEAD` のときだけ `head` を呼ぶ)。
        if unborn && is_text(&current, "HEAD") && source.head(budget)?.oid.is_none() {
            return Ok(None);
        }
        return Err(REVISION_UNAVAILABLE.into());
    }
    let stripped = strip(&raw);
    if !stripped.is_ascii() {
        return Err(Error::Unicode);
    }
    if !is_lower_hex(stripped, width) {
        return Err(REVISION_UNAVAILABLE.into());
    }
    Ok(Some(ascii_text(stripped)))
}

#[cfg(test)]
mod tests {
    use super::super::budget::Kind;
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    fn fresh() -> (Budget, Rc<Cell<u64>>) {
        let calls = Rc::new(Cell::new(0_u64));
        let counter = Rc::clone(&calls);
        let clock = Box::new(move || {
            counter.set(counter.get() + 1);
            0.0
        });
        (
            Budget::for_test(300.0, clock, Kind::ALL.map(Kind::default_limit)),
            calls,
        )
    }

    fn failure(reason: &'static str, code: u8) -> Error {
        Error::Failure(Failure::new(reason, code))
    }

    const A: &str = "1111111111111111111111111111111111111111";
    const B: &str = "2222222222222222222222222222222222222222";
    const C: &str = "abcdef0123456789abcdef0123456789abcdef01";

    type Response = Result<(i32, Vec<u8>), Error>;
    /// 受けた引数の bytes・標準入力・上限。
    type Call = (Vec<Vec<u8>>, Option<Vec<u8>>, Option<u64>);

    #[derive(Default)]
    struct Fake {
        responses: Vec<Response>,
        calls: Vec<Call>,
        refs: Option<Result<Resolution, Error>>,
        refs_calls: u32,
        head: Option<Result<Head, Error>>,
        head_calls: u32,
    }

    impl GitQuery for Fake {
        fn run(
            &mut self,
            _budget: &mut Budget,
            args: &[GitArg],
            input: Option<&[u8]>,
            limit: Option<u64>,
        ) -> Result<(i32, Vec<u8>), Error> {
            let mut raw = Vec::new();
            for arg in args {
                raw.push(arg.to_bytes()?);
            }
            self.calls.push((raw, input.map(<[u8]>::to_vec), limit));
            if self.responses.is_empty() {
                Ok((0, Vec::new()))
            } else {
                self.responses.remove(0)
            }
        }
    }

    impl RefSource for Fake {
        fn refs(&mut self, _budget: &mut Budget) -> Result<Resolution, Error> {
            self.refs_calls += 1;
            self.refs
                .clone()
                .unwrap_or_else(|| Ok(Resolution::default()))
        }
    }

    impl OidSource for Fake {
        fn head(&mut self, _budget: &mut Budget) -> Result<Head, Error> {
            self.head_calls += 1;
            self.head.clone().unwrap_or_else(|| Ok(Head::default()))
        }
    }

    /// (名前, object の名前, 指す先)。
    type Item<'a> = (&'a [u8], Option<&'a str>, Option<&'a [u8]>);

    fn map(items: &[Item<'_>]) -> RefMap {
        let mut out = RefMap::new();
        for (name, oid, target) in items {
            out.insert(
                name.to_vec(),
                oid.map(str::to_owned),
                target.map(<[u8]>::to_vec),
            );
        }
        out
    }

    #[test]
    fn valid_ref_checks_each_rule() {
        assert!(valid_ref(b"refs/heads/main"));
        assert!(valid_ref(
            &[b"refs/".as_slice(), &b"a".repeat(1019)].concat()
        ));
        assert!(!valid_ref(
            &[b"refs/".as_slice(), &b"a".repeat(1020)].concat()
        ));
        for bad in [
            &b"heads/main"[..],
            b"refs/",
            b"refs/a.",
            b"refs/a..b",
            b"refs/a@{",
            b"refs//a",
            b"refs/.a",
            b"refs/a.lock",
            b"refs/a b",
            b"refs/a\x7f",
            b"refs/a\x00",
            b"refs/a\\",
            b"refs/a[",
        ] {
            assert!(!valid_ref(bad), "{bad:?}");
        }
        assert!(valid_ref(b"refs/a\xff"));
    }

    #[test]
    fn ref_map_keeps_dict_order() {
        let mut values = map(&[(b"b", Some(A), None), (b"a", Some(A), None)]);
        values.insert(b"b".to_vec(), Some(B.into()), None);
        let names: Vec<&[u8]> = values.iter().map(|(name, _)| name).collect();
        assert_eq!(names, vec![&b"b"[..], b"a"]);
        assert_eq!(values.get(b"b").unwrap().oid.as_deref(), Some(B));
        assert_eq!(values.len(), 2);
        assert!(!values.is_empty());
    }

    #[test]
    fn python_byte_helpers_match_the_legacy_split_rules() {
        assert_eq!(
            splitlines(b"a\nb\r\nc\rd"),
            vec![&b"a"[..], b"b", b"c", b"d"]
        );
        assert_eq!(splitlines(b"a\n"), vec![&b"a"[..]]);
        assert_eq!(splitlines(b"\n\n"), vec![&b""[..], b""]);
        assert_eq!(splitlines(b"a\r\r\n"), vec![&b"a"[..], b""]);
        assert_eq!(splitlines(b""), Vec::<&[u8]>::new());
        assert_eq!(
            splitlines(b"a\x0bb\x0c\x1cc"),
            vec![&b"a\x0bb\x0c\x1cc"[..]]
        );
        assert_eq!(strip(b" \t\n\x0b\x0c\rab \r"), b"ab");
        assert_eq!(strip(b"\x1cab\x1f"), b"\x1cab\x1f");
        assert_eq!(strip(b"  "), b"");
        assert_eq!(
            split_whitespace(b" a\t\x0bb\x1cc  "),
            vec![&b"a"[..], b"b\x1cc"]
        );
    }

    #[test]
    fn packed_refs_check_each_line_first() {
        let (mut budget, calls) = fresh();
        let mut values = RefMap::new();
        let raw = format!("# pack\n{A} refs/heads/a\n^{B}\r\n{B} refs/heads/b\r{A} refs/heads/a\n");
        assert_eq!(
            parse_packed_refs(raw.as_bytes(), 40, &mut budget, &mut values),
            Ok(())
        );
        assert_eq!(calls.get() - 1, 5);
        let names: Vec<&[u8]> = values.iter().map(|(name, _)| name).collect();
        assert_eq!(names, vec![&b"refs/heads/a"[..], b"refs/heads/b"]);
        let bad: [String; 6] = [
            "\n".into(),
            format!("{C}  refs/x\n"),
            format!("{} refs/x\n", C.to_uppercase()),
            format!("{C}\n"),
            format!("{C} refs/x\n {C} refs/y\n"),
            format!("{} refs/x\n", &C[..39]),
        ];
        for raw in bad {
            let (mut budget, _) = fresh();
            let result = parse_packed_refs(raw.as_bytes(), 40, &mut budget, &mut RefMap::new());
            assert_eq!(result, Err(INVALID_REF), "{raw:?}");
        }
        let (mut budget, _) = fresh();
        assert_eq!(
            parse_packed_refs(
                format!("{C} refs/x\n").as_bytes(),
                64,
                &mut budget,
                &mut RefMap::new()
            ),
            Err(INVALID_REF)
        );
        let (mut budget, _) = fresh();
        budget.request_stop();
        assert_eq!(
            parse_packed_refs(b"# x\n", 40, &mut budget, &mut RefMap::new()),
            Err(Failure::new("interrupted", 20))
        );
    }

    #[test]
    fn loose_refs_read_targets_and_object_names() {
        assert_eq!(
            parse_loose_ref(b"ref: refs/heads/a\n\n", 40),
            Ok((None, Some(b"refs/heads/a".to_vec())))
        );
        assert_eq!(
            parse_loose_ref(b"ref: \n", 40),
            Ok((None, Some(Vec::new())))
        );
        assert_eq!(
            parse_loose_ref(format!("{A}\n").as_bytes(), 40),
            Ok((Some(A.into()), None))
        );
        assert_eq!(
            parse_loose_ref(A.as_bytes(), 40),
            Ok((Some(A.into()), None))
        );
        let bad: [String; 6] = [
            format!("{C}\n\n"),
            format!("{C} "),
            format!(" {C}"),
            C.to_uppercase(),
            format!("{C}\r\n"),
            "ref:x".into(),
        ];
        for raw in bad {
            assert_eq!(
                parse_loose_ref(raw.as_bytes(), 40),
                Err(INVALID_REF),
                "{raw:?}"
            );
        }
        assert_eq!(
            parse_loose_ref(format!("{C}\n").as_bytes(), 64),
            Err(INVALID_REF)
        );
    }

    #[test]
    fn loose_refs_and_head_look_only_at_the_first_4097_bytes() {
        // 旧版の read(4097) と同じく、4097 バイトを超える分は見ない。
        let mut long = b"ref: ".to_vec();
        long.extend(b"a".repeat(4092));
        long.push(b'b');
        assert_eq!(long.len(), LOOSE_REF_HEAD + 1);
        assert_eq!(
            parse_loose_ref(&long, 40),
            Ok((None, Some(b"a".repeat(4092))))
        );
        let mut padded = b"ref: refs/heads/a".to_vec();
        padded.extend(b"\n".repeat(5000));
        padded.extend(b"garbage");
        assert_eq!(
            parse_loose_ref(&padded, 40),
            Ok((None, Some(b"refs/heads/a".to_vec())))
        );
        let mut oid_then_text = format!("{C}\n").into_bytes();
        oid_then_text.extend(b"x".repeat(5000));
        assert_eq!(parse_loose_ref(&oid_then_text, 40), Err(INVALID_REF));
        let (mut budget, _) = fresh();
        let mut refs = Fake {
            refs: Some(Ok(Resolution {
                records: vec![record(b"refs/heads/a", Some(A))],
                head: None,
            })),
            ..Fake::default()
        };
        assert_eq!(
            head_from_file(&padded, 40, &mut budget, &mut refs),
            Ok(Head {
                target: Some(b"refs/heads/a".to_vec()),
                oid: Some(A.into())
            })
        );
        assert_eq!(refs.refs_calls, 1);
        let mut oid_padded = C.as_bytes().to_vec();
        oid_padded.extend(b"\n".repeat(5000));
        assert_eq!(
            head_from_file(&oid_padded, 40, &mut budget, &mut refs),
            Err(INVALID_REF.into())
        );
        assert_eq!(refs.refs_calls, 1);
    }

    #[test]
    fn git_arg_bytes_use_surrogateescape() {
        let text = CodepointString::try_from_codepoints(vec![0x61, 0xdc80, 0xdcff, 0xe9]).unwrap();
        assert_eq!(
            GitArg::Text(text).to_bytes(),
            Ok(vec![0x61, 0x80, 0xff, 0xc3, 0xa9])
        );
        for point in [0xd800, 0xdc7f, 0xdd00, 0xdfff] {
            let text = CodepointString::try_from_codepoints(vec![point]).unwrap();
            assert_eq!(GitArg::Text(text).to_bytes(), Err(Error::Unicode));
        }
        assert_eq!(GitArg::Bytes(vec![0xff]).to_bytes(), Ok(vec![0xff]));
    }

    #[test]
    fn resolve_follows_chains_in_dict_order_and_checks_types() {
        let common = map(&[
            (b"refs/heads/b", Some(B), None),
            (b"refs/heads/a", None, Some(b"refs/heads/b")),
            (b"refs/worktree/x", Some(A), None),
            (b"HEAD", None, Some(b"refs/heads/a")),
            (b"FETCH_HEAD", Some(A), None),
            (b"refs/heads/dangling", None, Some(b"refs/heads/none")),
        ]);
        let local = map(&[
            (b"refs/worktree/y", Some(A), None),
            (b"HEAD", Some(A), None),
            (b"refs/heads/z", Some(A), None),
        ]);
        let mut git = Fake {
            responses: vec![Ok((0, format!("{A} tree\n{B} commit\n").into_bytes()))],
            ..Fake::default()
        };
        let (mut budget, calls) = fresh();
        let resolution = resolve_refs(&common, &local, false, &mut budget, &mut git).unwrap();
        assert_eq!(calls.get() - 1, 4);
        assert_eq!(
            resolution.head,
            Some(RefValue {
                oid: Some(A.into()),
                target: None
            })
        );
        let names: Vec<&[u8]> = resolution
            .records
            .iter()
            .map(|r| r.name.as_slice())
            .collect();
        assert_eq!(
            names,
            vec![
                &b"refs/heads/a"[..],
                b"refs/heads/b",
                b"refs/heads/dangling",
                b"refs/worktree/y"
            ]
        );
        assert_eq!(resolution.records[0].oid.as_deref(), Some(B));
        assert!(resolution.records[0].resolved);
        assert!(!resolution.records[2].resolved);
        assert_eq!(git.calls.len(), 1);
        assert_eq!(
            git.calls[0].1.as_deref(),
            Some(format!("{A}\n{B}\n").as_bytes())
        );
        assert_eq!(git.calls[0].2, None);
        // 同じフォルダなら、共有の値をすべて入れ直す(除いた名前は後ろに付く)。
        let mut git = Fake {
            responses: vec![Ok((0, format!("{A} blob\n{B} tag\n").into_bytes()))],
            ..Fake::default()
        };
        let (mut budget, _) = fresh();
        let resolution = resolve_refs(&common, &local, true, &mut budget, &mut git).unwrap();
        assert_eq!(
            resolution.head,
            Some(RefValue {
                oid: None,
                target: Some(b"refs/heads/a".to_vec())
            })
        );
        assert!(
            resolution
                .records
                .iter()
                .any(|r| r.name == b"refs/worktree/x")
        );
    }

    #[test]
    fn resolve_rejects_names_cycles_and_bad_type_rows() {
        let (mut budget, _) = fresh();
        let bad_name = map(&[(b"refs/heads/a..b", Some(A), None)]);
        assert_eq!(
            resolve_refs(
                &bad_name,
                &RefMap::new(),
                true,
                &mut budget,
                &mut Fake::default()
            ),
            Err(INVALID_REF.into())
        );
        let bad_target = map(&[(b"refs/heads/a", None, Some(b"HEAD"))]);
        assert_eq!(
            resolve_refs(
                &bad_target,
                &RefMap::new(),
                true,
                &mut budget,
                &mut Fake::default()
            ),
            Err(INVALID_REF.into())
        );
        let cycle = map(&[
            (b"refs/a", None, Some(b"refs/b")),
            (b"refs/b", None, Some(b"refs/a")),
        ]);
        let (mut budget, calls) = fresh();
        assert_eq!(
            resolve_refs(
                &cycle,
                &RefMap::new(),
                true,
                &mut budget,
                &mut Fake::default()
            ),
            Err(SYMBOLIC_REF_CYCLE.into())
        );
        assert_eq!(calls.get() - 1, 1);
        // 行き着いた名前が無ければ cat-file を起動しない。
        let mut git = Fake::default();
        let (mut budget, _) = fresh();
        let dangling = map(&[(b"refs/a", None, Some(b"refs/b"))]);
        assert!(resolve_refs(&dangling, &RefMap::new(), true, &mut budget, &mut git).is_ok());
        assert!(git.calls.is_empty());
        let one = map(&[(b"refs/a", Some(A), None)]);
        for (rc, out) in [
            (1, format!("{A} commit\n")),
            (0, String::new()),
            (0, format!("{A} commit\n{A} commit\n")),
            (0, format!("{A} commit x\n")),
            (0, format!("{B} commit\n")),
            (0, format!("{A} object\n")),
            (0, format!("{A}\rcommit\n")),
        ] {
            let mut git = Fake {
                responses: vec![Ok((rc, out.clone().into_bytes()))],
                ..Fake::default()
            };
            let (mut budget, _) = fresh();
            assert_eq!(
                resolve_refs(&one, &RefMap::new(), true, &mut budget, &mut git),
                Err(INCOMPLETE_METADATA.into()),
                "{rc} {out:?}"
            );
        }
        for out in [format!("{A}\tcommit\n"), format!(" {A}  commit \r\n")] {
            let mut git = Fake {
                responses: vec![Ok((0, out.into_bytes()))],
                ..Fake::default()
            };
            let (mut budget, _) = fresh();
            assert!(resolve_refs(&one, &RefMap::new(), true, &mut budget, &mut git).is_ok());
        }
        let mut git = Fake {
            responses: vec![Err(Error::Os(2))],
            ..Fake::default()
        };
        let (mut budget, _) = fresh();
        assert_eq!(
            resolve_refs(&one, &RefMap::new(), true, &mut budget, &mut git),
            Err(Error::Os(2))
        );
    }

    fn record(name: &[u8], oid: Option<&str>) -> RefRecord {
        RefRecord {
            name: name.to_vec(),
            oid: oid.map(str::to_owned),
            target: None,
            resolved: oid.is_some(),
        }
    }

    #[test]
    fn heads_follow_the_legacy_order() {
        let (mut budget, _) = fresh();
        let mut refs = Fake {
            refs: Some(Ok(Resolution {
                records: vec![record(b"refs/heads/a", Some(A))],
                head: None,
            })),
            ..Fake::default()
        };
        assert_eq!(
            head_from_file(b"ref: refs/heads/a\n", 40, &mut budget, &mut refs),
            Ok(Head {
                target: Some(b"refs/heads/a".to_vec()),
                oid: Some(A.into())
            })
        );
        assert_eq!(
            head_from_file(b"ref: refs/heads/x\n", 40, &mut budget, &mut refs),
            Ok(Head {
                target: Some(b"refs/heads/x".to_vec()),
                oid: None
            })
        );
        assert_eq!(refs.refs_calls, 2);
        let mut failing = Fake {
            refs: Some(Err(failure("symbolic-ref-cycle", 23))),
            ..Fake::default()
        };
        assert_eq!(
            head_from_file(format!("{A}\n").as_bytes(), 40, &mut budget, &mut failing),
            Ok(Head {
                target: None,
                oid: Some(A.into())
            })
        );
        assert_eq!(
            head_from_file(b"ref: heads/a\n", 40, &mut budget, &mut failing),
            Err(INVALID_REF.into())
        );
        assert_eq!(
            head_from_file(b"ref: refs/heads/a\n", 40, &mut budget, &mut failing),
            Err(failure("symbolic-ref-cycle", 23))
        );
        assert_eq!(
            head_from_file(format!("{A}\n").as_bytes(), 64, &mut budget, &mut failing),
            Err(INVALID_REF.into())
        );
        assert_eq!(failing.refs_calls, 1);
        assert_eq!(
            head_from_reftable(&Resolution::default()),
            Err(INCOMPLETE_METADATA)
        );
        let symbolic = Resolution {
            records: vec![record(b"refs/heads/a", Some(A))],
            head: Some(RefValue {
                oid: None,
                target: Some(b"refs/heads/a".to_vec()),
            }),
        };
        assert_eq!(
            head_from_reftable(&symbolic),
            Ok(Head {
                target: Some(b"refs/heads/a".to_vec()),
                oid: Some(A.into())
            })
        );
        let direct = Resolution {
            records: Vec::new(),
            head: Some(RefValue {
                oid: Some("x".into()),
                target: None,
            }),
        };
        assert_eq!(
            head_from_reftable(&direct),
            Ok(Head {
                target: None,
                oid: Some("x".into())
            })
        );
        let invalid = Resolution {
            records: Vec::new(),
            head: Some(RefValue {
                oid: None,
                target: Some(b"HEAD".to_vec()),
            }),
        };
        assert_eq!(head_from_reftable(&invalid), Err(INVALID_REF));
    }

    fn text(value: &str) -> CodepointString {
        CodepointString::from(value)
    }

    #[test]
    fn oid_follows_the_legacy_order() {
        let (mut budget, _) = fresh();
        // 生まれていない HEAD。
        let mut source = Fake::default();
        assert_eq!(
            oid(&text("HEAD"), true, 40, &mut budget, &mut source),
            Ok(None)
        );
        assert_eq!(
            oid(&text("HEAD"), false, 40, &mut budget, &mut source),
            Err(REVISION_UNAVAILABLE.into())
        );
        assert!(source.calls.is_empty());
        // refs/ と記録 0 件は、孤立したサロゲートでも変換しない。
        let lone =
            CodepointString::try_from_codepoints(vec![114, 101, 102, 115, 47, 0xd800]).unwrap();
        assert_eq!(
            oid(&lone, false, 40, &mut budget, &mut source),
            Err(REVISION_UNAVAILABLE.into())
        );
        source.refs = Some(Ok(Resolution {
            records: vec![record(b"refs/x", Some(A))],
            head: None,
        }));
        assert_eq!(
            oid(&lone, false, 40, &mut budget, &mut source),
            Err(Error::Unicode)
        );
        // 16 進はそのまま rev-parse へ渡す。
        let mut source = Fake {
            responses: vec![Ok((0, format!(" {A}\n").into_bytes()))],
            ..Fake::default()
        };
        assert_eq!(
            oid(&text(A), false, 40, &mut budget, &mut source),
            Ok(Some(A.into()))
        );
        assert_eq!(
            source.calls[0].0.last().unwrap(),
            &format!("{A}^{{commit}}").into_bytes()
        );
        assert_eq!(source.calls[0].0[0], b"rev-parse");
        assert_eq!(source.calls[0].1, None);
        assert_eq!(source.calls[0].2, Some(4096));
        for (rc, out, expected) in [
            (129, String::new(), failure("git-failed", 23)),
            (1, String::new(), failure("revision-unavailable", 23)),
            (128, String::new(), failure("revision-unavailable", 23)),
            (0, format!("{A}x"), failure("revision-unavailable", 23)),
            (0, "\u{e9}".to_string(), Error::Unicode),
        ] {
            let mut source = Fake {
                responses: vec![Ok((rc, out.into_bytes()))],
                ..Fake::default()
            };
            assert_eq!(
                oid(&text("main"), false, 40, &mut budget, &mut source),
                Err(expected)
            );
        }
        // 置き換えた後の値が HEAD のときだけ、:129 で head を呼ぶ。
        let mut source = Fake {
            responses: vec![Ok((1, Vec::new()))],
            refs: Some(Ok(Resolution {
                records: vec![record(b"refs/x", Some("HEAD"))],
                head: None,
            })),
            ..Fake::default()
        };
        assert_eq!(
            oid(&text("refs/x"), true, 40, &mut budget, &mut source),
            Ok(None)
        );
        assert_eq!(source.head_calls, 1);
        let mut source = Fake {
            responses: vec![Ok((1, Vec::new()))],
            head: Some(Ok(Head {
                target: None,
                oid: Some(A.into()),
            })),
            ..Fake::default()
        };
        assert_eq!(
            oid(&text("HEAD"), true, 40, &mut budget, &mut source),
            Err(REVISION_UNAVAILABLE.into())
        );
        assert_eq!(source.head_calls, 1);
        // rev-parse の値の孤立したサロゲート: U+DC80 は通り、U+D800 は変換で失敗する。
        let escaped = CodepointString::try_from_codepoints(vec![0xdc80]).unwrap();
        let mut source = Fake {
            responses: vec![Ok((0, format!("{A}\n").into_bytes()))],
            ..Fake::default()
        };
        assert_eq!(
            oid(&escaped, false, 40, &mut budget, &mut source),
            Ok(Some(A.into()))
        );
        assert_eq!(source.calls[0].0.last().unwrap(), b"\x80^{commit}");
        let surrogate = CodepointString::try_from_codepoints(vec![0xd800]).unwrap();
        let mut source = Fake::default();
        assert_eq!(
            oid(&surrogate, false, 40, &mut budget, &mut source),
            Err(Error::Unicode)
        );
        assert!(source.calls.is_empty());
    }
}
