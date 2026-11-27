//! The snapshot helper's purpose-specific YAML contract.
//!
//! Parsing is pure. Filesystem inspection, source names, default patterns, and
//! ordered union construction remain with the caller. Python code points are
//! retained until the caller explicitly asks for strict UTF-8 output.
pub mod codec;
pub mod recursion;
#[cfg(test)]
mod recursion_tests;

use crate::loop_state::{CodepointString, LegacyIntLimit, PatternInput, PatternItem};

/// CPython inputs used by the old standalone helper, supplied rather than read
/// from process-global state inside the parser.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SnapshotYamlPolicy {
    pub integer_limit: LegacyIntLimit,
    pub recursion: recursion::RecursionPolicy,
}
impl SnapshotYamlPolicy {
    pub fn python_3_10(integer_limit: LegacyIntLimit) -> Self {
        Self {
            integer_limit,
            recursion: recursion::RecursionPolicy::standalone_python_3_10(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SnapshotProfileError {
    InvalidUtf8,
    UnreadableYaml,
    NotMapping,
    NullPatterns,
    NotStringList,
    InvalidGlob,
}
impl SnapshotProfileError {
    /// The caller adds the source name (current profile or immutable reference).
    pub fn message(self) -> &'static str {
        match self {
            Self::InvalidUtf8 => "の profile は UTF-8 でなければならない",
            Self::UnreadableYaml => "の profile YAML を読めない",
            Self::NotMapping => "の profile は map でなければならない",
            Self::NullPatterns => "の secret_paths は配列でなければならない",
            Self::NotStringList => "の secret_paths は文字列の配列でなければならない",
            Self::InvalidGlob => "の secret_paths に不正な glob がある",
        }
    }
}
impl std::fmt::Display for SnapshotProfileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}
impl std::error::Error for SnapshotProfileError {}

/// Parse the value contributed by one profile. Empty strings are invalid globs,
/// not non-string items; the whole item-type check precedes every glob check.
/// No defaults or deduplication are performed by this function.
pub fn snapshot_patterns(
    raw: &[u8],
    policy: SnapshotYamlPolicy,
) -> Result<Vec<CodepointString>, SnapshotProfileError> {
    std::str::from_utf8(raw).map_err(|_| SnapshotProfileError::InvalidUtf8)?;
    let mut graph =
        crate::loop_state::graph::compose_snapshot(raw, policy.integer_limit, policy.recursion)
            .map_err(|()| SnapshotProfileError::UnreadableYaml)?;
    recursion::check_duplicate_walk(&graph, policy.recursion)
        .map_err(|()| SnapshotProfileError::UnreadableYaml)?;
    graph
        .check_duplicates()
        .map_err(|()| SnapshotProfileError::UnreadableYaml)?;
    use crate::loop_state::construct::SnapshotInput;
    let input = crate::loop_state::construct::snapshot_input(
        &mut graph,
        policy.integer_limit,
        policy.recursion,
    )
    .map_err(|()| SnapshotProfileError::UnreadableYaml)?;
    match input {
        SnapshotInput::Empty | SnapshotInput::Null | SnapshotInput::Missing => Ok(Vec::new()),
        SnapshotInput::NotMapping => Err(SnapshotProfileError::NotMapping),
        SnapshotInput::NullPatterns => Err(SnapshotProfileError::NullPatterns),
        SnapshotInput::Patterns(input) => validate_patterns(input),
    }
}

fn validate_patterns(input: PatternInput) -> Result<Vec<CodepointString>, SnapshotProfileError> {
    let PatternInput::List(items) = input else {
        return Err(SnapshotProfileError::NotStringList);
    };
    let values: Vec<CodepointString> = items
        .into_iter()
        .map(|item| match item {
            PatternItem::Text(value) => Ok(value),
            PatternItem::Other => Err(SnapshotProfileError::NotStringList),
        })
        .collect::<Result<_, _>>()?;
    for value in &values {
        validate_glob(value)?;
    }
    Ok(values)
}

pub fn validate_glob(value: &CodepointString) -> Result<(), SnapshotProfileError> {
    let points = value.as_codepoints();
    if points.is_empty()
        || points.iter().all(|&cp| cp == u32::from('/'))
        || points.windows(2).any(|pair| pair == [47, 47])
        || points
            .iter()
            .any(|&cp| cp < 32 || cp == 127 || [39, 34, 96, 36, 91, 93, 123, 125].contains(&cp))
    {
        return Err(SnapshotProfileError::InvalidGlob);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse(raw: &[u8]) -> Result<Vec<CodepointString>, SnapshotProfileError> {
        snapshot_patterns(
            raw,
            SnapshotYamlPolicy::python_3_10(LegacyIntLimit::from_python_value(4300).unwrap()),
        )
    }
    #[test]
    fn snapshot_types_do_not_inherit_loop_state_defaults_or_rejections() {
        for yaml in ["", "null", "~", "{}", "unused: true", "secret_paths: []"] {
            assert_eq!(parse(yaml.as_bytes()), Ok(Vec::new()), "{yaml}");
        }
        for yaml in ["[]", "true", "7", "safe", "!!set {a: null}", "!!omap []"] {
            assert_eq!(
                parse(yaml.as_bytes()),
                Err(SnapshotProfileError::NotMapping),
                "{yaml}"
            );
        }
        for yaml in ["secret_paths:", "secret_paths: null", "secret_paths: ~"] {
            assert_eq!(
                parse(yaml.as_bytes()),
                Err(SnapshotProfileError::NullPatterns),
                "{yaml}"
            );
        }
        for yaml in [
            "secret_paths: safe",
            "secret_paths: [1]",
            "secret_paths: ['', 1]",
        ] {
            assert_eq!(
                parse(yaml.as_bytes()),
                Err(SnapshotProfileError::NotStringList),
                "{yaml}"
            );
        }
        for yaml in [
            "secret_paths: ['']",
            "secret_paths: ['/']",
            "secret_paths: [a//b]",
        ] {
            assert_eq!(
                parse(yaml.as_bytes()),
                Err(SnapshotProfileError::InvalidGlob),
                "{yaml}"
            );
        }
    }
    #[test]
    fn duplicates_merge_unknown_tags_and_unused_construction_keep_old_order() {
        for yaml in [
            "secret_paths: []\nsecret_paths: []",
            "unused: {secret_paths: [], secret_paths: []}",
            "unused: !unknown safe",
            "unused: !!int nope",
            "secret_paths: ['']\nunused: !!int nope",
        ] {
            assert_eq!(
                parse(yaml.as_bytes()),
                Err(SnapshotProfileError::UnreadableYaml),
                "{yaml}"
            );
        }
        assert_eq!(
            parse(b"common: &c {secret_paths: [first]}\n<<: *c\nsecret_paths: [second, second]"),
            Ok(vec!["second".into(), "second".into()])
        );
        assert_eq!(parse(&[0xff]), Err(SnapshotProfileError::InvalidUtf8));
    }
    #[test]
    fn yaml_surrogates_survive_until_explicit_utf8_encoding() {
        let value = parse(b"secret_paths: [\"\\ud800\"]").unwrap();
        assert_eq!(value[0].as_codepoints(), [0xd800]);
        assert_eq!(codec::strict_utf8(&value[0]).unwrap_err().start, 0);
    }
    #[test]
    fn block_header_comments_do_not_supply_indentation() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("block_scalar_cases.json")).unwrap();
        let rows = fixture["cases"].as_array().unwrap();
        assert_eq!(rows.len(), 126);
        for row in rows {
            let actual = parse(row["raw"].as_str().unwrap().as_bytes());
            if row["expected"].is_null() {
                assert_eq!(actual, Ok(vec!["safe".into()]), "{}", row["id"]);
            } else {
                assert_eq!(
                    actual,
                    Err(SnapshotProfileError::UnreadableYaml),
                    "{}",
                    row["id"]
                );
            }
        }
    }
}
