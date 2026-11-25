//! PyYAML's parser-state calls used solely to observe the old recursion budget.
use super::legacy_scanner::{Scanner, Token};
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum Kind {
    StreamStart,
    StreamEnd,
    DocStart,
    DocEnd,
    Scalar,
    Alias,
    Seq,
    Map,
    EndSeq,
    EndMap,
}
#[derive(Clone, Copy)]
enum State {
    StreamStart,
    ImplicitDocument,
    DocumentStart,
    DocumentEnd,
    DocumentContent,
    BlockNode,
    BlockSeqFirst,
    BlockSeq,
    IndentlessSeq,
    BlockMapFirst,
    BlockMapKey,
    BlockMapValue,
    FlowSeqFirst,
    FlowSeq,
    InlineKey,
    InlineValue,
    InlineEnd,
    FlowMapFirst,
    FlowMapKey,
    FlowMapValue,
    FlowMapEmpty,
}
pub(super) struct Parser {
    scanner: Scanner,
    state: State,
    returns: Vec<State>,
}
impl Parser {
    pub fn new(text: &str) -> Self {
        Self {
            scanner: Scanner::new(text),
            state: State::StreamStart,
            returns: Vec::new(),
        }
    }
    pub fn peak(&self) -> usize {
        self.scanner.peak
    }
    fn peek(&mut self, d: usize) -> Result<Token, ()> {
        self.scanner.peek(d + 1)
    }
    fn take(&mut self, d: usize) -> Result<Token, ()> {
        self.scanner.take(d + 1)
    }
    fn back(&mut self) -> Result<(), ()> {
        self.state = self.returns.pop().ok_or(())?;
        Ok(())
    }
    fn emit(&mut self, kind: Kind, d: usize) -> Kind {
        self.scanner.touch(d + 1);
        kind
    }
    fn empty(&mut self, d: usize) -> Kind {
        self.scanner.touch(d + 2);
        Kind::Scalar
    }
    /// `depth` is the active check_event/get_event frame. State methods add one.
    pub fn event(&mut self, depth: usize) -> Result<Kind, ()> {
        let d = depth + 1;
        self.scanner.touch(d);
        match self.state {
            State::StreamStart => {
                self.take(d)?;
                self.state = State::ImplicitDocument;
                Ok(self.emit(Kind::StreamStart, d))
            }
            State::ImplicitDocument => {
                if matches!(
                    self.peek(d)?,
                    Token::Directive | Token::DocStart | Token::StreamEnd
                ) {
                    self.document_start(d + 1)
                } else {
                    self.peek(d)?;
                    self.returns.push(State::DocumentEnd);
                    self.state = State::BlockNode;
                    Ok(self.emit(Kind::DocStart, d))
                }
            }
            State::DocumentStart => self.document_start(d),
            State::DocumentEnd => {
                self.peek(d)?;
                if self.peek(d)? == Token::DocEnd {
                    self.take(d)?;
                }
                self.state = State::DocumentStart;
                Ok(self.emit(Kind::DocEnd, d))
            }
            State::DocumentContent => {
                if matches!(
                    self.peek(d)?,
                    Token::Directive | Token::DocStart | Token::DocEnd | Token::StreamEnd
                ) {
                    self.peek(d)?;
                    self.back()?;
                    Ok(self.empty(d))
                } else {
                    self.node(d + 2, true, false)
                }
            }
            State::BlockNode => self.node(d + 1, true, false),
            State::BlockSeqFirst => {
                self.take(d)?;
                self.block_seq(d + 1)
            }
            State::BlockSeq => self.block_seq(d),
            State::IndentlessSeq => self.indentless(d),
            State::BlockMapFirst => {
                self.take(d)?;
                self.block_map_key(d + 1)
            }
            State::BlockMapKey => self.block_map_key(d),
            State::BlockMapValue => self.block_map_value(d),
            State::FlowSeqFirst => {
                self.take(d)?;
                self.flow_seq(d + 1, true)
            }
            State::FlowSeq => self.flow_seq(d, false),
            State::InlineKey => {
                self.take(d)?;
                if !matches!(self.peek(d)?, Token::Value | Token::Comma | Token::EndSeq) {
                    self.returns.push(State::InlineValue);
                    self.node(d + 2, false, false)
                } else {
                    self.state = State::InlineValue;
                    Ok(self.empty(d))
                }
            }
            State::InlineValue => {
                if self.peek(d)? == Token::Value {
                    self.take(d)?;
                    if !matches!(self.peek(d)?, Token::Comma | Token::EndSeq) {
                        self.returns.push(State::InlineEnd);
                        return self.node(d + 2, false, false);
                    }
                } else {
                    self.peek(d)?;
                }
                self.state = State::InlineEnd;
                Ok(self.empty(d))
            }
            State::InlineEnd => {
                self.state = State::FlowSeq;
                self.peek(d)?;
                Ok(self.emit(Kind::EndMap, d))
            }
            State::FlowMapFirst => {
                self.take(d)?;
                self.flow_map(d + 1, true)
            }
            State::FlowMapKey => self.flow_map(d, false),
            State::FlowMapValue => self.flow_map_value(d),
            State::FlowMapEmpty => {
                self.state = State::FlowMapKey;
                self.peek(d)?;
                Ok(self.empty(d))
            }
        }
    }
    fn document_start(&mut self, d: usize) -> Result<Kind, ()> {
        while self.peek(d)? == Token::DocEnd {
            self.take(d)?;
        }
        if self.peek(d)? == Token::StreamEnd {
            self.take(d)?;
            return Ok(self.emit(Kind::StreamEnd, d));
        }
        self.peek(d)?;
        while self.peek(d + 1)? == Token::Directive {
            self.take(d + 1)?;
        }
        if self.peek(d)? != Token::DocStart {
            return Err(());
        }
        self.take(d)?;
        self.returns.push(State::DocumentEnd);
        self.state = State::DocumentContent;
        Ok(self.emit(Kind::DocStart, d))
    }
    fn node(&mut self, d: usize, block: bool, indentless: bool) -> Result<Kind, ()> {
        self.scanner.touch(d);
        if self.peek(d)? == Token::Alias {
            self.take(d)?;
            self.back()?;
            return Ok(self.emit(Kind::Alias, d));
        }
        let mut properties = false;
        if self.peek(d)? == Token::Anchor {
            self.take(d)?;
            properties = true;
            if self.peek(d)? == Token::Tag {
                self.take(d)?;
            }
        } else if self.peek(d)? == Token::Tag {
            self.take(d)?;
            properties = true;
            if self.peek(d)? == Token::Anchor {
                self.take(d)?;
            }
        }
        if !properties {
            self.peek(d)?;
        }
        if indentless && self.peek(d)? == Token::Entry {
            self.peek(d)?;
            self.state = State::IndentlessSeq;
            return Ok(self.emit(Kind::Seq, d));
        }
        if self.peek(d)? == Token::Scalar {
            self.take(d)?;
            self.back()?;
            return Ok(self.emit(Kind::Scalar, d));
        }
        if self.peek(d)? == Token::FlowSeq {
            self.peek(d)?;
            self.state = State::FlowSeqFirst;
            return Ok(self.emit(Kind::Seq, d));
        }
        if self.peek(d)? == Token::FlowMap {
            self.peek(d)?;
            self.state = State::FlowMapFirst;
            return Ok(self.emit(Kind::Map, d));
        }
        if block && self.peek(d)? == Token::BlockSeq {
            self.peek(d)?;
            self.state = State::BlockSeqFirst;
            return Ok(self.emit(Kind::Seq, d));
        }
        if block && self.peek(d)? == Token::BlockMap {
            self.peek(d)?;
            self.state = State::BlockMapFirst;
            return Ok(self.emit(Kind::Map, d));
        }
        if properties {
            self.back()?;
            return Ok(self.emit(Kind::Scalar, d));
        }
        Err(())
    }
    fn block_seq(&mut self, d: usize) -> Result<Kind, ()> {
        if self.peek(d)? == Token::Entry {
            self.take(d)?;
            if !matches!(self.peek(d)?, Token::Entry | Token::BlockEnd) {
                self.returns.push(State::BlockSeq);
                return self.node(d + 2, true, false);
            }
            self.state = State::BlockSeq;
            return Ok(self.empty(d));
        }
        if self.peek(d)? != Token::BlockEnd {
            return Err(());
        }
        self.take(d)?;
        self.back()?;
        Ok(self.emit(Kind::EndSeq, d))
    }
    fn indentless(&mut self, d: usize) -> Result<Kind, ()> {
        if self.peek(d)? == Token::Entry {
            self.take(d)?;
            if !matches!(
                self.peek(d)?,
                Token::Entry | Token::Key | Token::Value | Token::BlockEnd
            ) {
                self.returns.push(State::IndentlessSeq);
                return self.node(d + 2, true, false);
            }
            self.state = State::IndentlessSeq;
            return Ok(self.empty(d));
        }
        self.peek(d)?;
        self.back()?;
        Ok(self.emit(Kind::EndSeq, d))
    }
    fn block_map_key(&mut self, d: usize) -> Result<Kind, ()> {
        if self.peek(d)? == Token::Key {
            self.take(d)?;
            if !matches!(self.peek(d)?, Token::Key | Token::Value | Token::BlockEnd) {
                self.returns.push(State::BlockMapValue);
                return self.node(d + 2, true, true);
            }
            self.state = State::BlockMapValue;
            return Ok(self.empty(d));
        }
        if self.peek(d)? != Token::BlockEnd {
            return Err(());
        }
        self.take(d)?;
        self.back()?;
        Ok(self.emit(Kind::EndMap, d))
    }
    fn block_map_value(&mut self, d: usize) -> Result<Kind, ()> {
        if self.peek(d)? == Token::Value {
            self.take(d)?;
            if !matches!(self.peek(d)?, Token::Key | Token::Value | Token::BlockEnd) {
                self.returns.push(State::BlockMapKey);
                return self.node(d + 2, true, true);
            }
        } else {
            self.peek(d)?;
        }
        self.state = State::BlockMapKey;
        Ok(self.empty(d))
    }
    fn flow_seq(&mut self, d: usize, first: bool) -> Result<Kind, ()> {
        if self.peek(d)? != Token::EndSeq {
            if !first {
                if self.peek(d)? != Token::Comma {
                    return Err(());
                }
                self.take(d)?;
            }
            if self.peek(d)? == Token::Key {
                self.peek(d)?;
                self.state = State::InlineKey;
                return Ok(self.emit(Kind::Map, d));
            }
            if self.peek(d)? != Token::EndSeq {
                self.returns.push(State::FlowSeq);
                return self.node(d + 2, false, false);
            }
        }
        self.take(d)?;
        self.back()?;
        Ok(self.emit(Kind::EndSeq, d))
    }
    fn flow_map(&mut self, d: usize, first: bool) -> Result<Kind, ()> {
        if self.peek(d)? != Token::EndMap {
            if !first {
                if self.peek(d)? != Token::Comma {
                    return Err(());
                }
                self.take(d)?;
            }
            if self.peek(d)? == Token::Key {
                self.take(d)?;
                if !matches!(self.peek(d)?, Token::Value | Token::Comma | Token::EndMap) {
                    self.returns.push(State::FlowMapValue);
                    return self.node(d + 2, false, false);
                }
                self.state = State::FlowMapValue;
                return Ok(self.empty(d));
            }
            if self.peek(d)? != Token::EndMap {
                self.returns.push(State::FlowMapEmpty);
                return self.node(d + 2, false, false);
            }
        }
        self.take(d)?;
        self.back()?;
        Ok(self.emit(Kind::EndMap, d))
    }
    fn flow_map_value(&mut self, d: usize) -> Result<Kind, ()> {
        if self.peek(d)? == Token::Value {
            self.take(d)?;
            if !matches!(self.peek(d)?, Token::Comma | Token::EndMap) {
                self.returns.push(State::FlowMapKey);
                return self.node(d + 2, false, false);
            }
        } else {
            self.peek(d)?;
        }
        self.state = State::FlowMapKey;
        Ok(self.empty(d))
    }
}
