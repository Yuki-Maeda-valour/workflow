// Adapted from PyYAML 6.0.3. Copyright and MIT permission: PYYAML-LICENSE.
//! 旧parserのevent cacheとfirst_*ラッパーを保つS2L専用監視。
use super::{
    legacy_recursion::Calls,
    legacy_scanner::{Scanner, Token as T},
};
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Event {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    Alias,
    Scalar,
    SequenceStart,
    SequenceEnd,
    MappingStart,
    MappingEnd,
}
#[derive(Clone, Copy)]
enum State {
    StreamStart,
    ImplicitDocumentStart,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    FlowNode,
    BlockNodeOrIndentless,
    BlockSequenceFirst,
    BlockSequenceEntry,
    IndentlessSequenceEntry,
    BlockMappingFirst,
    BlockMappingKey,
    BlockMappingValue,
    FlowSequenceFirst,
    FlowSequenceEntry(bool),
    FlowSequenceMappingKey,
    FlowSequenceMappingValue,
    FlowSequenceMappingEnd,
    FlowMappingFirst,
    FlowMappingKey(bool),
    FlowMappingValue,
    FlowMappingEmptyValue,
    Done,
}
pub(super) struct Parser {
    calls: Calls,
    scanner: Scanner,
    current: Option<Event>,
    state: State,
    states: Vec<State>,
}
impl Parser {
    pub(super) fn new(text: &str, calls: Calls) -> Self {
        Self {
            calls,
            scanner: Scanner::new(text, calls),
            current: None,
            state: State::StreamStart,
            states: Vec::new(),
        }
    }
    pub(super) fn event(&mut self, d: usize, take: bool, check: bool) -> Result<Event, ()> {
        self.calls.enter(d)?;
        if self.current.is_none() {
            self.current = Some(self.step(self.state, d + 1)?);
        }
        let event = self.current.ok_or(())?;
        if take {
            self.current = None
        } else if check {
            self.calls.enter(d + 1)?;
        }
        Ok(event)
    }
    fn check(&mut self, d: usize, choices: &[T]) -> Result<bool, ()> {
        let t = self.scanner.token(d, false, true)?;
        Ok(choices.contains(&t)
            || choices.iter().any(|c| {
                matches!(
                    (c, t),
                    (T::Scalar(_), T::Scalar(_)) | (T::Tag(_), T::Tag(_))
                )
            }))
    }
    fn peek(&mut self, d: usize) -> Result<T, ()> {
        self.scanner.token(d, false, false)
    }
    fn get(&mut self, d: usize) -> Result<T, ()> {
        self.scanner.token(d, true, false)
    }
    fn pop(&mut self, d: usize) -> Result<State, ()> {
        self.calls.enter(d)?;
        self.states.pop().ok_or(())
    }
    fn make(&self, d: usize, event: Event) -> Result<Event, ()> {
        self.calls.enter(d + 2)?;
        Ok(event)
    }
    fn empty(&self, d: usize) -> Result<Event, ()> {
        self.calls.enter(d)?;
        self.make(d, Event::Scalar)
    }
    fn step(&mut self, state: State, d: usize) -> Result<Event, ()> {
        self.calls.enter(d)?;
        match state {
            State::StreamStart => {
                self.get(d + 1)?;
                let e = self.make(d, Event::StreamStart)?;
                self.state = State::ImplicitDocumentStart;
                Ok(e)
            }
            State::ImplicitDocumentStart => {
                if !self.check(d + 1, &[T::Directive, T::DocumentStart, T::StreamEnd])? {
                    self.peek(d + 1)?;
                    let e = self.make(d, Event::DocumentStart)?;
                    self.calls.enter(d + 1)?;
                    self.states.push(State::DocumentEnd);
                    self.state = State::BlockNode;
                    Ok(e)
                } else {
                    self.step(State::DocumentStart, d + 1)
                }
            }
            State::DocumentStart => {
                while self.check(d + 1, &[T::DocumentEnd])? {
                    self.get(d + 1)?;
                }
                if !self.check(d + 1, &[T::StreamEnd])? {
                    self.peek(d + 1)?;
                    self.directives(d + 1)?;
                    if !self.check(d + 1, &[T::DocumentStart])? {
                        return Err(());
                    }
                    self.get(d + 1)?;
                    let e = self.make(d, Event::DocumentStart)?;
                    self.calls.enter(d + 1)?;
                    self.states.push(State::DocumentEnd);
                    self.state = State::DocumentContent;
                    Ok(e)
                } else {
                    self.get(d + 1)?;
                    let e = self.make(d, Event::StreamEnd)?;
                    if !self.states.is_empty() {
                        return Err(());
                    }
                    self.state = State::Done;
                    Ok(e)
                }
            }
            State::DocumentEnd => {
                self.peek(d + 1)?;
                if self.check(d + 1, &[T::DocumentEnd])? {
                    self.get(d + 1)?;
                }
                let e = self.make(d, Event::DocumentEnd)?;
                self.state = State::DocumentStart;
                Ok(e)
            }
            State::DocumentContent => {
                if self.check(
                    d + 1,
                    &[T::Directive, T::DocumentStart, T::DocumentEnd, T::StreamEnd],
                )? {
                    self.peek(d + 1)?;
                    let e = self.empty(d + 1)?;
                    self.state = self.pop(d + 1)?;
                    Ok(e)
                } else {
                    self.step(State::BlockNode, d + 1)
                }
            }
            State::BlockNode => self.node(d + 1, true, false),
            State::FlowNode => self.node(d + 1, false, false),
            State::BlockNodeOrIndentless => self.node(d + 1, true, true),
            State::BlockSequenceFirst => {
                self.get(d + 1)?;
                self.calls.enter(d + 1)?;
                self.step(State::BlockSequenceEntry, d + 1)
            }
            State::BlockSequenceEntry => {
                if self.check(d + 1, &[T::BlockEntry])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::BlockEntry, T::BlockEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::BlockSequenceEntry);
                        self.step(State::BlockNode, d + 1)
                    } else {
                        self.state = State::BlockSequenceEntry;
                        self.empty(d + 1)
                    }
                } else {
                    if !self.check(d + 1, &[T::BlockEnd])? {
                        return Err(());
                    }
                    self.get(d + 1)?;
                    let e = self.make(d, Event::SequenceEnd)?;
                    self.state = self.pop(d + 1)?;
                    self.calls.enter(d + 1)?;
                    Ok(e)
                }
            }
            State::IndentlessSequenceEntry => {
                if self.check(d + 1, &[T::BlockEntry])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::BlockEntry, T::Key, T::Value, T::BlockEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::IndentlessSequenceEntry);
                        self.step(State::BlockNode, d + 1)
                    } else {
                        self.state = State::IndentlessSequenceEntry;
                        self.empty(d + 1)
                    }
                } else {
                    self.peek(d + 1)?;
                    let e = self.make(d, Event::SequenceEnd)?;
                    self.state = self.pop(d + 1)?;
                    Ok(e)
                }
            }
            State::BlockMappingFirst => {
                self.get(d + 1)?;
                self.calls.enter(d + 1)?;
                self.step(State::BlockMappingKey, d + 1)
            }
            State::BlockMappingKey => {
                if self.check(d + 1, &[T::Key])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::Key, T::Value, T::BlockEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::BlockMappingValue);
                        self.step(State::BlockNodeOrIndentless, d + 1)
                    } else {
                        self.state = State::BlockMappingValue;
                        self.empty(d + 1)
                    }
                } else {
                    if !self.check(d + 1, &[T::BlockEnd])? {
                        return Err(());
                    }
                    self.get(d + 1)?;
                    let e = self.make(d, Event::MappingEnd)?;
                    self.state = self.pop(d + 1)?;
                    self.calls.enter(d + 1)?;
                    Ok(e)
                }
            }
            State::BlockMappingValue => {
                if self.check(d + 1, &[T::Value])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::Key, T::Value, T::BlockEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::BlockMappingKey);
                        self.step(State::BlockNodeOrIndentless, d + 1)
                    } else {
                        self.state = State::BlockMappingKey;
                        self.empty(d + 1)
                    }
                } else {
                    self.state = State::BlockMappingKey;
                    self.peek(d + 1)?;
                    self.empty(d + 1)
                }
            }
            State::FlowSequenceFirst => {
                self.get(d + 1)?;
                self.calls.enter(d + 1)?;
                self.step(State::FlowSequenceEntry(true), d + 1)
            }
            State::FlowSequenceEntry(first) => {
                if !self.check(d + 1, &[T::FlowSequenceEnd])? {
                    if !first {
                        if !self.check(d + 1, &[T::FlowEntry])? {
                            return Err(());
                        }
                        self.get(d + 1)?;
                    }
                    if self.check(d + 1, &[T::Key])? {
                        self.peek(d + 1)?;
                        let e = self.make(d, Event::MappingStart)?;
                        self.state = State::FlowSequenceMappingKey;
                        return Ok(e);
                    } else if !self.check(d + 1, &[T::FlowSequenceEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::FlowSequenceEntry(false));
                        return self.step(State::FlowNode, d + 1);
                    }
                }
                self.get(d + 1)?;
                let e = self.make(d, Event::SequenceEnd)?;
                self.state = self.pop(d + 1)?;
                self.calls.enter(d + 1)?;
                Ok(e)
            }
            State::FlowSequenceMappingKey => {
                self.get(d + 1)?;
                if !self.check(d + 1, &[T::Value, T::FlowEntry, T::FlowSequenceEnd])? {
                    self.calls.enter(d + 1)?;
                    self.states.push(State::FlowSequenceMappingValue);
                    self.step(State::FlowNode, d + 1)
                } else {
                    self.state = State::FlowSequenceMappingValue;
                    self.empty(d + 1)
                }
            }
            State::FlowSequenceMappingValue => {
                if self.check(d + 1, &[T::Value])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::FlowEntry, T::FlowSequenceEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::FlowSequenceMappingEnd);
                        self.step(State::FlowNode, d + 1)
                    } else {
                        self.state = State::FlowSequenceMappingEnd;
                        self.empty(d + 1)
                    }
                } else {
                    self.state = State::FlowSequenceMappingEnd;
                    self.peek(d + 1)?;
                    self.empty(d + 1)
                }
            }
            State::FlowSequenceMappingEnd => {
                self.state = State::FlowSequenceEntry(false);
                self.peek(d + 1)?;
                self.make(d, Event::MappingEnd)
            }
            State::FlowMappingFirst => {
                self.get(d + 1)?;
                self.calls.enter(d + 1)?;
                self.step(State::FlowMappingKey(true), d + 1)
            }
            State::FlowMappingKey(first) => {
                if !self.check(d + 1, &[T::FlowMappingEnd])? {
                    if !first {
                        if !self.check(d + 1, &[T::FlowEntry])? {
                            return Err(());
                        }
                        self.get(d + 1)?;
                    }
                    if self.check(d + 1, &[T::Key])? {
                        self.get(d + 1)?;
                        if !self.check(d + 1, &[T::Value, T::FlowEntry, T::FlowMappingEnd])? {
                            self.calls.enter(d + 1)?;
                            self.states.push(State::FlowMappingValue);
                            return self.step(State::FlowNode, d + 1);
                        } else {
                            self.state = State::FlowMappingValue;
                            return self.empty(d + 1);
                        }
                    } else if !self.check(d + 1, &[T::FlowMappingEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::FlowMappingEmptyValue);
                        return self.step(State::FlowNode, d + 1);
                    }
                }
                self.get(d + 1)?;
                let e = self.make(d, Event::MappingEnd)?;
                self.state = self.pop(d + 1)?;
                self.calls.enter(d + 1)?;
                Ok(e)
            }
            State::FlowMappingValue => {
                if self.check(d + 1, &[T::Value])? {
                    self.get(d + 1)?;
                    if !self.check(d + 1, &[T::FlowEntry, T::FlowMappingEnd])? {
                        self.calls.enter(d + 1)?;
                        self.states.push(State::FlowMappingKey(false));
                        self.step(State::FlowNode, d + 1)
                    } else {
                        self.state = State::FlowMappingKey(false);
                        self.empty(d + 1)
                    }
                } else {
                    self.state = State::FlowMappingKey(false);
                    self.peek(d + 1)?;
                    self.empty(d + 1)
                }
            }
            State::FlowMappingEmptyValue => {
                self.state = State::FlowMappingKey(false);
                self.peek(d + 1)?;
                self.empty(d + 1)
            }
            State::Done => Err(()),
        }
    }
    fn directives(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        while self.check(d + 1, &[T::Directive])? {
            self.get(d + 1)?;
        }
        Ok(())
    }
    fn node(&mut self, d: usize, block: bool, indentless: bool) -> Result<Event, ()> {
        self.calls.enter(d)?;
        if self.check(d + 1, &[T::Alias])? {
            self.get(d + 1)?;
            let e = self.make(d, Event::Alias)?;
            self.state = self.pop(d + 1)?;
            return Ok(e);
        }
        let mut anchor = false;
        let mut tag = false;
        if self.check(d + 1, &[T::Anchor])? {
            self.get(d + 1)?;
            anchor = true;
            if self.check(d + 1, &[T::Tag(false)])? {
                self.get(d + 1)?;
                tag = true;
            }
        } else if self.check(d + 1, &[T::Tag(false)])? {
            self.get(d + 1)?;
            tag = true;
            if self.check(d + 1, &[T::Anchor])? {
                self.get(d + 1)?;
                anchor = true;
            }
        }
        if tag {
            self.calls.enter(d + 1)?;
        }
        if !anchor && !tag {
            self.peek(d + 1)?;
        }
        if indentless && self.check(d + 1, &[T::BlockEntry])? {
            self.peek(d + 1)?;
            let e = self.make(d, Event::SequenceStart)?;
            self.state = State::IndentlessSequenceEntry;
            return Ok(e);
        }
        if self.check(d + 1, &[T::Scalar(true)])? {
            self.get(d + 1)?;
            let e = self.make(d, Event::Scalar)?;
            self.state = self.pop(d + 1)?;
            return Ok(e);
        }
        for (token, next, event) in [
            (
                T::FlowSequenceStart,
                State::FlowSequenceFirst,
                Event::SequenceStart,
            ),
            (
                T::FlowMappingStart,
                State::FlowMappingFirst,
                Event::MappingStart,
            ),
            (
                T::BlockSequenceStart,
                State::BlockSequenceFirst,
                Event::SequenceStart,
            ),
            (
                T::BlockMappingStart,
                State::BlockMappingFirst,
                Event::MappingStart,
            ),
        ] {
            if matches!(token, T::BlockSequenceStart | T::BlockMappingStart) && !block {
                continue;
            }
            if self.check(d + 1, &[token])? {
                self.peek(d + 1)?;
                let e = self.make(d, event)?;
                self.state = next;
                return Ok(e);
            }
        }
        if anchor || tag {
            let e = self.make(d, Event::Scalar)?;
            self.state = self.pop(d + 1)?;
            Ok(e)
        } else {
            Err(())
        }
    }
}
