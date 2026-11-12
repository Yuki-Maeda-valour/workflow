//! 所属の印・`.git`ファイル・`HEAD`・`commondir`の短い中身の形(旧版の`parse_mark`・`pointer_target`・
//! `head_is_valid`)。印の形式の正本は旧`implement-guard.sh`。

/// 印の13キー(旧版の`MARK_KEYS`の順)。
pub const MARK_KEYS: [&str; 13] = [
    "version",
    "guard_dev",
    "guard_ino",
    "top",
    "top_dev",
    "top_ino",
    "common",
    "common_dev",
    "common_ino",
    "taskdir",
    "taskdir_dev",
    "taskdir_ino",
    "tmpdir",
];

/// 形の正しい印。番号は20桁までの十進(u64を超える値もそのまま持つ)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Mark {
    pub guard: (i128, i128),
    /// `top`・`common`・`taskdir`の順に、パスと番号。
    pub paths: [(Vec<u8>, (i128, i128)); 3],
    /// 空なら`tmpdir`は無い。
    pub tmpdir: Vec<u8>,
}

/// 旧版の`decimal`: `0|[1-9][0-9]{0,19}`の全体に合う値だけ。
pub fn decimal(value: &[u8]) -> Option<i128> {
    let valid = match value {
        [b'0'] => true,
        [first, rest @ ..] => {
            (b'1'..=b'9').contains(first) && rest.len() <= 19 && rest.iter().all(u8::is_ascii_digit)
        }
        [] => false,
    };
    if !valid {
        return None;
    }
    Some(
        value
            .iter()
            .fold(0i128, |total, digit| total * 10 + i128::from(digit - b'0')),
    )
}

/// 小文字の16進の組の並び(`(?:[0-9a-f]{2})+`)を元のバイトへ戻す。
fn lower_hex(value: &[u8]) -> Option<Vec<u8>> {
    if value.is_empty()
        || !value.len().is_multiple_of(2)
        || !value
            .iter()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        return None;
    }
    let digit = |byte: u8| match byte {
        b'0'..=b'9' => byte - b'0',
        _ => byte - b'a' + 10,
    };
    Some(
        value
            .chunks(2)
            .map(|pair| digit(pair[0]) * 16 + digit(pair[1]))
            .collect(),
    )
}

/// 旧版の`parse_mark`。全キーが1回ずつ・`version`は1・十進と小文字16進・絶対パス。外れたら`None`。
pub fn parse_mark(data: &[u8]) -> Option<Mark> {
    let body = data.strip_suffix(b"\n")?;
    let mut values: Vec<(&'static str, &[u8])> = Vec::new();
    for line in body.split(|byte| *byte == b'\n') {
        let tab = line.iter().position(|byte| *byte == b'\t')?;
        let (key, value) = (&line[..tab], &line[tab + 1..]);
        let key = MARK_KEYS
            .iter()
            .find(|name| name.as_bytes() == key)
            .copied()?;
        if values.iter().any(|(seen, _)| *seen == key) {
            return None;
        }
        values.push((key, value));
    }
    let get = |name: &str| {
        values
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| *value)
    };
    if values.len() != MARK_KEYS.len() || get("version") != Some(b"1".as_slice()) {
        return None;
    }
    let number = |name: &str| decimal(get(name)?);
    let path = |name: &str| {
        let raw = lower_hex(get(name)?)?;
        (raw.starts_with(b"/") && !raw.contains(&0)).then_some(raw)
    };
    // 旧版の検査の順(キーの順)に、最初に外れた所で None になる。どこで外れても結果は同じ。
    let guard = (number("guard_dev")?, number("guard_ino")?);
    let top = (path("top")?, (number("top_dev")?, number("top_ino")?));
    let common = (
        path("common")?,
        (number("common_dev")?, number("common_ino")?),
    );
    let taskdir = (
        path("taskdir")?,
        (number("taskdir_dev")?, number("taskdir_ino")?),
    );
    let tmpdir = if get("tmpdir")?.is_empty() {
        Vec::new()
    } else {
        path("tmpdir")?
    };
    Some(Mark {
        guard,
        paths: [top, common, taskdir],
        tmpdir,
    })
}

/// 旧版の`pointer_target`。`prefix`の後の1行(末尾の`\r`と`\n`を除く)。空・NUL・改行を含めば`None`。
pub fn pointer_target<'a>(data: Option<&'a [u8]>, prefix: &[u8]) -> Option<&'a [u8]> {
    let rest = data?.strip_prefix(prefix)?;
    let end = rest
        .iter()
        .rposition(|byte| *byte != b'\r' && *byte != b'\n')
        .map_or(0, |index| index + 1);
    let raw = &rest[..end];
    (!raw.is_empty() && !raw.iter().any(|byte| matches!(byte, 0 | b'\n' | b'\r'))).then_some(raw)
}

/// 旧版の`head_is_valid`。`ref: refs/`で始まるか、40桁か64桁の16進に`\r?\n?`が続くだけ。
pub fn head_is_valid(data: Option<&[u8]>) -> bool {
    let Some(data) = data else {
        return false;
    };
    if data.starts_with(b"ref: refs/") {
        return true;
    }
    let digits = data
        .iter()
        .take_while(|byte| byte.is_ascii_hexdigit())
        .count();
    let rest = &data[digits..];
    (digits == 40 || digits == 64) && matches!(rest, b"" | b"\n" | b"\r" | b"\r\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mark_bytes(pairs: &[(&str, &[u8])]) -> Vec<u8> {
        let mut out = Vec::new();
        for (key, value) in pairs {
            out.extend_from_slice(key.as_bytes());
            out.push(b'\t');
            out.extend_from_slice(value);
            out.push(b'\n');
        }
        out
    }

    fn good() -> Vec<(&'static str, Vec<u8>)> {
        vec![
            ("version", b"1".to_vec()),
            ("guard_dev", b"18446744073709551616".to_vec()),
            ("guard_ino", b"0".to_vec()),
            ("top", b"2f61".to_vec()),
            ("top_dev", b"1".to_vec()),
            ("top_ino", b"2".to_vec()),
            ("common", b"2f62".to_vec()),
            ("common_dev", b"3".to_vec()),
            ("common_ino", b"99999999999999999999".to_vec()),
            ("taskdir", b"2f63".to_vec()),
            ("taskdir_dev", b"5".to_vec()),
            ("taskdir_ino", b"6".to_vec()),
            ("tmpdir", b"".to_vec()),
        ]
    }

    fn encode(pairs: &[(&'static str, Vec<u8>)]) -> Vec<u8> {
        let borrowed: Vec<(&str, &[u8])> = pairs.iter().map(|(k, v)| (*k, v.as_slice())).collect();
        mark_bytes(&borrowed)
    }

    #[test]
    fn numbers_are_twenty_digit_decimals_compared_as_integers() {
        assert_eq!(decimal(b"0"), Some(0));
        assert_eq!(decimal(b"18446744073709551615"), Some(18446744073709551615));
        assert_eq!(decimal(b"18446744073709551616"), Some(18446744073709551616));
        assert_eq!(decimal(b"99999999999999999999"), Some(99999999999999999999));
        for bad in [
            &b"100000000000000000000"[..],
            b"01",
            b"00",
            b"",
            b"-1",
            b"+1",
            b"1a",
            b" 1",
        ] {
            assert_eq!(decimal(bad), None, "{bad:?}");
        }
        let mark = parse_mark(&encode(&good())).unwrap();
        assert_eq!(mark.guard, (18446744073709551616, 0));
        assert_eq!(mark.paths[1].1, (3, 99999999999999999999));
        assert!(mark.tmpdir.is_empty());
    }

    #[test]
    fn malformed_marks_are_rejected() {
        let base = good();
        let mut cases: Vec<Vec<u8>> = Vec::new();
        cases.push(encode(&base[..12]));
        let mut duplicate = encode(&base);
        duplicate.extend_from_slice(b"version\t1\n");
        cases.push(duplicate);
        let mut extra = encode(&base);
        extra.extend_from_slice(b"extra\t1\n");
        cases.push(extra);
        for (key, value) in [
            ("version", &b"2"[..]),
            ("guard_dev", b"1a"),
            ("top_ino", b"-2"),
            ("top", b"2F61"),
            ("top", b"2f6"),
            ("top", b"7265"),
            ("top", b""),
            ("top", b"2f00"),
            ("tmpdir", b"746d70"),
            ("guard_ino", b"100000000000000000000"),
        ] {
            let changed: Vec<(&'static str, Vec<u8>)> = base
                .iter()
                .map(|(k, v)| (*k, if *k == key { value.to_vec() } else { v.clone() }))
                .collect();
            cases.push(encode(&changed));
        }
        let good_bytes = encode(&base);
        let mut crlf = Vec::new();
        for byte in &good_bytes {
            if *byte == b'\n' {
                crlf.push(b'\r');
            }
            crlf.push(*byte);
        }
        cases.push(crlf);
        cases.push(good_bytes[..good_bytes.len() - 1].to_vec());
        cases.push(Vec::new());
        cases.push(b"\xff\t1\n".to_vec());
        for case in cases {
            assert_eq!(
                parse_mark(&case),
                None,
                "{:?}",
                String::from_utf8_lossy(&case)
            );
        }
        assert!(parse_mark(&good_bytes).is_some());
    }

    #[test]
    fn pointers_and_heads_follow_the_old_forms() {
        assert_eq!(
            pointer_target(Some(b"gitdir: ../a\r\n\n"), b"gitdir: "),
            Some(&b"../a"[..])
        );
        assert_eq!(pointer_target(Some(b"gitdir: \n"), b"gitdir: "), None);
        assert_eq!(pointer_target(Some(b"gitdir: a\nb\n"), b"gitdir: "), None);
        assert_eq!(pointer_target(Some(b"gitdir: a\0"), b"gitdir: "), None);
        assert_eq!(pointer_target(Some(b"other"), b"gitdir: "), None);
        assert_eq!(pointer_target(None, b""), None);
        assert_eq!(pointer_target(Some(b"../.."), b""), Some(&b"../.."[..]));
        assert!(head_is_valid(Some(b"ref: refs/heads/main\n")));
        assert!(head_is_valid(Some(&[b'a'; 40])));
        let mut long = vec![b'F'; 64];
        long.extend_from_slice(b"\r\n");
        assert!(head_is_valid(Some(&long)));
        for bad in [
            &b"ref: heads/main"[..],
            &[b'a'; 41],
            &[b'a'; 39],
            b"",
            b"garbage\n",
        ] {
            assert!(!head_is_valid(Some(bad)), "{bad:?}");
        }
        let mut two = vec![b'a'; 40];
        two.extend_from_slice(b"\n\n");
        assert!(!head_is_valid(Some(&two)));
        assert!(!head_is_valid(None));
    }
}
