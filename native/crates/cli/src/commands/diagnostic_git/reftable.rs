//! 専用コピーの reftable の ref block を上限付きで読む内部部品。旧版 `diagnostic-reftable.py` と同じ順で処理する。
//!
//! 予算を確かめる位置・読む範囲・失敗の理由の決まり方・閉じる順まで 1 対 1 で合わせる。
//! 元の管理領域を渡さないこと。名前と指す先は bytes のまま返し、名前の文法・シンボリック参照の解決・
//! linked worktree の合成は呼出元の仕事である。log と object の section の本文は解釈しない。
use super::budget::{Budget, Failure, Kind};
use dev_workflow_platform::fs::{FileSnapshot, FsError, HeldDir, PlainFile, lstat_path};
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

const MAX_INPUT: i128 = 200 * 1024 * 1024;
const MAX_REF: i128 = 1024;
const MAX_UINT64: i128 = u64::MAX as i128;
const READ_CHUNK: i128 = 64 * 1024;
const LIST_LINE_MAX: usize = 4096;

/// 合成した参照 1 件。OID は小文字の 16 進。値の種類 1 と 2 は OID、3 は指す先を持つ。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub name: Vec<u8>,
    pub oid: Option<String>,
    pub target: Option<Vec<u8>>,
}

/// 合成の結果。Python の dict と同じ並び(上書きは位置を保つ・削除の後に足すと末尾)で持つ。
/// 名前から位置の表と、位置ごとの記録の列(削除は空き)で、上書きと削除を定数の手間にする。
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Stack {
    index: HashMap<Vec<u8>, usize>,
    entries: Vec<Option<Entry>>,
}

impl Stack {
    pub fn new() -> Self {
        Self::default()
    }

    fn set(&mut self, name: Vec<u8>, oid: Option<String>, target: Option<Vec<u8>>) {
        if let Some(&position) = self.index.get(&name) {
            self.entries[position] = Some(Entry { name, oid, target });
        } else {
            self.index.insert(name.clone(), self.entries.len());
            self.entries.push(Some(Entry { name, oid, target }));
        }
    }

    fn remove(&mut self, name: &[u8]) {
        if let Some(position) = self.index.remove(name) {
            self.entries[position] = None;
        }
    }

    pub fn iter(&self) -> impl Iterator<Item = &Entry> {
        self.entries.iter().flatten()
    }

    pub fn get(&self, name: &[u8]) -> Option<&Entry> {
        self.index
            .get(name)
            .and_then(|&position| self.entries[position].as_ref())
    }

    pub fn len(&self) -> usize {
        self.index.len()
    }

    pub fn is_empty(&self) -> bool {
        self.index.is_empty()
    }
}

/// `read_stack` の失敗。reftable の失敗・予算の失敗・旧版で OSError でない例外になる入力に分ける。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadError {
    /// 理由と終了値(23 か 20)。
    Reftable(Failure),
    /// `interrupted`・`time-limit`・`<量>-limit`(終了値 20)。
    Budget(Failure),
    /// パスに NUL を含む(旧版は `ValueError`)。入口での割り当ては呼出元が行う。
    Other(&'static str),
}

enum Fail {
    Reftable(&'static str, u8),
    Budget(Failure),
    Os(FsError),
}

impl From<Failure> for Fail {
    fn from(failure: Failure) -> Self {
        Fail::Budget(failure)
    }
}

impl From<FsError> for Fail {
    fn from(error: FsError) -> Self {
        Fail::Os(error)
    }
}

type R<T> = Result<T, Fail>;

fn require(condition: bool) -> R<()> {
    require_as(condition, "invalid-reftable")
}

fn require_as(condition: bool, reason: &'static str) -> R<()> {
    if condition {
        Ok(())
    } else {
        Err(Fail::Reftable(reason, 23))
    }
}

fn into_read_error(fail: Fail) -> ReadError {
    match fail {
        Fail::Reftable(reason, code) => ReadError::Reftable(Failure::new(reason, code)),
        Fail::Budget(failure) => ReadError::Budget(failure),
        Fail::Os(error) if error.is_not_found() => {
            ReadError::Reftable(Failure::new("incomplete-metadata", 23))
        }
        Fail::Os(FsError::Io(_)) => ReadError::Reftable(Failure::new("reftable-io", 20)),
        // 名前やパスに NUL など。事前の検査で起きない。旧版の ValueError に当たる。
        Fail::Os(FsError::InvalidInput) => ReadError::Other("ValueError"),
        // 既存の HeldFile 系の失敗は、R4D-1 では使わないので起きない。
        Fail::Os(_) => ReadError::Other("unexpected-fs-error"),
    }
}

/// OS の部品の差し込み口。検査のときに、故障を起こすために使う。製品の経路は `NoHooks` だけを使う。
trait Hooks {
    fn open_plain(&mut self, dir: &HeldDir, name: &OsStr) -> Result<PlainFile, FsError> {
        dir.open_plain(name)
    }
    fn read_at(
        &mut self,
        _budget: &mut Budget,
        file: &PlainFile,
        offset: u64,
        buffer: &mut [u8],
    ) -> Result<usize, FsError> {
        file.read_at(offset, buffer)
    }
    /// 全 table を読み終えた後、確かめ直しの前に呼ぶ。
    fn before_verify(&mut self) {}
    fn close_file(&mut self, file: PlainFile) -> Result<(), FsError> {
        file.close()
    }
    fn close_dir(&mut self, dir: HeldDir) -> Result<(), FsError> {
        dir.close()
    }
}

struct NoHooks;
impl Hooks for NoHooks {}

struct Session<'a, H: Hooks> {
    dir: &'a HeldDir,
    budget: &'a mut Budget,
    hooks: &'a mut H,
}

fn uint(data: &[u8]) -> i128 {
    data.iter()
        .fold(0, |value, byte| (value << 8) | *byte as i128)
}

/// 親のフォルダに結び付けた通常ファイル。読み取りの前後で同じ実体かを確かめる。
struct Input {
    file: Option<PlainFile>,
    name: Vec<u8>,
    initial: FileSnapshot,
    size: i128,
}

impl Input {
    fn open<H: Hooks>(s: &mut Session<'_, H>, name: &OsStr, totals: &mut i128) -> R<Self> {
        s.budget.check()?;
        let before = s.dir.metadata(name)?;
        require_as(before.is_regular(), "invalid-reftable-file")?;
        let file = s.hooks.open_plain(s.dir, name)?;
        match Self::admit(&file, &before, totals) {
            Ok(initial) => Ok(Self {
                file: Some(file),
                name: name.as_bytes().to_vec(),
                initial,
                size: initial.size,
            }),
            Err(error) => {
                // 途中の失敗は fd を閉じてから出す。閉じる失敗は、その失敗に置き換わる。
                s.hooks.close_file(file)?;
                Err(error)
            }
        }
    }

    fn admit(file: &PlainFile, before: &FileSnapshot, totals: &mut i128) -> R<FileSnapshot> {
        let initial = file.snapshot()?;
        require_as(*before == initial, "changed-reftable")?;
        *totals += initial.size;
        if initial.size < 0 || *totals > MAX_INPUT {
            return Err(Fail::Reftable("management-input-limit", 20));
        }
        Ok(initial)
    }

    fn read<H: Hooks>(&self, s: &mut Session<'_, H>, offset: i128, count: i128) -> R<Vec<u8>> {
        s.budget.check()?;
        require(0 <= offset && offset <= self.size && 0 <= count && count <= self.size - offset)?;
        let file = self.file.as_ref().expect("開いたままの table");
        let mut result: Vec<u8> = Vec::new();
        while (result.len() as i128) < count {
            s.budget.check()?;
            let want = READ_CHUNK.min(count - result.len() as i128) as usize;
            let mut buffer = vec![0_u8; want];
            let position = (offset + result.len() as i128) as u64;
            let got = s.hooks.read_at(s.budget, file, position, &mut buffer)?;
            require_as(got != 0, "changed-reftable")?;
            result.extend_from_slice(&buffer[..got]);
        }
        s.budget.check()?;
        Ok(result)
    }

    fn verify<H: Hooks>(&self, s: &mut Session<'_, H>) -> R<()> {
        s.budget.check()?;
        let file = self.file.as_ref().expect("開いたままの table");
        require_as(file.snapshot()? == self.initial, "changed-reftable")?;
        let name = OsStr::from_bytes(&self.name);
        require_as(s.dir.metadata(name)? == self.initial, "changed-reftable")
    }

    fn close<H: Hooks>(mut self, hooks: &mut H) -> Result<(), FsError> {
        match self.file.take() {
            Some(file) => hooks.close_file(file),
            None => Ok(()),
        }
    }
}

/// CRC-32(zlib と同じ多項式)。新しい crate は足さない。
const fn crc_table() -> [u32; 256] {
    let mut table = [0_u32; 256];
    let mut n = 0;
    while n < 256 {
        let mut c = n as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        table[n] = c;
        n += 1;
    }
    table
}

const CRC_TABLE: [u32; 256] = crc_table();

fn crc32(data: &[u8]) -> u32 {
    let mut c = 0xFFFF_FFFF_u32;
    for byte in data {
        c = CRC_TABLE[((c ^ *byte as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

fn hex(data: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut text = String::with_capacity(data.len() * 2);
    for byte in data {
        text.push(DIGITS[(byte >> 4) as usize] as char);
        text.push(DIGITS[(byte & 15) as usize] as char);
    }
    text
}

/// 1 block の restart 配列と、境界を持つ記録の読み取り。
struct Records<'a> {
    data: &'a [u8],
    position: i128,
    end: i128,
    restarts: Vec<i128>,
    restart_index: usize,
    previous: Vec<u8>,
}

impl<'a> Records<'a> {
    fn new(data: &'a [u8], header_size: i128, budget: &mut Budget) -> R<Self> {
        let length = data.len() as i128;
        let position = header_size + 4;
        require(length >= position + 6)?;
        let count = uint(&data[data.len() - 2..]);
        let end = length - 2 - count * 3;
        require(count > 0 && end > position)?;
        let mut restarts = Vec::new();
        let mut previous = -1_i128;
        let mut offset = end;
        while offset < length - 2 {
            budget.check()?;
            let at = offset as usize;
            let value = uint(&data[at..at + 3]);
            require(position <= value && value < end && value > previous)?;
            restarts.push(value);
            previous = value;
            offset += 3;
        }
        require(restarts[0] == position)?;
        Ok(Self {
            data,
            position,
            end,
            restarts,
            restart_index: 0,
            previous: Vec::new(),
        })
    }

    fn take(&mut self, length: i128) -> R<&'a [u8]> {
        require(0 <= length && length <= self.end - self.position)?;
        let start = self.position as usize;
        self.position += length;
        Ok(&self.data[start..self.position as usize])
    }

    fn varint(&mut self) -> R<i128> {
        let mut value: i128 = 0;
        for _ in 0..10 {
            let byte = self.take(1)?[0];
            value = (value << 7) | (byte & 127) as i128;
            require(value <= MAX_UINT64)?;
            if byte & 128 == 0 {
                return Ok(value);
            }
            value += 1;
            require(value <= MAX_UINT64)?;
        }
        Err(Fail::Reftable("invalid-reftable-varint", 23))
    }

    fn key(&mut self, budget: &mut Budget) -> R<(Vec<u8>, i128)> {
        budget.check()?;
        let start = self.position;
        let prefix = self.varint()?;
        let length_and_type = self.varint()?;
        let suffix_length = length_and_type >> 3;
        require(
            prefix <= self.previous.len() as i128
                && 0 < prefix + suffix_length
                && prefix + suffix_length <= MAX_REF,
        )?;
        let mut name = self.previous[..prefix as usize].to_vec();
        name.extend_from_slice(self.take(suffix_length)?);
        require(name > self.previous)?;
        if self.restart_index < self.restarts.len() {
            let restart = self.restarts[self.restart_index];
            require(start <= restart)?;
            if start == restart {
                require(prefix == 0)?;
                self.restart_index += 1;
            }
        }
        self.previous = name.clone();
        Ok((name, length_and_type & 7))
    }

    fn finish(&self) -> R<()> {
        require(self.position == self.end && self.restart_index == self.restarts.len())
    }
}

/// 非圧縮 block と次の位置。最後の block の padding の省略も受け入れる。
fn block<H: Hooks>(
    source: &Input,
    s: &mut Session<'_, H>,
    position: i128,
    header_size: i128,
    limit: i128,
    alignment: i128,
    expected: u8,
) -> R<(Vec<u8>, i128)> {
    require(position + header_size + 4 <= limit)?;
    let header = source.read(s, position + header_size, 4)?;
    require(header[0] == expected)?;
    let length = uint(&header[1..]);
    require(header_size + 10 <= length && length <= limit - position)?;
    if expected == b'r' && alignment != 0 {
        require(length <= alignment)?;
    }
    let data = source.read(s, position, length)?;
    let end = position + length;
    let mut next_position = end;
    if end < limit && source.read(s, end, 1)? == [0] {
        require(alignment > length)?;
        next_position = position + alignment;
        require(next_position <= limit)?;
        let mut offset = end;
        while offset < next_position {
            s.budget.check()?;
            let count = READ_CHUNK.min(next_position - offset);
            require(source.read(s, offset, count)?.iter().all(|byte| *byte == 0))?;
            offset += count;
        }
    }
    Ok((data, next_position))
}

fn section_header<H: Hooks>(
    source: &Input,
    s: &mut Session<'_, H>,
    position: i128,
    expected: u8,
    limit: i128,
) -> R<()> {
    require(position + 4 <= limit)?;
    let header = source.read(s, position, 4)?;
    require(header[0] == expected)?;
    let length = uint(&header[1..]);
    require(length >= 10)?;
    // log の block の長さは展開後の長さなので、圧縮した bytes の長さと比べない。
    if expected != b'g' {
        require(length <= limit - position)?;
    }
    Ok(())
}

/// 1 つの table を解釈して `merged` に合成する。最小と最大の更新番号を返す。
fn table<H: Hooks>(
    source: &Input,
    s: &mut Session<'_, H>,
    hash_format: &str,
    merged: &mut Stack,
) -> R<(i128, i128)> {
    require(source.size >= 5)?;
    let prefix = source.read(s, 0, 5)?;
    require(&prefix[..4] == b"REFT" && (prefix[4] == 1 || prefix[4] == 2))?;
    let header_size: i128 = if prefix[4] == 1 { 24 } else { 28 };
    let footer_size = header_size + 44;
    require(source.size >= header_size + footer_size)?;
    let header = source.read(s, 0, header_size)?;
    let footer_position = source.size - footer_size;
    let footer = source.read(s, footer_position, footer_size)?;
    let hs = header_size as usize;
    require(footer[..hs] == header[..])?;
    require(crc32(&footer[..footer.len() - 4]) as i128 == uint(&footer[footer.len() - 4..]))?;
    let actual_hash: &[u8] = if prefix[4] == 1 {
        b"sha1"
    } else {
        &header[24..28]
    };
    require(actual_hash == b"sha1" || actual_hash == b"s256")?;
    let expected_hash: &[u8] = if hash_format == "sha1" {
        b"sha1"
    } else {
        b"s256"
    };
    require_as(actual_hash == expected_hash, "reftable-hash-mismatch")?;
    let hash_size: i128 = if hash_format == "sha1" { 20 } else { 32 };
    let alignment = uint(&header[5..8]);
    let minimum = uint(&header[8..16]);
    let maximum = uint(&header[16..24]);
    require(minimum <= maximum)?;
    let fields: Vec<i128> = (0..5)
        .map(|i| uint(&footer[hs + i * 8..hs + i * 8 + 8]))
        .collect();
    let (ref_index, packed_object, object_index, log_position, log_index) =
        (fields[0], fields[1], fields[2], fields[3], fields[4]);
    let (object_position, object_length) = (packed_object >> 5, packed_object & 31);
    let positions = [
        ref_index,
        object_position,
        object_index,
        log_position,
        log_index,
    ];
    let nonzero: Vec<i128> = positions
        .iter()
        .copied()
        .filter(|value| *value != 0)
        .collect();
    require(
        nonzero
            .iter()
            .all(|value| header_size <= *value && *value < footer_position),
    )?;
    require(nonzero.windows(2).all(|pair| pair[0] < pair[1]))?;
    require(if object_position != 0 {
        2 <= object_length && object_length <= 31.min(hash_size)
    } else {
        object_length == 0
    })?;
    require(object_index == 0 || object_position != 0)?;
    if footer_position == header_size {
        require(positions.iter().all(|value| *value == 0))?;
        return Ok((minimum, maximum));
    }
    let first = source.read(s, header_size, 1)?[0];
    require(first == b'r' || first == b'g')?;
    if first == b'g' {
        require(
            [ref_index, object_position, object_index, log_position]
                .iter()
                .all(|value| *value == 0),
        )?;
        section_header(source, s, header_size, b'g', footer_position)?;
    } else {
        require(log_index == 0 || log_position != 0)?;
    }
    let destinations = [
        (ref_index, b'i'),
        (object_position, b'o'),
        (object_index, b'i'),
        (log_position, b'g'),
        (log_index, b'i'),
    ];
    for (position, expected) in destinations {
        if position != 0 {
            // 次の宣言済みの section に block が食い込むことを禁止する。
            let limit = nonzero
                .iter()
                .copied()
                .filter(|value| *value > position)
                .chain([footer_position])
                .min()
                .expect("footer の位置がある");
            section_header(source, s, position, expected, limit)?;
        }
    }
    if first == b'g' {
        return Ok((minimum, maximum));
    }

    let limit = [object_position, log_position]
        .into_iter()
        .filter(|value| *value != 0)
        .chain([footer_position])
        .min()
        .expect("footer の位置がある");
    let mut position: i128 = 0;
    let mut last_name: Vec<u8> = Vec::new();
    let mut block_keys: HashMap<i128, Vec<u8>> = HashMap::new();
    let mut ref_blocks = 0_u64;
    let mut saw_index = false;
    let mut saw_root_index = false;
    while position < limit {
        s.budget.check()?;
        let skip = if position == 0 { header_size } else { 0 };
        require(position + skip < limit)?;
        let tag = source.read(s, position + skip, 1)?[0];
        require(tag == b'r' || tag == b'i')?;
        if tag == b'r' {
            require(!saw_index && (ref_index == 0 || position < ref_index))?;
            ref_blocks += 1;
        } else {
            require(ref_index != 0 && ref_blocks > 0)?;
            saw_index = true;
            saw_root_index = saw_root_index || position == ref_index;
        }
        let (data, next_position) = block(source, s, position, skip, limit, alignment, tag)?;
        let mut records = Records::new(&data, skip, s.budget)?;
        while records.position < records.end {
            let (name, value_type) = records.key(s.budget)?;
            if tag == b'i' {
                require(value_type == 0)?;
                let child = records.varint()?;
                require(child < position && block_keys.get(&child) == Some(&name))?;
                continue;
            }
            require(name > last_name)?;
            last_name.clone_from(&name);
            let delta = records.varint()?;
            require(delta <= maximum - minimum)?;
            let (mut oid, mut target) = (None, None);
            if value_type == 1 || value_type == 2 {
                oid = Some(hex(records.take(hash_size)?));
                if value_type == 2 {
                    records.take(hash_size)?;
                }
            } else if value_type == 3 {
                let length = records.varint()?;
                require(0 < length && length <= MAX_REF)?;
                target = Some(records.take(length)?.to_vec());
            } else {
                require(value_type == 0)?;
            }
            let charge = name.len()
                + oid.as_ref().map_or(0, String::len)
                + target.as_ref().map_or(0, Vec::len)
                + 32;
            s.budget.charge(Kind::ManagementOutput, charge as u64)?;
            if value_type == 0 {
                merged.remove(&name);
            } else {
                merged.set(name, oid, target);
            }
        }
        records.finish()?;
        block_keys.insert(position, records.previous.clone());
        require(next_position > position)?;
        position = next_position;
    }
    require(position == limit && (ref_index != 0) == saw_root_index)?;
    require(alignment != 0 || ref_blocks <= 1 || ref_index != 0)?;
    Ok((minimum, maximum))
}

/// 私有コピーの stack を古い table から合成する。Git は起動しない。
///
/// `path` は作業場所から見たパス(相対パスも受ける)。`hash_format` は `sha1` か `sha256`。
/// 予算は呼出元から受け取ったものを使い、中で作り直さない。
pub fn read_stack(
    path: &OsStr,
    hash_format: &str,
    budget: &mut Budget,
) -> Result<Stack, ReadError> {
    read_stack_with(path, hash_format, budget, &mut NoHooks)
}

fn read_stack_with<H: Hooks>(
    path: &OsStr,
    hash_format: &str,
    budget: &mut Budget,
    hooks: &mut H,
) -> Result<Stack, ReadError> {
    // 旧版と同じく、この検査は予算を確かめる前に行う。
    if hash_format != "sha1" && hash_format != "sha256" {
        return Err(ReadError::Reftable(Failure::new(
            "reftable-hash-mismatch",
            23,
        )));
    }
    let mut dir: Option<HeldDir> = None;
    let mut listing: Option<Input> = None;
    let mut tables: Vec<Input> = Vec::new();
    let outcome = run(
        path,
        hash_format,
        budget,
        hooks,
        &mut dir,
        &mut listing,
        &mut tables,
    );
    // 旧版の finally と同じ順(table を開いた順 → tables.list → フォルダ)で全部閉じる。
    // 閉じる失敗が 1 つでもあれば、それまでの結果や失敗より優先して reftable-io(20)にする。
    let mut close_failed = false;
    for input in tables {
        close_failed |= input.close(hooks).is_err();
    }
    if let Some(input) = listing {
        close_failed |= input.close(hooks).is_err();
    }
    if let Some(dir) = dir {
        close_failed |= hooks.close_dir(dir).is_err();
    }
    if close_failed {
        return Err(ReadError::Reftable(Failure::new("reftable-io", 20)));
    }
    outcome.map_err(into_read_error)
}

fn run<H: Hooks>(
    path: &OsStr,
    hash_format: &str,
    budget: &mut Budget,
    hooks: &mut H,
    dir_slot: &mut Option<HeldDir>,
    listing_slot: &mut Option<Input>,
    tables: &mut Vec<Input>,
) -> R<Stack> {
    budget.check()?;
    if path.as_bytes().contains(&0) {
        return Err(Fail::Os(FsError::InvalidInput));
    }
    let path = Path::new(path);
    let before = lstat_path(path)?;
    require_as(before.is_directory(), "invalid-reftable-directory")?;
    let dir: &HeldDir = dir_slot.insert(HeldDir::open_path(path)?);
    let original = dir.snapshot()?;
    require_as(before == original, "changed-reftable")?;
    let mut s = Session { dir, budget, hooks };
    let mut totals: i128 = 0;
    let listing: &Input =
        listing_slot.insert(Input::open(&mut s, OsStr::new("tables.list"), &mut totals)?);
    let raw = listing.read(&mut s, 0, listing.size)?;
    require_as(
        raw.last().is_none_or(|byte| *byte == b'\n'),
        "invalid-reftable-list",
    )?;
    let mut seen: HashSet<&[u8]> = HashSet::new();
    let mut merged = Stack::new();
    let mut previous_maximum: Option<i128> = None;
    let mut cursor = 0;
    while cursor < raw.len() {
        s.budget.check()?;
        let newline = raw[cursor..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|found| found + cursor);
        let newline = match newline {
            Some(found) if found - cursor <= LIST_LINE_MAX => found,
            _ => return Err(Fail::Reftable("invalid-reftable-list", 23)),
        };
        let name = &raw[cursor..newline];
        cursor = newline + 1;
        require_as(
            ![&b""[..], b".", b"..", b"tables.list"].contains(&name)
                && !name.contains(&b'/')
                && !name.contains(&0)
                && !seen.contains(name),
            "invalid-reftable-list",
        )?;
        seen.insert(name);
        tables.push(Input::open(&mut s, OsStr::from_bytes(name), &mut totals)?);
        let current = tables.last().expect("今 push した table");
        let (minimum, maximum) = table(current, &mut s, hash_format, &mut merged)?;
        require_as(
            previous_maximum.is_none_or(|previous| minimum > previous),
            "invalid-reftable-stack-order",
        )?;
        previous_maximum = Some(maximum);
    }
    s.hooks.before_verify();
    for input in tables.iter() {
        input.verify(&mut s)?;
    }
    listing.verify(&mut s)?;
    require_as(s.dir.snapshot()? == original, "changed-reftable")?;
    require_as(lstat_path(path)? == original, "changed-reftable")?;
    s.budget.check()?;
    Ok(merged)
}

/// 比較器(`native/ci/check_diagnostic_reftable.py`)の T7 と同じバイト列(2 段の index。参照 3 件)。
///
/// 作り方: `scripts/test_diagnostic_reftable.py` の符号化の関数(`table`・`ref_block`・`record`・`varint`・
/// `uint`)で作った値で、製品の解釈のコードでは作っていない。version 1・sha1・更新番号 1。
/// 3 つの ref block(`refs/heads/a` と `refs/heads/b` は OID、`refs/heads/c` は指す先 `refs/heads/a`)、
/// それを指す下段の index block、その下段を指す上段の index block、footer の順。
/// 比較の検査(`tests/diagnostic_reftable.rs`)は、比較器が書いた T7 とこの値が同じことを確かめる。
#[cfg(test)]
pub(crate) const T7_TABLE: &[u8] = b"REFT\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x01r\x00\x00D\x00arefs/heads/a\x00\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x11\x00\x00\x1c\x00\x01r\x00\x00,\x00arefs/heads/b\x00\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x22\x00\x00\x04\x00\x01r\x00\x00%\x00crefs/heads/c\x00\x0crefs/heads/a\x00\x00\x04\x00\x01i\x00\x00<\x00`refs/heads/a\x00\x00`refs/heads/bD\x00`refs/heads/cp\x00\x00\x04\x00\x00\x13\x00\x00\x22\x00\x03i\x00\x00\x19\x00`refs/heads/c\x80\x15\x00\x00\x04\x00\x01REFT\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\xd1\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x87\xf3\x88\xe2";

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::fs as stdfs;
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::rc::Rc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};

    // Linux と Mac で同じ値の errno。
    const ENOENT: i32 = 2;
    const EIO: i32 = 5;
    const EMFILE: i32 = 24;

    fn os_error(errno: i32) -> FsError {
        FsError::Io(std::io::Error::from_raw_os_error(errno))
    }

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Tmp(PathBuf);
    impl Tmp {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4d1-reftable-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            stdfs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        /// T7 の stack を 1 つ置く。
        fn t7() -> Self {
            let tmp = Self::new();
            tmp.table("table-0.ref", T7_TABLE);
            tmp.list(b"table-0.ref\n");
            tmp
        }
        fn table(&self, name: &str, bytes: &[u8]) {
            stdfs::write(self.0.join(name), bytes).unwrap();
        }
        fn list(&self, bytes: &[u8]) {
            stdfs::write(self.0.join("tables.list"), bytes).unwrap();
        }
        fn read(&self, format: &str) -> Result<Stack, ReadError> {
            read_stack(self.0.as_os_str(), format, &mut Budget::new())
        }
    }
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = stdfs::remove_dir_all(&self.0);
        }
    }

    fn rt(reason: &'static str, code: u8) -> ReadError {
        ReadError::Reftable(Failure::new(reason, code))
    }

    fn names(stack: &Stack) -> Vec<Vec<u8>> {
        stack.iter().map(|entry| entry.name.clone()).collect()
    }

    #[test]
    fn crc32_matches_the_zlib_check_value() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(crc32(b""), 0);
    }

    #[test]
    fn t7_is_read_as_three_refs_in_order() {
        let tmp = Tmp::t7();
        let stack = tmp.read("sha1").unwrap();
        assert_eq!(
            names(&stack),
            vec![
                b"refs/heads/a".to_vec(),
                b"refs/heads/b".to_vec(),
                b"refs/heads/c".to_vec()
            ]
        );
        assert_eq!(
            stack.get(b"refs/heads/a").unwrap().oid.as_deref(),
            Some("1111111111111111111111111111111111111111")
        );
        assert_eq!(
            stack.get(b"refs/heads/b").unwrap().oid.as_deref(),
            Some("2222222222222222222222222222222222222222")
        );
        let symbolic = stack.get(b"refs/heads/c").unwrap();
        assert_eq!(symbolic.oid, None);
        assert_eq!(symbolic.target.as_deref(), Some(&b"refs/heads/a"[..]));
        assert_eq!(stack.len(), 3);
        // 汎用の読み取りは、空の tables.list を空の結果にする。
        tmp.list(b"");
        assert!(tmp.read("sha1").unwrap().is_empty());
    }

    #[test]
    fn stack_keeps_dict_order_for_overwrite_and_delete() {
        let mut stack = Stack::new();
        for name in [&b"a"[..], b"b", b"c"] {
            stack.set(name.to_vec(), Some("00".into()), None);
        }
        stack.set(b"a".to_vec(), Some("11".into()), None);
        assert_eq!(names(&stack), [b"a".to_vec(), b"b".to_vec(), b"c".to_vec()]);
        assert_eq!(stack.get(b"a").unwrap().oid.as_deref(), Some("11"));
        stack.remove(b"a");
        stack.remove(b"missing");
        stack.set(b"a".to_vec(), None, Some(b"t".to_vec()));
        assert_eq!(names(&stack), [b"b".to_vec(), b"c".to_vec(), b"a".to_vec()]);
        assert_eq!(stack.len(), 3);
    }

    #[test]
    fn arguments_are_checked_in_the_legacy_order() {
        let tmp = Tmp::t7();
        let mut stopped = Budget::new();
        stopped.request_stop();
        // ハッシュの形式は予算より先に確かめる。
        for format in ["SHA1", "", "sha512"] {
            assert_eq!(
                read_stack(tmp.0.as_os_str(), format, &mut stopped),
                Err(rt("reftable-hash-mismatch", 23))
            );
        }
        // NUL は、最初の予算の確かめの後に区別して返す。
        let nul = OsStr::from_bytes(b"a\0b");
        assert_eq!(
            read_stack(nul, "sha1", &mut stopped),
            Err(ReadError::Budget(Failure::new("interrupted", 20)))
        );
        assert_eq!(
            read_stack(nul, "sha1", &mut Budget::new()),
            Err(ReadError::Other("ValueError"))
        );
        assert_eq!(tmp.read("sha256"), Err(rt("reftable-hash-mismatch", 23)));
    }

    #[test]
    fn directory_and_list_failures_use_the_legacy_reasons() {
        let tmp = Tmp::t7();
        let missing = tmp.0.join("missing");
        assert_eq!(
            read_stack(missing.as_os_str(), "sha1", &mut Budget::new()),
            Err(rt("incomplete-metadata", 23))
        );
        assert_eq!(
            read_stack(OsStr::new(""), "sha1", &mut Budget::new()),
            Err(rt("incomplete-metadata", 23))
        );
        let file = tmp.0.join("table-0.ref");
        assert_eq!(
            read_stack(file.as_os_str(), "sha1", &mut Budget::new()),
            Err(rt("invalid-reftable-directory", 23))
        );
        for raw in [
            &b"../outside\n"[..],
            b"table-0.ref\ntable-0.ref\n",
            b"table-0.ref",
            b"\n",
            b".\n",
            b"tables.list\n",
            b"a/b\n",
        ] {
            tmp.list(raw);
            assert_eq!(
                tmp.read("sha1"),
                Err(rt("invalid-reftable-list", 23)),
                "{raw:?}"
            );
        }
        // 4096 バイトちょうどの行は文法を通るが、名前が長すぎて OS が拒む(ENAMETOOLONG は reftable-io)。
        let long = [vec![b'x'; 4096], vec![b'\n']].concat();
        tmp.list(&long);
        assert_eq!(tmp.read("sha1"), Err(rt("reftable-io", 20)));
        let long = [vec![b'x'; 4097], vec![b'\n']].concat();
        tmp.list(&long);
        assert_eq!(tmp.read("sha1"), Err(rt("invalid-reftable-list", 23)));
        tmp.list(b"table-0.ref\n");
        stdfs::remove_file(tmp.0.join("table-0.ref")).unwrap();
        assert_eq!(tmp.read("sha1"), Err(rt("incomplete-metadata", 23)));
        let target = tmp.0.join("other");
        stdfs::write(&target, T7_TABLE).unwrap();
        std::os::unix::fs::symlink(&target, tmp.0.join("table-0.ref")).unwrap();
        assert_eq!(tmp.read("sha1"), Err(rt("invalid-reftable-file", 23)));
    }

    #[test]
    fn stack_order_and_truncation_are_failures() {
        let tmp = Tmp::t7();
        tmp.table("table-1.ref", T7_TABLE);
        tmp.list(b"table-0.ref\ntable-1.ref\n");
        assert_eq!(
            tmp.read("sha1"),
            Err(rt("invalid-reftable-stack-order", 23))
        );
        tmp.list(b"table-0.ref\n");
        for cut in 0..T7_TABLE.len() {
            tmp.table("table-0.ref", &T7_TABLE[..cut]);
            assert!(
                matches!(tmp.read("sha1"), Err(ReadError::Reftable(_))),
                "{cut}"
            );
        }
    }

    #[test]
    fn input_total_limit_is_exact() {
        let tmp = Tmp::t7();
        let limit = MAX_INPUT as u64;
        // tables.list は 12 バイト。table との合計が上限ちょうどなら読み始め、1 多ければ止める。
        for (extra, reason) in [(0, "invalid-reftable"), (1, "management-input-limit")] {
            let file = stdfs::File::create(tmp.0.join("table-0.ref")).unwrap();
            file.set_len(limit - 12 + extra).unwrap();
            match tmp.read("sha1") {
                Err(ReadError::Reftable(failure)) => assert_eq!(failure.reason, reason),
                other => panic!("{other:?}"),
            }
        }
    }

    /// 故障の差し込み。何回目の呼び出しで故障させるかを持つ。
    #[derive(Default)]
    struct Faults {
        opens: usize,
        open_fail_at: Option<(usize, i32)>,
        reads: usize,
        zero_read_at: Option<usize>,
        read_fail_at: Option<usize>,
        wait_after_read: Option<(usize, Duration, Option<String>)>,
        /// 予算を作ってから k 回目の読み取りに着くまでの時間(CI の機械ごとの測定用)。
        created: Option<Instant>,
        arrival: Option<Duration>,
        before_verify: Option<Box<dyn FnMut()>>,
        closes: usize,
        close_fail_at: Option<usize>,
        dir_close_fails: bool,
    }

    impl Faults {
        fn with_before_verify(action: impl FnMut() + 'static) -> Self {
            Self {
                before_verify: Some(Box::new(action)),
                ..Self::default()
            }
        }
    }

    impl Hooks for Faults {
        fn open_plain(&mut self, dir: &HeldDir, name: &OsStr) -> Result<PlainFile, FsError> {
            self.opens += 1;
            if let Some((at, errno)) = self.open_fail_at
                && at == self.opens
            {
                return Err(os_error(errno));
            }
            dir.open_plain(name)
        }
        fn read_at(
            &mut self,
            budget: &mut Budget,
            file: &PlainFile,
            offset: u64,
            buffer: &mut [u8],
        ) -> Result<usize, FsError> {
            self.reads += 1;
            if self.zero_read_at == Some(self.reads) {
                return Ok(0);
            }
            if self.read_fail_at == Some(self.reads) {
                return Err(os_error(EIO));
            }
            let size = file.read_at(offset, buffer)?;
            if let Some((at, limit, report)) = self.wait_after_read.as_mut()
                && *at == self.reads
            {
                let started = Instant::now();
                self.arrival = self.created.map(|created| created.elapsed());
                while budget.remaining() > 0.0 {
                    if started.elapsed() > *limit {
                        *report = Some("上限まで待っても期限を過ぎなかった".into());
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
            }
            Ok(size)
        }
        fn before_verify(&mut self) {
            if let Some(action) = self.before_verify.as_mut() {
                action();
            }
        }
        fn close_file(&mut self, file: PlainFile) -> Result<(), FsError> {
            self.closes += 1;
            let result = file.close();
            if self.close_fail_at == Some(self.closes) {
                return Err(os_error(EIO));
            }
            result
        }
        fn close_dir(&mut self, dir: HeldDir) -> Result<(), FsError> {
            let result = dir.close();
            if self.dir_close_fails {
                return Err(os_error(EIO));
            }
            result
        }
    }

    fn run_with(tmp: &Tmp, hooks: &mut Faults) -> Result<Stack, ReadError> {
        read_stack_with(tmp.0.as_os_str(), "sha1", &mut Budget::new(), hooks)
    }

    #[test]
    fn short_read_zero_bytes_means_changed() {
        let tmp = Tmp::t7();
        // 1 回目は tables.list、2 回目以降は table。どの読み取りでも同じ。
        for at in [1, 2, 5] {
            let mut hooks = Faults {
                zero_read_at: Some(at),
                ..Faults::default()
            };
            assert!(
                matches!(run_with(&tmp, &mut hooks), Err(e) if e == rt("changed-reftable", 23)),
                "{at}"
            );
        }
    }

    #[test]
    fn io_errors_on_read_and_open_are_reftable_io() {
        let tmp = Tmp::t7();
        let mut hooks = Faults {
            read_fail_at: Some(3),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        for at in [1, 2] {
            let mut hooks = Faults {
                open_fail_at: Some((at, EMFILE)),
                ..Faults::default()
            };
            assert_eq!(
                run_with(&tmp, &mut hooks).unwrap_err(),
                rt("reftable-io", 20),
                "{at}"
            );
        }
        let mut hooks = Faults {
            open_fail_at: Some((2, ENOENT)),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("incomplete-metadata", 23)
        );
    }

    #[test]
    fn close_failure_wins_over_every_other_result() {
        let tmp = Tmp::t7();
        // 成功の結果も、table → tables.list → フォルダのどの閉じる失敗でも reftable-io になる。
        for at in [1, 2] {
            let mut hooks = Faults {
                close_fail_at: Some(at),
                ..Faults::default()
            };
            assert_eq!(
                run_with(&tmp, &mut hooks).unwrap_err(),
                rt("reftable-io", 20),
                "{at}"
            );
        }
        let mut hooks = Faults {
            dir_close_fails: true,
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        // 閉じても全部を閉じ終える(失敗した 1 つで止めない)。
        let mut hooks = Faults {
            close_fail_at: Some(1),
            ..Faults::default()
        };
        let _ = run_with(&tmp, &mut hooks);
        assert_eq!(hooks.closes, 2);
        // 読み取りの失敗・解釈の失敗・予算の失敗より優先する。
        let mut hooks = Faults {
            read_fail_at: Some(3),
            close_fail_at: Some(1),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        tmp.table("table-0.ref", &T7_TABLE[..40]);
        let mut hooks = Faults {
            close_fail_at: Some(2),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        // 予算の失敗(確かめ直しの前に停止の印を立てる)より優先する。
        let mut stopping = Budget::new();
        let flag = stopping.stop_flag();
        let mut hooks = Faults {
            dir_close_fails: true,
            before_verify: Some(Box::new(move || {
                flag.store(true, Ordering::SeqCst);
            })),
            ..Faults::default()
        };
        assert_eq!(
            read_stack_with(tmp.0.as_os_str(), "sha1", &mut stopping, &mut hooks),
            Err(rt("reftable-io", 20))
        );
    }

    #[test]
    fn close_failure_while_a_table_fails_to_open_is_reftable_io() {
        let tmp = Tmp::t7();
        tmp.table("table-0.ref", T7_TABLE);
        // 開いた後の状態の取得や比較で失敗する経路は、fd を閉じてから失敗を出す。
        // ここでは大きさの上限で止める: tables.list(12 バイト)と table の合計が上限を超える。
        let file = stdfs::File::create(tmp.0.join("table-0.ref")).unwrap();
        file.set_len(MAX_INPUT as u64).unwrap();
        let mut hooks = Faults {
            close_fail_at: Some(1),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        // 閉じる失敗が無ければ、元の失敗(management-input-limit)のまま。
        // 閉じる処理は 2 回: 1 回目が途中で失敗した table(Input::open の中)、2 回目が後始末の tables.list。
        // 途中の失敗で閉じる道を通らなければ、1 回になる。
        let mut hooks = Faults::default();
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("management-input-limit", 20)
        );
        assert_eq!(hooks.closes, 2);
        // 1 回目(途中の失敗の table)の閉じる失敗は、元の失敗に置き換わる。2 回目だけの失敗とも同じ理由になる。
        let mut hooks = Faults {
            close_fail_at: Some(2),
            ..Faults::default()
        };
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("reftable-io", 20)
        );
        assert_eq!(hooks.closes, 2);
    }

    #[test]
    fn changes_after_reading_are_detected_with_all_six_fields() {
        let tmp = Tmp::t7();
        let path = tmp.0.join("table-0.ref");
        let target = path.clone();
        let mut hooks = Faults::with_before_verify(move || {
            let mode = stdfs::metadata(&target).unwrap().permissions().mode();
            stdfs::set_permissions(
                &target,
                stdfs::Permissions::from_mode((mode & 0o7777) ^ 0o100),
            )
            .unwrap();
        });
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("changed-reftable", 23)
        );
        // tables.list の変更。
        let tmp = Tmp::t7();
        let list = tmp.0.join("tables.list");
        let mut hooks = Faults::with_before_verify(move || {
            let mode = stdfs::metadata(&list).unwrap().permissions().mode();
            stdfs::set_permissions(
                &list,
                stdfs::Permissions::from_mode((mode & 0o7777) ^ 0o100),
            )
            .unwrap();
        });
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("changed-reftable", 23)
        );
        // フォルダの中身を変えると、size と時刻が変わるので changed-reftable(DirectoryPath は使わない)。
        let tmp = Tmp::t7();
        let root = tmp.0.clone();
        let mut hooks = Faults::with_before_verify(move || {
            // ファイルの時刻は粗い刻みなので、刻みが進んでから変える。
            std::thread::sleep(Duration::from_millis(50));
            stdfs::write(root.join("extra"), b"x").unwrap();
        });
        assert_eq!(
            run_with(&tmp, &mut hooks).unwrap_err(),
            rt("changed-reftable", 23)
        );
        // 変更が無ければ成功する。
        let tmp = Tmp::t7();
        assert!(run_with(&tmp, &mut Faults::default()).is_ok());
    }

    /// 読み取りの k 回目の後に期限を過ぎるまで待つと、次の確かめが必ず time-limit になり、部分的な結果を返さない。
    #[test]
    fn real_deadline_fires_at_first_middle_and_last_read() {
        let tmp = Tmp::t7();
        let mut counting = Faults::default();
        run_with(&tmp, &mut counting).unwrap();
        let total = counting.reads;
        assert!(total >= 3, "{total}");
        for at in [1, total.div_ceil(2), total] {
            let mut hooks = Faults {
                wait_after_read: Some((at, Duration::from_secs(10), None)),
                ..Faults::default()
            };
            let created = Instant::now();
            hooks.created = Some(created);
            let mut budget = Budget::with_seconds(2.0);
            let result = read_stack_with(tmp.0.as_os_str(), "sha1", &mut budget, &mut hooks);
            let (_, _, report) = hooks.wait_after_read.take().unwrap();
            // 合格したテストの出力は libtest が隠すので、直接 stderr へ書く(CI のログに残す)。
            let _ = writeln!(
                std::io::stderr(),
                "R4D-1 期限の検査: k={at}/{total} 予算を作ってから {:.3} ミリ秒で着いた",
                hooks.arrival.map_or(f64::NAN, |d| d.as_secs_f64() * 1000.0)
            );
            assert_eq!(report, None, "k={at}");
            // 差し込み口に着く前に期限が過ぎていたら、理由と経過したミリ秒を出す。
            assert!(
                hooks.reads >= at,
                "k={at}: 差し込み口に着く前に期限が過ぎた(経過 {} ミリ秒・結果 {result:?})",
                created.elapsed().as_millis()
            );
            assert_eq!(
                result.map(|stack| stack.len()),
                Err(ReadError::Budget(Failure::new("time-limit", 20))),
                "k={at}"
            );
            // 期限を過ぎた後は、1 回も読まない。
            assert_eq!(hooks.reads, at, "k={at}");
        }
    }

    #[test]
    fn budget_failures_and_charge_follow_the_legacy_order() {
        let tmp = Tmp::t7();
        // 記録 1 件ごとに len(名前)+len(OID の 16 進)+len(指す先)+32 を数える。T7 の合計は 3 件分。
        let mut budget = Budget::new();
        read_stack(tmp.0.as_os_str(), "sha1", &mut budget).unwrap();
        let expected = (12 + 40 + 32) * 2 + (12 + 12 + 32);
        assert_eq!(budget.used(Kind::ManagementOutput), expected as u64);
        // 上限ちょうどは成功し、1 少ないと management_output-limit。
        let clock: Box<dyn FnMut() -> f64> = Box::new(|| 0.0);
        let mut limits = Kind::ALL.map(Kind::default_limit);
        limits[1] = expected as u64;
        let mut exact = Budget::for_test(300.0, clock, limits);
        assert!(read_stack(tmp.0.as_os_str(), "sha1", &mut exact).is_ok());
        limits[1] = expected as u64 - 1;
        let clock: Box<dyn FnMut() -> f64> = Box::new(|| 0.0);
        let mut below = Budget::for_test(300.0, clock, limits);
        assert_eq!(
            read_stack(tmp.0.as_os_str(), "sha1", &mut below).unwrap_err(),
            ReadError::Budget(Failure::new("management_output-limit", 20))
        );
        // 期限は作るときに固定する。作った直後に期限なら、最初の確かめで time-limit。
        let calls = Rc::new(Cell::new(0_u32));
        let counter = Rc::clone(&calls);
        let clock: Box<dyn FnMut() -> f64> = Box::new(move || {
            counter.set(counter.get() + 1);
            if counter.get() == 1 { 0.0 } else { 1000.0 }
        });
        let mut expired = Budget::for_test(300.0, clock, Kind::ALL.map(Kind::default_limit));
        assert_eq!(
            read_stack(tmp.0.as_os_str(), "sha1", &mut expired).unwrap_err(),
            ReadError::Budget(Failure::new("time-limit", 20))
        );
        assert_eq!(calls.get(), 2);
    }
}
