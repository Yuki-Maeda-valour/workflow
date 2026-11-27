//! In-process replacements for the coreutils and findutils the old script ran
//! (2026-11-11 23:19). Failure texts follow the base versions (coreutils 9.4,
//! findutils 4.9.0, glibc 2.39 `strerror`, C locale) recorded in the
//! start-location table of the comparison fixtures.
use super::bytes::joined;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
// errno values shared by Linux and macOS.
const ENOENT: i32 = 2;
const EEXIST: i32 = 17;
const ENOTDIR: i32 = 20;
const EISDIR: i32 = 21;

/// `strerror` text: the fixed glibc 2.39 table on Linux, Darwin's own on macOS.
pub fn strerror(error: &io::Error) -> String {
    let Some(code) = error.raw_os_error() else {
        return error.to_string();
    };
    #[cfg(target_os = "linux")]
    {
        usize::try_from(code)
            .ok()
            .and_then(|i| super::strerror_table::GLIBC_2_39.get(i))
            .map_or_else(|| format!("Unknown error {code}"), |s| (*s).to_owned())
    }
    #[cfg(not(target_os = "linux"))]
    {
        dev_workflow_platform::diff_snapshot::fs::os_strerror(code)
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Style {
    /// gnulib `locale_quoting_style` in the C locale (`quote`).
    Locale,
    /// `shell_escape_always_quoting_style` (`quoteaf`).
    Shell,
    /// `c_quoting_style`, used when a shell-quoted name only has an apostrophe.
    C,
}

/// gnulib quotearg_buffer_restyled for the unibyte C locale.
fn restyled(arg: &[u8], style: Style) -> Vec<u8> {
    let shell = style == Style::Shell;
    let quote: &[u8] = if style == Style::C { b"\"" } else { b"'" };
    let mut out = Vec::new();
    let mut pending = false;
    let mut encountered_single_quote = false;
    let mut all_compat = true;
    let mut scanning = false;
    let mut restarted = false;
    'process: loop {
        out.clear();
        out.extend_from_slice(quote);
        let mut i = 0;
        while i < arg.len() {
            let mut c = arg[i];
            let mut escaping = false;
            let mut compat = false;
            let mut store_escape = false;
            let is_right_quote = !shell && arg[i..].starts_with(quote);
            let escape = match c {
                0x07 => Some(b'a'),
                0x08 => Some(b'b'),
                0x0c => Some(b'f'),
                b'\n' => Some(b'n'),
                b'\r' => Some(b'r'),
                b'\t' => Some(b't'),
                0x0b => Some(b'v'),
                _ => None,
            };
            let mut octal = None;
            if let Some(e) = escape {
                c = e;
                store_escape = true;
            } else {
                match c {
                    b'\\' => {
                        if !shell {
                            store_escape = true;
                        }
                    }
                    b'{' | b'}' if arg.len() == 1 => compat = true,
                    b'#' | b'~' if i == 0 => compat = true,
                    b' ' => compat = true,
                    b'{' | b'}' | b'#' | b'~' => {}
                    b'!' | b'"' | b'$' | b'&' | b'(' | b')' | b'*' | b';' | b'<' | b'=' | b'>'
                    | b'[' | b'^' | b'`' | b'|' | b'?' => {}
                    b'\'' => {
                        encountered_single_quote = true;
                        compat = true;
                        if shell {
                            if !restarted && !scanning {
                                scanning = true;
                            }
                            // close, backslash, and reopen around the quote itself
                            out.extend_from_slice(b"'\\'");
                            pending = false;
                        }
                    }
                    b'%'
                    | b'+'
                    | b','
                    | b'-'
                    | b'.'
                    | b'/'
                    | b'0'..=b'9'
                    | b':'
                    | b'A'..=b'Z'
                    | b']'
                    | b'_'
                    | b'a'..=b'z' => compat = true,
                    _ => {
                        let printable = (0x20..=0x7e).contains(&c);
                        compat = printable;
                        if !printable {
                            octal = Some(c);
                        }
                    }
                }
            }
            let start_escape = |out: &mut Vec<u8>, pending: &mut bool| {
                if shell && !*pending {
                    out.extend_from_slice(b"'$'");
                    *pending = true;
                }
                out.push(b'\\');
            };
            if let Some(byte) = octal {
                escaping = true;
                start_escape(&mut out, &mut pending);
                out.push(b'0' + (byte >> 6));
                out.push(b'0' + ((byte >> 3) & 7));
                c = b'0' + (byte & 7);
            } else if store_escape || is_right_quote {
                escaping = true;
                start_escape(&mut out, &mut pending);
            }
            if pending && !escaping {
                out.extend_from_slice(b"''");
                pending = false;
            }
            out.push(c);
            if !compat {
                all_compat = false;
            }
            i += 1;
        }
        if shell && encountered_single_quote {
            if all_compat {
                return restyled(arg, Style::C);
            }
            if scanning && !restarted {
                // gnulib rescans to write the long form; the shift state of the
                // first pass is kept, as the measured coreutils output shows.
                restarted = true;
                all_compat = true;
                continue 'process;
            }
        }
        out.extend_from_slice(quote);
        return out;
    }
}
/// coreutils `quote` (locale style).
pub fn quote(arg: &[u8]) -> Vec<u8> {
    restyled(arg, Style::Locale)
}
/// coreutils `quotef` (shell-escape style, also quoting `:`): the name as it
/// is when it needs no quoting, otherwise the `quoteaf` form.
pub fn quotef(arg: &[u8]) -> Vec<u8> {
    let plain = !arg.is_empty()
        && arg.iter().enumerate().all(|(i, b)| {
            b.is_ascii_alphanumeric()
                || b"%+,-./@_".contains(b)
                || (i > 0 && matches!(b, b'#' | b'~'))
        });
    if plain { arg.to_vec() } else { quoteaf(arg) }
}
/// coreutils `quoteaf` (shell-escape-always style).
pub fn quoteaf(arg: &[u8]) -> Vec<u8> {
    restyled(arg, Style::Shell)
}
fn line(parts: &[&[u8]]) -> Vec<u8> {
    let mut v = joined(parts);
    v.push(b'\n');
    v
}
fn bytes(path: &Path) -> &[u8] {
    path.as_os_str().as_bytes()
}

/// `mktemp -d [TEMPLATE]`: the created directory, or mktemp's diagnostic.
/// gnulib `file_name_concat` (coreutils 9.4 `mktemp`): the trailing slashes
/// of DIR's last component are dropped and one `/` joins; a DIR of only
/// slashes is kept as it is.
fn file_name_concat(dir: &[u8], base: &[u8]) -> Vec<u8> {
    let lead = dir.iter().take_while(|b| **b == b'/').count();
    let mut start = lead;
    let mut after_slash = false;
    for (i, b) in dir.iter().enumerate().skip(lead) {
        if *b == b'/' {
            after_slash = true;
        } else if after_slash {
            start = i;
            after_slash = false;
        }
    }
    let mut length = dir.len() - start;
    while length > 1 && dir[start + length - 1] == b'/' {
        length -= 1;
    }
    let end = start + length;
    let separator: &[u8] = if length > 0 {
        if dir[end - 1] != b'/' && base.first() != Some(&b'/') {
            b"/"
        } else {
            b""
        }
    } else if base.first() == Some(&b'/') {
        b"."
    } else {
        b""
    };
    joined(&[&dir[..end], separator, base])
}
pub fn mktemp_dir(template: Option<&[u8]>) -> Result<PathBuf, Vec<u8>> {
    let full = match template {
        Some(t) => t.to_vec(),
        None => {
            let dir = std::env::var_os("TMPDIR")
                .filter(|d| !d.is_empty())
                .map_or(b"/tmp".to_vec(), |d| d.as_bytes().to_vec());
            file_name_concat(&dir, b"tmp.XXXXXXXXXX")
        }
    };
    let x = full.iter().rev().take_while(|b| **b == b'X').count();
    let prefix = &full[..full.len() - x];
    const CHARS: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    let mut last = io::Error::from_raw_os_error(EEXIST);
    for _ in 0..100_000 {
        let mut random = vec![0u8; x];
        dev_workflow_platform::diff_snapshot::fs::random_bytes(&mut random);
        let name = joined(&[
            prefix,
            &random
                .iter()
                .map(|b| CHARS[usize::from(*b) % CHARS.len()])
                .collect::<Vec<_>>(),
        ]);
        let path = PathBuf::from(std::ffi::OsStr::from_bytes(&name));
        use std::os::unix::fs::DirBuilderExt;
        match std::fs::DirBuilder::new().mode(0o700).create(&path) {
            Ok(()) => return Ok(path),
            Err(e) if e.raw_os_error() == Some(EEXIST) => last = e,
            Err(e) => {
                last = e;
                break;
            }
        }
    }
    Err(line(&[
        b"mktemp: failed to create directory via template ",
        &quote(&full),
        b": ",
        strerror(&last).as_bytes(),
    ]))
}

/// `mkdir -p -- PATH`: creates each missing prefix; reports the first one
/// that fails (an existing non-directory reports ENOTDIR).
pub fn mkdir_parents(path: &[u8]) -> Result<(), Vec<u8>> {
    let mut ends = vec![];
    for (i, b) in path.iter().enumerate() {
        if *b == b'/' && i > 0 && path[i - 1] != b'/' {
            ends.push(i);
        }
    }
    let trimmed = path.len() - path.iter().rev().take_while(|b| **b == b'/').count();
    if trimmed > 0 && ends.last() != Some(&trimmed) {
        ends.push(trimmed);
    }
    for end in ends {
        let prefix = &path[..end];
        let p = Path::new(std::ffi::OsStr::from_bytes(prefix));
        if p.is_dir() {
            continue;
        }
        if let Err(e) = std::fs::create_dir(p) {
            let e = if e.raw_os_error() == Some(EEXIST) && !p.is_dir() {
                io::Error::from_raw_os_error(ENOTDIR)
            } else {
                e
            };
            if p.is_dir() {
                continue;
            }
            return Err(line(&[
                b"mkdir: cannot create directory ",
                &quote(prefix),
                b": ",
                strerror(&e).as_bytes(),
            ]));
        }
    }
    Ok(())
}

/// `cp -- SOURCE DEST` of bytes the old script had written to SOURCE.
pub fn copy_bytes(source: &Path, data: &[u8], dest: &Path) -> Result<(), Vec<u8>> {
    use std::io::Write;
    copy_bytes_with(
        source,
        data,
        dest,
        |path: &Path| std::fs::File::create(path),
        |file: &mut std::fs::File, data: &[u8]| file.write_all(data),
    )
}
/// The `cp` diagnostics around the two system steps, which a typed test may substitute.
pub fn copy_bytes_with<F>(
    source: &Path,
    data: &[u8],
    dest: &Path,
    create: impl FnOnce(&Path) -> io::Result<F>,
    write: impl FnOnce(&mut F, &[u8]) -> io::Result<()>,
) -> Result<(), Vec<u8>> {
    let mut file = match create(dest) {
        Ok(f) => f,
        Err(e) => {
            return Err(line(&[
                b"cp: cannot create regular file ",
                &quoteaf(bytes(dest)),
                b": ",
                strerror(&e).as_bytes(),
            ]));
        }
    };
    write(&mut file, data).map_err(|e| {
        line(&[
            b"cp: error copying ",
            &quoteaf(bytes(source)),
            b" to ",
            &quoteaf(bytes(dest)),
            b": ",
            strerror(&e).as_bytes(),
        ])
    })
}

/// `mv -- SOURCE DEST`, including the move into an existing directory.
pub fn move_path(source: &Path, dest: &Path) -> Result<(), Vec<u8>> {
    move_path_with(source, dest, |from, to| std::fs::rename(from, to))
}
/// The `mv` diagnostics around rename(2), which a typed test may substitute.
pub fn move_path_with(
    source: &Path,
    dest: &Path,
    rename: impl FnOnce(&Path, &Path) -> io::Result<()>,
) -> Result<(), Vec<u8>> {
    let mut target = dest.to_path_buf();
    if dest.is_dir() {
        let name = super::bytes::basename(bytes(source));
        target = PathBuf::from(std::ffi::OsStr::from_bytes(&joined(&[
            bytes(dest),
            b"/",
            &name,
        ])));
        if std::fs::symlink_metadata(&target).is_ok_and(|m| m.is_dir()) {
            return Err(line(&[
                b"mv: cannot overwrite directory ",
                &quoteaf(bytes(&target)),
                b" with non-directory",
            ]));
        }
    }
    rename(source, &target).map_err(|e| {
        line(&[
            b"mv: cannot move ",
            &quoteaf(bytes(source)),
            b" to ",
            &quoteaf(bytes(&target)),
            b": ",
            strerror(&e).as_bytes(),
        ])
    })
}

/// `rm -f -- PATH`.
pub fn remove_file(path: &Path) -> Result<(), Vec<u8>> {
    let failure = |e: io::Error| {
        line(&[
            b"rm: cannot remove ",
            &quoteaf(bytes(path)),
            b": ",
            strerror(&e).as_bytes(),
        ])
    };
    match std::fs::symlink_metadata(path) {
        Err(e) if e.raw_os_error() == Some(ENOENT) => Ok(()),
        Ok(m) if m.is_dir() => Err(failure(io::Error::from_raw_os_error(EISDIR))),
        _ => match std::fs::remove_file(path) {
            Err(e) if e.raw_os_error() != Some(ENOENT) => Err(failure(e)),
            _ => Ok(()),
        },
    }
}

/// `rm -rf -- PATH`: every entry that cannot be removed is reported.
pub fn remove_tree(path: &Path) -> Vec<u8> {
    let mut out = vec![];
    fn walk(path: &Path, out: &mut Vec<u8>) -> bool {
        let report = |e: io::Error, out: &mut Vec<u8>| {
            out.extend(line(&[
                b"rm: cannot remove ",
                &quoteaf(bytes(path)),
                b": ",
                strerror(&e).as_bytes(),
            ]));
        };
        match std::fs::symlink_metadata(path) {
            Err(e) if e.raw_os_error() == Some(ENOENT) => true,
            Ok(m) if m.is_dir() => {
                let mut ok = true;
                match std::fs::read_dir(path) {
                    Ok(entries) => {
                        for entry in entries.flatten() {
                            ok &= walk(&entry.path(), out);
                        }
                    }
                    Err(e) => {
                        report(e, out);
                        return false;
                    }
                }
                if ok && let Err(e) = std::fs::remove_dir(path) {
                    report(e, out);
                    return false;
                }
                ok
            }
            _ => match std::fs::remove_file(path) {
                Err(e) if e.raw_os_error() != Some(ENOENT) => {
                    report(e, out);
                    false
                }
                _ => true,
            },
        }
    }
    walk(path, &mut out);
    out
}

/// GNU `realpath -m`: symlinks of existing components are resolved; missing
/// components and `..` after them are handled lexically.
pub fn realpath_missing(path: &Path) -> Option<PathBuf> {
    let base = if path.is_absolute() {
        PathBuf::from("/")
    } else {
        std::env::current_dir().ok()?
    };
    let mut pending: Vec<Vec<u8>> = bytes(path)
        .split(|b| *b == b'/')
        .filter(|s| !s.is_empty())
        .map(<[u8]>::to_vec)
        .rev()
        .collect();
    let mut resolved = base;
    let mut links = 0;
    let mut missing = false;
    while let Some(part) = pending.pop() {
        match part.as_slice() {
            b"." => continue,
            b".." => {
                resolved.pop();
                continue;
            }
            _ => {}
        }
        let next = resolved.join(std::ffi::OsStr::from_bytes(&part));
        if !missing {
            match std::fs::symlink_metadata(&next) {
                Ok(m) if m.file_type().is_symlink() => {
                    links += 1;
                    if links > 40 {
                        return None;
                    }
                    let target = std::fs::read_link(&next).ok()?;
                    let t = bytes(&target);
                    if t.starts_with(b"/") {
                        resolved = PathBuf::from("/");
                    }
                    for s in t.split(|b| *b == b'/').filter(|s| !s.is_empty()).rev() {
                        pending.push(s.to_vec());
                    }
                    continue;
                }
                Ok(_) => {}
                Err(_) => missing = true,
            }
        }
        resolved = next;
    }
    Some(resolved)
}

/// `find . -path ./.git -prune -o -name .git -prune -print0 -o -type d -print0`
/// run in ROOT: pre-order, readdir order, symlinks not followed. The flag is
/// false when a directory could not be read (find's exit status 1).
pub fn find_scan(root: &Path) -> (Vec<Vec<u8>>, bool) {
    fn visit(root: &Path, relative: &[u8], out: &mut Vec<Vec<u8>>, ok: &mut bool) {
        let directory = root.join(std::ffi::OsStr::from_bytes(relative));
        let entries = match std::fs::read_dir(&directory) {
            Ok(e) => e,
            Err(_) => {
                *ok = false;
                return;
            }
        };
        for entry in entries {
            let Ok(entry) = entry else {
                *ok = false;
                continue;
            };
            let name = entry.file_name();
            let path = joined(&[relative, b"/", name.as_bytes()]);
            // fts (FTS_NOSTAT|FTS_PHYSICAL) knows the type from d_type and
            // lstats a directory (or an unknown type); find lstats an entry
            // that `-prune` sees (named `.git`). A failed lstat (a directory
            // that can be read but not searched) prints nothing and makes
            // find's status 1.
            let Ok(kind) = entry.file_type() else {
                *ok = false;
                continue;
            };
            let git_name = name.as_bytes() == b".git";
            let is_dir = if kind.is_dir() || git_name {
                match std::fs::symlink_metadata(root.join(std::ffi::OsStr::from_bytes(&path))) {
                    Ok(m) => m.is_dir(),
                    Err(_) => {
                        *ok = false;
                        continue;
                    }
                }
            } else {
                false
            };
            if path == b"./.git" {
                continue;
            }
            if git_name {
                out.push(path);
                continue;
            }
            if is_dir {
                out.push(path.clone());
                visit(root, &path, out, ok);
            }
        }
    }
    let mut out = vec![b".".to_vec()];
    let mut ok = true;
    visit(root, b".", &mut out, &mut ok);
    (out, ok)
}

/// `date '+%Y-%m-%d %H:%M:%S'` in the process time zone.
pub fn now() -> Vec<u8> {
    dev_workflow_platform::diff_snapshot::fs::local_time().into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn quoting_matches_measured_coreutils_9_4() {
        let s = b"/t/a'b\nc$\xc3\xa9";
        assert_eq!(
            quote(&[s.as_slice(), b"/.d"].concat()),
            b"'/t/a\\'b\\nc$\\303\\251/.d'".to_vec()
        );
        assert_eq!(
            quoteaf(&[s.as_slice(), b"/out.tmp"].concat()),
            b"'/t/a'\\''b'$'\\n''c$'$'\\303\\251''/out.tmp'".to_vec()
        );
        assert_eq!(
            quoteaf(s),
            b"'''/t/a'\\''b'$'\\n''c$'$'\\303\\251'".to_vec()
        );
        assert_eq!(quoteaf(b"/plain/path"), b"'/plain/path'".to_vec());
        assert_eq!(quoteaf(b"it's"), b"\"it's\"".to_vec());
    }
}
