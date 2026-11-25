//! `pathlib.PurePosixPath` と `Path.absolute()` の字句だけの扱い。OS でリンクを解決しない。
//! `.` は消すが `..` は残す(`..` の拒否は呼出側が決める)。値は生バイト(surrogateescape)のまま持つ。
use super::bytes::{concat, repr};

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PyPath {
    /// 相対は空、絶対は `/`。先頭がちょうど 2 個の `/` のときだけ `//`(POSIX の特例)。
    root: Vec<u8>,
    /// ルートを含まない各成分。
    parts: Vec<Vec<u8>>,
}

impl PyPath {
    pub(crate) fn parse(value: &[u8]) -> Self {
        let slashes = value.iter().take_while(|&&byte| byte == b'/').count();
        let root = match slashes {
            0 => Vec::new(),
            2 => b"//".to_vec(),
            _ => b"/".to_vec(),
        };
        let parts = value[slashes..]
            .split(|&byte| byte == b'/')
            .filter(|part| !part.is_empty() && *part != b".")
            .map(<[u8]>::to_vec)
            .collect();
        Self { root, parts }
    }

    pub(crate) fn is_absolute(&self) -> bool {
        !self.root.is_empty()
    }

    /// `Path.absolute()`。相対なら `cwd` の成分の後ろへ並べる(`cwd` の字面も同じ規則で読む)。
    pub(crate) fn absolute(&self, cwd: &[u8]) -> Self {
        if self.is_absolute() {
            return self.clone();
        }
        let mut base = Self::parse(cwd);
        if !base.is_absolute() {
            // `os.getcwd()` は絶対パスを返す。相対が来ても相対のまま成分を連ねる(旧版と同じ)。
            base.root = Vec::new();
        }
        base.parts.extend(self.parts.iter().cloned());
        base
    }

    /// `str(path)`。
    pub(crate) fn to_bytes(&self) -> Vec<u8> {
        if self.is_absolute() {
            concat(&[&self.root, &self.parts.join(&b'/')])
        } else if self.parts.is_empty() {
            b".".to_vec()
        } else {
            self.parts.join(&b'/')
        }
    }

    /// ルートを除いた成分(`Path.parts[1:]` 相当)。
    pub(crate) fn components(&self) -> &[Vec<u8>] {
        &self.parts
    }

    /// `path / name`(名前は 1 成分)。
    pub(crate) fn join(&self, name: &[u8]) -> Self {
        let mut parts = self.parts.clone();
        parts.push(name.to_vec());
        Self {
            root: self.root.clone(),
            parts,
        }
    }

    /// `Path.name`。成分が無ければ空。
    pub(crate) fn name(&self) -> &[u8] {
        self.parts.last().map_or(&[], Vec::as_slice)
    }

    pub(crate) fn parent(&self) -> Self {
        let mut parts = self.parts.clone();
        parts.pop();
        Self {
            root: self.root.clone(),
            parts,
        }
    }

    /// `Path.with_name`。採用名の検査の後に呼ぶ前提で、旧版の拒否条件を同じ順で持つ。
    pub(crate) fn with_name(&self, name: &[u8]) -> Result<Self, Vec<u8>> {
        if self.name().is_empty() {
            return Err(concat(&[&repr(&self.to_bytes()), b" has an empty name"]));
        }
        let parsed = Self::parse(name);
        if name.is_empty()
            || name.ends_with(b"/")
            || parsed.is_absolute()
            || parsed.parts.len() != 1
        {
            return Err(concat(&[b"Invalid name ", &repr(name)]));
        }
        let mut parts = self.parts.clone();
        parts.pop();
        parts.push(name.to_vec());
        Ok(Self {
            root: self.root.clone(),
            parts,
        })
    }

    /// `PurePath.relative_to`。失敗の文は旧 Python 3.10 の `ValueError` と同じ。
    pub(crate) fn relative_to(&self, other: &Self) -> Result<Self, Vec<u8>> {
        let inside = self.root == other.root && self.parts.starts_with(&other.parts);
        if inside {
            return Ok(Self {
                root: Vec::new(),
                parts: self.parts[other.parts.len()..].to_vec(),
            });
        }
        Err(concat(&[
            &repr(&self.to_bytes()),
            b" is not in the subpath of ",
            &repr(&other.to_bytes()),
            b" OR one path is relative and the other is absolute.",
        ]))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn path(value: &str) -> PyPath {
        PyPath::parse(value.as_bytes())
    }

    #[test]
    fn parse_collapses_dots_and_slashes_but_keeps_parent_references() {
        assert_eq!(path("").to_bytes(), b".");
        assert_eq!(path(".").to_bytes(), b".");
        assert_eq!(path("//a//b/./c/").to_bytes(), b"//a/b/c");
        assert_eq!(path("///a").to_bytes(), b"/a");
        assert_eq!(path("/a/../b").to_bytes(), b"/a/../b");
        assert_eq!(path("a/..").components(), [b"a".to_vec(), b"..".to_vec()]);
        assert_eq!(path("/").to_bytes(), b"/");
        assert_eq!(path("/").name(), b"");
    }

    #[test]
    fn absolute_prepends_the_working_directory_without_resolving_links() {
        assert_eq!(path("a/./b").absolute(b"/w/x").to_bytes(), b"/w/x/a/b");
        assert_eq!(path("").absolute(b"/w").to_bytes(), b"/w");
        assert_eq!(path("/z/..").absolute(b"/w").to_bytes(), b"/z/..");
        assert_eq!(path("a").absolute(b"//w").to_bytes(), b"//w/a");
    }

    #[test]
    fn parent_and_with_name_follow_pathlib() {
        assert_eq!(path("/a/..").parent().to_bytes(), b"/a");
        assert_eq!(path("/").parent().to_bytes(), b"/");
        assert_eq!(path("a").parent().to_bytes(), b".");
        assert_eq!(
            path("/r/候補_x.md")
                .with_name("進行中_x.md".as_bytes())
                .unwrap()
                .to_bytes(),
            "/r/進行中_x.md".as_bytes()
        );
        assert_eq!(
            path("/").with_name(b"x"),
            Err(b"'/' has an empty name".to_vec())
        );
        assert_eq!(
            path("/r/a").with_name(b"x/"),
            Err(b"Invalid name 'x/'".to_vec())
        );
    }

    #[test]
    fn relative_to_compares_the_root_and_whole_components() {
        assert_eq!(
            path("/a/b/c")
                .relative_to(&path("/a/b"))
                .unwrap()
                .to_bytes(),
            b"c"
        );
        assert_eq!(
            path("/a").relative_to(&path("/a")).unwrap().to_bytes(),
            b"."
        );
        assert_eq!(
            path("/a/../x").relative_to(&path("/a")).unwrap().to_bytes(),
            b"../x"
        );
        for (source, root, shown) in [
            ("/a/b", "//a", "'/a/b' is not in the subpath of '//a'"),
            ("/ab", "/a", "'/ab' is not in the subpath of '/a'"),
            ("/b", "/a/c", "'/b' is not in the subpath of '/a/c'"),
        ] {
            let message = path(source).relative_to(&path(root)).unwrap_err();
            assert_eq!(
                message,
                format!("{shown} OR one path is relative and the other is absolute.").into_bytes()
            );
        }
    }
}
