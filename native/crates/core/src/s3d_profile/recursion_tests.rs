//! Fixed standalone-helper observations; no reference Python runs in this test.
use super::{SnapshotProfileError, SnapshotYamlPolicy, snapshot_patterns};
use crate::loop_state::LegacyIntLimit;
use serde_json::Value;
use sha2::{Digest, Sha256};

const WRAPPERS: [(&str, &str); 8] = [
    ("[", "]"),
    ("{a: ", "}"),
    ("{\"a\": ", "}"),
    ("!!seq [", "]"),
    ("!!map {a: ", "}"),
    ("[0,", "]"),
    ("[#comment\n", "]"),
    ("[? a : ", "]"),
];
fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}
fn number(value: &Value) -> usize {
    value.as_u64().unwrap() as usize
}
fn indented(n: usize, entry: &str, leaf: &str) -> String {
    let mut out = String::new();
    for i in 0..n {
        out.push_str(&"  ".repeat(i));
        out.push_str(entry);
    }
    out.push_str(&"  ".repeat(n));
    out.push_str(leaf);
    out
}
fn recipe(row: &Value) -> String {
    let a = row.as_array().unwrap();
    match text(&a[0]) {
        "mix" => {
            let (left, right, n) = (number(&a[1]), number(&a[2]), number(&a[3]));
            let mut out = String::new();
            for k in 0..n {
                out.push_str(WRAPPERS[if k % 2 == 0 { left } else { right }].0);
            }
            out.push_str(["safe", "\"日\"", "[]", "{}"][right % 4]);
            for k in (0..n).rev() {
                out.push_str(WRAPPERS[if k % 2 == 0 { left } else { right }].1);
            }
            out
        }
        "scalar" => {
            let context = text(&a[1]);
            let n = number(&a[2]);
            let lines: Vec<_> = (0..n)
                .map(|i| {
                    let definition = format!(
                        "&a{i} !!str {}",
                        if i == 0 {
                            "safe".to_owned()
                        } else {
                            format!("{{=: *a{}}}", i - 1)
                        }
                    );
                    match context {
                        "seq" => format!("- {definition}"),
                        "map" => format!("a{i}: {definition}"),
                        "pairs" => format!("- a{i}: {definition}"),
                        _ => panic!("unknown context"),
                    }
                })
                .collect();
            format!(
                "{}{}",
                if context == "pairs" { "!!pairs\n" } else { "" },
                lines.join("\n")
            )
        }
        "lookahead" => format!(
            "{}{}\"safe\"{}",
            "[".repeat(number(&a[2])),
            " ".repeat(number(&a[1])),
            "]".repeat(number(&a[2]))
        ),
        "block" => {
            let n = number(&a[2]);
            let leaf = match text(&a[3]) {
                "plain" => "safe\n",
                "quoted" => "\"safe\"\n",
                "literal" => "|\n  safe\n",
                "folded" => ">\n  safe\n",
                "anchor" => "&end safe\n",
                _ => panic!("unknown leaf"),
            };
            indented(
                n,
                if text(&a[1]) == "seq" { "-\n" } else { "a:\n" },
                &leaf.replace("\n  ", &format!("\n{}", "  ".repeat(n + 1))),
            )
        }
        "value" => {
            let family = text(&a[1]);
            let n = number(&a[2]);
            match family {
                "seq" => format!("{}{}", "[".repeat(n), "]".repeat(n)),
                "map" => format!("{}null{}", "{a: ".repeat(n), "}".repeat(n)),
                "unused_seq" => format!("unused: {}{}", "[".repeat(n), "]".repeat(n)),
                "unused_map" => format!("unused: {}null{}", "{a: ".repeat(n), "}".repeat(n)),
                "scalar" => format!("!!str {}safe{}", "{=: !!str ".repeat(n), "}".repeat(n)),
                "alias" | "merge" | "scalar_alias" => {
                    let mut out = match family {
                        "alias" => "- &a0 []\n",
                        "merge" => "- &a0 {}\n",
                        _ => "- &a0 !!str safe\n",
                    }
                    .to_owned();
                    for i in 1..n {
                        out.push_str(&match family {
                            "alias" => format!("- &a{i} [*a{}]\n", i - 1),
                            "merge" => format!("- &a{i} {{<<: *a{}}}\n", i - 1),
                            _ => format!("- &a{i} !!str {{=: *a{}}}\n", i - 1),
                        });
                    }
                    out
                }
                _ => panic!("unknown family"),
            }
        }
        "data" => {
            let family = text(&a[1]);
            let n = number(&a[2]);
            if family.starts_with("block_seq") {
                return indented(
                    n,
                    "-\n",
                    if family.ends_with("scalar") {
                        "safe\n"
                    } else {
                        "[]\n"
                    },
                );
            }
            if family == "block_map" {
                return indented(n, "a:\n", "null\n");
            }
            let long = "a".repeat(1020);
            if let Some(kind) = family.strip_prefix("sequence_") {
                let leaf = match kind {
                    "empty" => "",
                    "plain" => "safe",
                    "double" => "\"safe\"",
                    "single" => "'safe'",
                    "tag" => "!!str safe",
                    "alias" => "*a",
                    "comment" => "#comment\n",
                    "null" => "null",
                    "number" => "12",
                    "long" => &long,
                    _ => panic!("unknown sequence"),
                };
                return format!(
                    "{}{}{leaf}{}",
                    if kind == "alias" {
                        "anchor: &a []\nunused: "
                    } else {
                        ""
                    },
                    "[".repeat(n),
                    "]".repeat(n)
                );
            }
            let leaf = match family.strip_prefix("mapping_").unwrap() {
                "plain" => "safe",
                "double" => "\"safe\"",
                "single" => "'safe'",
                "tag" => "!!str safe",
                "empty" => "{}",
                "null" => "null",
                _ => panic!("unknown mapping"),
            };
            format!("{}{leaf}{}", "{a: ".repeat(n), "}".repeat(n))
        }
        _ => panic!("unknown recipe"),
    }
}
#[test]
fn standalone_helper_recursion_boundaries_include_scanner_lookahead_and_constructor_context() {
    let fixture: Value = serde_json::from_str(include_str!("recursion_cases.json")).unwrap();
    assert_eq!(fixture["python"], "3.10.11");
    assert_eq!(fixture["yaml"], "6.0.3");
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 421);
    for row in cases {
        let id = text(&row["id"]);
        let input = recipe(&row["recipe"]);
        assert_eq!(
            format!("{:x}", Sha256::digest(input.as_bytes())),
            text(&row["sha256"]),
            "{id}: recipe changed"
        );
        let result = snapshot_patterns(
            input.as_bytes(),
            SnapshotYamlPolicy::python_3_10(LegacyIntLimit::from_python_value(4300).unwrap()),
        );
        let expected = match row["expected"].as_str() {
            Some("UnreadableYaml") => Err(SnapshotProfileError::UnreadableYaml),
            Some("NotMapping") => Err(SnapshotProfileError::NotMapping),
            None => Ok(Vec::new()),
            other => panic!("unexpected reference result {other:?}"),
        };
        assert_eq!(result, expected, "{id}");
    }
}
