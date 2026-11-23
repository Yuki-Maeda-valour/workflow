// CPython3.10.11/12 Lib/posixpath.py call-order adaptation, strict=False,
// str inputs only. See CPYTHON-LICENSE. No Python runtime is executed here.
use super::{
    error::{LegacyException as E, Result},
    json::{self, Text},
    runtime,
    walk_recursion::Calls,
};
use std::{collections::HashMap, ffi::OsString, os::unix::ffi::OsStringExt, path::PathBuf};
fn frame(error: E, function: &'static str, line: u32, source: &'static str) -> E {
    error.external("posixpath.py", function, line, source)
}
fn sep(caller: Calls) -> Result<()> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "_get_sep", 42, "if isinstance(path, bytes):"))
}
fn isabs(caller: Calls, value: &[u32]) -> Result<bool> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "isabs", 62, "s = os.fspath(s)"))?;
    sep(calls).map_err(|e| frame(e, "isabs", 63, "sep = _get_sep(s)"))?;
    calls
        .builtin()
        .map_err(|e| frame(e, "isabs", 64, "return s.startswith(sep)"))?;
    Ok(value.starts_with(&[47]))
}
fn join(caller: Calls, base: &[u32], parts: &[&[u32]]) -> Result<Vec<u32>> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "join", 76, "a = os.fspath(a)"))?;
    sep(calls).map_err(|e| frame(e, "join", 77, "sep = _get_sep(a)"))?;
    let mut out = base.to_vec();
    for part in parts {
        calls
            .builtin()
            .map_err(|e| frame(e, "join", 82, "for b in map(os.fspath, p):"))?;
        calls
            .builtin()
            .map_err(|e| frame(e, "join", 83, "if b.startswith(sep):"))?;
        if part.starts_with(&[47]) {
            out = part.to_vec();
        } else {
            if !out.is_empty() {
                calls
                    .builtin()
                    .map_err(|e| frame(e, "join", 85, "elif not path or path.endswith(sep):"))?;
            }
            if !out.is_empty() && !out.ends_with(&[47]) {
                out.push(47);
            }
            out.extend_from_slice(part);
        }
    }
    Ok(out)
}
fn split(caller: Calls, value: &[u32]) -> Result<(Vec<u32>, Vec<u32>)> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "split", 103, "p = os.fspath(p)"))?;
    sep(calls).map_err(|e| frame(e, "split", 104, "sep = _get_sep(p)"))?;
    calls
        .builtin()
        .map_err(|e| frame(e, "split", 105, "i = p.rfind(sep) + 1"))?;
    let at = value.iter().rposition(|c| *c == 47).map_or(0, |i| i + 1);
    let mut head = value[..at].to_vec();
    let tail = value[at..].to_vec();
    if !head.is_empty() {
        calls
            .builtin()
            .and_then(|_| calls.compare())
            .map_err(|e| frame(e, "split", 107, "if head and head != sep*len(head):"))?;
        if !head.iter().all(|c| *c == 47) {
            calls
                .builtin()
                .map_err(|e| frame(e, "split", 108, "head = head.rstrip(sep)"))?;
            while head.ends_with(&[47]) {
                head.pop();
            }
        }
    }
    Ok((head, tail))
}
fn raw_path(value: &[u32]) -> Result<PathBuf> {
    // os.lstat の引数の符号化の失敗。標準出力への書込みの失敗(location "output")と分ける。
    let raw = runtime::bytes(&json::text(value.to_vec())).map_err(|e| match e {
        E::Python { kind, reason, .. } => E::Python {
            kind,
            reason,
            location: "realpath",
        },
        other => other,
    })?;
    if raw.contains(&0) {
        return Err(E::Python {
            kind: "ValueError",
            reason: "embedded null byte".into(),
            location: "realpath",
        });
    }
    Ok(PathBuf::from(OsString::from_vec(raw)))
}
fn resolve(
    caller: Calls,
    mut path: Vec<u32>,
    mut rest: &[u32],
    seen: &mut HashMap<Vec<u32>, Option<Vec<u32>>>,
) -> Result<(Vec<u32>, bool)> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "_joinrealpath", 402, "if isinstance(path, bytes):"))?;
    if isabs(calls, rest).map_err(|e| frame(e, "_joinrealpath", 411, "if isabs(rest):"))? {
        rest = &rest[1..];
        path = vec![47];
    }
    while !rest.is_empty() {
        calls.builtin().map_err(|e| {
            frame(
                e,
                "_joinrealpath",
                416,
                "name, _, rest = rest.partition(sep)",
            )
        })?;
        let at = rest.iter().position(|c| *c == 47).unwrap_or(rest.len());
        let name = &rest[..at];
        rest = if at < rest.len() {
            &rest[at + 1..]
        } else {
            &[]
        };
        if !name.is_empty() {
            calls
                .compare()
                .map_err(|e| frame(e, "_joinrealpath", 417, "if not name or name == curdir:"))?;
        }
        if name.is_empty() || name == [46] {
            continue;
        }
        calls
            .compare()
            .map_err(|e| frame(e, "_joinrealpath", 420, "if name == pardir:"))?;
        if name == [46, 46] {
            if path.is_empty() {
                path = vec![46, 46];
            } else {
                let (head, tail) = split(calls, &path)
                    .map_err(|e| frame(e, "_joinrealpath", 423, "path, name = split(path)"))?;
                path = head;
                calls
                    .compare()
                    .map_err(|e| frame(e, "_joinrealpath", 424, "if name == pardir:"))?;
                if tail == [46, 46] {
                    path = join(calls, &path, &[&[46, 46], &[46, 46]]).map_err(|e| {
                        frame(e, "_joinrealpath", 425, "path = join(path, pardir, pardir)")
                    })?;
                }
            }
            continue;
        }
        let next = join(calls, &path, &[name])
            .map_err(|e| frame(e, "_joinrealpath", 429, "newpath = join(path, name)"))?;
        calls
            .builtin()
            .map_err(|e| frame(e, "_joinrealpath", 431, "st = os.lstat(newpath)"))?;
        let object = raw_path(&next)
            .map_err(|e| frame(e, "_joinrealpath", 431, "st = os.lstat(newpath)"))?;
        let is_link = match object.symlink_metadata() {
            Ok(st) => {
                calls.builtin().map_err(|e| {
                    frame(
                        e,
                        "_joinrealpath",
                        437,
                        "is_link = stat.S_ISLNK(st.st_mode)",
                    )
                })?;
                st.file_type().is_symlink()
            }
            Err(_) => false, // only OSError is suppressed, as strict=False specifies.
        };
        if !is_link {
            path = next;
            continue;
        }
        if let Some(cached) = seen.get(&next) {
            if let Some(value) = cached {
                path = value.clone();
                continue;
            }
            return Ok((
                join(calls, &next, &[rest]).map_err(|e| {
                    frame(e, "_joinrealpath", 454, "return join(newpath, rest), False")
                })?,
                false,
            ));
        }
        seen.insert(next.clone(), None);
        calls.builtin().map_err(|e| {
            frame(
                e,
                "_joinrealpath",
                456,
                "path, ok = _joinrealpath(path, os.readlink(newpath), strict, seen)",
            )
        })?;
        let target = std::fs::read_link(&object).map_err(|error| {
            frame(
                E::Os {
                    error,
                    path: Some(runtime::bytes(&json::text(next.clone())).unwrap()),
                    location: "realpath.readlink",
                },
                "_joinrealpath",
                456,
                "path, ok = _joinrealpath(path, os.readlink(newpath), strict, seen)",
            )
        })?;
        let target = runtime::os_text(target.as_os_str());
        let (resolved, ok) = resolve(calls, path, target.as_codepoints(), seen).map_err(|e| {
            frame(
                e,
                "_joinrealpath",
                456,
                "path, ok = _joinrealpath(path, os.readlink(newpath), strict, seen)",
            )
        })?;
        if !ok {
            return Ok((
                join(calls, &resolved, &[rest]).map_err(|e| {
                    frame(e, "_joinrealpath", 458, "return join(path, rest), False")
                })?,
                false,
            ));
        }
        path = resolved;
        seen.insert(next, Some(path.clone()));
    }
    Ok((path, true))
}
/// os.path.realpath(str)。旧版と同じく文字番号のまま返す(符号化は呼出元が旧版の位置で行う)。
pub fn real(value: &Text, caller: Calls) -> Result<Text> {
    let calls = caller.enter()?;
    calls
        .builtin()
        .map_err(|e| frame(e, "realpath", 395, "filename = os.fspath(filename)"))?;
    let (mut path, _) = resolve(
        calls,
        Vec::new(),
        value.as_codepoints(),
        &mut HashMap::new(),
    )
    .map_err(|e| {
        frame(
            e,
            "realpath",
            396,
            "path, ok = _joinrealpath(filename[:0], filename, strict, {})",
        )
    })?;
    // abspath starts only after all symlink frames have returned.
    let absolute = (|| -> Result<Text> {
        let abs = calls.enter()?;
        abs.builtin()
            .map_err(|e| frame(e, "abspath", 379, "path = os.fspath(path)"))?;
        if !isabs(abs, &path).map_err(|e| frame(e, "abspath", 380, "if not isabs(path):"))? {
            abs.builtin()
                .map_err(|e| frame(e, "abspath", 381, "if isinstance(path, bytes):"))?;
            abs.builtin()
                .map_err(|e| frame(e, "abspath", 384, "cwd = os.getcwd()"))?;
            let cwd = std::env::current_dir()
                .map_err(|e| frame(e.into(), "abspath", 384, "cwd = os.getcwd()"))?;
            path = join(
                abs,
                runtime::os_text(cwd.as_os_str()).as_codepoints(),
                &[&path],
            )
            .map_err(|e| frame(e, "abspath", 385, "path = join(cwd, path)"))?;
        }
        let norm = abs
            .enter()
            .map_err(|e| frame(e, "abspath", 386, "return normpath(path)"))?;
        norm.builtin()
            .map_err(|e| frame(e, "normpath", 340, "path = os.fspath(path)"))?;
        norm.builtin()
            .map_err(|e| frame(e, "normpath", 341, "if isinstance(path, bytes):"))?;
        Ok(json::text(super::state::normpath_codes(&path)))
    })();
    absolute.map_err(|e| frame(e, "realpath", 397, "return abspath(path)"))
}
