//! Python 3.10 の `pathlib.Path`(POSIX)と `os.path` の再現。字面だけを扱い、OS は呼ばない。
//! `..` を字面で潰さない。先頭の `//` は 2 個ちょうどのときだけ残る。
use super::error::PyException;

const SLASH: u32 = 0x2f;
const DOT: u32 = 0x2e;

/// `Path` の字面。`root` は空・`/`・`//` のどれか。
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct PyPath {
    root: Vec<u32>,
    names: Vec<Vec<u32>>,
}

/// 1 つの文字列を `parse_parts` と同じに切る(根と、空と `.` を除いた名前)。
fn split(text: &[u32]) -> (Vec<u32>, Vec<Vec<u32>>) {
    let stripped = text.iter().position(|&c| c != SLASH).unwrap_or(text.len());
    let root = match stripped {
        0 => Vec::new(),
        2 => vec![SLASH, SLASH],
        _ => vec![SLASH],
    };
    let names = text[stripped..]
        .split(|&c| c == SLASH)
        .filter(|part| !part.is_empty() && *part != [DOT])
        .map(<[u32]>::to_vec)
        .collect();
    (root, names)
}

impl PyPath {
    /// `Path(text)`。
    pub fn new(text: &[u32]) -> Self {
        let (root, names) = split(text);
        Self { root, names }
    }

    /// `Path / text`。`text` が絶対なら置き換わる。
    pub fn join(&self, text: &[u32]) -> Self {
        let (root, names) = split(text);
        if !root.is_empty() {
            return Self { root, names };
        }
        let mut joined = self.names.clone();
        joined.extend(names);
        Self {
            root: self.root.clone(),
            names: joined,
        }
    }

    pub fn is_absolute(&self) -> bool {
        !self.root.is_empty()
    }

    /// `Path.parts`。根があれば先頭に入る。
    pub fn parts(&self) -> Vec<Vec<u32>> {
        let mut parts = Vec::new();
        if !self.root.is_empty() {
            parts.push(self.root.clone());
        }
        parts.extend(self.names.iter().cloned());
        parts
    }

    /// `Path.name`。名前が無ければ空。
    pub fn name(&self) -> Vec<u32> {
        self.names.last().cloned().unwrap_or_default()
    }

    /// `Path.parent`。根と `.` の親は自分自身。
    pub fn parent(&self) -> Self {
        let mut names = self.names.clone();
        names.pop();
        Self {
            root: self.root.clone(),
            names,
        }
    }

    /// `Path.parents`。近い順。
    pub fn parents(&self) -> Vec<Self> {
        (1..=self.names.len())
            .map(|drop| Self {
                root: self.root.clone(),
                names: self.names[..self.names.len() - drop].to_vec(),
            })
            .collect()
    }

    /// `str(Path)`。
    pub fn to_cps(&self) -> Vec<u32> {
        let mut out = self.root.clone();
        for (index, name) in self.names.iter().enumerate() {
            if index > 0 {
                out.push(SLASH);
            }
            out.extend_from_slice(name);
        }
        if out.is_empty() {
            out.push(DOT);
        }
        out
    }

    pub fn has_part(&self, part: &[u32]) -> bool {
        self.names.iter().any(|name| name == part)
    }
}

/// `os.path.isabs(text)`。
pub fn isabs(text: &[u32]) -> bool {
    text.first() == Some(&SLASH)
}

/// `pwd` の引き方。Linux の musl では `/etc/passwd` だけを引き、Mac では旧版と同じ仕組みを使う(U6)。
/// 段階 2 の platform が実装する。段階 1 は差し込み口だけで、テストでは偽の表を使う。
pub trait UserLookup {
    /// `pwd.getpwnam(name).pw_dir`。名前が無ければ `Ok(None)`(旧版の `KeyError`)。
    fn home_of_name(&self, name: &[u8]) -> Result<Option<Vec<u8>>, PyException>;
    /// `pwd.getpwuid(os.getuid()).pw_dir`。
    fn home_of_current_user(&self) -> Result<Option<Vec<u8>>, PyException>;
}

/// `os.path.expanduser(text)`。`home_env` は環境変数 `HOME`(未設定は `None`)。
pub fn expanduser(
    text: &[u32],
    home_env: Option<&[u32]>,
    users: &dyn UserLookup,
) -> Result<Vec<u32>, PyException> {
    if text.first() != Some(&0x7e) {
        return Ok(text.to_vec());
    }
    let end = text[1..]
        .iter()
        .position(|&c| c == SLASH)
        .map_or(text.len(), |i| i + 1);
    let home: Vec<u32> = if end == 1 {
        match home_env {
            Some(home) => home.to_vec(),
            None => match users.home_of_current_user()? {
                Some(home) => super::pystr::decode_fs(&home),
                None => return Ok(text.to_vec()),
            },
        }
    } else {
        let name = &text[1..end];
        // `pwd.getpwnam` は名前を先に符号化し、その後で NUL を確かめる。
        let bytes = super::pystr::encode_fs(name)
            .map_err(|failure| super::pystr::encode_error(&failure))?;
        if bytes.contains(&0) {
            return Err(PyException::value_error("embedded null byte"));
        }
        match users.home_of_name(&bytes)? {
            Some(home) => super::pystr::decode_fs(&home),
            None => return Ok(text.to_vec()),
        }
    };
    let mut out = super::pystr::rstrip_char(&home, SLASH).to_vec();
    out.extend_from_slice(&text[end..]);
    if out.is_empty() {
        out.push(SLASH);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::super::pystr::{cps, to_string};
    use super::super::testing::{cps_of, fixture};
    use super::*;

    struct Table;
    impl UserLookup for Table {
        fn home_of_name(&self, name: &[u8]) -> Result<Option<Vec<u8>>, PyException> {
            Ok((name == b"root").then(|| b"/root".to_vec()))
        }
        fn home_of_current_user(&self) -> Result<Option<Vec<u8>>, PyException> {
            Ok(Some(b"/current/".to_vec()))
        }
    }

    fn s(path: &PyPath) -> String {
        to_string(&path.to_cps())
    }

    #[test]
    fn path_normalization_follows_pathlib() {
        for (input, expect) in [
            ("", "."),
            (".", "."),
            ("./", "."),
            ("a//b/./c/", "a/b/c"),
            ("/", "/"),
            ("//", "//"),
            ("///", "/"),
            ("//a", "//a"),
            ("///a", "/a"),
            ("/a/../b", "/a/../b"),
            ("..", ".."),
            ("a/./", "a"),
        ] {
            assert_eq!(s(&PyPath::new(&cps(input))), expect, "{input:?}");
        }
        let path = PyPath::new(&cps("/a/b"));
        assert_eq!(s(&path.join(&cps("c//d"))), "/a/b/c/d");
        assert_eq!(s(&path.join(&cps("/x"))), "/x");
        assert_eq!(s(&path.join(&cps("//x"))), "//x");
        assert_eq!(s(&path.join(&cps(""))), "/a/b");
        assert_eq!(s(&PyPath::new(&cps("")).join(&cps("x"))), "x");
        assert_eq!(path.parts(), vec![cps("/"), cps("a"), cps("b")]);
        assert_eq!(to_string(&path.name()), "b");
        assert_eq!(s(&path.parent()), "/a");
        assert_eq!(s(&PyPath::new(&cps("/")).parent()), "/");
        assert_eq!(s(&PyPath::new(&cps("a")).parent()), ".");
        assert_eq!(to_string(&PyPath::new(&cps("/")).name()), "");
        assert_eq!(
            path.parents().iter().map(s).collect::<Vec<_>>(),
            ["/a", "/"]
        );
        assert!(path.has_part(&cps("a")));
        assert!(!PyPath::new(&cps("/a/../b")).has_part(&cps("x")));
        assert!(PyPath::new(&cps("/a/../b")).has_part(&cps("..")));
        assert!(isabs(&cps("//x")));
        assert!(!isabs(&cps("x")));
    }

    #[test]
    fn expanduser_follows_posixpath() {
        let e = |text: &str, home: Option<&str>| {
            let home = home.map(cps);
            to_string(&expanduser(&cps(text), home.as_deref(), &Table).unwrap())
        };
        assert_eq!(e("~", Some("/h")), "/h");
        assert_eq!(e("~/x", Some("/h/")), "/h/x");
        assert_eq!(e("~/x", Some("/h//")), "/h/x");
        assert_eq!(e("~", Some("")), "/");
        assert_eq!(e("~/x", Some("")), "/x");
        assert_eq!(e("~", Some("/")), "/");
        assert_eq!(e("~/x", None), "/current/x");
        assert_eq!(e("~root/y", Some("/h")), "/root/y");
        assert_eq!(e("~nobody-here/y", Some("/h")), "~nobody-here/y");
        assert_eq!(e("a~", Some("/h")), "a~");
        assert!(
            expanduser(&cps("~a\u{0}b"), None, &Table)
                .unwrap_err()
                .is_instance("ValueError")
        );
        assert!(
            expanduser(&[0x7e, 0xd800], None, &Table)
                .unwrap_err()
                .is_instance("UnicodeEncodeError")
        );
        assert_eq!(e("~/", Some("rel")), "rel/");
    }

    #[test]
    fn path_cases_match_python_3_10() {
        for case in fixture!("path_cases.json")["paths"].as_array().unwrap() {
            let text = cps_of(&case["text"]);
            let path = PyPath::new(&text);
            assert_eq!(path.to_cps(), cps_of(&case["str"]), "{case}");
            let parts: Vec<Vec<u32>> = case["parts"]
                .as_array()
                .unwrap()
                .iter()
                .map(cps_of)
                .collect();
            assert_eq!(path.parts(), parts, "{case}");
            assert_eq!(path.name(), cps_of(&case["name"]), "{case}");
            assert_eq!(path.parent().to_cps(), cps_of(&case["parent"]), "{case}");
            let parents: Vec<Vec<u32>> = case["parents"]
                .as_array()
                .unwrap()
                .iter()
                .map(cps_of)
                .collect();
            assert_eq!(
                path.parents()
                    .iter()
                    .map(PyPath::to_cps)
                    .collect::<Vec<_>>(),
                parents,
                "{case}"
            );
            assert_eq!(
                path.is_absolute(),
                case["is_absolute"].as_bool().unwrap(),
                "{case}"
            );
            assert_eq!(isabs(&text), case["isabs"].as_bool().unwrap(), "{case}");
            let other = cps_of(&case["other"]);
            assert_eq!(
                path.join(&other).to_cps(),
                cps_of(&case["joined"]),
                "{case}"
            );
        }
    }

    #[test]
    fn expanduser_cases_match_python_3_10() {
        for case in fixture!("path_cases.json")["expand"].as_array().unwrap() {
            let text = cps_of(&case["text"]);
            let home = case["home"].as_array().map(|_| cps_of(&case["home"]));
            let result = expanduser(&text, home.as_deref(), &CaseUsers);
            match case["error"].as_str() {
                Some(name) => {
                    let error = result.unwrap_err();
                    assert_eq!(error.type_name, name, "{case}");
                    assert_eq!(
                        super::super::pystr::to_string(error.message.as_codepoints()),
                        case["msg"].as_str().unwrap(),
                        "{case}"
                    );
                }
                None => assert_eq!(result.unwrap(), cps_of(&case["result"]), "{case}"),
            }
        }
    }

    struct CaseUsers;
    impl UserLookup for CaseUsers {
        fn home_of_name(&self, name: &[u8]) -> Result<Option<Vec<u8>>, PyException> {
            Ok((name == b"root").then(|| b"/root".to_vec()))
        }
        fn home_of_current_user(&self) -> Result<Option<Vec<u8>>, PyException> {
            Ok(Some(b"/root".to_vec()))
        }
    }
}
