//! 実名と REV の検査(R4D-3)。旧版 `diagnostic-paths.py` の `literal`・`revision`(:11-22)と同じ順で処理する。
//!
//! 残り(:24-100。`glob_ere`・`Matcher`・`selected`)は R4D-4 #486・R4D-5 #487 の範囲である。
use super::budget::Failure;
use super::state::{Error, is_lower_hex};
use dev_workflow_core::loop_state::CodepointString;

const PATH: Failure = Failure::new("path", 2);
const REVISION: Failure = Failure::new("revision", 2);

/// 実名(利用者が渡すパスの指定)を確かめる(旧版の `literal`)。旧版は `raw` をそのまま返すので、成否だけを返す。
pub fn literal(raw: &[u8], under: bool) -> Result<(), Failure> {
    if raw.is_empty() || raw.len() > 4096 || raw[0] == b'/' || raw.contains(&0) {
        return Err(PATH);
    }
    if under && raw == b"." {
        return Ok(());
    }
    if raw
        .split(|&c| c == b'/')
        .any(|part| part.is_empty() || part == b"." || part == b"..")
    {
        return Err(PATH);
    }
    Ok(())
}

/// 文字の番号の列を UTF-8 の bytes にする(旧版の `str.encode('utf-8')`)。孤立したサロゲートがあれば `Error::Unicode`。
pub(crate) fn encode_utf8(value: &CodepointString) -> Result<Vec<u8>, Error> {
    let mut out = Vec::with_capacity(value.as_codepoints().len());
    let mut buffer = [0_u8; 4];
    for &point in value.as_codepoints() {
        let character = char::from_u32(point).ok_or(Error::Unicode)?;
        out.extend_from_slice(character.encode_utf8(&mut buffer).as_bytes());
    }
    Ok(out)
}

fn contains(raw: &[u8], needle: &[u8]) -> bool {
    raw.windows(needle.len()).any(|window| window == needle)
}

/// REV(見る版の指定)を確かめる(旧版の `revision`)。`None` は文字列でない値(旧版の `isinstance(value,str)` が偽)。
pub fn revision(value: Option<&CodepointString>) -> Result<(), Error> {
    let Some(value) = value else {
        return Err(REVISION.into());
    };
    let raw = encode_utf8(value)?;
    if raw.len() > 1024 {
        return Err(REVISION.into());
    }
    if raw == b"HEAD" || is_lower_hex(&raw, 40) || is_lower_hex(&raw, 64) {
        return Ok(());
    }
    let forbidden = |c: &u8| *c < 32 || *c == 127 || b" ~^:?*[\\".contains(c);
    if !raw.starts_with(b"refs/")
        || raw.iter().any(forbidden)
        || contains(&raw, b"..")
        || contains(&raw, b"@{")
        || raw
            .split(|&c| c == b'/')
            .any(|part| part.is_empty() || part.starts_with(b".") || part.ends_with(b".lock"))
        || raw.ends_with(b"/")
        || raw.ends_with(b".")
    {
        return Err(REVISION.into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> CodepointString {
        CodepointString::from(value)
    }

    #[test]
    fn literal_checks_length_and_parts() {
        assert_eq!(literal(&b"a".repeat(4096), false), Ok(()));
        assert_eq!(literal(&b"a".repeat(4097), false), Err(PATH));
        assert_eq!(literal(b".", true), Ok(()));
        assert_eq!(literal(b".", false), Err(PATH));
        assert_eq!(literal(b"a/b/c", false), Ok(()));
        for bad in [
            &b""[..],
            b"/a",
            b"a\0",
            b"a//b",
            b"a/",
            b"./a",
            b"a/..",
            b"..",
            b"a/./b",
        ] {
            assert_eq!(literal(bad, true), Err(PATH), "{bad:?}");
        }
        assert_eq!(literal(b"a/.b/c..", false), Ok(()));
    }

    #[test]
    fn revision_checks_in_the_legacy_order() {
        assert_eq!(revision(None), Err(REVISION.into()));
        for good in [
            "HEAD",
            "refs/heads/main",
            "refs/x.y/z",
            &"a".repeat(40),
            &"0".repeat(64),
        ] {
            assert_eq!(revision(Some(&text(good))), Ok(()), "{good}");
        }
        let long = format!("refs/{}", "a".repeat(1019));
        assert_eq!(revision(Some(&text(&long))), Ok(()));
        let longer = format!("refs/{}", "a".repeat(1020));
        assert_eq!(revision(Some(&text(&longer))), Err(REVISION.into()));
        for bad in [
            "head",
            "main",
            &"a".repeat(39),
            &"a".repeat(41),
            &"A".repeat(40),
            &"a".repeat(63),
            &"a".repeat(65),
            "refs/",
            "refs/a/",
            "refs/a.",
            "refs/a..b",
            "refs/a@{b",
            "refs/.a",
            "refs/a.lock",
            "refs//a",
            "refs/a b",
            "refs/a~",
            "refs/a^",
            "refs/a:",
            "refs/a?",
            "refs/a*",
            "refs/a[",
            "refs/a\\",
            "refs/a\x7f",
            "refs/a\x1f",
        ] {
            assert_eq!(revision(Some(&text(bad))), Err(REVISION.into()), "{bad:?}");
        }
        // UTF-8 の文字は、制御の文字でなければ通る。
        assert_eq!(revision(Some(&text("refs/é"))), Ok(()));
        // 孤立したサロゲートは、長さの確かめより先に変換で失敗する。
        let mut points = vec![0xdc80];
        points.extend(std::iter::repeat_n(u32::from('a'), 2000));
        let lone = CodepointString::try_from_codepoints(points).unwrap();
        assert_eq!(revision(Some(&lone)), Err(Error::Unicode));
        let refs =
            CodepointString::try_from_codepoints(vec![114, 101, 102, 115, 47, 0xd800]).unwrap();
        assert_eq!(revision(Some(&refs)), Err(Error::Unicode));
    }
}
