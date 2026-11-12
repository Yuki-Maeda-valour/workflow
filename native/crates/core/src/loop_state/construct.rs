//! SafeLoader's lazy collection construction and in-place merge semantics.
//! Values irrelevant to the profile retain only their type/hashability. Every
//! constructor still validates its own payload, including overwritten entries.
use super::{
    CodepointString, LegacyIntLimit, PatternInput, PatternItem,
    graph::{Data, Graph, PREFIX, Pairs},
    scalar,
};
use std::{
    cell::RefCell,
    collections::{HashSet, VecDeque},
    rc::Rc,
};
#[derive(Clone, Debug)]
enum Value {
    Text(CodepointString),
    Atom,
    Null,
    List(Vec<Option<usize>>),
    Map(Option<usize>),
    Set,
}
struct Constructor<'a> {
    graph: &'a mut Graph,
    values: Vec<Option<Value>>,
    pending: VecDeque<usize>,
    limit: LegacyIntLimit,
    recursion: Option<super::snapshot_recursion::RecursionPolicy>,
    scalar_frames: usize,
}
impl Constructor<'_> {
    fn scalar(&self, mut id: usize) -> Result<Vec<u32>, ()> {
        let mut seen = HashSet::new();
        loop {
            if !seen.insert(id) {
                return Err(());
            }
            if let Some(policy) = self.recursion {
                policy.scalar(self.scalar_frames, seen.len())?;
            }
            match &self.graph.nodes[id].data {
                Data::Scalar(v) => return Ok(v.clone()),
                Data::Mapping(pairs) => {
                    let next = pairs
                        .borrow()
                        .iter()
                        .find_map(|&(k, v)| {
                            (self.graph.nodes[k].tag == format!("{PREFIX}value")).then_some(v)
                        })
                        .ok_or(())?;
                    id = next;
                }
                _ => return Err(()),
            }
        }
    }
    fn ensure(&mut self, id: usize) -> Result<(), ()> {
        if self.values[id].is_some() {
            return Ok(());
        }
        let tag = self.graph.nodes[id]
            .tag
            .strip_prefix(PREFIX)
            .ok_or(())?
            .to_owned();
        let value = match tag.as_str() {
            "seq" => {
                if !matches!(self.graph.nodes[id].data, Data::Sequence(_)) {
                    return Err(());
                }
                self.pending.push_back(id);
                Value::List(Vec::new())
            }
            "map" | "set" => {
                self.pending.push_back(id);
                if tag == "map" {
                    Value::Map(None)
                } else {
                    Value::Set
                }
            }
            "omap" | "pairs" => {
                self.pending.push_back(id);
                Value::List(Vec::new())
            }
            "str" => Value::Text(CodepointString(self.scalar(id)?)),
            "null" | "bool" | "int" | "float" | "binary" | "timestamp" => {
                let scalar = self.scalar(id)?;
                if tag == "timestamp" && !matches!(self.graph.nodes[id].data, Data::Scalar(_)) {
                    return Err(());
                }
                if !scalar::validate(&tag, &scalar, self.limit) {
                    return Err(());
                }
                if tag == "null" {
                    Value::Null
                } else {
                    Value::Atom
                }
            }
            _ => return Err(()),
        };
        self.values[id] = Some(value);
        Ok(())
    }
    fn mapping(&self, id: usize) -> Result<Pairs, ()> {
        match &self.graph.nodes[id].data {
            Data::Mapping(p) => Ok(p.clone()),
            _ => Err(()),
        }
    }
    fn flatten(&mut self, id: usize) -> Result<(), ()> {
        enum Wait {
            None,
            One(usize),
            Many {
                ids: Vec<usize>,
                next: usize,
                parts: Vec<Pairs>,
            },
        }
        struct Frame {
            id: usize,
            index: usize,
            merge: Vec<(usize, usize)>,
            wait: Wait,
        }
        impl Frame {
            fn new(id: usize) -> Self {
                Self {
                    id,
                    index: 0,
                    merge: Vec::new(),
                    wait: Wait::None,
                }
            }
        }
        let mut stack = vec![Frame::new(id)];
        while !stack.is_empty() {
            if let Some(policy) = self.recursion {
                policy.merge(stack.len())?;
            }
            let frame = stack.last_mut().unwrap();
            match &mut frame.wait {
                Wait::One(child) => {
                    frame
                        .merge
                        .extend(self.mapping(*child)?.borrow().iter().copied());
                    frame.wait = Wait::None;
                    continue;
                }
                Wait::Many { ids, next, parts } => {
                    parts.push(self.mapping(ids[*next - 1])?);
                    if *next < ids.len() {
                        let child = ids[*next];
                        *next += 1;
                        stack.push(Frame::new(child));
                        continue;
                    }
                    for part in parts.iter().rev() {
                        frame.merge.extend(part.borrow().iter().copied());
                    }
                    frame.wait = Wait::None;
                    continue;
                }
                Wait::None => {}
            }
            let pairs = self.mapping(frame.id)?;
            let pair = pairs.borrow().get(frame.index).copied();
            let Some((key, value)) = pair else {
                if !frame.merge.is_empty() {
                    frame.merge.extend(pairs.borrow().iter().copied());
                    self.graph.nodes[frame.id].data =
                        Data::Mapping(Rc::new(RefCell::new(std::mem::take(&mut frame.merge))));
                }
                stack.pop();
                continue;
            };
            match self.graph.nodes[key].tag.strip_prefix(PREFIX) {
                Some("merge") => {
                    pairs.borrow_mut().remove(frame.index);
                    match self.graph.nodes[value].data.clone() {
                        Data::Mapping(_) => {
                            frame.wait = Wait::One(value);
                            stack.push(Frame::new(value));
                        }
                        Data::Sequence(ids) => {
                            if ids
                                .iter()
                                .any(|&id| !matches!(self.graph.nodes[id].data, Data::Mapping(_)))
                            {
                                return Err(());
                            }
                            if let Some(&child) = ids.first() {
                                frame.wait = Wait::Many {
                                    ids,
                                    next: 1,
                                    parts: Vec::new(),
                                };
                                stack.push(Frame::new(child));
                            }
                        }
                        _ => return Err(()),
                    }
                }
                Some("value") => {
                    self.graph.nodes[key].tag = format!("{PREFIX}str");
                    frame.index += 1;
                }
                _ => frame.index += 1,
            }
        }
        Ok(())
    }
    fn finish(&mut self) -> Result<(), ()> {
        while let Some(id) = self.pending.pop_front() {
            let tag = self.graph.nodes[id]
                .tag
                .strip_prefix(PREFIX)
                .ok_or(())?
                .to_owned();
            self.scalar_frames = match tag.as_str() {
                "seq" => 14,
                "map" | "set" => 14,
                _ => 12,
            };
            let value = match tag.as_str() {
                "seq" => {
                    let Data::Sequence(children) = self.graph.nodes[id].data.clone() else {
                        return Err(());
                    };
                    for &child in &children {
                        self.ensure(child)?;
                    }
                    Value::List(children.into_iter().map(Some).collect())
                }
                "map" | "set" => {
                    self.flatten(id)?;
                    let pairs = self.mapping(id)?.borrow().clone();
                    let mut secret = None;
                    for (key, value) in pairs {
                        self.ensure(key)?;
                        if matches!(
                            self.values[key],
                            Some(Value::List(_) | Value::Map(_) | Value::Set)
                        ) {
                            return Err(());
                        }
                        self.ensure(value)?;
                        if matches!(&self.values[key],Some(Value::Text(s)) if scalar::equals(&s.0,"secret_paths"))
                        {
                            secret = Some(value);
                        }
                    }
                    if tag == "map" {
                        Value::Map(secret)
                    } else {
                        Value::Set
                    }
                }
                "omap" | "pairs" => {
                    let Data::Sequence(children) = self.graph.nodes[id].data.clone() else {
                        return Err(());
                    };
                    let mut items = Vec::new();
                    for child in children {
                        let pairs = self.mapping(child)?;
                        let p = pairs.borrow();
                        if p.len() != 1 {
                            return Err(());
                        }
                        let (key, value) = p[0];
                        self.ensure(key)?;
                        self.ensure(value)?;
                        items.push(None);
                    }
                    Value::List(items)
                }
                _ => return Err(()),
            };
            self.values[id] = Some(value);
        }
        Ok(())
    }
}
pub(super) fn patterns(graph: &mut Graph, limit: LegacyIntLimit) -> Result<PatternInput, ()> {
    if graph.nodes.is_empty() {
        return Ok(PatternInput::List(Vec::new()));
    }
    let mut c = Constructor {
        values: vec![None; graph.nodes.len()],
        graph,
        pending: VecDeque::new(),
        limit,
        recursion: None,
        scalar_frames: 10,
    };
    c.ensure(0)?;
    c.finish()?;
    let Some(Value::Map(Some(id))) = c.values[0] else {
        return Ok(PatternInput::List(Vec::new()));
    };
    let Some(Value::List(items)) = &c.values[id] else {
        return Ok(PatternInput::NotList);
    };
    Ok(PatternInput::List(
        items
            .iter()
            .map(|item| match item.and_then(|i| c.values[i].as_ref()) {
                Some(Value::Text(s)) => PatternItem::Text(s.clone()),
                _ => PatternItem::Other,
            })
            .collect(),
    ))
}

/// Purpose-specific results; the legacy loop-state adapter remains unchanged.
pub(crate) enum SnapshotInput {
    Empty,
    Null,
    NotMapping,
    Missing,
    NullPatterns,
    Patterns(PatternInput),
}
pub(crate) fn snapshot_input(
    graph: &mut Graph,
    limit: LegacyIntLimit,
    recursion: super::snapshot_recursion::RecursionPolicy,
) -> Result<SnapshotInput, ()> {
    if graph.nodes.is_empty() {
        return Ok(SnapshotInput::Empty);
    }
    let mut c = Constructor {
        values: vec![None; graph.nodes.len()],
        graph,
        pending: VecDeque::new(),
        limit,
        recursion: Some(recursion),
        scalar_frames: 10,
    };
    c.ensure(0)?;
    c.finish()?;
    let id = match &c.values[0] {
        Some(Value::Null) => return Ok(SnapshotInput::Null),
        Some(Value::Map(None)) => return Ok(SnapshotInput::Missing),
        Some(Value::Map(Some(id))) => *id,
        _ => return Ok(SnapshotInput::NotMapping),
    };
    let Some(value) = &c.values[id] else {
        return Err(());
    };
    match value {
        Value::Null => Ok(SnapshotInput::NullPatterns),
        Value::List(items) => Ok(SnapshotInput::Patterns(PatternInput::List(
            items
                .iter()
                .map(|item| match item.and_then(|i| c.values[i].as_ref()) {
                    Some(Value::Text(s)) => PatternItem::Text(s.clone()),
                    _ => PatternItem::Other,
                })
                .collect(),
        ))),
        _ => Ok(SnapshotInput::Patterns(PatternInput::NotList)),
    }
}
