//! `--cp` の解決。Python 3.10.12 の `os.path.realpath`(strict でない)と同じ規則にする。
//!
//! - OS に解決させない。`..` は解決済みの文字列の上で 1 段戻す。
//! - 不在の段・リンクでない段は、そのまま残して続ける。ループは解決せずに残す。
//! - 旧 helper の CLI では、リンクを解決する入れ子(`_joinrealpath` の再帰)が 992 段を超えると
//!   Python の `RecursionError` になる(実測: `/usr/bin/cp` を指す 991 段の連鎖は成功、992 段は
//!   `runtime-unavailable`。`/bin` がリンクの機械の `/bin/cp` 経由は 1 段多く数える)。
use std::collections::HashMap;

/// 入れ子の上限。最上位の呼び出しを 1 段目と数える。
pub(super) const NESTING_LIMIT: usize = 992;

#[derive(Debug)]
pub(super) enum RealpathError {
    /// `readlink` の失敗(Python の OSError)。
    Os,
    /// 入れ子の上限(Python の RecursionError)。
    Recursion,
}

/// 実際の `lstat` と `readlink`。検査では差し替える。
pub(super) trait Links {
    /// リンクなら true。取得の失敗はリンクでないと扱う(strict でない realpath と同じ)。
    fn is_link(&mut self, path: &[u8]) -> bool;
    fn read_link(&mut self, path: &[u8]) -> std::io::Result<Vec<u8>>;
}

/// posixpath.join(a, b)。
fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    if b.first() == Some(&b'/') {
        return b.to_vec();
    }
    let mut path = a.to_vec();
    if path.is_empty() || path.last() == Some(&b'/') {
        path.extend_from_slice(b);
    } else {
        path.push(b'/');
        path.extend_from_slice(b);
    }
    path
}

/// posixpath.split(p)。
fn split(p: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let i = p.iter().rposition(|c| *c == b'/').map_or(0, |i| i + 1);
    let (head, tail) = p.split_at(i);
    let mut head = head.to_vec();
    if !head.is_empty() && !head.iter().all(|c| *c == b'/') {
        while head.last() == Some(&b'/') {
            head.pop();
        }
    }
    (head, tail.to_vec())
}

/// posixpath.normpath(path)。
pub(super) fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.first() == Some(&b'/'));
    if initial == 1 && path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|c| *c == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part != b".." || (initial == 0 && parts.is_empty()) || parts.last() == Some(&&b".."[..])
        {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let mut result = vec![b'/'; initial];
    result.extend_from_slice(&parts.join(&b'/'));
    if result.is_empty() {
        b".".to_vec()
    } else {
        result
    }
}

type Seen = HashMap<Vec<u8>, Option<Vec<u8>>>;

fn join_real(
    links: &mut dyn Links,
    mut path: Vec<u8>,
    rest: &[u8],
    seen: &mut Seen,
    depth: usize,
) -> Result<(Vec<u8>, bool), RealpathError> {
    if depth > NESTING_LIMIT {
        return Err(RealpathError::Recursion);
    }
    let mut rest = rest.to_vec();
    if rest.first() == Some(&b'/') {
        rest.remove(0);
        path = b"/".to_vec();
    }
    while !rest.is_empty() {
        // name, _, rest = rest.partition(sep)
        let (name, remainder) = match rest.iter().position(|c| *c == b'/') {
            Some(i) => (
                rest.get(..i).unwrap_or_default().to_vec(),
                rest.get(i + 1..).unwrap_or_default().to_vec(),
            ),
            None => (rest.clone(), Vec::new()),
        };
        rest = remainder;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if path.is_empty() {
                path = b"..".to_vec();
            } else {
                let (head, tail) = split(&path);
                path = head;
                if tail == b".." {
                    path = join(&join(&path, b".."), b"..");
                }
            }
            continue;
        }
        let newpath = join(&path, &name);
        if !links.is_link(&newpath) {
            path = newpath;
            continue;
        }
        if let Some(cached) = seen.get(&newpath) {
            match cached {
                Some(resolved) => {
                    path = resolved.clone();
                    continue;
                }
                // 解決中のリンクに戻った(ループ)。解決済みの部分と残りをそのまま返す。
                None => return Ok((join(&newpath, &rest), false)),
            }
        }
        seen.insert(newpath.clone(), None);
        let target = links.read_link(&newpath).map_err(|_| RealpathError::Os)?;
        let (resolved, ok) = join_real(links, path, &target, seen, depth + 1)?;
        if !ok {
            return Ok((join(&resolved, &rest), false));
        }
        seen.insert(newpath, Some(resolved.clone()));
        path = resolved;
    }
    Ok((path, true))
}

/// os.path.realpath(filename)。`filename` は `/` で始まる。
pub(super) fn realpath(links: &mut dyn Links, filename: &[u8]) -> Result<Vec<u8>, RealpathError> {
    let mut seen = Seen::new();
    let (path, _) = join_real(links, Vec::new(), filename, &mut seen, 1)?;
    // abspath: 絶対パスなので normpath だけ。
    Ok(normpath(&path))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    struct Fake(HashMap<Vec<u8>, Vec<u8>>);
    impl Links for Fake {
        fn is_link(&mut self, path: &[u8]) -> bool {
            self.0.contains_key(path)
        }
        fn read_link(&mut self, path: &[u8]) -> std::io::Result<Vec<u8>> {
            self.0
                .get(path)
                .cloned()
                .ok_or_else(|| std::io::Error::from_raw_os_error(2))
        }
    }

    fn fake(pairs: &[(&str, &str)]) -> Fake {
        Fake(
            pairs
                .iter()
                .map(|(a, b)| (a.as_bytes().to_vec(), b.as_bytes().to_vec()))
                .collect(),
        )
    }

    #[test]
    fn lexical_parent_absent_steps_loops_and_relative_targets_match_posixpath() {
        let mut links = fake(&[
            ("/bin", "usr/bin"),
            ("/loop/a", "b"),
            ("/loop/b", "a"),
            ("/x/up", "../y/./z"),
        ]);
        for (input, expected) in [
            ("/nonexistent/../bin/cp", "/usr/bin/cp"),
            ("/bin/cp", "/usr/bin/cp"),
            ("//usr///bin/./cp", "/usr/bin/cp"),
            ("/..", "/"),
            ("/loop/a/rest/../more", "/loop/a/more"),
            ("/x/up/w", "/y/z/w"),
        ] {
            assert_eq!(
                realpath(&mut links, input.as_bytes()).unwrap(),
                expected.as_bytes(),
                "{input}"
            );
        }
    }

    #[test]
    fn nesting_limit_is_992_levels() {
        // /l1 → /l2 → … → /lN → /target。最上位が 1 段目で、リンクごとに 1 段深くなる。
        for (count, ok) in [(991, true), (992, false)] {
            let mut map = HashMap::new();
            for i in 1..=count {
                let target = if i == count {
                    "/target".to_string()
                } else {
                    format!("/l{}", i + 1)
                };
                map.insert(format!("/l{i}").into_bytes(), target.into_bytes());
            }
            let mut links = Fake(map);
            let result = realpath(&mut links, b"/l1");
            assert_eq!(result.is_ok(), ok, "{count}");
            if !ok {
                assert!(matches!(result, Err(RealpathError::Recursion)));
            }
        }
    }
}
