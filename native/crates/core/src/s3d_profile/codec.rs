//! Strict UTF-8 for the snapshot helper's NUL union (`item.encode("utf-8")`).
//!
//! Text-codec settings of the old helper are fixed to UTF-8 (2026-11-11 22:24),
//! so no codec table is kept. Unpaired surrogates still fail as in the helper.
use crate::loop_state::CodepointString;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Utf8EncodeError {
    pub start: usize,
    pub end: usize,
    pub points: Vec<u32>,
}
impl Utf8EncodeError {
    pub fn message(&self) -> String {
        if self.end == self.start + 1 {
            format!(
                "'utf-8' codec can't encode character '\\u{:04x}' in position {}: surrogates not allowed",
                self.points[0], self.start
            )
        } else {
            format!(
                "'utf-8' codec can't encode characters in position {}-{}: surrogates not allowed",
                self.start,
                self.end - 1
            )
        }
    }
}
/// UTF-8 for sys.stdout.buffer's NUL union. Surrogates fail rather than change.
pub fn strict_utf8(text: &CodepointString) -> Result<Vec<u8>, Utf8EncodeError> {
    let points = text.as_codepoints();
    if let Some(start) = points.iter().position(|cp| (0xd800..=0xdfff).contains(cp)) {
        let end = start
            + points[start..]
                .iter()
                .take_while(|cp| (0xd800..=0xdfff).contains(*cp))
                .count();
        return Err(Utf8EncodeError {
            start,
            end,
            points: points[start..end].to_vec(),
        });
    }
    let mut result = Vec::new();
    for &cp in points {
        let mut buffer = [0; 4];
        result.extend_from_slice(
            char::from_u32(cp)
                .expect("validated code point")
                .encode_utf8(&mut buffer)
                .as_bytes(),
        );
    }
    Ok(result)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn surrogates_fail_with_the_python_position() {
        let one = CodepointString::try_from_codepoints(vec![0x61, 0xd800, 0x62]).unwrap();
        let error = strict_utf8(&one).unwrap_err();
        assert_eq!((error.start, error.end), (1, 2));
        assert_eq!(
            error.message(),
            "'utf-8' codec can't encode character '\\ud800' in position 1: surrogates not allowed"
        );
        let two = CodepointString::try_from_codepoints(vec![0xd800, 0xdfff]).unwrap();
        assert_eq!(
            strict_utf8(&two).unwrap_err().message(),
            "'utf-8' codec can't encode characters in position 0-1: surrogates not allowed"
        );
        assert_eq!(
            strict_utf8(&CodepointString::from("é")).unwrap(),
            "é".as_bytes()
        );
    }
}
