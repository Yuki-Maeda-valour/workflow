//! 地域設定: glibc 2.39 の `setlocale`(旧 bash の警告と、道具の `setlocale(LC_ALL, "")`)、
//! `LC_CTYPE` の文字コードの名前(`nl_langinfo(CODESET)`)、gettext の翻訳の選び方と翻訳ファイル
//! (`coreutils.mo`・`libc.mo`)の読み方、訳した文の文字コードへの変え方。
//!
//! - setlocale は R4G の `reviews_dir/locale.rs` の判定を写し、見つけた所(保管庫か、種類のファイル)を
//!   返すようにした(文字コードの名前を読むため)。glibc は一度試したファイルを覚え、2 回目は開かない
//!   ので `errno` が立たない。その状態を 1 つのプロセスの中で持つ。
//! - gettext(`dcigettext.c`・`l10nflist.c`・Ubuntu の `locale-langpack` の追加): 名前は `LANGUAGE`
//!   (`LC_MESSAGES` の地域設定が `C`・`POSIX`・`C.UTF-8` のときは見ない)か `LC_MESSAGES` の地域設定の名前。
//!   各名前は `locale.alias` で 1 段展開し、`/usr/share/locale` の全部の形、次に
//!   `/usr/share/locale-langpack` の全部の形を探す(基準の容器で strace で確かめた順)。
//! - 翻訳は実行時に読む(基準の表を埋め込まない)。見出しの charset が UTF-8 でなければ、Rust の中で
//!   変えずに道具を起動する(呼び出し側)。
//! - 変え方: 文字コードが `UTF-8` ならそのまま、`ANSI_X3.4-1968` なら ASCII で表せない字を 1 字ずつ `?`
//!   にする(glibc の `//TRANSLIT`。coreutils と libc の日本語の文で、基準の容器の道具と同じことを確かめた)。
use std::collections::HashMap;
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

/// glibc の種類の番号の順(`LC_ALL` の 6 を除く)。`setlocale(LC_ALL, …)` はこの逆の順に読む。
pub const CATEGORIES: [&str; 12] = [
    "LC_CTYPE",
    "LC_NUMERIC",
    "LC_TIME",
    "LC_COLLATE",
    "LC_MONETARY",
    "LC_MESSAGES",
    "LC_PAPER",
    "LC_NAME",
    "LC_ADDRESS",
    "LC_TELEPHONE",
    "LC_MEASUREMENT",
    "LC_IDENTIFICATION",
];

const DEFAULT_PATH: &str = "/usr/lib/locale";
const ARCHIVE: &str = "/usr/lib/locale/locale-archive";
const ALIAS_FILE: &str = "/usr/share/locale/locale.alias";
/// gettext の翻訳ファイルを探す所(基準の容器の glibc。Ubuntu は `locale-langpack` を後に足す)。
pub const CATALOG_DIRS: [&str; 2] = ["/usr/share/locale", "/usr/share/locale-langpack"];
/// `LC_CTYPE` のデータの `CODESET` の番号(glibc 2.39 の `_NL_ITEM_INDEX(_NL_CTYPE_CODESET_NAME)`)。
pub const CODESET_INDEX: usize = 14;
/// `LC_CTYPE` のデータの印(glibc 2.39 の `LIMAGIC(LC_CTYPE)`)。
const CTYPE_MAGIC: u32 = 0x2009_0720;

/// glibc の `_nl_normalize_codeset`: 英数字だけを小文字で残し、数字だけなら `iso` を前に付ける。
pub fn normalize_codeset(codeset: &[u8]) -> Vec<u8> {
    let kept: Vec<u8> = codeset
        .iter()
        .filter(|byte| byte.is_ascii_alphanumeric())
        .map(u8::to_ascii_lowercase)
        .collect();
    if !kept.is_empty() && kept.iter().all(u8::is_ascii_digit) {
        let mut out = b"iso".to_vec();
        out.extend_from_slice(&kept);
        out
    } else {
        kept
    }
}

/// glibc の `valid_locale_name`。
fn valid_name(name: &[u8]) -> bool {
    let n = name.len();
    if n > 255 || name.windows(4).any(|window| window == b"/../") || name == b".." {
        return false;
    }
    if n >= 3 && (name.starts_with(b"../") || name.ends_with(b"/..")) {
        return false;
    }
    !(name.contains(&b'/') && name.first() != Some(&b'/'))
}

fn word(data: &[u8], offset: usize) -> Option<usize> {
    data.get(offset..offset + 4)
        .map(|b| u32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as usize)
}

/// `locale-archive` の名前の表(名前と、その記録の位置)。読めなければ errno。
fn archive_names(data: &[u8]) -> Vec<(Vec<u8>, usize)> {
    let mut names = Vec::new();
    // struct locarhead { magic, serial, namehash_offset, namehash_used, namehash_size, ... }
    let (Some(magic), Some(offset), Some(size)) = (word(data, 0), word(data, 8), word(data, 16))
    else {
        return names;
    };
    if magic != 0xde02_0109 {
        return names;
    }
    for index in 0..size {
        // struct namehashent { hashval, name_offset, locrec_offset }
        let entry = offset + index * 12;
        let (Some(name_offset), Some(record)) = (word(data, entry + 4), word(data, entry + 8))
        else {
            break;
        };
        if name_offset == 0 || record == 0 {
            continue;
        }
        if let Some(rest) = data.get(name_offset..) {
            let end = rest
                .iter()
                .position(|byte| *byte == 0)
                .unwrap_or(rest.len());
            names.push((rest.get(..end).unwrap_or_default().to_vec(), record));
        }
    }
    names
}

/// `_nl_load_locale_from_archive` の名前(文字コードの部分を正規化した名前)。
fn archive_name(name: &[u8]) -> Vec<u8> {
    let Some(dot) = name.iter().position(|byte| *byte == b'.') else {
        return name.to_vec();
    };
    if matches!(name.get(dot + 1), None | Some(b'@')) {
        return name.to_vec();
    }
    let rest = name
        .get(dot + 1..)
        .unwrap_or_default()
        .iter()
        .position(|byte| *byte == b'@')
        .map_or(name.len(), |index| dot + 1 + index);
    let mut out = name.get(..=dot).unwrap_or_default().to_vec();
    out.extend_from_slice(&normalize_codeset(
        name.get(dot + 1..rest).unwrap_or_default(),
    ));
    out.extend_from_slice(name.get(rest..).unwrap_or_default());
    out
}

/// `_nl_expand_alias`(`locale.alias` の最初の行。名前は大文字小文字を区別しない)。
pub fn expand_alias(name: &[u8]) -> Option<Vec<u8>> {
    let data = std::fs::read(ALIAS_FILE).ok()?;
    for line in data.split(|byte| *byte == b'\n') {
        let mut fields = line
            .split(|byte| matches!(byte, b' ' | b'\t' | b'\r'))
            .filter(|field| !field.is_empty());
        let Some(alias) = fields.next() else {
            continue;
        };
        if alias.starts_with(b"#") {
            continue;
        }
        let Some(value) = fields.next() else {
            continue;
        };
        if alias.eq_ignore_ascii_case(name) {
            return Some(value.to_vec());
        }
    }
    None
}

/// `_nl_explode_name` の部分と `mask`。
pub struct Parts {
    pub language: Vec<u8>,
    pub territory: Vec<u8>,
    pub codeset: Option<Vec<u8>>,
    pub normalized: Vec<u8>,
    pub modifier: Vec<u8>,
    pub mask: u32,
}

const XPG_NORM_CODESET: u32 = 1;
const XPG_CODESET: u32 = 2;
const XPG_TERRITORY: u32 = 4;
const XPG_MODIFIER: u32 = 8;

pub fn explode(name: &[u8]) -> Parts {
    let mut parts = Parts {
        language: Vec::new(),
        territory: Vec::new(),
        codeset: None,
        normalized: Vec::new(),
        modifier: Vec::new(),
        mask: 0,
    };
    let language_end = name
        .iter()
        .position(|byte| matches!(byte, b'_' | b'.' | b'@'))
        .unwrap_or(name.len());
    let mut index;
    if language_end == 0 {
        parts.language = name.to_vec();
        index = name.len();
    } else {
        parts.language = name.get(..language_end).unwrap_or_default().to_vec();
        index = language_end;
        if name.get(index) == Some(&b'_') {
            let start = index + 1;
            let end = name
                .get(start..)
                .unwrap_or_default()
                .iter()
                .position(|byte| matches!(byte, b'.' | b'@'))
                .map_or(name.len(), |offset| start + offset);
            parts.territory = name.get(start..end).unwrap_or_default().to_vec();
            parts.mask |= XPG_TERRITORY;
            index = end;
        }
        if name.get(index) == Some(&b'.') {
            let start = index + 1;
            let end = name
                .get(start..)
                .unwrap_or_default()
                .iter()
                .position(|byte| *byte == b'@')
                .map_or(name.len(), |offset| start + offset);
            let codeset = name.get(start..end).unwrap_or_default().to_vec();
            parts.mask |= XPG_CODESET;
            if !codeset.is_empty() {
                let normalized = normalize_codeset(&codeset);
                if normalized != codeset {
                    parts.normalized = normalized;
                    parts.mask |= XPG_NORM_CODESET;
                }
            }
            parts.codeset = Some(codeset);
            index = end;
        }
    }
    if name.get(index) == Some(&b'@') {
        parts.modifier = name.get(index + 1..).unwrap_or_default().to_vec();
        if !parts.modifier.is_empty() {
            parts.mask |= XPG_MODIFIER;
        }
    }
    if parts.territory.is_empty() {
        parts.mask &= !XPG_TERRITORY;
    }
    if parts.codeset.as_deref() == Some(b"") {
        parts.mask &= !XPG_CODESET;
    }
    parts
}

/// `mask` の名前の部分(`<言語>[_地域][.文字コード][.正規化][@修飾]`)。
pub fn variant(parts: &Parts, mask: u32) -> Vec<u8> {
    let mut out = parts.language.clone();
    if mask & XPG_TERRITORY != 0 {
        out.push(b'_');
        out.extend_from_slice(&parts.territory);
    }
    if mask & XPG_CODESET != 0 {
        out.push(b'.');
        out.extend_from_slice(parts.codeset.as_deref().unwrap_or_default());
    }
    if mask & XPG_NORM_CODESET != 0 {
        out.push(b'.');
        out.extend_from_slice(&parts.normalized);
    }
    if mask & XPG_MODIFIER != 0 {
        out.push(b'@');
        out.extend_from_slice(&parts.modifier);
    }
    out
}

/// `_nl_make_l10nflist` が試す名前の順(文字コードと正規化した文字コードの両方を持つ形は除く)。
pub fn variants(name: &[u8]) -> Vec<Vec<u8>> {
    let parts = explode(name);
    let mask = parts.mask;
    let mut out = Vec::new();
    for value in (0..=mask).rev() {
        if value & !mask != 0 {
            continue;
        }
        if value & XPG_CODESET != 0 && value & XPG_NORM_CODESET != 0 {
            continue;
        }
        out.push(variant(&parts, value));
    }
    out
}

/// 1 つの種類の候補のファイル(`(パス, 名前, 試す)` の並び。試さないのは擬似の項目)。
fn candidates(parts: &Parts, dirs: &[PathBuf], category: &str) -> Vec<(PathBuf, Vec<u8>, bool)> {
    let mut out = Vec::new();
    let file = |dir: &Path, mask: u32| -> (PathBuf, Vec<u8>, bool) {
        let name = variant(parts, mask);
        let pseudo = mask & XPG_CODESET != 0 && mask & XPG_NORM_CODESET != 0;
        let mut path = dir.as_os_str().as_bytes().to_vec();
        path.push(b'/');
        path.extend_from_slice(&name);
        path.push(b'/');
        path.extend_from_slice(category.as_bytes());
        (PathBuf::from(OsStr::from_bytes(&path)), name, !pseudo)
    };
    let mask = parts.mask;
    let mut cnt: i64 = if dirs.len() == 1 {
        if let Some(first) = dirs.first() {
            out.push(file(first, mask));
        }
        i64::from(mask) - 1
    } else {
        i64::from(mask)
    };
    while cnt >= 0 {
        let value = cnt as u32;
        if value & !mask == 0 {
            for dir in dirs {
                out.push(file(dir, value));
            }
        }
        cnt -= 1;
    }
    out
}

/// glibc の `_nl_load_locale` の開き方(ディレクトリなら `SYS_<種類>`)。開けなければ errno。
fn open_locale_file(path: &Path, category: &str) -> Result<PathBuf, i32> {
    match std::fs::File::open(path) {
        Ok(file) => {
            let directory = file.metadata().map(|meta| meta.is_dir()).unwrap_or(false);
            if !directory {
                return Ok(path.to_path_buf());
            }
            let inner = path.join(format!("SYS_{category}"));
            std::fs::File::open(&inner)
                .map(|_| inner)
                .map_err(|error| error.raw_os_error().unwrap_or(5))
        }
        Err(error) => Err(error.raw_os_error().unwrap_or(5)),
    }
}

/// 種類のデータを見つけた所。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Source {
    /// 組み込みの C の地域設定。
    Builtin,
    /// 保管庫の記録(位置)。
    Archive(usize),
    /// 種類のファイル。
    File(PathBuf),
}

/// glibc の `setlocale` を 1 つのプロセスの中で続けて呼ぶ状態(試したファイルと、開いた結果)。
pub struct Glibc {
    locpath: Option<Vec<u8>>,
    decided: Vec<(PathBuf, Option<PathBuf>)>,
    archive_tried: bool,
    archive: Vec<(Vec<u8>, usize)>,
    errno: i32,
}

impl Glibc {
    pub fn new(locpath: Option<&[u8]>) -> Self {
        Self {
            locpath: locpath
                .filter(|value| !value.is_empty())
                .map(<[u8]>::to_vec),
            decided: Vec::new(),
            archive_tried: false,
            archive: Vec::new(),
            errno: 0,
        }
    }

    fn in_archive(&mut self, name: &[u8]) -> Option<usize> {
        if !self.archive_tried {
            self.archive_tried = true;
            match std::fs::read(ARCHIVE) {
                Ok(data) => self.archive = archive_names(&data),
                Err(error) => self.errno = error.raw_os_error().unwrap_or(5),
            }
        }
        let wanted = archive_name(name);
        self.archive
            .iter()
            .find(|(stored, _)| *stored == wanted)
            .map(|(_, record)| *record)
    }

    /// `_nl_find_locale`(1 つの種類)。見つけた所を返す。
    pub fn find(&mut self, category: &str, name: &[u8]) -> Option<Source> {
        if matches!(name, b"C" | b"POSIX") {
            return Some(Source::Builtin);
        }
        if !valid_name(name) {
            self.errno = 22;
            return None;
        }
        let mut lookup = name.to_vec();
        let dirs: Vec<PathBuf> = match &self.locpath {
            None => {
                if let Some(record) = self.in_archive(name) {
                    return Some(Source::Archive(record));
                }
                if let Some(alias) = expand_alias(name) {
                    if let Some(record) = self.in_archive(&alias) {
                        return Some(Source::Archive(record));
                    }
                    lookup = alias;
                }
                vec![PathBuf::from(DEFAULT_PATH)]
            }
            Some(path) => {
                if let Some(alias) = expand_alias(name) {
                    lookup = alias;
                }
                let mut dirs: Vec<PathBuf> = path
                    .split(|byte| *byte == b':')
                    .filter(|part| !part.is_empty())
                    .map(|part| PathBuf::from(OsStr::from_bytes(part)))
                    .collect();
                dirs.push(PathBuf::from(DEFAULT_PATH));
                dirs
            }
        };
        let parts = explode(&lookup);
        for (path, found_name, real) in candidates(&parts, &dirs, category) {
            if !real {
                continue;
            }
            let known = self
                .decided
                .iter()
                .find(|(known, _)| *known == path)
                .map(|(_, ok)| ok.clone());
            let opened = match known {
                Some(opened) => opened,
                None => {
                    let opened = match open_locale_file(&path, category) {
                        Ok(file) => Some(file),
                        Err(errno) => {
                            self.errno = errno;
                            None
                        }
                    };
                    self.decided.push((path.clone(), opened.clone()));
                    opened
                }
            };
            if let Some(file) = opened {
                // 文字コードを名前に含むなら、見つけた地域設定の文字コードと同じこと。
                let ok = match &parts.codeset {
                    None => true,
                    Some(codeset) => {
                        let found = explode(&found_name);
                        let found_codeset = if !found.normalized.is_empty() {
                            found.normalized
                        } else {
                            normalize_codeset(found.codeset.as_deref().unwrap_or_default())
                        };
                        !found_codeset.is_empty() && normalize_codeset(codeset) == found_codeset
                    }
                };
                return if ok { Some(Source::File(file)) } else { None };
            }
        }
        None
    }

    /// `setlocale(LC_ALL, …)`。名前は種類ごと。成り立てば種類ごとの (名前, 所)。
    pub fn setlocale_all(
        &mut self,
        name_of: &dyn Fn(&str) -> Vec<u8>,
    ) -> Option<Vec<(Vec<u8>, Source)>> {
        let mut found: Vec<(Vec<u8>, Source)> = Vec::new();
        for category in CATEGORIES.iter().rev() {
            let name = name_of(category);
            let source = self.find(category, &name)?;
            let name = if matches!(name.as_slice(), b"POSIX") {
                b"C".to_vec()
            } else {
                name
            };
            found.push((name, source));
        }
        found.reverse();
        Some(found)
    }

    pub fn clear_errno(&mut self) {
        self.errno = 0;
    }

    pub fn errno(&self) -> i32 {
        self.errno
    }
}

/// `LC_CTYPE` のデータの `CODESET` の名前。読めなければ `None`。
pub fn codeset_of(source: &Source) -> Option<Vec<u8>> {
    match source {
        Source::Builtin => Some(b"ANSI_X3.4-1968".to_vec()),
        Source::Archive(record) => {
            let data = std::fs::read(ARCHIVE).ok()?;
            // struct locrecent { refs, record[__LC_LAST] { offset, len } }。LC_CTYPE は 0 番。
            let offset = word(&data, record + 4)?;
            let length = word(&data, record + 8)?;
            ctype_codeset(data.get(offset..offset + length)?)
        }
        Source::File(path) => ctype_codeset(&std::fs::read(path).ok()?),
    }
}

/// `struct locale_file { magic, nstrings, strindex[] }` の `CODESET`。
fn ctype_codeset(data: &[u8]) -> Option<Vec<u8>> {
    if word(data, 0)? != CTYPE_MAGIC as usize {
        return None;
    }
    let count = word(data, 4)?;
    if CODESET_INDEX >= count {
        return None;
    }
    let at = word(data, 8 + CODESET_INDEX * 4)?;
    let rest = data.get(at..)?;
    let end = rest.iter().position(|b| *b == 0)?;
    Some(rest.get(..end)?.to_vec())
}

/// 訳した文の文字コード。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Codeset {
    Utf8,
    Ascii,
    /// UTF-8 でも ASCII でもない(Rust の中で変えず、道具を起動する)。
    Other(Vec<u8>),
}

impl Codeset {
    pub fn from_name(name: &[u8]) -> Self {
        match name {
            b"UTF-8" => Self::Utf8,
            b"ANSI_X3.4-1968" => Self::Ascii,
            other => Self::Other(other.to_vec()),
        }
    }
}

/// 翻訳ファイル(`.mo`)。
pub struct Catalog {
    data: Vec<u8>,
    big_endian: bool,
    count: usize,
    originals: usize,
    translations: usize,
    /// 見出しの charset が UTF-8 か。
    pub utf8: bool,
}

impl Catalog {
    pub fn parse(data: Vec<u8>) -> Option<Self> {
        let magic = data.get(..4)?;
        // 印 0x950412de を、書いた機械のバイト順で置く。
        let big_endian = match magic {
            [0xde, 0x12, 0x04, 0x95] => false,
            [0x95, 0x04, 0x12, 0xde] => true,
            _ => return None,
        };
        let mut catalog = Self {
            data,
            big_endian,
            count: 0,
            originals: 0,
            translations: 0,
            utf8: true,
        };
        catalog.count = catalog.u32_at(8)?;
        catalog.originals = catalog.u32_at(12)?;
        catalog.translations = catalog.u32_at(16)?;
        let header = catalog.lookup(b"").unwrap_or_default();
        catalog.utf8 = header_charset(&header)
            .map(|c| normalize_codeset(&c) == b"utf8")
            .unwrap_or(true);
        Some(catalog)
    }

    fn u32_at(&self, offset: usize) -> Option<usize> {
        let b = self.data.get(offset..offset + 4)?;
        let bytes = [b[0], b[1], b[2], b[3]];
        Some(if self.big_endian {
            u32::from_be_bytes(bytes)
        } else {
            u32::from_le_bytes(bytes)
        } as usize)
    }

    fn string(&self, table: usize, index: usize) -> Option<&[u8]> {
        let length = self.u32_at(table + index * 8)?;
        let offset = self.u32_at(table + index * 8 + 4)?;
        self.data.get(offset..offset + length)
    }

    /// msgid の訳(無ければ `None`)。
    pub fn lookup(&self, msgid: &[u8]) -> Option<Vec<u8>> {
        // 原文の表は並べてあるので二分探索する(glibc は hash を使うが、結果は同じ)。
        let (mut low, mut high) = (0usize, self.count);
        while low < high {
            let middle = (low + high) / 2;
            let original = self.string(self.originals, middle)?;
            match original.cmp(msgid) {
                std::cmp::Ordering::Equal => {
                    let translated = self.string(self.translations, middle)?;
                    // 複数形の訳は NUL で区切る。単数の形だけを使う。
                    let end = translated
                        .iter()
                        .position(|b| *b == 0)
                        .unwrap_or(translated.len());
                    return Some(translated.get(..end)?.to_vec());
                }
                std::cmp::Ordering::Less => low = middle + 1,
                std::cmp::Ordering::Greater => high = middle,
            }
        }
        None
    }
}

/// 見出しの `Content-Type: text/plain; charset=…` の値。
fn header_charset(header: &[u8]) -> Option<Vec<u8>> {
    let key = b"charset=";
    let at = header.windows(key.len()).position(|w| w == key)?;
    let rest = header.get(at + key.len()..)?;
    let end = rest
        .iter()
        .position(|b| matches!(b, b' ' | b'\t' | b'\n' | b';'))
        .unwrap_or(rest.len());
    Some(rest.get(..end)?.to_vec())
}

/// 訳を得られなかった(Rust の中で変えられない。道具を起動する)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NeedTool;

/// 道具の `setlocale(LC_ALL, "")` と gettext の結果。
pub struct ToolLocale {
    /// `LC_MESSAGES` の地域設定の名前(`setlocale` が失敗したら `C`)。
    pub messages: Vec<u8>,
    /// gettext が試す名前の並び(空なら訳さない)。
    pub languages: Vec<Vec<u8>>,
    /// `LC_CTYPE` の文字コード。
    pub codeset: Codeset,
    /// 翻訳ファイルを探す所(既定は `CATALOG_DIRS`。単体テストは差し替える)。
    pub dirs: Vec<PathBuf>,
    cache: HashMap<PathBuf, Option<Catalog>>,
}

/// gettext が試す名前の並び(`guess_category_value` と `LANGUAGE` の読み方)。`LC_MESSAGES` の地域設定が
/// `C`・`POSIX`・`C.UTF-8` なら `LANGUAGE` を見ずに空(訳さない)。`LANGUAGE` は `:` で区切り、空の項目は捨てる。
pub fn select_languages(messages: &[u8], language: Option<&[u8]>) -> Vec<Vec<u8>> {
    if is_c_locale(messages) {
        return Vec::new();
    }
    let list = language.filter(|v| !v.is_empty()).unwrap_or(messages);
    list.split(|b| *b == b':')
        .filter(|entry| !entry.is_empty())
        .map(<[u8]>::to_vec)
        .collect()
}

/// 1 つの名前の翻訳ファイルの候補(探す順。`locale.alias` で 1 段展開した名前の、所ごとの全部の形)。
pub fn catalog_candidates(dirs: &[PathBuf], language: &[u8], domain: &str) -> Vec<PathBuf> {
    let name = expand_alias(language).unwrap_or_else(|| language.to_vec());
    let names = variants(&name);
    let mut out = Vec::new();
    for dir in dirs {
        for variant in &names {
            let mut path = dir.clone();
            path.push(OsStr::from_bytes(variant));
            path.push("LC_MESSAGES");
            path.push(format!("{domain}.mo"));
            out.push(path);
        }
    }
    out
}

impl ToolLocale {
    /// 道具の環境(bash が子へ渡す環境)から決める。`lookup` は環境の最初の値(libc の getenv)。
    pub fn from_env(lookup: &dyn Fn(&[u8]) -> Option<Vec<u8>>) -> Self {
        let present = |name: &[u8]| lookup(name).filter(|value| !value.is_empty());
        if cfg!(target_os = "macos") {
            // Mac の道具は BSD の dirname・basename で、gettext を使わない。文字コードだけを libSystem で決める。
            let name = present(b"LC_ALL")
                .or_else(|| present(b"LC_CTYPE"))
                .or_else(|| present(b"LANG"))
                .unwrap_or_else(|| b"C".to_vec());
            let codeset = match mac_codeset(&name).as_deref() {
                Some(b"UTF-8") => Codeset::Utf8,
                Some(b"US-ASCII") | None => Codeset::Ascii,
                Some(other) => Codeset::Other(other.to_vec()),
            };
            return Self {
                messages: b"C".to_vec(),
                languages: Vec::new(),
                codeset,
                dirs: Vec::new(),
                cache: HashMap::new(),
            };
        }
        let mut glibc = Glibc::new(present(b"LOCPATH").as_deref());
        let found = glibc.setlocale_all(&|category: &str| {
            present(b"LC_ALL")
                .or_else(|| present(category.as_bytes()))
                .or_else(|| present(b"LANG"))
                .unwrap_or_else(|| b"C".to_vec())
        });
        let (messages, codeset) = match &found {
            Some(list) => {
                let messages = list
                    .get(5)
                    .map(|(name, _)| name.clone())
                    .unwrap_or_else(|| b"C".to_vec());
                let codeset = list
                    .first()
                    .and_then(|(_, source)| codeset_of(source))
                    .map(|name| Codeset::from_name(&name))
                    .unwrap_or_else(|| Codeset::Other(b"?".to_vec()));
                (messages, codeset)
            }
            None => (b"C".to_vec(), Codeset::Ascii),
        };
        let languages = select_languages(&messages, present(b"LANGUAGE").as_deref());
        Self {
            messages,
            languages,
            codeset,
            dirs: CATALOG_DIRS.iter().map(PathBuf::from).collect(),
            cache: HashMap::new(),
        }
    }

    /// 決めた値から作る(単体テスト)。
    #[cfg(test)]
    pub fn fixed(messages: &[u8], languages: &[&[u8]], codeset: Codeset) -> Self {
        Self {
            messages: messages.to_vec(),
            languages: languages.iter().map(|l| l.to_vec()).collect(),
            codeset,
            dirs: CATALOG_DIRS.iter().map(PathBuf::from).collect(),
            cache: HashMap::new(),
        }
    }

    fn catalog(&mut self, path: PathBuf) -> Option<&Catalog> {
        let entry = self
            .cache
            .entry(path.clone())
            .or_insert_with(|| std::fs::read(&path).ok().and_then(Catalog::parse));
        entry.as_ref()
    }

    /// `dgettext(domain, msgid)` を、道具の出力の文字コードに変えて返す。訳が UTF-8 でない翻訳ファイルに
    /// あれば `NeedTool`。
    pub fn gettext(&mut self, domain: &str, msgid: &[u8]) -> Result<Vec<u8>, NeedTool> {
        if let Codeset::Other(_) = self.codeset {
            return Err(NeedTool);
        }
        let codeset = self.codeset.clone();
        for language in self.languages.clone() {
            if matches!(language.as_slice(), b"C" | b"POSIX") {
                break;
            }
            // glibc は、最初に見つけた翻訳ファイルに msgid が無ければ、同じ名前の後の候補(ほかの名前の形と
            // locale-langpack)でも探す(`_nl_find_msg` の `successor`)。在る候補を探す順に全部見る。
            for path in catalog_candidates(&self.dirs, &language, domain) {
                let Some(catalog) = self.catalog(path) else {
                    continue;
                };
                if let Some(text) = catalog.lookup(msgid) {
                    if !catalog.utf8 {
                        return Err(NeedTool);
                    }
                    return Ok(convert(&text, &codeset));
                }
            }
        }
        Ok(msgid.to_vec())
    }

    /// 訳した errno の文(`strerror` を `libc.mo` で訳したもの。R4X-2 の `mktemp` の文で使う)。
    #[allow(dead_code)]
    pub fn strerror(&mut self, errno: i32) -> Result<Vec<u8>, NeedTool> {
        let english = super::strerror(errno);
        self.gettext("libc", english.as_bytes())
    }
}

#[cfg(target_os = "macos")]
fn mac_codeset(name: &[u8]) -> Option<Vec<u8>> {
    dev_workflow_platform::implement_agent::mac_codeset(name)
}

#[cfg(not(target_os = "macos"))]
fn mac_codeset(_name: &[u8]) -> Option<Vec<u8>> {
    None
}

/// `C`・`POSIX`・`C.UTF-8`(gettext は `LANGUAGE` を見ずに訳さない。基準の容器で確かめた)。
pub fn is_c_locale(name: &[u8]) -> bool {
    matches!(name, b"C" | b"POSIX")
        || name
            .strip_prefix(b"C.")
            .is_some_and(|codeset| normalize_codeset(codeset) == b"utf8")
}

/// UTF-8 の訳を、出力の文字コードへ変える。
pub fn convert(text: &[u8], codeset: &Codeset) -> Vec<u8> {
    match codeset {
        Codeset::Utf8 | Codeset::Other(_) => text.to_vec(),
        Codeset::Ascii => {
            let mut out = Vec::with_capacity(text.len());
            let mut index = 0usize;
            while let Some(&byte) = text.get(index) {
                if byte < 0x80 {
                    out.push(byte);
                    index += 1;
                    continue;
                }
                let length = match byte {
                    0xc0..=0xdf => 2,
                    0xe0..=0xef => 3,
                    0xf0..=0xf7 => 4,
                    _ => 1,
                };
                out.push(b'?');
                index += length;
            }
            out
        }
    }
}

/// 旧 bash の起動の `LC_ALL` の警告と、352 行の `LC_ALL=C` を戻すときの警告を決める状態。
pub struct BashLocale {
    glibc: Glibc,
}

impl BashLocale {
    /// 起動: `set_default_locale` の `setlocale(LC_ALL, "")`(名前は環境の最初の値)。
    pub fn start(first: &dyn Fn(&[u8]) -> Option<Vec<u8>>) -> Self {
        let present = |name: &[u8]| first(name).filter(|value| !value.is_empty());
        let mut glibc = Glibc::new(present(b"LOCPATH").as_deref());
        let _ = glibc.setlocale_all(&|category: &str| {
            present(b"LC_ALL")
                .or_else(|| present(category.as_bytes()))
                .or_else(|| present(b"LANG"))
                .unwrap_or_else(|| b"C".to_vec())
        });
        Self { glibc }
    }

    /// `set_locale_var("LC_ALL", 値)`: 空でない値の `setlocale` が失敗したら、警告の文
    /// (`setlocale: LC_ALL: cannot change locale (<名前>)` と、errno が 0 でなければ `: <strerror>`)。
    pub fn set_lc_all(&mut self, value: &[u8]) -> Option<(Vec<u8>, i32)> {
        if value.is_empty() {
            return None;
        }
        self.glibc.clear_errno();
        let ok = if value == b"C" || value == b"POSIX" {
            true
        } else {
            self.glibc
                .setlocale_all(&|_: &str| value.to_vec())
                .is_some()
        };
        if ok {
            return None;
        }
        let mut text = b"setlocale: LC_ALL: cannot change locale (".to_vec();
        text.extend_from_slice(value);
        text.push(b')');
        Some((text, self.glibc.errno()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_exploded_and_listed_like_glibc() {
        assert_eq!(normalize_codeset(b"UTF-8"), b"utf8");
        assert_eq!(normalize_codeset(b"8859-1"), b"iso88591");
        assert_eq!(archive_name(b"ja_JP.UTF-8@x"), b"ja_JP.utf8@x");
        // 基準の容器で strace で確かめた gettext の名前の順。
        assert_eq!(
            variants(b"ja_JP.UTF-8"),
            [
                &b"ja_JP.UTF-8"[..],
                b"ja_JP.utf8",
                b"ja_JP",
                b"ja.UTF-8",
                b"ja.utf8",
                b"ja"
            ]
        );
        assert_eq!(
            variants(b"ja_JP.eucJP@foo"),
            [
                &b"ja_JP.eucJP@foo"[..],
                b"ja_JP.eucjp@foo",
                b"ja_JP@foo",
                b"ja.eucJP@foo",
                b"ja.eucjp@foo",
                b"ja@foo",
                b"ja_JP.eucJP",
                b"ja_JP.eucjp",
                b"ja_JP",
                b"ja.eucJP",
                b"ja.eucjp",
                b"ja"
            ]
        );
        assert_eq!(variants(b"fr"), [&b"fr"[..]]);
        assert!(is_c_locale(b"C.UTF-8"));
        assert!(is_c_locale(b"C.utf8"));
        assert!(is_c_locale(b"POSIX"));
        assert!(!is_c_locale(b"ja_JP.UTF-8"));
    }

    #[test]
    fn ascii_conversion_replaces_each_character() {
        let text = "無効なオプション -- 'x'".as_bytes();
        assert_eq!(convert(text, &Codeset::Ascii), b"???????? -- 'x'");
        assert_eq!(convert(text, &Codeset::Utf8), text);
    }

    #[test]
    fn mo_files_are_read_in_both_byte_orders() {
        // 2 つの項目(見出しと 1 つの訳)を持つ小さな .mo を組む。
        fn build(big: bool, charset: &str) -> Vec<u8> {
            let put = |v: u32| {
                if big {
                    v.to_be_bytes()
                } else {
                    v.to_le_bytes()
                }
            };
            let header = format!("Content-Type: text/plain; charset={charset}\n");
            let originals: [&[u8]; 2] = [b"", b"missing operand"];
            let translations: [&[u8]; 2] = [header.as_bytes(), "オペランドがありません".as_bytes()];
            let mut data = Vec::new();
            data.extend_from_slice(&put(0x9504_12de));
            data.extend_from_slice(&put(0));
            data.extend_from_slice(&put(2));
            data.extend_from_slice(&put(28));
            data.extend_from_slice(&put(44));
            data.extend_from_slice(&put(0));
            data.extend_from_slice(&put(0));
            let mut strings = Vec::new();
            let base = 60u32;
            let mut table = Vec::new();
            for s in originals.iter().chain(translations.iter()) {
                table.push((s.len() as u32, base + strings.len() as u32));
                strings.extend_from_slice(s);
                strings.push(0);
            }
            for (length, offset) in table {
                data.extend_from_slice(&put(length));
                data.extend_from_slice(&put(offset));
            }
            data.extend_from_slice(&strings);
            data
        }
        for big in [false, true] {
            let catalog = Catalog::parse(build(big, "UTF-8")).unwrap();
            assert!(catalog.utf8);
            assert_eq!(
                catalog.lookup(b"missing operand").unwrap(),
                "オペランドがありません".as_bytes()
            );
            assert_eq!(catalog.lookup(b"nothing"), None);
            let euc = Catalog::parse(build(big, "EUC-JP")).unwrap();
            assert!(!euc.utf8);
        }
    }

    #[test]
    fn ctype_codeset_reads_index_14() {
        let mut data = Vec::new();
        data.extend_from_slice(&CTYPE_MAGIC.to_ne_bytes());
        data.extend_from_slice(&20u32.to_ne_bytes());
        let table_end = 8 + 20 * 4;
        for index in 0..20u32 {
            let at = if index == 14 { table_end as u32 } else { 0 };
            data.extend_from_slice(&at.to_ne_bytes());
        }
        data.extend_from_slice(b"EUC-JP\0");
        assert_eq!(ctype_codeset(&data).unwrap(), b"EUC-JP");
    }
}
