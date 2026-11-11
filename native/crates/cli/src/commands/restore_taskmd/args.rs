//! 引数の検査(旧 helper の `parse_arguments` :48-70)。判定はバイト列で行う。
use std::ffi::OsString;
use std::os::unix::ffi::OsStrExt;

/// 検査済みの要求。値は argv のバイト列のまま持つ。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub(super) source: Vec<u8>,
    pub(super) destination: Vec<u8>,
    pub(super) expected_mode: u32,
    pub(super) expected_sha256: Vec<u8>,
    pub(super) cp: Vec<u8>,
}

/// 引数の誤り(`internal-argument`)。
#[derive(Debug, PartialEq, Eq)]
pub struct ArgumentError;

const KEYS: [&[u8]; 5] = [
    b"--source",
    b"--destination",
    b"--expected-mode",
    b"--expected-sha256",
    b"--cp",
];

fn mode(value: &[u8]) -> Option<u32> {
    // re.fullmatch('[0-7]{1,4}')
    if value.is_empty() || value.len() > 4 || !value.iter().all(|c| (b'0'..=b'7').contains(c)) {
        return None;
    }
    Some(
        value
            .iter()
            .fold(0u32, |total, c| total * 8 + u32::from(c - b'0')),
    )
}

fn sha256(value: &[u8]) -> bool {
    // re.fullmatch('[0-9a-f]{64}')
    value.len() == 64
        && value
            .iter()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c))
}

/// 引数の検査。argv はプログラム名を除いたもの。
pub fn parse_request(argv: &[OsString]) -> Result<Request, ArgumentError> {
    if argv.len() != 11 || argv.first().map(|v| v.as_bytes()) != Some(b"restore") {
        return Err(ArgumentError);
    }
    let mut values: [Option<Vec<u8>>; 5] = Default::default();
    for pair in argv.get(1..).unwrap_or_default().chunks(2) {
        let [key, value] = pair else {
            return Err(ArgumentError);
        };
        let Some(index) = KEYS.iter().position(|k| *k == key.as_bytes()) else {
            return Err(ArgumentError);
        };
        let slot = values.get_mut(index).ok_or(ArgumentError)?;
        if slot.is_some() || value.is_empty() {
            return Err(ArgumentError);
        }
        *slot = Some(value.as_bytes().to_vec());
    }
    let [
        Some(source),
        Some(destination),
        Some(expected_mode),
        Some(expected_sha256),
        Some(cp),
    ] = values
    else {
        return Err(ArgumentError);
    };
    for (index, value) in [&source, &destination, &cp].into_iter().enumerate() {
        if value.first() != Some(&b'/') || value.contains(&0) {
            return Err(ArgumentError);
        }
        // --source と --destination だけ、2 番目以降の要素に空・`.`・`..` を許さない。
        if index < 2
            && value
                .get(1..)
                .unwrap_or_default()
                .split(|c| *c == b'/')
                .any(|part| part.is_empty() || part == b"." || part == b"..")
        {
            return Err(ArgumentError);
        }
    }
    let expected_mode = mode(&expected_mode).ok_or(ArgumentError)?;
    if !sha256(&expected_sha256) {
        return Err(ArgumentError);
    }
    Ok(Request {
        source,
        destination,
        expected_mode,
        expected_sha256,
        cp,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    fn argv(items: &[&[u8]]) -> Vec<OsString> {
        items
            .iter()
            .map(|item| OsStr::from_bytes(item).to_os_string())
            .collect()
    }

    fn valid() -> Vec<&'static [u8]> {
        vec![
            b"restore",
            b"--source",
            b"/tmp/a",
            b"--destination",
            b"/tmp/b",
            b"--expected-mode",
            b"640",
            b"--expected-sha256",
            b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            b"--cp",
            b"/bin/cp",
        ]
    }

    #[test]
    fn accepts_any_order_and_rejects_every_malformed_shape() {
        assert!(parse_request(&argv(&valid())).is_ok());
        let mut reordered = valid();
        reordered.swap(1, 9);
        reordered.swap(2, 10);
        assert!(parse_request(&argv(&reordered)).is_ok());
        let mut cases: Vec<Vec<&[u8]>> = vec![vec![], vec![b"restore"], {
            let mut v = valid();
            v[0] = b"other";
            v
        }];
        for (index, value) in [
            (2, &b"/"[..]),
            (2, b"/tmp//a"),
            (2, b"/tmp/../a"),
            (2, b"/tmp/./a"),
            (4, b"/tmp/b/"),
            (4, b"relative"),
            (6, b"10000"),
            (6, b"888"),
            (6, b"9"),
            (6, b""),
            (
                8,
                b"ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
            ),
            (
                8,
                b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde",
            ),
            (
                8,
                b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0",
            ),
            (10, b"cp"),
            (1, b"--source2"),
            (1, b"--cp"),
        ] {
            let mut v = valid();
            v[index] = value;
            cases.push(v);
        }
        for case in cases {
            assert_eq!(parse_request(&argv(&case)), Err(ArgumentError), "{case:?}");
        }
        // --cp だけは `..` などの段を許す。
        let mut v = valid();
        v[10] = b"/nonexistent/../bin/./cp";
        assert!(parse_request(&argv(&v)).is_ok());
        // 非 UTF-8 のパスはそのまま受ける。
        let mut v = valid();
        v[4] = b"/tmp/\xff\xfe";
        assert_eq!(
            parse_request(&argv(&v)).unwrap().destination,
            b"/tmp/\xff\xfe"
        );
        assert_eq!(parse_request(&argv(&valid())).unwrap().expected_mode, 0o640);
    }
}
