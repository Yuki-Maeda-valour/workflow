use super::scalar;
use std::{cell::RefCell, collections::HashMap, rc::Rc};
use workflow_yaml_parser::{Event, Parser, ScalarStyle};
pub(crate) const PREFIX: &str = "tag:yaml.org,2002:";
pub(crate) type Pairs = Rc<RefCell<Vec<(usize, usize)>>>;
#[derive(Clone, Debug)]
pub(crate) enum Data {
    Scalar(Vec<u32>),
    Sequence(Vec<usize>),
    Mapping(Pairs),
}
#[derive(Clone, Debug)]
pub(crate) struct Node {
    pub tag: String,
    pub data: Data,
}
#[derive(Debug)]
pub(crate) struct Graph {
    pub nodes: Vec<Node>,
}
impl Graph {
    pub(crate) fn check_duplicates(&self) -> Result<(), ()> {
        for node in &self.nodes {
            if let Data::Mapping(pairs) = &node.data {
                let mut found = false;
                for &(key, _) in pairs.borrow().iter() {
                    let key = &self.nodes[key];
                    if key.tag == format!("{PREFIX}str")
                        && matches!(&key.data,Data::Scalar(v) if scalar::equals(v,"secret_paths"))
                    {
                        if found {
                            return Err(());
                        }
                        found = true;
                    }
                }
            }
        }
        Ok(())
    }
}
/// Compose one document without recursion. This function is also observed by the
/// test-only graph bridge; the public profile path uses these exact nodes.
pub(crate) fn compose(raw: &[u8], limit: super::LegacyIntLimit) -> Result<Graph, ()> {
    let text = std::str::from_utf8(raw).map_err(|_| ())?;
    if !text.chars().all(|c| matches!(u32::from(c),9|10|13|0x20..=0x7e|0x85|0xa0..=0xd7ff|0xe000..=0xfffd|0x10000..=0x10ffff)) { return Err(()); }
    let mut graph = Graph { nodes: Vec::new() };
    let mut stack: Vec<(usize, Option<usize>)> = Vec::new();
    let mut anchors = HashMap::new();
    let mut documents = 0;
    for event in Parser::new_from_iter(text.strip_prefix('\u{feff}').unwrap_or(text).chars())
        .with_legacy_integer_limit(limit.0)
    {
        let (event, _) = event.map_err(|_| ())?;
        let (node, anchor, opens) = match event {
            Event::DocumentStart(_) => {
                documents += 1;
                if documents > 1 {
                    return Err(());
                }
                continue;
            }
            Event::SequenceEnd | Event::MappingEnd => {
                if stack.pop().is_none() {
                    return Err(());
                }
                continue;
            }
            Event::Alias(anchor) => (Some(*anchors.get(&anchor).ok_or(())?), 0, false),
            Event::Scalar(value, style, anchor, tag) => {
                let tag = tag.map(|t| format!("{}{}", t.handle, t.suffix));
                let tag = match tag.as_deref() {
                    None | Some("!") => format!(
                        "{PREFIX}{}",
                        if style == ScalarStyle::Plain || tag.as_deref() == Some("!") {
                            scalar::resolve(&value)
                        } else {
                            "str"
                        }
                    ),
                    Some(s) => s.to_owned(),
                };
                graph.nodes.push(Node {
                    tag,
                    data: Data::Scalar(value),
                });
                (None, anchor, false)
            }
            Event::SequenceStart(anchor, tag) => {
                let tag = tag.map(|t| format!("{}{}", t.handle, t.suffix));
                graph.nodes.push(Node {
                    tag: tag
                        .filter(|s| s != "!")
                        .unwrap_or_else(|| format!("{PREFIX}seq")),
                    data: Data::Sequence(Vec::new()),
                });
                (None, anchor, true)
            }
            Event::MappingStart(anchor, tag) => {
                let tag = tag.map(|t| format!("{}{}", t.handle, t.suffix));
                graph.nodes.push(Node {
                    tag: tag
                        .filter(|s| s != "!")
                        .unwrap_or_else(|| format!("{PREFIX}map")),
                    data: Data::Mapping(Rc::new(RefCell::new(Vec::new()))),
                });
                (None, anchor, true)
            }
            _ => continue,
        };
        let id = node.unwrap_or(graph.nodes.len() - 1);
        if anchor != 0 {
            anchors.insert(anchor, id);
        }
        if let Some((parent, pending)) = stack.last_mut() {
            match &mut graph.nodes[*parent].data {
                Data::Sequence(children) => children.push(id),
                Data::Mapping(pairs) => {
                    if let Some(key) = pending.take() {
                        pairs.borrow_mut().push((key, id));
                    } else {
                        *pending = Some(id);
                    }
                }
                Data::Scalar(_) => return Err(()),
            }
        }
        if opens {
            stack.push((id, None));
        }
    }
    if !stack.is_empty() {
        return Err(());
    }
    Ok(graph)
}
