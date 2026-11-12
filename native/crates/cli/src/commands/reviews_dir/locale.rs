//! 旧bashの`setlocale`の警告(`LC_ALL`)。2026-11-12 00:35の利用者決定(警告は英語の文で出す)。
//!
//! 旧bash 5.2.21は、起動の`set_default_locale`で`setlocale(LC_ALL, "")`(名前はlibcが環境の最初の値から
//! 読む)を呼び、変数を取り込んだ後の`set_default_lang`で`errno = 0`にしてから`setlocale(LC_ALL, <最後の値>)`
//! を呼ぶ。失敗したら`setlocale: LC_ALL: cannot change locale (<名前>)`を出し、`errno`が0でなければ
//! `: <strerror>`を足す。
//!
//! Linuxはglibc 2.39の`setlocale`・`_nl_find_locale`・`_nl_load_locale_from_archive`・`_nl_expand_alias`・
//! `_nl_make_l10nflist`の手順を移した: 種類を`LC_IDENTIFICATION`から`LC_CTYPE`へ逆に読み、最初の失敗で
//! 止める。名前の形の検査(`valid_locale_name`。不正なら`EINVAL`)・`C`と`POSIX`・`LOCPATH`が無いときの
//! `locale-archive`(文字コードを正規化した名前)・別名・ディレクトリの候補(`XPG`の部分を外していく順)・
//! 文字コードの確かめ。glibcは一度試したファイルを覚え、2回目は開かないので`errno`が立たない。
//! ファイルは実際に開いて、glibcと同じ`errno`を得る。
//! Macは`/usr/share/locale/<名前>/`を見る(単位6のCIで確かめる)。

use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

/// glibcの種類の番号の順(`LC_ALL`の6を除く)。`setlocale(LC_ALL, ...)`はこの逆の順に読む。
const CATEGORIES: [&str; 12] = [
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

/// glibcの`_nl_normalize_codeset`: 英数字だけを小文字で残し、数字だけなら`iso`を前に付ける。
fn normalize_codeset(codeset: &[u8]) -> Vec<u8> {
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

/// glibcの`valid_locale_name`。
fn valid_name(name: &[u8]) -> bool {
    let n = name.len();
    if n > 255 || name.windows(4).any(|window| window == b"/../") || name == b".." {
        return false;
    }
    if n >= 3 && (name.starts_with(b"../") || name.ends_with(b"/..")) {
        return false;
    }
    !(name.contains(&b'/') && name[0] != b'/')
}

/// `locale-archive`の名前の表(`locarhead`の`namehash`)。読めなければ`None`(開けない理由のerrno)。
fn archive_names() -> Result<Vec<Vec<u8>>, i32> {
    let data = std::fs::read(ARCHIVE).map_err(|error| error.raw_os_error().unwrap_or(5))?;
    let word = |offset: usize| -> Option<usize> {
        data.get(offset..offset + 4)
            .map(|bytes| u32::from_ne_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as usize)
    };
    let mut names = Vec::new();
    // struct locarhead { magic, serial, namehash_offset, namehash_used, namehash_size, ... }
    let (Some(magic), Some(offset), Some(size)) = (word(0), word(8), word(16)) else {
        return Ok(names);
    };
    if magic != 0xde02_0109 {
        return Ok(names);
    }
    for index in 0..size {
        // struct namehashent { hashval, name_offset, locrec_offset }
        let entry = offset + index * 12;
        let (Some(name_offset), Some(record)) = (word(entry + 4), word(entry + 8)) else {
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
            names.push(rest[..end].to_vec());
        }
    }
    Ok(names)
}

/// `_nl_load_locale_from_archive`の名前(文字コードの部分を正規化した名前)。
fn archive_name(name: &[u8]) -> Vec<u8> {
    let Some(dot) = name.iter().position(|byte| *byte == b'.') else {
        return name.to_vec();
    };
    if matches!(name.get(dot + 1), None | Some(b'@')) {
        return name.to_vec();
    }
    let rest = name[dot + 1..]
        .iter()
        .position(|byte| *byte == b'@')
        .map_or(name.len(), |index| dot + 1 + index);
    let mut out = name[..dot + 1].to_vec();
    out.extend_from_slice(&normalize_codeset(&name[dot + 1..rest]));
    out.extend_from_slice(&name[rest..]);
    out
}

/// `_nl_expand_alias`(`locale.alias`の最初の行。名前は大文字小文字を区別しない)。
fn expand_alias(name: &[u8]) -> Option<Vec<u8>> {
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

/// `_nl_explode_name`の部分(言語・地域・文字コード・正規化した文字コード・修飾)と`mask`。
struct Parts {
    language: Vec<u8>,
    territory: Vec<u8>,
    codeset: Option<Vec<u8>>,
    normalized: Vec<u8>,
    modifier: Vec<u8>,
    mask: u32,
}

const XPG_NORM_CODESET: u32 = 1;
const XPG_CODESET: u32 = 2;
const XPG_TERRITORY: u32 = 4;
const XPG_MODIFIER: u32 = 8;

fn explode(name: &[u8]) -> Parts {
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
        parts.language = name[..language_end].to_vec();
        index = language_end;
        if name.get(index) == Some(&b'_') {
            let start = index + 1;
            let end = name[start..]
                .iter()
                .position(|byte| matches!(byte, b'.' | b'@'))
                .map_or(name.len(), |offset| start + offset);
            parts.territory = name[start..end].to_vec();
            parts.mask |= XPG_TERRITORY;
            index = end;
        }
        if name.get(index) == Some(&b'.') {
            let start = index + 1;
            let end = name[start..]
                .iter()
                .position(|byte| *byte == b'@')
                .map_or(name.len(), |offset| start + offset);
            let codeset = name[start..end].to_vec();
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
        parts.modifier = name[index + 1..].to_vec();
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

/// `mask`の名前の部分(`_nl_make_l10nflist`のファイル名の`<言語>[_地域][.文字コード][.正規化][@修飾]`)。
fn variant(parts: &Parts, mask: u32) -> Vec<u8> {
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

/// 1つの種類の候補のファイル(`(パス, 試す)`の並び。試さないのは擬似の項目)。
fn candidates(parts: &Parts, dirs: &[PathBuf], category: &str) -> Vec<(PathBuf, Vec<u8>, bool)> {
    let mut out = Vec::new();
    let file = |dir: &Path, mask: u32| -> (PathBuf, Vec<u8>, bool) {
        let name = variant(parts, mask);
        let pseudo = mask & XPG_CODESET != 0 && mask & XPG_NORM_CODESET != 0;
        // glibcは文字列をつなぐ(`<dir>/<name>/<category>`)。名前が`/`で始まっても前を捨てない。
        let mut path = dir.as_os_str().as_bytes().to_vec();
        path.push(b'/');
        path.extend_from_slice(&name);
        path.push(b'/');
        path.extend_from_slice(category.as_bytes());
        (PathBuf::from(OsStr::from_bytes(&path)), name, !pseudo)
    };
    let mask = parts.mask;
    let mut cnt: i64 = if dirs.len() == 1 {
        out.push(file(&dirs[0], mask));
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

/// glibcの`_nl_load_locale`の開き方(ディレクトリなら`SYS_<種類>`)。開けなければerrno。
fn open_locale_file(path: &Path, category: &str) -> Result<(), i32> {
    match std::fs::File::open(path) {
        Ok(file) => {
            let directory = file.metadata().map(|meta| meta.is_dir()).unwrap_or(false);
            if !directory {
                return Ok(());
            }
            std::fs::File::open(path.join(format!("SYS_{category}")))
                .map(|_| ())
                .map_err(|error| error.raw_os_error().unwrap_or(5))
        }
        Err(error) => Err(error.raw_os_error().unwrap_or(5)),
    }
}

/// glibcの`setlocale`を1つのプロセスの中で続けて呼ぶ状態(試したファイルと、開いた結果)。
pub struct Glibc {
    locpath: Option<Vec<u8>>,
    decided: Vec<(PathBuf, bool)>,
    archive_tried: bool,
    archive: Vec<Vec<u8>>,
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

    fn in_archive(&mut self, name: &[u8]) -> bool {
        if !self.archive_tried {
            self.archive_tried = true;
            match archive_names() {
                Ok(names) => self.archive = names,
                Err(errno) => self.errno = errno,
            }
        }
        let wanted = archive_name(name);
        self.archive.contains(&wanted)
    }

    /// `_nl_find_locale`(1つの種類)。成り立てば真。
    fn find(&mut self, category: &str, name: &[u8]) -> bool {
        if matches!(name, b"C" | b"POSIX") {
            return true;
        }
        if !valid_name(name) {
            self.errno = 22;
            return false;
        }
        let mut lookup = name.to_vec();
        let dirs: Vec<PathBuf> = match &self.locpath {
            None => {
                if self.in_archive(name) {
                    return true;
                }
                if let Some(alias) = expand_alias(name) {
                    if self.in_archive(&alias) {
                        return true;
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
                .map(|(_, ok)| *ok);
            let ok = match known {
                Some(ok) => ok,
                None => {
                    let ok = match open_locale_file(&path, category) {
                        Ok(()) => true,
                        Err(errno) => {
                            self.errno = errno;
                            false
                        }
                    };
                    self.decided.push((path.clone(), ok));
                    ok
                }
            };
            if ok {
                // 文字コードを名前に含むなら、見つけた地域設定の文字コードと同じこと。
                return match &parts.codeset {
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
            }
        }
        false
    }

    /// `setlocale(LC_ALL, ...)`。名前は種類ごと(`""`のときの環境の読み方は呼出側が渡す)。
    pub fn setlocale_all(&mut self, name_of: &dyn Fn(&str) -> Vec<u8>) -> bool {
        for category in CATEGORIES.iter().rev() {
            let name = name_of(category);
            if !self.find(category, &name) {
                return false;
            }
        }
        true
    }

    pub fn clear_errno(&mut self) {
        self.errno = 0;
    }

    pub fn errno(&self) -> i32 {
        self.errno
    }
}

/// 旧bashの起動の`LC_ALL`の警告の行(無ければ`None`)。
///
/// `first`は環境の最初の値を読む(libcの`getenv`)。`lc_all`は旧bashが取り込んだ最後の値。
pub fn lc_all_warning(first: &dyn Fn(&[u8]) -> Option<Vec<u8>>, lc_all: &[u8]) -> Option<Vec<u8>> {
    if lc_all.is_empty() {
        return None;
    }
    let (ok, errno) = if cfg!(target_os = "linux") {
        let mut glibc = Glibc::new(first(b"LOCPATH").as_deref());
        let present = |name: &[u8]| first(name).filter(|value| !value.is_empty());
        // set_default_locale: setlocale(LC_ALL, "")
        let _ = glibc.setlocale_all(&|category: &str| {
            present(b"LC_ALL")
                .or_else(|| present(category.as_bytes()))
                .or_else(|| present(b"LANG"))
                .unwrap_or_else(|| b"C".to_vec())
        });
        // set_default_lang: errno = 0; setlocale(LC_ALL, lc_all)
        glibc.clear_errno();
        let ok = glibc.setlocale_all(&|_: &str| lc_all.to_vec());
        (ok, glibc.errno())
    } else {
        let directory = PathBuf::from("/usr/share/locale").join(OsStr::from_bytes(lc_all));
        (
            matches!(lc_all, b"C" | b"POSIX") || directory.join("LC_CTYPE").exists(),
            0,
        )
    };
    if ok {
        return None;
    }
    let mut line = b"bash: warning: setlocale: LC_ALL: cannot change locale (".to_vec();
    line.extend_from_slice(lc_all);
    line.push(b')');
    if errno != 0 {
        line.extend_from_slice(b": ");
        line.extend_from_slice(super::super::r4g_support::strerror::strerror(errno).as_bytes());
    }
    line.push(b'\n');
    Some(line)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_are_exploded_and_listed_like_glibc() {
        assert_eq!(normalize_codeset(b"UTF-8"), b"utf8");
        assert_eq!(normalize_codeset(b"8859-1"), b"iso88591");
        assert_eq!(archive_name(b"ja_JP.UTF-8@x"), b"ja_JP.utf8@x");
        assert!(!valid_name(b"a/b"));
        assert!(!valid_name(b"ja_JP.utf8/../C"));
        assert!(valid_name(b"/usr/lib/locale/C.utf8"));
        let parts = explode(b"C.UTF-8@x");
        assert_eq!(parts.mask, XPG_CODESET | XPG_NORM_CODESET | XPG_MODIFIER);
        let names: Vec<Vec<u8>> = candidates(&parts, &[PathBuf::from("/d")], "LC_CTYPE")
            .into_iter()
            .filter(|(_, _, real)| *real)
            .map(|(_, name, _)| name)
            .collect();
        assert_eq!(
            names,
            [
                &b"C.UTF-8@x"[..],
                b"C.utf8@x",
                b"C@x",
                b"C.UTF-8",
                b"C.utf8",
                b"C"
            ]
        );
    }

    /// この機械の`locale -a`の名前は成り立ち、入っていない名前は成り立たない。
    #[test]
    fn installed_locales_match_locale_a() {
        if !cfg!(target_os = "linux") {
            return;
        }
        let Ok(output) = std::process::Command::new("locale").arg("-a").output() else {
            return;
        };
        let none = |_: &[u8]| None;
        for name in String::from_utf8_lossy(&output.stdout).lines() {
            assert_eq!(lc_all_warning(&none, name.as_bytes()), None, "{name}");
        }
        assert!(lc_all_warning(&none, b"xx_YY.UTF-8").is_some());
    }
}
