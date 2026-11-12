//! Whether the old Bash could switch to the `LC_ALL` it inherited.
//!
//! Bash 5.2.21 warns at start-up when `setlocale(LC_ALL, value)` fails. The
//! Linux build links musl, whose `setlocale` accepts any name, so the answer is
//! not asked of the C library: the glibc 2.39 rules are read from the files
//! (`LOCPATH`, the locale archive, the per-locale directories, the normalized
//! codeset name and `locale.alias`). On macOS, `newlocale` answers without
//! changing the locale of the process.

/// The outcome of the old shell's `setlocale(LC_ALL, value)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Locale {
    /// The locale is available; Bash prints nothing.
    Found,
    /// Not available, with `errno` left at 0.
    Missing,
    /// Not available, with this `errno` (Bash adds `: strerror(errno)`).
    Failed(i32),
}

/// `setlocale(LC_ALL, name)` of the old shell for a non-empty `name`.
pub fn lc_all(name: &[u8], locpath: Option<&[u8]>) -> Locale {
    imp::lc_all(name, locpath)
}

#[cfg(target_os = "macos")]
mod imp {
    use super::Locale;
    pub fn lc_all(name: &[u8], _locpath: Option<&[u8]>) -> Locale {
        let Ok(name) = std::ffi::CString::new(name) else {
            return Locale::Missing;
        };
        // SAFETY: a valid C string and a null base; the result is freed below.
        let found =
            unsafe { libc::newlocale(libc::LC_ALL_MASK, name.as_ptr(), std::ptr::null_mut()) };
        if found.is_null() {
            match std::io::Error::last_os_error().raw_os_error() {
                Some(0) | None => Locale::Missing,
                Some(code) => Locale::Failed(code),
            }
        } else {
            // SAFETY: returned by newlocale above and not used afterwards.
            unsafe { libc::freelocale(found) };
            Locale::Found
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    use super::Locale;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    /// glibc's compiled-in places on the CI's Ubuntu 24.04.
    const DEFAULT_PATH: &[u8] = b"/usr/lib/locale";
    const ARCHIVE: &str = "/usr/lib/locale/locale-archive";
    const ALIAS: &str = "/usr/share/locale/locale.alias";
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
    const XPG_NORM_CODESET: u8 = 1;
    const XPG_CODESET: u8 = 2;
    const XPG_TERRITORY: u8 = 4;
    const XPG_MODIFIER: u8 = 8;

    pub fn lc_all(name: &[u8], locpath: Option<&[u8]>) -> Locale {
        // setlocale.c: LOCPATH (when not empty) replaces the archive and is
        // searched before the default directory.
        let dirs = match locpath.filter(|p| !p.is_empty()) {
            Some(p) => {
                let mut dirs: Vec<Vec<u8>> = p
                    .split(|b| *b == b':')
                    .filter(|d| !d.is_empty())
                    .map(<[u8]>::to_vec)
                    .collect();
                dirs.push(DEFAULT_PATH.to_vec());
                Some(dirs)
            }
            None => None,
        };
        if !name.contains(&b';') {
            return match find(name, dirs.as_deref()) {
                Ok(()) => Locale::Found,
                Err(code) => code.map_or(Locale::Missing, Locale::Failed),
            };
        }
        // A composite name `CATEGORY=VALUE;…` must name every category.
        let mut values: [Option<&[u8]>; 12] = [None; 12];
        let mut rest = name;
        while let Some(eq) = rest.iter().position(|b| *b == b'=') {
            let Some(index) = CATEGORIES.iter().position(|c| c.as_bytes() == &rest[..eq]) else {
                return Locale::Failed(libc::EINVAL);
            };
            let value = &rest[eq + 1..];
            match value.iter().position(|b| *b == b';') {
                Some(end) => {
                    values[index] = Some(&value[..end]);
                    rest = &value[end + 1..];
                }
                None => {
                    values[index] = Some(value);
                    break;
                }
            }
        }
        if values.iter().any(Option::is_none) {
            return Locale::Failed(libc::EINVAL);
        }
        // Loaded from the last category down, as setlocale.c does.
        for value in values.iter().rev().flatten() {
            if let Err(code) = find(value, dirs.as_deref()) {
                // Observed: the failed open's ENOENT is left in errno here.
                return Locale::Failed(code.unwrap_or(libc::ENOENT));
            }
        }
        Locale::Found
    }

    /// `_nl_find_locale` for every category: `Err(None)` when not found with
    /// errno 0, `Err(Some(EINVAL))` for an invalid name.
    fn find(name: &[u8], dirs: Option<&[Vec<u8>]>) -> Result<(), Option<i32>> {
        if name.is_empty() || name == b"C" || name == b"POSIX" {
            return Ok(());
        }
        if !valid(name) {
            return Err(Some(libc::EINVAL));
        }
        let (dirs, loc_name) = match dirs {
            None => {
                if in_archive(name) {
                    return Ok(());
                }
                let alias = expand_alias(name);
                if alias.as_deref().is_some_and(in_archive) {
                    return Ok(());
                }
                (
                    vec![DEFAULT_PATH.to_vec()],
                    alias.unwrap_or_else(|| name.to_vec()),
                )
            }
            Some(dirs) => (
                dirs.to_vec(),
                expand_alias(name).unwrap_or_else(|| name.to_vec()),
            ),
        };
        let candidates = l10nflist(&loc_name);
        let found = CATEGORIES.iter().all(|category| {
            dirs.iter().any(|dir| {
                candidates.iter().any(|candidate| {
                    let mut path = dir.clone();
                    path.push(b'/');
                    path.extend_from_slice(candidate);
                    path.push(b'/');
                    path.extend_from_slice(category.as_bytes());
                    category_file(&path, category)
                })
            })
        });
        if found { Ok(()) } else { Err(None) }
    }

    /// `_nl_load_locale`: the category file, or `SYS_<CATEGORY>` in a directory.
    fn category_file(path: &[u8], category: &str) -> bool {
        let path = Path::new(std::ffi::OsStr::from_bytes(path));
        match std::fs::metadata(path) {
            Ok(m) if m.is_file() => std::fs::File::open(path).is_ok(),
            Ok(m) if m.is_dir() => {
                let inner = path.join(format!("SYS_{category}"));
                std::fs::metadata(&inner).is_ok_and(|m| m.is_file())
                    && std::fs::File::open(&inner).is_ok()
            }
            _ => false,
        }
    }

    /// findlocale.c `valid_locale_name`.
    fn valid(name: &[u8]) -> bool {
        let n = name.len();
        if n > 255 || name.windows(4).any(|w| w == b"/../") || name == b".." {
            return false;
        }
        if n >= 3 && (name.starts_with(b"../") || name.ends_with(b"/..")) {
            return false;
        }
        !(name.contains(&b'/') && name[0] != b'/')
    }

    /// l10nflist.c `_nl_normalize_codeset`.
    fn normalize(codeset: &[u8]) -> Vec<u8> {
        let digits_only = codeset
            .iter()
            .filter(|b| b.is_ascii_alphanumeric())
            .all(u8::is_ascii_digit);
        let mut out = if digits_only { b"iso".to_vec() } else { vec![] };
        for b in codeset {
            if b.is_ascii_alphabetic() {
                out.push(b.to_ascii_lowercase());
            } else if b.is_ascii_digit() {
                out.push(*b);
            }
        }
        out
    }

    /// explodename.c and the names `_nl_make_l10nflist` tries for them.
    fn l10nflist(name: &[u8]) -> Vec<Vec<u8>> {
        let stop = |b: &u8| matches!(b, b'_' | b'.' | b'@');
        let lang_end = name.iter().position(stop).unwrap_or(name.len());
        if lang_end == 0 {
            return vec![name.to_vec()];
        }
        let language = &name[..lang_end];
        let mut cp = lang_end;
        let mut mask = 0;
        let (mut territory, mut codeset, mut normalized, mut modifier): (
            &[u8],
            &[u8],
            Vec<u8>,
            &[u8],
        ) = (b"", b"", vec![], b"");
        if name.get(cp) == Some(&b'_') {
            let start = cp + 1;
            cp = start
                + name[start..]
                    .iter()
                    .position(|b| matches!(b, b'.' | b'@'))
                    .unwrap_or(name.len() - start);
            territory = &name[start..cp];
            mask |= XPG_TERRITORY;
        }
        if name.get(cp) == Some(&b'.') {
            let start = cp + 1;
            cp = start
                + name[start..]
                    .iter()
                    .position(|b| *b == b'@')
                    .unwrap_or(name.len() - start);
            codeset = &name[start..cp];
            mask |= XPG_CODESET;
            if !codeset.is_empty() {
                normalized = normalize(codeset);
                // strcmp sees the codeset up to the end of the name here.
                if normalized != name[start..] {
                    mask |= XPG_NORM_CODESET;
                }
            }
        }
        if name.get(cp) == Some(&b'@') {
            modifier = &name[cp + 1..];
            if !modifier.is_empty() {
                mask |= XPG_MODIFIER;
            }
        }
        if territory.is_empty() {
            mask &= !XPG_TERRITORY;
        }
        if codeset.is_empty() {
            mask &= !XPG_CODESET;
        }
        let mut out = vec![];
        for subset in (0..=mask).rev() {
            if subset & !mask != 0 || (subset & XPG_CODESET != 0 && subset & XPG_NORM_CODESET != 0)
            {
                continue;
            }
            let mut candidate = language.to_vec();
            if subset & XPG_TERRITORY != 0 {
                candidate.push(b'_');
                candidate.extend_from_slice(territory);
            }
            if subset & XPG_CODESET != 0 {
                candidate.push(b'.');
                candidate.extend_from_slice(codeset);
            }
            if subset & XPG_NORM_CODESET != 0 {
                candidate.push(b'.');
                candidate.extend_from_slice(&normalized);
            }
            if subset & XPG_MODIFIER != 0 {
                candidate.push(b'@');
                candidate.extend_from_slice(modifier);
            }
            out.push(candidate);
        }
        out
    }

    /// loadarchive.c: the name with its codeset normalized, in the name table.
    fn in_archive(name: &[u8]) -> bool {
        let mut name = name.to_vec();
        if let Some(dot) = name.iter().position(|b| *b == b'.')
            && !matches!(name.get(dot + 1), Some(b'@') | None)
        {
            let end = name[dot + 1..]
                .iter()
                .position(|b| *b == b'@')
                .map_or(name.len(), |p| dot + 1 + p);
            let normalized = normalize(&name[dot + 1..end]);
            name = [&name[..=dot], &normalized[..], &name[end..]].concat();
        }
        let Ok(data) = std::fs::read(ARCHIVE) else {
            return false;
        };
        let word = |at: usize| -> Option<usize> {
            data.get(at..at + 4)
                .map(|b| u32::from_ne_bytes([b[0], b[1], b[2], b[3]]) as usize)
        };
        // struct locarhead: magic, serial, namehash_offset, namehash_used, namehash_size, string_offset…
        if word(0) != Some(0xde02_0109) {
            return false;
        }
        let (Some(offset), Some(size)) = (word(8), word(16)) else {
            return false;
        };
        (0..size).any(|i| {
            // struct namehashent: hashval, name_offset, locrec_offset.
            let entry = offset + i * 12;
            match (word(entry + 4), word(entry + 8)) {
                (Some(at), Some(record)) if at != 0 && record != 0 => data
                    .get(at..)
                    .and_then(|s| s.iter().position(|b| *b == 0).map(|e| &s[..e]))
                    .is_some_and(|stored| stored == name.as_slice()),
                _ => false,
            }
        })
    }

    /// localealias.c: one level, the alias compared without case.
    fn expand_alias(name: &[u8]) -> Option<Vec<u8>> {
        let text = std::fs::read(ALIAS).ok()?;
        for line in text.split(|b| *b == b'\n') {
            let line = trim_start(line);
            if line.is_empty() || line[0] == b'#' {
                continue;
            }
            let end = line
                .iter()
                .position(|b| b.is_ascii_whitespace())
                .unwrap_or(line.len());
            let (alias, rest) = line.split_at(end);
            let rest = trim_start(rest);
            let value_end = rest
                .iter()
                .position(|b| b.is_ascii_whitespace())
                .unwrap_or(rest.len());
            let value = &rest[..value_end];
            if !value.is_empty() && alias.eq_ignore_ascii_case(name) {
                return Some(value.to_vec());
            }
        }
        None
    }

    fn trim_start(s: &[u8]) -> &[u8] {
        let start = s
            .iter()
            .position(|b| !b.is_ascii_whitespace())
            .unwrap_or(s.len());
        &s[start..]
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn names_follow_the_glibc_explosion() {
            assert_eq!(normalize(b"UTF-8"), b"utf8");
            assert_eq!(normalize(b"8859-1"), b"iso88591");
            let names = l10nflist(b"xx_YY.UTF-8@m");
            assert_eq!(
                names.first().map(Vec::as_slice),
                Some(&b"xx_YY.UTF-8@m"[..])
            );
            assert!(names.iter().any(|n| n == b"xx_YY.utf8@m"));
            assert!(names.iter().any(|n| n == b"xx"));
            assert!(!names.iter().any(|n| n == b"xx_YY.UTF-8.utf8@m"));
            assert!(valid(b"/x") && !valid(b"a/b") && !valid(b"..") && !valid(&[b'a'; 256]));
        }
    }
}
