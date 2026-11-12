//! Pure loop-state profile parsing and secret-path matching.
//!
//! Text uses Python code points, including unpaired surrogates. No function in
//! this module opens files or consults process-global configuration.
mod casefold;
pub(crate) mod construct;
mod glob;
pub(crate) mod graph;
mod scalar;
#[path = "../s3d_profile/parser_budget.rs"]
pub(crate) mod snapshot_recursion;

use std::collections::HashSet;

/// A sequence of Python Unicode code points (surrogates remain separate).
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct CodepointString(Vec<u32>);
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InvalidCodepoint {
    pub index: usize,
    pub value: u32,
}
impl CodepointString {
    pub fn try_from_codepoints(value: Vec<u32>) -> Result<Self, InvalidCodepoint> {
        if let Some((index, &value)) = value.iter().enumerate().find(|(_, c)| **c > 0x10ffff) {
            return Err(InvalidCodepoint { index, value });
        }
        Ok(Self(value))
    }
    pub fn as_codepoints(&self) -> &[u32] {
        &self.0
    }
}
impl From<&str> for CodepointString {
    fn from(value: &str) -> Self {
        Self(value.chars().map(u32::from).collect())
    }
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PatternItem {
    Text(CodepointString),
    Other,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PatternInput {
    NotList,
    List(Vec<PatternItem>),
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProfileError {
    UnreadableYaml,
    NotStringList,
    InvalidGlob,
}
impl ProfileError {
    pub fn message(self) -> &'static str {
        match self {
            Self::UnreadableYaml => "secret_paths を安全に読めない",
            Self::NotStringList => "secret_paths が文字列配列でない",
            Self::InvalidGlob => "secret_paths に不正な glob がある",
        }
    }
}
impl std::fmt::Display for ProfileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}
impl std::error::Error for ProfileError {}
/// Explicitly supplied CPython integer-conversion configuration; zero disables it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LegacyIntLimit(u32);
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidLegacyIntLimit {
    pub value: i64,
}
impl LegacyIntLimit {
    pub fn from_python_value(value: i64) -> Result<Self, InvalidLegacyIntLimit> {
        if value == 0 || (640..=i64::from(i32::MAX)).contains(&value) {
            Ok(Self(value as u32))
        } else {
            Err(InvalidLegacyIntLimit { value })
        }
    }
}
pub fn default_secret_patterns() -> Vec<CodepointString> {
    [".env", ".env.*", ".dev.vars"]
        .into_iter()
        .map(CodepointString::from)
        .collect()
}
pub fn canonical_secret_patterns(
    input: &PatternInput,
) -> Result<Vec<CodepointString>, ProfileError> {
    let PatternInput::List(items) = input else {
        return Err(ProfileError::NotStringList);
    };
    let mut texts = Vec::with_capacity(items.len());
    for item in items {
        match item {
            PatternItem::Text(text) if !text.0.is_empty() => texts.push(text),
            _ => return Err(ProfileError::NotStringList),
        }
    }
    for text in &texts {
        let c = &text.0;
        if c.iter().all(|&c| c == 47)
            || c.windows(2).any(|v| v == [47, 47])
            || c.iter()
                .any(|&c| c < 32 || c == 127 || [39, 34, 96, 36, 91, 93, 123, 125].contains(&c))
        {
            return Err(ProfileError::InvalidGlob);
        }
    }
    let mut result = default_secret_patterns();
    let mut seen: HashSet<_> = result.iter().cloned().collect();
    for text in texts {
        if seen.insert(text.clone()) {
            result.push(text.clone());
        }
    }
    Ok(result)
}
pub fn profile_patterns(
    raw: &[u8],
    int_limit: LegacyIntLimit,
) -> Result<Vec<CodepointString>, ProfileError> {
    let mut graph = graph::compose(raw, int_limit).map_err(|()| ProfileError::UnreadableYaml)?;
    graph
        .check_duplicates()
        .map_err(|()| ProfileError::UnreadableYaml)?;
    let input =
        construct::patterns(&mut graph, int_limit).map_err(|()| ProfileError::UnreadableYaml)?;
    canonical_secret_patterns(&input)
}
pub fn secret_path(rel: &CodepointString, patterns: &[CodepointString]) -> bool {
    patterns.iter().any(|p| glob::matches(&rel.0, &p.0))
}

#[cfg(test)]
mod tests {
    #[test]
    fn codepoints_preserve_surrogates_and_report_invalid_index() {
        use super::*;
        let points = vec![0, 0xd800, 0xdc00, 0x10ffff];
        assert_eq!(
            CodepointString::try_from_codepoints(points.clone())
                .unwrap()
                .as_codepoints(),
            points
        );
        assert_eq!(
            CodepointString::try_from_codepoints(vec![42, 0x110000]),
            Err(InvalidCodepoint {
                index: 1,
                value: 0x110000
            })
        );
        assert_eq!(CodepointString::from("𐀀").as_codepoints(), [0x10000]);
    }
    #[test]
    fn integer_configuration_is_explicit_and_validated() {
        use super::*;
        for value in [0, 640, 4300, 5000, i64::from(i32::MAX)] {
            assert!(LegacyIntLimit::from_python_value(value).is_ok());
        }
        for value in [i64::MIN, -1, 1, 639, i64::from(i32::MAX) + 1, i64::MAX] {
            assert_eq!(
                LegacyIntLimit::from_python_value(value),
                Err(InvalidLegacyIntLimit { value })
            );
        }
    }
    #[test]
    fn numeric_whitespace_uses_conversion_rules_not_str_isspace() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for tag in ["int", "float"] {
            for cp in 0x1c..=0x1f {
                for value in [format!("\\u{cp:04x}1"), format!("1\\u{cp:04x}")] {
                    let yaml = format!("unused: !!{tag} \"{value}\"\n");
                    assert_eq!(
                        profile_patterns(yaml.as_bytes(), limit),
                        Err(ProfileError::UnreadableYaml),
                        "{yaml}"
                    );
                }
            }
            for cp in [
                9, 10, 11, 12, 13, 32, 0x85, 0xa0, 0x1680, 0x2000, 0x2028, 0x202f, 0x3000,
            ] {
                let yaml = format!("unused: !!{tag} \"\\u{cp:04x}1\\u{cp:04x}\"\n");
                assert_eq!(
                    profile_patterns(yaml.as_bytes(), limit),
                    Ok(default_secret_patterns()),
                    "{yaml}"
                );
            }
        }
    }
    #[test]
    fn nonspecific_tag_resolves_quoted_scalars_before_construction() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for value in [".inf\\n", ".nan\\n", "!", "&", "*"] {
            let yaml = format!("unused: ! \"{value}\"\n");
            assert_eq!(
                profile_patterns(yaml.as_bytes(), limit),
                Err(ProfileError::UnreadableYaml)
            );
        }
        for value in ["true", "null", "012", "1.0", "2001-12-14"] {
            let yaml = format!("secret_paths: [! \"{value}\"]\n");
            assert_eq!(
                profile_patterns(yaml.as_bytes(), limit),
                Err(ProfileError::NotStringList)
            );
        }
        assert_eq!(
            profile_patterns(b"unused: ! \"\\n\"\n", limit),
            Ok(default_secret_patterns())
        );
    }
    #[test]
    fn timestamp_resolution_allows_exactly_one_final_lf() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for value in [
            "2001-12-14",
            "2001-12-14T21:59:43Z",
            "2001-12-14T21:59:43+23:59",
        ] {
            for newlines in 0..=3 {
                let quoted = format!("\"{value}{}\"", "\\n".repeat(newlines));
                let source = format!("! {quoted}\n");
                let nodes = graph::compose(source.as_bytes(), limit).unwrap().nodes;
                let tag = if newlines <= 1 { "timestamp" } else { "str" };
                assert_eq!(
                    nodes[0].tag,
                    format!("{}{}", graph::PREFIX, tag),
                    "{source}"
                );
                let graph::Data::Scalar(points) = &nodes[0].data else {
                    panic!("scalar expected")
                };
                let expected =
                    CodepointString::from(format!("{value}{}", "\n".repeat(newlines)).as_str());
                assert_eq!(points, expected.as_codepoints());
                let yaml = format!("unused: ! {quoted}\n");
                assert_eq!(
                    profile_patterns(yaml.as_bytes(), limit),
                    Ok(default_secret_patterns()),
                    "{yaml}"
                );
                let yaml = format!("unused: !!timestamp {quoted}\n");
                let expected = if newlines <= 1 {
                    Ok(default_secret_patterns())
                } else {
                    Err(ProfileError::UnreadableYaml)
                };
                assert_eq!(profile_patterns(yaml.as_bytes(), limit), expected, "{yaml}");
                let yaml = format!("secret_paths: [! {quoted}]\n");
                let error = if newlines <= 1 {
                    ProfileError::NotStringList
                } else {
                    ProfileError::InvalidGlob
                };
                assert_eq!(
                    profile_patterns(yaml.as_bytes(), limit),
                    Err(error),
                    "{yaml}"
                );
            }
        }
    }
    #[test]
    fn directives_stop_at_eof_and_keep_every_handle() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for source in [
            "%FOO bar",
            "%FOO",
            "%FOO bar#comment",
            "%FOO bar\u{feff}baz",
        ] {
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Err(ProfileError::UnreadableYaml)
            );
        }
        for middle in ["%TAG !b! tag:yaml.org,2002:", "%YAML 1.1", "%FOO ignored"] {
            let source =
                format!("%TAG !a! tag:yaml.org,2002:\n{middle}\n---\nsecret_paths: [!a!str safe]");
            let mut expected = default_secret_patterns();
            expected.push("safe".into());
            assert_eq!(profile_patterns(source.as_bytes(), limit), Ok(expected));
        }
        let source =
            b"%TAG !a! tag:yaml.org,2002:\n%TAG !a! tag:yaml.org,2002:\n---\nsecret_paths: [safe]";
        assert_eq!(
            profile_patterns(source, limit),
            Err(ProfileError::UnreadableYaml)
        );
    }
    #[test]
    fn structural_tabs_are_distinct_from_quoted_and_comment_tabs() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for source in [
            "secret_paths:\t[safe]",
            "secret_paths: [safe,\tother]",
            "secret_paths: [\t vault]",
            "secret_paths: [a\tb]",
        ] {
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Err(ProfileError::UnreadableYaml)
            );
        }
        for source in [
            "unused: \"a\tb\"",
            "unused: \"a\n\tb\"",
            "unused: |\n  a\tb",
            "unused: x #comment\tlegal",
        ] {
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Ok(default_secret_patterns())
            );
        }
    }
    #[test]
    fn old_simple_key_limit_applies_in_flow_but_not_to_values() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for n in [1023, 1024, 1025] {
            let source = format!("{{{}: x}}", "a".repeat(n));
            let expected = if n <= 1024 {
                Ok(default_secret_patterns())
            } else {
                Err(ProfileError::UnreadableYaml)
            };
            assert_eq!(profile_patterns(source.as_bytes(), limit), expected);
        }
        for br in ['\n', '\u{85}', '\u{2028}', '\u{2029}'] {
            let source = format!("{{a{br}b: x}}");
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Err(ProfileError::UnreadableYaml)
            );
        }
        let source = format!("unused: [{}]", "a".repeat(1025));
        assert_eq!(
            profile_patterns(source.as_bytes(), limit),
            Ok(default_secret_patterns())
        );
    }
    #[test]
    fn block_scalar_only_preserves_real_content_breaks() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for style in ["|", ">", "|+", ">+"] {
            let source = format!("secret_paths:\n  - {style}\n    safe");
            let mut expected = default_secret_patterns();
            expected.push("safe".into());
            assert_eq!(profile_patterns(source.as_bytes(), limit), Ok(expected));
            assert_eq!(
                profile_patterns(format!("{source}\n").as_bytes(), limit),
                Err(ProfileError::InvalidGlob)
            );
            assert_eq!(
                profile_patterns(format!("secret_paths:\n  - {style}\n").as_bytes(), limit),
                Err(ProfileError::NotStringList)
            );
        }
        for style in ["|", ">"] {
            assert_eq!(
                profile_patterns(format!("unused: {style}#bad\n  safe").as_bytes(), limit),
                Err(ProfileError::UnreadableYaml)
            );
        }
    }
    #[test]
    fn flow_grammar_retains_explicit_keys_and_adjacent_comments() {
        use super::*;
        let limit = LegacyIntLimit::from_python_value(4300).unwrap();
        for source in [
            "unused: [?]",
            "unused: [?: x]",
            "{?: x}",
            "{?a: x}",
            "unused: [\nx\n]",
            "unused: [-, safe]",
            "unused: [safe]#comment",
            "unused: [\"safe\"#comment\n]",
            "unused: \"a\"#comment",
        ] {
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Ok(default_secret_patterns()),
                "{source}"
            );
        }
        for source in [
            ": x",
            "{: x}",
            "unused: [? , :]",
            "unused: [!!str]",
            "unused: [!!str, safe]",
        ] {
            assert_eq!(
                profile_patterns(source.as_bytes(), limit),
                Err(ProfileError::UnreadableYaml),
                "{source}"
            );
        }
    }
    #[test]
    fn directive_decimal_limit_is_explicit_including_leading_zeroes() {
        use super::*;
        for configured in [0, 640, 4300] {
            let limit = LegacyIntLimit::from_python_value(configured).unwrap();
            for count in [639, 640, 641, 4300, 4301] {
                for version in [
                    format!("1.{}", "0".repeat(count)),
                    format!("{}1.1", "0".repeat(count - 1)),
                ] {
                    let source = format!("%YAML {version}\n---\nunused: x");
                    let expected = if configured == 0 || count <= configured as usize {
                        Ok(default_secret_patterns())
                    } else {
                        Err(ProfileError::UnreadableYaml)
                    };
                    assert_eq!(profile_patterns(source.as_bytes(), limit), expected);
                }
            }
        }
    }
    #[test]
    fn finite_deep_nodes_do_not_use_the_process_stack() {
        use super::*;
        let input = format!("unused: {}0{}\n", "[".repeat(3000), "]".repeat(3000));
        assert_eq!(
            profile_patterns(
                input.as_bytes(),
                LegacyIntLimit::from_python_value(4300).unwrap()
            ),
            Ok(default_secret_patterns())
        );
    }
}
