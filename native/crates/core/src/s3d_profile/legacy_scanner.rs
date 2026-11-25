//! Token lookahead accounting for PyYAML 6.0.3's pure Python scanner.
//! This observer does not construct values. The R4S parser remains authoritative
//! for grammar, tags and scalar contents. A token's acquisition is delayed until
//! the parser asks for it, including unresolved simple-key lookahead.
use std::collections::{BTreeMap, VecDeque};
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum Token {
    StreamStart,
    StreamEnd,
    Directive,
    DocStart,
    DocEnd,
    BlockSeq,
    BlockMap,
    BlockEnd,
    FlowSeq,
    FlowMap,
    EndSeq,
    EndMap,
    Entry,
    Comma,
    Key,
    Value,
    Alias,
    Anchor,
    Tag,
    Scalar,
}
#[derive(Clone, Copy)]
struct Key {
    number: usize,
    index: usize,
    line: usize,
    column: isize,
}
pub(super) struct Scanner {
    text: Vec<char>,
    pos: usize,
    line: usize,
    col: isize,
    flow: usize,
    indent: isize,
    indents: Vec<isize>,
    queue: VecDeque<Token>,
    taken: usize,
    keys: BTreeMap<usize, Key>,
    allow: bool,
    done: bool,
    pub peak: usize,
}
impl Scanner {
    pub fn new(text: &str) -> Self {
        Self {
            text: text.chars().chain(std::iter::once('\0')).collect(),
            pos: 0,
            line: 0,
            col: 0,
            flow: 0,
            indent: -1,
            indents: Vec::new(),
            queue: VecDeque::from([Token::StreamStart]),
            taken: 0,
            keys: BTreeMap::new(),
            allow: true,
            done: false,
            peak: 0,
        }
    }
    pub fn touch(&mut self, depth: usize) {
        self.peak = self.peak.max(depth);
    }
    fn ch(&self, n: usize) -> char {
        self.text.get(self.pos + n).copied().unwrap_or('\0')
    }
    fn br(c: char) -> bool {
        matches!(c, '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
    }
    fn space(c: char) -> bool {
        c == '\0' || c == ' ' || c == '\t' || Self::br(c)
    }
    fn forward(&mut self, n: usize) {
        for _ in 0..n {
            let c = self.ch(0);
            self.pos += 1;
            if c == '\n'
                || matches!(c, '\u{85}' | '\u{2028}' | '\u{2029}')
                || c == '\r' && self.ch(0) != '\n'
            {
                self.line += 1;
                self.col = 0;
            } else if c != '\u{feff}' {
                self.col += 1;
            }
        }
    }
    fn line_break(&mut self) -> bool {
        if !Self::br(self.ch(0)) {
            return false;
        }
        let crlf = self.ch(0) == '\r' && self.ch(1) == '\n';
        self.forward(if crlf { 2 } else { 1 });
        true
    }
    fn stale(&mut self) {
        self.keys
            .retain(|_, k| k.line == self.line && self.pos - k.index <= 1024);
    }
    fn need(&mut self, depth: usize) -> bool {
        self.touch(depth);
        if self.done {
            return false;
        }
        if self.queue.is_empty() {
            return true;
        }
        self.touch(depth + 1);
        self.stale();
        self.keys.values().map(|k| k.number).min() == Some(self.taken)
    }
    pub fn peek(&mut self, depth: usize) -> Result<Token, ()> {
        self.touch(depth);
        while self.need(depth + 1) {
            self.fetch(depth + 1)?;
        }
        self.queue.front().copied().ok_or(())
    }
    pub fn take(&mut self, depth: usize) -> Result<Token, ()> {
        self.peek(depth)?;
        self.taken += 1;
        self.queue.pop_front().ok_or(())
    }
    fn save(&mut self, depth: usize) {
        self.touch(depth);
        if self.allow {
            self.touch(depth + 3); // get_mark -> type call -> Mark.__init__
            self.keys.insert(
                self.flow,
                Key {
                    number: self.taken + self.queue.len(),
                    index: self.pos,
                    line: self.line,
                    column: self.col,
                },
            );
        }
    }
    fn unwind(&mut self, column: isize, depth: usize) {
        self.touch(depth);
        if self.flow != 0 {
            return;
        }
        while self.indent > column {
            self.touch(depth + 3);
            self.indent = self.indents.pop().unwrap_or(-1);
            self.queue.push_back(Token::BlockEnd);
        }
    }
    fn add_indent(&mut self, column: isize) -> bool {
        if self.indent < column {
            self.indents.push(self.indent);
            self.indent = column;
            true
        } else {
            false
        }
    }
    fn skip(&mut self, depth: usize) {
        self.touch(depth + 2); // scan_line_break -> peek, even without a break
        if self.pos == 0 && self.ch(0) == '\u{feff}' {
            self.forward(1);
        }
        loop {
            while self.ch(0) == ' ' {
                self.forward(1);
            }
            if self.ch(0) == '#' {
                while self.ch(0) != '\0' && !Self::br(self.ch(0)) {
                    self.forward(1);
                }
            }
            if self.line_break() {
                if self.flow == 0 {
                    self.allow = true;
                }
            } else {
                break;
            }
        }
    }
    fn fetch(&mut self, depth: usize) -> Result<(), ()> {
        self.touch(depth);
        self.skip(depth + 1);
        self.stale();
        self.unwind(self.col, depth + 1);
        let ch = self.ch(0);
        let directive = self.col == 0 && ch == '%';
        let document = self.col == 0
            && (self.text.get(self.pos..self.pos + 3) == Some(&['-', '-', '-'])
                || self.text.get(self.pos..self.pos + 3) == Some(&['.', '.', '.']))
            && Self::space(self.ch(3));
        if ch == '\0' {
            self.unwind(-1, depth + 2);
            self.keys.clear();
            self.allow = false;
            self.done = true;
            self.queue.push_back(Token::StreamEnd);
            self.touch(depth + 4);
            return Ok(());
        }
        if directive || document {
            self.unwind(-1, depth + 2);
            self.keys.remove(&self.flow);
            self.allow = false;
            if directive {
                while self.ch(0) != '\0' && !Self::br(self.ch(0)) {
                    self.forward(1);
                }
                self.line_break();
                self.queue.push_back(Token::Directive);
                self.touch(depth + 6);
            } else {
                self.forward(3);
                self.queue.push_back(if ch == '-' {
                    Token::DocStart
                } else {
                    Token::DocEnd
                });
                self.touch(depth + 4);
            }
            return Ok(());
        }
        match ch {
            '[' | '{' => {
                // fetch_flow_* -> fetch_flow_collection_start -> save -> mark
                self.save(depth + 3);
                self.touch(depth + 5);
                self.flow += 1;
                self.allow = true;
                self.forward(1);
                self.queue.push_back(if ch == '[' {
                    Token::FlowSeq
                } else {
                    Token::FlowMap
                });
            }
            ']' | '}' => {
                self.keys.remove(&self.flow);
                self.flow = self.flow.checked_sub(1).ok_or(())?;
                self.allow = false;
                self.forward(1);
                self.touch(depth + 5);
                self.queue.push_back(if ch == ']' {
                    Token::EndSeq
                } else {
                    Token::EndMap
                });
            }
            ',' => {
                self.allow = true;
                self.keys.remove(&self.flow);
                self.forward(1);
                self.queue.push_back(Token::Comma);
                self.touch(depth + 4);
            }
            '-' if Self::space(self.ch(1)) => {
                if self.flow == 0 && self.add_indent(self.col) {
                    self.queue.push_back(Token::BlockSeq);
                }
                self.allow = true;
                self.keys.remove(&self.flow);
                self.forward(1);
                self.queue.push_back(Token::Entry);
                self.touch(depth + 4);
            }
            '?' if self.flow != 0 || Self::space(self.ch(1)) => {
                if self.flow == 0 && self.add_indent(self.col) {
                    self.queue.push_back(Token::BlockMap);
                }
                self.allow = self.flow == 0;
                self.keys.remove(&self.flow);
                self.forward(1);
                self.queue.push_back(Token::Key);
                self.touch(depth + 4);
            }
            ':' if self.flow != 0 || Self::space(self.ch(1)) => {
                if let Some(key) = self.keys.remove(&self.flow) {
                    let at = key.number.checked_sub(self.taken).ok_or(())?;
                    if at > self.queue.len() {
                        return Err(());
                    }
                    self.queue.insert(at, Token::Key);
                    if self.flow == 0 && self.add_indent(key.column) {
                        self.queue.insert(at, Token::BlockMap);
                    }
                    self.allow = false;
                } else {
                    if self.flow == 0 && self.add_indent(self.col) {
                        self.queue.push_back(Token::BlockMap);
                    }
                    self.allow = self.flow == 0;
                    self.keys.remove(&self.flow);
                }
                self.forward(1);
                self.queue.push_back(Token::Value);
                self.touch(depth + 4);
            }
            '*' | '&' => {
                self.save(depth + 2);
                self.allow = false;
                self.forward(1);
                while self.ch(0).is_ascii_alphanumeric() || matches!(self.ch(0), '-' | '_') {
                    self.forward(1);
                }
                self.queue.push_back(if ch == '*' {
                    Token::Alias
                } else {
                    Token::Anchor
                });
                self.touch(depth + 5);
            }
            '!' => {
                self.save(depth + 2);
                self.allow = false;
                self.forward(1);
                if self.ch(0) == '<' {
                    self.forward(1);
                    while !matches!(self.ch(0), '>' | '\0') {
                        self.forward(1);
                    }
                    if self.ch(0) == '>' {
                        self.forward(1);
                    }
                } else {
                    while !Self::space(self.ch(0)) && !matches!(self.ch(0), ']' | '}') {
                        self.forward(1);
                    }
                }
                self.queue.push_back(Token::Tag);
                self.touch(depth + 5);
            }
            '\'' | '"' => {
                self.save(depth + 3);
                self.allow = false;
                self.quoted(ch, depth + 3)?;
                self.queue.push_back(Token::Scalar);
            }
            '|' | '>' if self.flow == 0 => {
                self.allow = true;
                self.keys.remove(&self.flow);
                self.block_scalar(depth + 3)?;
                self.queue.push_back(Token::Scalar);
            }
            _ => {
                self.save(depth + 2);
                self.allow = false;
                self.plain(depth + 2)?;
                self.queue.push_back(Token::Scalar);
            }
        }
        Ok(())
    }
    fn quoted(&mut self, quote: char, depth: usize) -> Result<(), ()> {
        self.touch(depth + 3);
        self.forward(1);
        loop {
            let c = self.ch(0);
            if c == '\0' {
                return Err(());
            }
            if c == quote {
                self.forward(1);
                if quote == '\'' && self.ch(0) == '\'' {
                    self.forward(1);
                    continue;
                }
                return Ok(());
            }
            if c == '\\' && quote == '"' {
                self.forward(1);
                let c = self.ch(0);
                if Self::br(c) {
                    self.touch(depth + 4);
                    self.line_break();
                } else {
                    let skip = match c {
                        'x' => 3,
                        'u' => 5,
                        'U' => 9,
                        _ => 1,
                    };
                    self.forward(skip);
                }
            } else if Self::br(c) {
                self.touch(depth + 4);
                self.line_break();
            } else {
                self.forward(1);
            }
        }
    }
    fn plain(&mut self, depth: usize) -> Result<(), ()> {
        self.touch(depth + 3);
        let start = self.pos;
        let indent = self.indent + 1;
        loop {
            if self.ch(0) == '#' {
                break;
            }
            let mut n = 0;
            loop {
                let c = self.ch(n);
                let next = self.ch(n + 1);
                if Self::space(c)
                    || c == ':' && (Self::space(next) || self.flow != 0 && ",[]{}".contains(next))
                    || self.flow != 0 && ",?[]{}".contains(c)
                {
                    break;
                }
                n += 1;
            }
            if n == 0 {
                break;
            }
            self.allow = false;
            self.forward(n);
            let before = self.pos;
            while self.ch(0) == ' ' {
                self.forward(1);
            }
            if Self::br(self.ch(0)) {
                self.touch(depth + 3);
                self.line_break();
                self.allow = true;
                if self.ch(0) == '\0' {
                    self.touch(depth + 4);
                }
                while self.ch(0) == ' ' || Self::br(self.ch(0)) {
                    if self.ch(0) == ' ' {
                        self.forward(1);
                    } else {
                        self.line_break();
                    }
                }
                if self.col == 0
                    && (self.text.get(self.pos..self.pos + 3) == Some(&['-', '-', '-'])
                        || self.text.get(self.pos..self.pos + 3) == Some(&['.', '.', '.']))
                    && Self::space(self.ch(3))
                {
                    break;
                }
            }
            if before == self.pos || self.ch(0) == '#' || self.flow == 0 && self.col < indent {
                break;
            }
        }
        if self.pos == start {
            return Err(());
        }
        Ok(())
    }
    fn block_scalar(&mut self, depth: usize) -> Result<(), ()> {
        self.touch(depth + 3);
        let min_indent = (self.indent + 1).max(1);
        let mut explicit = None;
        self.forward(1);
        // Only the indicator prefix may contain the indentation digit. A digit
        // in the trailing header comment is ordinary comment text.
        for _ in 0..2 {
            if self.ch(0).is_ascii_digit() {
                explicit = self.ch(0).to_digit(10).map(|n| min_indent + n as isize - 1);
            } else if !matches!(self.ch(0), '+' | '-') {
                break;
            }
            self.forward(1);
        }
        while !Self::br(self.ch(0)) && self.ch(0) != '\0' {
            self.forward(1);
        }
        self.line_break();
        let mut indentation = explicit;
        loop {
            while self.ch(0) == ' ' {
                self.forward(1);
            }
            if Self::br(self.ch(0)) {
                self.line_break();
                continue;
            }
            if self.ch(0) == '\0' {
                break;
            }
            let required = *indentation.get_or_insert(self.col.max(min_indent));
            if self.col < required {
                break;
            }
            while !Self::br(self.ch(0)) && self.ch(0) != '\0' {
                self.forward(1);
            }
            if !self.line_break() {
                break;
            }
        }
        Ok(())
    }
}
