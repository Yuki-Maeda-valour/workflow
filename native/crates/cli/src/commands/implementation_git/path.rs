//! 旧Python 3.10.11の`pathlib.Path`のうち、旧版が使う部分(`str(Path(x))`・`is_dir`・`is_file`・
//! `resolve`)と、その中の`posixpath.realpath`。パスはバイト列で持つ(旧版の`str`と
//! `surrogateescape`で1対1)。OSの正規化(大文字小文字を揃える処理など)は使わない。

use dev_workflow_platform::r4g::{Errno, fs};
use std::ffi::CString;

/// 旧Pythonの例外(`OSError`か`RuntimeError`)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PathError {
    /// `OSError(errno, strerror, filename)`。`filename`は`None`か、そのパス。
    Os {
        errno: i32,
        filename: Option<Vec<u8>>,
    },
    /// `RuntimeError("Symlink loop from %r" % filename)`。
    SymlinkLoop(Vec<u8>),
}

fn cstring(path: &[u8]) -> CString {
    // 旧版へ渡るパスは厳密なUTF-8の引数かGitの出力から作り、NULを含まない(含めば呼出側が先に扱う)。
    CString::new(path.to_vec()).unwrap_or_default()
}

fn os(errno: Errno, filename: &[u8]) -> PathError {
    PathError::Os {
        errno: errno.raw_os_error(),
        filename: Some(filename.to_vec()),
    }
}

/// `str(Path(value))`(posixの`parse_parts`と`_format_parsed_parts`)。
pub fn pathlib_str(value: &[u8]) -> Vec<u8> {
    let leading = value.iter().take_while(|byte| **byte == b'/').count();
    let root: &[u8] = match leading {
        0 => b"",
        2 => b"//",
        _ => b"/",
    };
    let parts: Vec<&[u8]> = value[leading..]
        .split(|byte| *byte == b'/')
        .filter(|part| !part.is_empty() && *part != b".")
        .collect();
    let mut out = root.to_vec();
    out.extend(parts.join(&b'/'));
    if out.is_empty() {
        out.push(b'.');
    }
    out
}

/// `str(Path(file).with_name(name))`(`file`は名前を持つパス。旧版の`__file__`)。
pub fn sibling(file: &[u8], name: &[u8]) -> Vec<u8> {
    let path = pathlib_str(file);
    let index = path.iter().rposition(|byte| *byte == b'/');
    let mut out = match index {
        Some(index) => path[..=index].to_vec(),
        None => Vec::new(),
    };
    out.extend_from_slice(name);
    out
}

/// `_ignore_error`(`ENOENT`・`ENOTDIR`・`EBADF`・`ELOOP`)。
fn ignored(errno: Errno) -> bool {
    [Errno::NOENT, Errno::NOTDIR, Errno::BADF, Errno::LOOP].contains(&errno)
}

/// `Path(value).is_dir()`か`is_file()`。無視する失敗は偽、ほかの`OSError`は投げる。
fn is_kind(value: &[u8], directory: bool) -> Result<bool, PathError> {
    let path = pathlib_str(value);
    if path.contains(&0) {
        return Ok(false);
    }
    match fs::stat_at(None, &cstring(&path), true) {
        Ok(stat) => Ok(if directory {
            stat.is_dir()
        } else {
            stat.is_regular()
        }),
        Err(errno) if ignored(errno) => Ok(false),
        Err(errno) => Err(os(errno, &path)),
    }
}

pub fn is_dir(value: &[u8]) -> Result<bool, PathError> {
    is_kind(value, true)
}

pub fn is_file(value: &[u8]) -> Result<bool, PathError> {
    is_kind(value, false)
}

/// `os.access(str(path), os.R_OK)`。
pub fn readable(value: &[u8]) -> bool {
    let path = pathlib_str(value);
    !path.contains(&0) && fs::readable(&cstring(&path))
}

/// `posixpath.join(a, b)`。
fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    if b.starts_with(b"/") {
        return b.to_vec();
    }
    let mut path = a.to_vec();
    if path.is_empty() || path.ends_with(b"/") {
        path.extend_from_slice(b);
    } else {
        path.push(b'/');
        path.extend_from_slice(b);
    }
    path
}

/// `posixpath.split(p)`。
fn split(path: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let index = path
        .iter()
        .rposition(|byte| *byte == b'/')
        .map_or(0, |i| i + 1);
    let (head, tail) = (&path[..index], &path[index..]);
    let mut head = head.to_vec();
    if !head.is_empty() && head.iter().any(|byte| *byte != b'/') {
        while head.ends_with(b"/") {
            head.pop();
        }
    }
    (head, tail.to_vec())
}

/// `posixpath.normpath(path)`。
fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.starts_with(b"/"));
    if initial == 1 && path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut comps: Vec<&[u8]> = Vec::new();
    for comp in path.split(|byte| *byte == b'/') {
        if comp.is_empty() || comp == b"." {
            continue;
        }
        if comp != b".." || (initial == 0 && comps.is_empty()) || comps.last() == Some(&&b".."[..])
        {
            comps.push(comp);
        } else if !comps.is_empty() {
            comps.pop();
        }
    }
    let mut out = vec![b'/'; initial];
    out.extend(comps.join(&b'/'));
    if out.is_empty() {
        out.push(b'.');
    }
    out
}

/// `posixpath.abspath(path)`(相対なら`os.getcwd()`と結ぶ)。
fn abspath(path: &[u8]) -> Result<Vec<u8>, PathError> {
    if path.starts_with(b"/") {
        return Ok(normpath(path));
    }
    let cwd = fs::getcwd().map_err(|errno| PathError::Os {
        errno: errno.raw_os_error(),
        filename: None,
    })?;
    Ok(normpath(&join(&cwd, path)))
}

/// `posixpath._joinrealpath(path, rest, strict=False, seen)`。
fn join_realpath(
    mut path: Vec<u8>,
    rest: &[u8],
    seen: &mut Vec<(Vec<u8>, Option<Vec<u8>>)>,
) -> Result<(Vec<u8>, bool), PathError> {
    let mut rest = rest.to_vec();
    if rest.starts_with(b"/") {
        rest.remove(0);
        path = b"/".to_vec();
    }
    while !rest.is_empty() {
        let (name, remaining) = match rest.iter().position(|byte| *byte == b'/') {
            Some(index) => (rest[..index].to_vec(), rest[index + 1..].to_vec()),
            None => (rest.clone(), Vec::new()),
        };
        rest = remaining;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if !path.is_empty() {
                let (head, tail) = split(&path);
                path = head;
                if tail == b".." {
                    path = join(&join(&path, b".."), b"..");
                }
            } else {
                path = b"..".to_vec();
            }
            continue;
        }
        let newpath = join(&path, &name);
        let is_link = match fs::stat_at(None, &cstring(&newpath), false) {
            Ok(stat) => stat.is_symlink(),
            Err(_) => false,
        };
        if !is_link {
            path = newpath;
            continue;
        }
        if let Some((_, resolved)) = seen.iter().find(|(key, _)| *key == newpath) {
            match resolved {
                Some(resolved) => {
                    path = resolved.clone();
                    continue;
                }
                None => return Ok((join(&newpath, &rest), false)),
            }
        }
        seen.push((newpath.clone(), None));
        let target =
            fs::read_link(None, &cstring(&newpath)).map_err(|errno| os(errno, &newpath))?;
        let (resolved, ok) = join_realpath(path, &target, seen)?;
        if !ok {
            return Ok((join(&resolved, &rest), false));
        }
        if let Some(entry) = seen.iter_mut().find(|(key, _)| *key == newpath) {
            entry.1 = Some(resolved.clone());
        }
        path = resolved;
    }
    Ok((path, true))
}

/// `str(Path(value).resolve())`(strict=False)。`realpath`の`OSError`は、`ELOOP`なら
/// `RuntimeError`にして投げ、ほかはそのまま投げる。結果の`stat()`の`ELOOP`も`RuntimeError`。
pub fn resolve(value: &[u8]) -> Result<Vec<u8>, PathError> {
    let path = pathlib_str(value);
    let loop_error = |error: PathError| match error {
        PathError::Os {
            errno,
            filename: Some(filename),
        } if errno == Errno::LOOP.raw_os_error() => PathError::SymlinkLoop(filename),
        other => other,
    };
    let mut seen = Vec::new();
    let (joined, _) = join_realpath(Vec::new(), &path, &mut seen).map_err(loop_error)?;
    let resolved = abspath(&joined).map_err(loop_error)?;
    let result = pathlib_str(&resolved);
    if let Err(errno) = fs::stat_at(None, &cstring(&result), true)
        && errno == Errno::LOOP
    {
        return Err(PathError::SymlinkLoop(result));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pathlib_and_posixpath_normalize_like_python() {
        assert_eq!(pathlib_str(b"a//b/./c/"), b"a/b/c");
        assert_eq!(pathlib_str(b"//x/../y"), b"//x/../y");
        assert_eq!(pathlib_str(b"///x"), b"/x");
        assert_eq!(pathlib_str(b"./"), b".");
        assert_eq!(normpath(b"/a/../../b"), b"/b");
        assert_eq!(normpath(b"../a/.."), b"..");
        assert_eq!(normpath(b"//a//b"), b"//a/b");
        assert_eq!(split(b"/a/b"), (b"/a".to_vec(), b"b".to_vec()));
        assert_eq!(split(b"//"), (b"//".to_vec(), b"".to_vec()));
        assert_eq!(sibling(b"/a//b/./x.py", b"d.sh"), b"/a/b/d.sh");
        assert_eq!(sibling(b"/x.py", b"d.sh"), b"/d.sh");
        assert_eq!(sibling(b"//x.py", b"d.sh"), b"//d.sh");
        assert_eq!(sibling(b"x.py", b"d.sh"), b"d.sh");
    }

    /// 本物のPythonの`str(Path(x).resolve())`と、リンク・`..`・循環・相対で一致する。
    #[test]
    fn resolve_matches_python() {
        let base = std::env::temp_dir().join(format!("r4g-ig-path-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(base.join("real/sub")).unwrap();
        std::os::unix::fs::symlink("real", base.join("link")).unwrap();
        std::os::unix::fs::symlink("../real/sub", base.join("real/up")).unwrap();
        std::os::unix::fs::symlink("loop2", base.join("loop1")).unwrap();
        std::os::unix::fs::symlink("loop1", base.join("loop2")).unwrap();
        let cases: Vec<String> = [
            "link",
            "link/sub",
            "link/up",
            "link/up/..",
            "real/./sub//",
            "loop1",
            "loop1/x",
            "missing/x",
            "link/../real",
        ]
        .iter()
        .map(|case| base.join(case).to_string_lossy().into_owned())
        .chain(["relative/x".to_string(), "..".to_string()])
        .collect();
        let oracle = "import json, pathlib, sys\nout = []\nfor case in sys.argv[1:]:\n    try:\n        out.append(['ok', str(pathlib.Path(case).resolve())])\n    except RuntimeError as e:\n        out.append(['runtime', str(e)])\nprint(json.dumps(out))";
        let output = std::process::Command::new("python3")
            .args(["-I", "-B", "-c", oracle])
            .args(&cases)
            .current_dir(&base)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let expected: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        // 相対のケースは同じ作業場所で解く(このテストのプロセスの作業場所を変えない)。
        for (index, case) in cases.iter().enumerate() {
            let want = &expected[index];
            let input = if case.starts_with('/') {
                case.clone()
            } else {
                base.join(case).to_string_lossy().into_owned()
            };
            match resolve(input.as_bytes()) {
                Ok(path) => {
                    assert_eq!(want[0], "ok", "{case}");
                    if case.starts_with('/') {
                        assert_eq!(
                            String::from_utf8(path).unwrap(),
                            want[1].as_str().unwrap(),
                            "{case}"
                        );
                    }
                }
                Err(PathError::SymlinkLoop(path)) => {
                    assert_eq!(want[0], "runtime", "{case}");
                    let message =
                        format!("Symlink loop from '{}'", String::from_utf8(path).unwrap());
                    assert_eq!(message, want[1].as_str().unwrap(), "{case}");
                }
                Err(other) => panic!("{case}: {other:?}"),
            }
        }
        std::fs::remove_dir_all(&base).unwrap();
    }
}
