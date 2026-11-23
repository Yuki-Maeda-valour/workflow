//! 本家 3.10.11 の pathlib.PurePosixPath の整え方・文字列化・parent・parents・連結・並べ方。
//! パスは生のバイトで持つ。Python の文字列とは surrogateescape で 1 対 1 に対応し、
//! 区切りの `/` と `.` は ASCII なので、バイトの操作が文字列の操作と同じ結果になる。

use std::cmp::Ordering;

/// 旧版の EXCLUDE_DIRS。集めたパスの部品のどれかと一致すれば外す。
pub const EXCLUDE_DIRS: [&[u8]; 7] = [
    b".git",
    b"node_modules",
    b"dist",
    b"build",
    b".next",
    b"vendor",
    b"__pycache__",
];

/// 先頭の `/` の扱い(`_PosixFlavour.splitroot`)。ちょうど 2 個なら `//` を保ち、3 個以上は `/`。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Root {
    None,
    Single,
    Double,
}

impl Root {
    fn as_bytes(self) -> &'static [u8] {
        match self {
            Root::None => b"",
            Root::Single => b"/",
            Root::Double => b"//",
        }
    }
}

/// `PurePosixPath`。`names` は根を除く部品で、空と `.` を落とし、`..` は残す。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PurePath {
    root: Root,
    names: Vec<Vec<u8>>,
}

impl PurePath {
    /// `PurePosixPath(raw)`(`_parse_args` → `parse_parts`)。
    pub fn new(raw: &[u8]) -> Self {
        let slashes = raw.iter().take_while(|&&byte| byte == b'/').count();
        let root = match slashes {
            0 => Root::None,
            2 => Root::Double,
            _ => Root::Single,
        };
        let names = raw[slashes..]
            .split(|&byte| byte == b'/')
            .filter(|name| !name.is_empty() && *name != b".")
            .map(<[u8]>::to_vec)
            .collect();
        Self { root, names }
    }

    /// `str(path)`(`_format_parsed_parts` の結果が空なら `.`)。
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut result = self.root.as_bytes().to_vec();
        result.extend(self.names.join(&b'/'));
        if result.is_empty() {
            result.push(b'.');
        }
        result
    }

    /// `path.parts`。根があれば先頭の 1 部品にする。
    pub fn parts(&self) -> Vec<&[u8]> {
        let root = (self.root != Root::None).then(|| self.root.as_bytes());
        root.into_iter()
            .chain(self.names.iter().map(Vec::as_slice))
            .collect()
    }

    /// `path.parent`。部品が根だけか、何も無ければ自分。
    pub fn parent(&self) -> Self {
        let mut parent = self.clone();
        parent.names.pop();
        parent
    }

    /// `list(path.parents)`。近い祖先から順に並べる。
    pub fn parents(&self) -> Vec<Self> {
        (1..=self.names.len())
            .map(|drop| Self {
                root: self.root,
                names: self.names[..self.names.len() - drop].to_vec(),
            })
            .collect()
    }

    /// `path / other`(`_make_child` → `join_parsed_parts`)。`other` に根があれば `other` だけになる。
    pub fn join(&self, other: &[u8]) -> Self {
        let other = Self::new(other);
        if other.root != Root::None {
            return other;
        }
        let mut joined = self.clone();
        joined.names.extend(other.names);
        joined
    }

    /// `_make_child_relpath(name)`。走査で得た 1 部品をそのまま足す。
    pub fn child(&self, name: &[u8]) -> Self {
        let mut child = self.clone();
        child.names.push(name.to_vec());
        child
    }

    /// 旧版の `any(part in EXCLUDE_DIRS for part in f.parts)`。
    pub fn has_excluded_part(&self) -> bool {
        self.parts().iter().any(|part| EXCLUDE_DIRS.contains(part))
    }

    /// `_cparts` の比較の鍵。部品ごとの Unicode の符号位置の列(非 UTF-8 は U+DC80〜U+DCFF)。
    pub fn sort_key(&self) -> Vec<Vec<u32>> {
        self.parts().into_iter().map(super::code_points).collect()
    }
}

impl Ord for PurePath {
    fn cmp(&self, other: &Self) -> Ordering {
        self.sort_key().cmp(&other.sort_key())
    }
}

impl PartialOrd for PurePath {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// `sorted(paths)`。部品の列で並べ、同じものは元の順を保つ。
pub fn sort(paths: &mut [PurePath]) {
    paths.sort_by_cached_key(PurePath::sort_key);
}
