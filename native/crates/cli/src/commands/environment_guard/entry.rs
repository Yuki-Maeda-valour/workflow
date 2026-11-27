//! 控え(`entries`)に書く値の作り方。旧版が `list` で作る値と、同じ型・同じ順の `PyValue` にする。
//!
//! - 整数は `PyInt`(任意精度)。`st_dev` は、Mac では符号つき(旧 Python は `dev_t` を `long long` として出す)、
//!   Linux では符号なし。
//! - `entries` の辞書は `PyDict`。同じ鍵への代入は、最初の位置を保って値を置き換える(Python の `dict`)。
use super::fsio::{DirIdentity, Identity};
use super::pyops::{Cps, text, text_of};
use super::pyvalue::{PyDict, PyInt, PyValue};

pub fn int_i64(value: i64) -> PyValue {
    PyValue::Int(PyInt::from_i64(value))
}

pub fn int_u64(value: u64) -> PyValue {
    PyValue::Int(PyInt::from_decimal(false, &value.to_string()))
}

pub fn int_i128(value: i128) -> PyValue {
    PyValue::Int(PyInt::from_decimal(
        value < 0,
        &value.unsigned_abs().to_string(),
    ))
}

/// `st_dev`。Mac の `dev_t` は 32 ビットの符号つき整数で、`Stat.dev` には符号を広げた値が入る。
pub fn dev(value: u64) -> PyValue {
    dev_for(value, cfg!(target_os = "macos"))
}

/// `dev` の機種ごとの違い(単体テストが Mac の符号を Linux でも確かめるため、機種を引数にする)。
pub fn dev_for(value: u64, signed: bool) -> PyValue {
    if signed {
        #[allow(clippy::cast_possible_wrap)]
        int_i64(value as i64)
    } else {
        int_u64(value)
    }
}

pub fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

fn word(value: &str) -> PyValue {
    text_of(value)
}

fn identity_values(id: &Identity) -> Vec<PyValue> {
    vec![
        dev(id.dev),
        int_u64(id.ino),
        int_i64(i64::from(id.mode)),
        int_i64(id.size),
        int_i128(id.mtime_ns),
        int_i128(id.ctime_ns),
    ]
}

/// `directory_identity(st)` の 3 項目(dev・ino・mode)。
pub fn directory_values(id: &DirIdentity) -> Vec<PyValue> {
    vec![dev(id.dev), int_u64(id.ino), int_i64(i64::from(id.mode))]
}

/// `['file-stat', *identity(st)]`。
pub fn file_stat(id: &Identity) -> PyValue {
    let mut items = vec![word("file-stat")];
    items.extend(identity_values(id));
    list(items)
}

/// `['directory-stat', *directory_identity(st)]`。
pub fn directory_stat(id: &DirIdentity) -> PyValue {
    let mut items = vec![word("directory-stat")];
    items.extend(directory_values(id));
    list(items)
}

/// `['file', mode, sha]`。
pub fn file(mode: u32, sha: &str) -> PyValue {
    list(vec![word("file"), int_i64(i64::from(mode)), word(sha)])
}

/// `['directory', mode]`。
pub fn directory(mode: u32) -> PyValue {
    list(vec![word("directory"), int_i64(i64::from(mode))])
}

pub fn missing() -> PyValue {
    list(vec![word("missing")])
}

/// `['directory-step', name, *directory_identity(st)]`。
pub fn directory_step(name: &[u32], id: &DirIdentity) -> PyValue {
    let mut items = vec![word("directory-step"), text(name.to_vec())];
    items.extend(directory_values(id));
    list(items)
}

/// `['link-step', name, text, *directory_identity(st)]`。
pub fn link_step(name: &[u32], link: &[u32], id: &DirIdentity) -> PyValue {
    let mut items = vec![word("link-step"), text(name.to_vec()), text(link.to_vec())];
    items.extend(directory_values(id));
    list(items)
}

/// `['missing', name]`(`@route:` の不在の足取り)。
pub fn missing_step(name: &[u32]) -> PyValue {
    list(vec![word("missing"), text(name.to_vec())])
}

/// `['installation-link', text, *directory_identity(st)]`(部品の木の導入リンク)。
pub fn installation_link(link: &[u32], id: &DirIdentity) -> PyValue {
    let mut items = vec![word("installation-link"), text(link.to_vec())];
    items.extend(directory_values(id));
    list(items)
}

/// `['installation-link', text, st_dev, st_ino]`(`walk_at` の導入リンク。対象の 3 項目は後から足す)。
pub fn link_head(kind: &str, link: &[u32], dev_value: u64, ino: u64) -> Vec<PyValue> {
    vec![
        word(kind),
        text(link.to_vec()),
        dev(dev_value),
        int_u64(ino),
    ]
}

/// `['directory' か 'missing', index]`(`@route-end:`)。
pub fn route_end(missing: bool, index: usize) -> PyValue {
    list(vec![
        word(if missing { "missing" } else { "directory" }),
        int_u64(index as u64),
    ])
}

/// 文字列の一覧(`@names:`)。
pub fn names(items: &[Cps]) -> PyValue {
    list(items.iter().map(|name| text(name.clone())).collect())
}

/// `entries[key] = value`。
pub fn put(entries: &mut PyDict, key: &[u32], value: PyValue) {
    entries
        .insert(text(key.to_vec()), value)
        .expect("文字列は hash できる");
}

/// `entries.get(key)`。
pub fn get<'a>(entries: &'a PyDict, key: &[u32]) -> Option<&'a PyValue> {
    entries.get(&text(key.to_vec())).ok().flatten()
}

/// `key in entries`。
pub fn contains(entries: &PyDict, key: &[u32]) -> bool {
    get(entries, key).is_some()
}

#[cfg(test)]
mod tests {
    use super::super::pyjson::{PyCtx, dump_state};
    use super::super::pystr::to_string;
    use super::*;

    fn json(value: &PyValue) -> String {
        let raw = dump_state(value, PyCtx::new(0)).unwrap();
        String::from_utf8(raw).unwrap()
    }

    #[test]
    fn integers_are_exact() {
        assert_eq!(json(&int_u64(u64::MAX)), "18446744073709551615\n");
        assert_eq!(
            json(&int_i128(-1_700_000_000_123_456_789)),
            "-1700000000123456789\n"
        );
        assert_eq!(json(&int_i128(0)), "0\n");
    }

    #[test]
    fn the_device_number_is_signed_on_mac_only() {
        // Mac の `dev_t` は 32 ビットの符号つき。符号を広げた値を戻すと、旧 Python と同じ負の数になる。
        let extended = u64::from_ne_bytes((-5_i64).to_ne_bytes());
        assert_eq!(json(&dev_for(extended, true)), "-5\n");
        assert_eq!(json(&dev_for(extended, false)), "18446744073709551611\n");
        assert_eq!(json(&dev_for(0x801, true)), "2049\n");
        assert_eq!(json(&dev_for(0x801, false)), "2049\n");
    }

    #[test]
    fn values_have_the_old_shapes() {
        let id = Identity {
            dev: 7,
            ino: 8,
            mode: 0o100644,
            size: 9,
            mtime_ns: 10,
            ctime_ns: 11,
        };
        assert_eq!(json(&file_stat(&id)), "[\"file-stat\",7,8,33188,9,10,11]\n");
        let dir = DirIdentity {
            dev: 7,
            ino: 8,
            mode: 0o40755,
        };
        assert_eq!(
            json(&directory_stat(&dir)),
            "[\"directory-stat\",7,8,16877]\n"
        );
        assert_eq!(json(&file(0o644, "ab")), "[\"file\",420,\"ab\"]\n");
        assert_eq!(json(&directory(0o755)), "[\"directory\",493]\n");
        assert_eq!(json(&missing()), "[\"missing\"]\n");
        assert_eq!(
            json(&directory_step(&[0x61], &dir)),
            "[\"directory-step\",\"a\",7,8,16877]\n"
        );
        assert_eq!(
            json(&link_step(&[0x61], &[0x62], &dir)),
            "[\"link-step\",\"a\",\"b\",7,8,16877]\n"
        );
        assert_eq!(json(&missing_step(&[0x61])), "[\"missing\",\"a\"]\n");
        assert_eq!(
            json(&installation_link(&[0x62], &dir)),
            "[\"installation-link\",\"b\",7,8,16877]\n"
        );
        assert_eq!(json(&route_end(true, 3)), "[\"missing\",3]\n");
        assert_eq!(json(&route_end(false, 0)), "[\"directory\",0]\n");
        assert_eq!(json(&names(&[vec![0x61], vec![0x62]])), "[\"a\",\"b\"]\n");
        assert_eq!(
            to_string(&[0x61]),
            "a",
            "補助関数の確認(文字番号から文字列)"
        );
    }

    #[test]
    fn put_keeps_the_first_position_of_a_key() {
        let mut entries = PyDict::new();
        put(&mut entries, &[0x61], missing());
        put(&mut entries, &[0x62], missing());
        put(&mut entries, &[0x61], directory(1));
        let keys: Vec<String> = entries
            .keys()
            .map(|key| to_string(super::super::pyops::as_text(key).unwrap()))
            .collect();
        assert_eq!(keys, ["a", "b"]);
        assert!(contains(&entries, &[0x61]));
        assert!(!contains(&entries, &[0x63]));
        assert_eq!(json(get(&entries, &[0x61]).unwrap()), "[\"directory\",1]\n");
    }
}
