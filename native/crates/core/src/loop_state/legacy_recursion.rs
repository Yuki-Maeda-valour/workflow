// Adapted from PyYAML 6.0.3. Copyright and MIT permission: PYYAML-LICENSE.
//! S2Lだけが使うCPython/PyYAMLの仮想呼出し文脈。
//! Rustの呼出し階層ではなく、旧版が実行した関数と一時呼出しを数える。
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyRecursionModel {
    Cpython310PyYaml603,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LegacyRecursionContext {
    pub model: LegacyRecursionModel,
    pub recursion_limit: usize,
    pub entry_depth: usize,
}
impl LegacyRecursionContext {
    pub fn cpython310_pyyaml603() -> Self {
        Self {
            model: LegacyRecursionModel::Cpython310PyYaml603,
            recursion_limit: 1000,
            entry_depth: 4,
        }
    }
}
#[derive(Clone, Copy)]
pub(super) struct Calls {
    limit: usize,
}
impl Calls {
    pub(super) fn new(context: LegacyRecursionContext) -> Self {
        Self {
            limit: context.recursion_limit,
        }
    }
    pub(super) fn enter(self, depth: usize) -> Result<(), ()> {
        if depth > self.limit { Err(()) } else { Ok(()) }
    }
}
pub(super) fn compose(text: &str, context: LegacyRecursionContext, second: bool) -> Result<(), ()> {
    use super::legacy_parser::{Event, Parser};
    struct Composer {
        parser: Parser,
        calls: Calls,
    }
    impl Composer {
        fn node(&mut self, d: usize) -> Result<(), ()> {
            self.calls.enter(d)?;
            if self.parser.event(d + 1, false, true)? == Event::Alias {
                self.parser.event(d + 1, true, false)?;
                return Ok(());
            }
            self.parser.event(d + 1, false, false)?;
            self.calls.enter(d + 1)?;
            if self.parser.event(d + 1, false, true)? == Event::Scalar {
                self.scalar(d + 1)?;
            } else if self.parser.event(d + 1, false, true)? == Event::SequenceStart {
                self.collection(d + 1, false)?;
            } else if self.parser.event(d + 1, false, true)? == Event::MappingStart {
                self.collection(d + 1, true)?;
            } else {
                return Err(());
            }
            self.calls.enter(d + 1)
        }
        fn scalar(&mut self, d: usize) -> Result<(), ()> {
            self.calls.enter(d)?;
            self.parser.event(d + 1, true, false)?;
            self.calls.enter(d + 2)
        }
        fn collection(&mut self, d: usize, mapping: bool) -> Result<(), ()> {
            self.calls.enter(d)?;
            self.parser.event(d + 1, true, false)?;
            self.calls.enter(d + 2)?;
            let end = if mapping {
                Event::MappingEnd
            } else {
                Event::SequenceEnd
            };
            while self.parser.event(d + 1, false, true)? != end {
                self.node(d + 1)?;
                if mapping {
                    self.node(d + 1)?;
                }
                self.calls.enter(d + 1)?;
            }
            self.parser.event(d + 1, true, false)?;
            Ok(())
        }
        fn document(&mut self, d: usize) -> Result<(), ()> {
            self.calls.enter(d)?;
            self.parser.event(d + 1, true, false)?;
            self.node(d + 1)?;
            self.parser.event(d + 1, true, false)?;
            Ok(())
        }
        fn single(&mut self, d: usize) -> Result<(), ()> {
            self.calls.enter(d)?;
            self.parser.event(d + 1, true, false)?;
            if self.parser.event(d + 1, false, true)? != Event::StreamEnd {
                self.document(d + 1)?;
            }
            if self.parser.event(d + 1, false, true)? != Event::StreamEnd {
                return Err(());
            }
            self.parser.event(d + 1, true, false)?;
            Ok(())
        }
    }
    let calls = Calls::new(context);
    let mut composer = Composer {
        parser: Parser::new(text, calls),
        calls,
    };
    composer.single(context.entry_depth + if second { 4 } else { 2 })
}
pub(super) fn duplicates(
    graph: &super::graph::Graph,
    context: LegacyRecursionContext,
) -> Result<(), ()> {
    use super::graph::{Data, PREFIX};
    use std::collections::HashSet;
    fn visit(
        graph: &super::graph::Graph,
        id: usize,
        d: usize,
        calls: Calls,
        seen: &mut HashSet<usize>,
    ) -> Result<(), ()> {
        calls.enter(d)?;
        calls.enter(d + 1)?;
        if !seen.insert(id) {
            return Ok(());
        }
        let node = &graph.nodes[id];
        match &node.data {
            Data::Mapping(rows) => {
                let rows = rows.borrow();
                calls.enter(d + 1)?;
                let mut count = 0;
                for &(key, _) in rows.iter() {
                    calls.enter(d + 2)?;
                    calls.enter(d + 3)?;
                    if graph.nodes[key].tag == format!("{PREFIX}str")
                        && matches!(&graph.nodes[key].data,Data::Scalar(value)if super::scalar::equals(value,"secret_paths"))
                    {
                        count += 1;
                    }
                }
                if count > 1 {
                    return Err(());
                }
                for &(k, v) in rows.iter() {
                    visit(graph, k, d + 1, calls, seen)?;
                    visit(graph, v, d + 1, calls, seen)?;
                }
            }
            Data::Sequence(rows) => {
                calls.enter(d + 1)?;
                for &id in rows {
                    visit(graph, id, d + 1, calls, seen)?;
                }
            }
            Data::Scalar(_) => {
                calls.enter(d + 1)?;
            }
        }
        Ok(())
    }
    if graph.nodes.is_empty() {
        return Calls::new(context).enter(context.entry_depth + 2);
    }
    visit(
        graph,
        0,
        context.entry_depth + 1,
        Calls::new(context),
        &mut HashSet::new(),
    )
}
#[cfg(test)]
mod tests {
    #[test]
    fn flow_first_and_following_children_keep_parser_scanner_calls() {
        let limit = super::super::LegacyIntLimit::from_python_value(4300).unwrap();
        for n in [489, 490, 491] {
            for source in [
                format!("unused: {}0{}\n", "[".repeat(n), "]".repeat(n)),
                format!("unused: {}0{}\n", "[\n".repeat(n), "\n]".repeat(n)),
                format!("unused: {}0{}\n", "[0,".repeat(n), "]".repeat(n)),
            ] {
                let result = super::super::profile_patterns_for_loop(
                    source.as_bytes(),
                    limit,
                    super::LegacyRecursionContext::cpython310_pyyaml603(),
                );
                assert_eq!(result.is_ok(), n <= 490, "flow recursion at {n}");
            }
        }
    }
    #[test]
    fn context_depth_is_an_explicit_input() {
        let text = "unused: [[0]]\n";
        let context = super::LegacyRecursionContext::cpython310_pyyaml603();
        assert!(super::compose(text, context, true).is_ok());
        let raised = super::LegacyRecursionContext {
            entry_depth: 990,
            ..context
        };
        assert!(super::compose(text, raised, true).is_err());
        assert!(
            super::compose(
                text,
                super::LegacyRecursionContext {
                    recursion_limit: 2000,
                    ..raised
                },
                true
            )
            .is_ok()
        );
    }
    #[test]
    fn merge_frames_return_before_processing_siblings() {
        let limit = super::super::LegacyIntLimit::from_python_value(4300).unwrap();
        for n in [987, 988, 1100] {
            let mut source = String::from("a0: &a0 {other: ok}\n");
            for i in 1..=n {
                source.push_str(&format!("a{i}: &a{i} {{<<: *a{}}}\n", i - 1));
            }
            source.push_str(&format!("<<: *a{n}\n"));
            assert_eq!(
                super::super::profile_patterns_for_loop(
                    source.as_bytes(),
                    limit,
                    super::LegacyRecursionContext::cpython310_pyyaml603()
                )
                .is_ok(),
                n == 987,
                "merge recursion at {n}"
            );
            assert!(super::super::profile_patterns(source.as_bytes(), limit).is_ok());
        }
    }
}
