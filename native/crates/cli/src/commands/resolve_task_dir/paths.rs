//! pathlib.Path.resolve(strict=...) と、通常の exists/is_dir の範囲を移す。
use super::bytes::{concat, raw, repr};
use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs::{self, Metadata};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

pub fn parts(path: &[u8]) -> impl Iterator<Item = &[u8]> {
    path.split(|&byte| byte == b'/')
        .filter(|part| !part.is_empty() && *part != b".")
}

pub fn path(bytes: &[u8]) -> &Path {
    Path::new(OsStr::from_bytes(bytes))
}

pub fn join(left: &[u8], right: &[u8]) -> Vec<u8> {
    if right.starts_with(b"/") {
        return right.to_vec();
    }
    if left.is_empty() {
        return right.to_vec();
    }
    concat(&[left, if left.ends_with(b"/") { b"" } else { b"/" }, right])
}

fn parent(value: &[u8]) -> Vec<u8> {
    if value.is_empty() || value == b".." || value.ends_with(b"/..") {
        return join(value, b"..");
    }
    match value.iter().rposition(|&byte| byte == b'/') {
        Some(0) => b"/".to_vec(),
        Some(at) => value[..at].to_vec(),
        None => Vec::new(),
    }
}

pub fn lexical(value: &[u8]) -> Vec<u8> {
    let mut result = if value.starts_with(b"/") {
        b"/".to_vec()
    } else {
        Vec::new()
    };
    for part in parts(value) {
        if part == b".." {
            result = parent(&result)
        } else {
            result = join(&result, part)
        }
    }
    result
}

pub fn inside(value: &[u8], root: &[u8]) -> bool {
    value == root || value.starts_with(&join(root, b""))
}

pub fn relative(value: &[u8], root: &[u8]) -> Vec<u8> {
    if value == root {
        b".".to_vec()
    } else {
        value[if root == b"/" { 1 } else { root.len() + 1 }..].to_vec()
    }
}

fn eloop(error: &io::Error) -> bool {
    error.raw_os_error() == Some(if cfg!(target_os = "macos") { 62 } else { 40 })
}

fn ignored(error: &io::Error) -> bool {
    matches!(error.raw_os_error(), Some(2 | 20 | 9)) || eloop(error)
}

pub fn os_error(error: &io::Error, filename: &[u8]) -> Vec<u8> {
    let number = error.raw_os_error().unwrap_or(0);
    let reason = match error.kind() {
        io::ErrorKind::NotFound => "No such file or directory".to_owned(),
        io::ErrorKind::PermissionDenied => "Permission denied".to_owned(),
        io::ErrorKind::NotADirectory => "Not a directory".to_owned(),
        io::ErrorKind::InvalidFilename => "File name too long".to_owned(),
        _ => error
            .to_string()
            .split(" (os error ")
            .next()
            .unwrap()
            .to_owned(),
    };
    concat(&[
        format!("[Errno {number}] {reason}: ").as_bytes(),
        &repr(filename),
    ])
}

pub fn metadata(value: &[u8], follow: bool) -> Result<Option<Metadata>, Vec<u8>> {
    let result = if follow {
        fs::metadata(path(value))
    } else {
        fs::symlink_metadata(path(value))
    };
    match result {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if ignored(&error) => Ok(None),
        Err(error) => Err(os_error(&error, value)),
    }
}

pub fn is_dir(value: &[u8]) -> Result<bool, Vec<u8>> {
    Ok(metadata(value, true)?.is_some_and(|metadata| metadata.is_dir()))
}

pub fn is_link(value: &[u8]) -> Result<bool, Vec<u8>> {
    Ok(metadata(value, false)?.is_some_and(|metadata| metadata.is_symlink()))
}

fn resolve_parts(
    mut current: Vec<u8>,
    value: &[u8],
    strict: bool,
    seen: &mut HashMap<Vec<u8>, Option<Vec<u8>>>,
) -> Result<(Vec<u8>, bool), Vec<u8>> {
    if value.starts_with(b"/") {
        current = b"/".to_vec();
    }
    let names = parts(value).collect::<Vec<_>>();
    for (at, &name) in names.iter().enumerate() {
        if name == b".." {
            current = parent(&current);
            continue;
        }
        let next = join(&current, name);
        let is_link = match fs::symlink_metadata(path(&next)) {
            Ok(metadata) => metadata.is_symlink(),
            Err(error) if strict => return Err(os_error(&error, &next)),
            Err(_) => false,
        };
        if !is_link {
            current = next;
            continue;
        }
        if let Some(cached) = seen.get(&next) {
            if let Some(cached) = cached {
                current = cached.clone();
                continue;
            }
            // pathlib の strict なループは、検査した名前を repr にして示す。
            if strict {
                return Err(concat(&[b"Symlink loop from ", &repr(&next)]));
            }
            let rest = names[at + 1..].join(&b"/"[..]);
            return Ok((join(&next, &rest), false));
        }
        seen.insert(next.clone(), None);
        let target = fs::read_link(path(&next)).map_err(|error| os_error(&error, &next))?;
        let (resolved, complete) = resolve_parts(current, raw(target.as_os_str()), strict, seen)?;
        current = resolved;
        if !complete {
            let rest = names[at + 1..].join(&b"/"[..]);
            return Ok((join(&current, &rest), false));
        }
        seen.insert(next, Some(current.clone()));
    }
    Ok((current, true))
}

pub fn resolve(value: &[u8], strict: bool) -> Result<Vec<u8>, Vec<u8>> {
    // Path() は空文字を '.' にし、余分な '/' と '.' の成分を取り除く。
    let normalized = parts(value).collect::<Vec<_>>().join(&b"/"[..]);
    let input = if value.starts_with(b"/") {
        join(b"/", &normalized)
    } else {
        normalized
    };
    let (resolved, _) = resolve_parts(Vec::new(), &input, strict, &mut HashMap::new())?;
    let absolute = if resolved.starts_with(b"/") {
        lexical(&resolved)
    } else {
        let cwd = std::env::current_dir().map_err(|error| os_error(&error, b"."))?;
        lexical(&join(raw(cwd.as_os_str()), &resolved))
    };
    if !strict
        && let Err(error) = fs::metadata(path(&absolute))
        && eloop(&error)
    {
        return Err(concat(&[b"Symlink loop from ", &repr(&absolute)]));
    }
    Ok(absolute)
}
