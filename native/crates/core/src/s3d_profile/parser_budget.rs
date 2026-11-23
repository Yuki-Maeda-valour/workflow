//! Standalone helper recursion accounting, independent of the Rust call stack.
//!
//! CPython 3.10.11 and PyYAML 6.0.3 reference measurements accompany the tests.
//! Composition, duplicate traversal, merge expansion, and scalar traversal have
//! separate counters. Alias references do not recompose their target nodes.
use workflow_yaml_parser::{Event, Marker};
#[path = "legacy_parser.rs"]
mod legacy_parser;
#[path = "legacy_scanner.rs"]
mod legacy_scanner;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RecursionPolicy {
    pub limit: usize,
}
impl RecursionPolicy {
    pub fn standalone_python_3_10() -> Self {
        Self { limit: 1000 }
    }
    pub(crate) fn scalar(self, frames: usize, traversed: usize) -> Result<(), ()> {
        (frames.saturating_add(traversed) <= self.limit)
            .then_some(())
            .ok_or(())
    }
    pub(crate) fn merge(self, traversed: usize) -> Result<(), ()> {
        (11usize.saturating_add(traversed) <= self.limit)
            .then_some(())
            .ok_or(())
    }
}
#[derive(Clone, Copy)]
struct Frame {
    mapping: bool,
    next_value: bool,
}
/// Replays parser token requests rather than assigning a depth to a YAML shape.
/// Scanner lookahead runs at the frame that requested a token, not at the frame
/// where a previously scanned scalar or collection is eventually composed.
pub(crate) struct Composer {
    parser: self::legacy_parser::Parser,
    frames: Vec<Frame>,
    policy: RecursionPolicy,
}
impl Composer {
    pub(crate) fn new(raw: &[u8], policy: RecursionPolicy) -> Self {
        Self {
            parser: self::legacy_parser::Parser::new(
                std::str::from_utf8(raw).expect("checked UTF-8"),
            ),
            frames: Vec::new(),
            policy,
        }
    }
    pub(crate) fn observe(&mut self, event: &Event<'_>, _marker: Marker) -> Result<(), ()> {
        use self::legacy_parser::Kind;
        let expected = match event {
            Event::StreamStart => Kind::StreamStart,
            Event::StreamEnd => Kind::StreamEnd,
            Event::DocumentStart(_) => Kind::DocStart,
            Event::DocumentEnd => Kind::DocEnd,
            Event::Scalar(..) => Kind::Scalar,
            Event::Alias(..) => Kind::Alias,
            Event::SequenceStart(..) => Kind::Seq,
            Event::MappingStart(..) => Kind::Map,
            Event::SequenceEnd => Kind::EndSeq,
            Event::MappingEnd => Kind::EndMap,
            Event::Nothing => return Ok(()),
        };
        let depth = self.frames.len();
        let ends = matches!(expected, Kind::EndSeq | Kind::EndMap);
        let mapping_value = !ends
            && self
                .frames
                .last()
                .is_some_and(|f| f.mapping && f.next_value);
        let call_depth = if depth == 0 {
            10
        } else {
            9 + 2 * depth + usize::from(mapping_value)
        };
        let actual = self.parser.event(call_depth)?;
        if actual != expected || self.parser.peak() > self.policy.limit {
            return Err(());
        }
        if ends {
            self.frames.pop();
        } else if matches!(expected, Kind::Scalar | Kind::Alias | Kind::Seq | Kind::Map) {
            if let Some(parent) = self.frames.last_mut()
                && parent.mapping
            {
                parent.next_value = !parent.next_value;
            }
            if matches!(expected, Kind::Seq | Kind::Map) {
                self.frames.push(Frame {
                    mapping: expected == Kind::Map,
                    next_value: false,
                });
            }
        }
        Ok(())
    }
}
