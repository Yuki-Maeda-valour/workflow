// Adapted from PyYAML 6.0.3. Copyright and MIT permission: PYYAML-LICENSE.
//! PyYAMLのtoken待ち行列とreaderの位置をS2L用に保持する。
//! 値の解析は既存graphが担当する。この走査は旧呼出しの実行順を監視する。
use super::legacy_recursion::Calls;
use std::collections::{BTreeMap, VecDeque};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Token {
    StreamStart,
    StreamEnd,
    DocumentStart,
    DocumentEnd,
    Directive,
    FlowSequenceStart,
    FlowSequenceEnd,
    FlowMappingStart,
    FlowMappingEnd,
    FlowEntry,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    BlockEntry,
    Key,
    Value,
    Anchor,
    Alias,
    Tag(bool),
    Scalar(bool),
}
#[derive(Clone, Copy)]
struct Key {
    number: usize,
    required: bool,
    index: usize,
    line: usize,
    column: isize,
}
pub(super) struct Scanner {
    calls: Calls,
    raw: Vec<char>,
    at: usize,
    line: usize,
    column: isize,
    tokens: VecDeque<Token>,
    taken: usize,
    flow: usize,
    indent: isize,
    indents: Vec<isize>,
    keys: BTreeMap<usize, Key>,
    allow: bool,
    done: bool,
}
fn breaks(c: char) -> bool {
    matches!(c, '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}
fn blank(c: char) -> bool {
    c == '\0' || c == ' ' || c == '\t' || breaks(c)
}
impl Scanner {
    pub(super) fn new(text: &str, calls: Calls) -> Self {
        let mut raw = text.chars().collect::<Vec<_>>();
        raw.push('\0');
        Self {
            calls,
            raw,
            at: 0,
            line: 0,
            column: 0,
            tokens: VecDeque::from([Token::StreamStart]),
            taken: 0,
            flow: 0,
            indent: -1,
            indents: Vec::new(),
            keys: BTreeMap::new(),
            allow: true,
            done: false,
        }
    }
    fn peek(&self, d: usize, offset: usize) -> Result<char, ()> {
        self.calls.enter(d)?;
        if self.at + offset >= self.raw.len() {
            self.calls.enter(d + 1)?;
        }
        Ok(*self.raw.get(self.at + offset).unwrap_or(&'\0'))
    }
    fn prefix(&self, d: usize, n: usize) -> Result<String, ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        Ok(self.raw.iter().skip(self.at).take(n).collect())
    }
    fn forward(&mut self, d: usize, n: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        for _ in 0..n {
            let c = *self.raw.get(self.at).ok_or(())?;
            self.at += 1;
            if matches!(c, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
                || (c == '\r' && self.raw.get(self.at) != Some(&'\n'))
            {
                self.line += 1;
                self.column = 0;
            } else if c != '\u{feff}' {
                self.column += 1;
            }
        }
        Ok(())
    }
    fn mark(&self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 2)
    }
    fn append(&mut self, d: usize, token: Token) -> Result<(), ()> {
        self.calls.enter(d + 2)?;
        self.tokens.push_back(token);
        Ok(())
    }
    fn stale(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        let mut remove = Vec::new();
        for (&level, key) in &self.keys {
            self.calls.enter(d + 1)?;
            if key.line != self.line || self.at - key.index > 1024 {
                if key.required {
                    return Err(());
                }
                remove.push(level);
            }
        }
        for level in remove {
            self.keys.remove(&level);
        }
        Ok(())
    }
    fn next_key(&self, d: usize) -> Result<Option<usize>, ()> {
        self.calls.enter(d)?;
        let mut minimum = None;
        for key in self.keys.values() {
            if minimum.is_some() {
                self.calls.enter(d + 1)?;
            }
            minimum = Some(minimum.map_or(key.number, |v: usize| v.min(key.number)));
        }
        Ok(minimum)
    }
    fn need(&mut self, d: usize) -> Result<bool, ()> {
        self.calls.enter(d)?;
        if self.done {
            return Ok(false);
        }
        if self.tokens.is_empty() {
            return Ok(true);
        }
        self.stale(d + 1)?;
        let key = self.next_key(d + 1)?;
        self.calls.enter(d + 1)?;
        Ok(key == Some(self.taken))
    }
    pub(super) fn token(&mut self, d: usize, take: bool, check: bool) -> Result<Token, ()> {
        self.calls.enter(d)?;
        while self.need(d + 1)? {
            self.fetch(d + 1)?;
        }
        let token = *self.tokens.front().ok_or(())?;
        if take {
            self.calls.enter(d + 1)?;
            self.taken += 1;
            self.tokens.pop_front();
        } else if check {
            self.calls.enter(d + 1)?;
        }
        Ok(token)
    }
    fn remove(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        if self.keys.remove(&self.flow).is_some_and(|k| k.required) {
            self.mark(d + 1)?;
            return Err(());
        }
        Ok(())
    }
    fn save(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        if self.flow == 0 {
            self.calls.enter(d + 1)?;
        }
        let required = self.flow == 0 && self.indent == self.column;
        if self.allow {
            self.remove(d + 1)?;
            self.calls.enter(d + 1)?;
            self.mark(d + 1)?;
            self.calls.enter(d + 2)?;
            self.keys.insert(
                self.flow,
                Key {
                    number: self.taken + self.tokens.len(),
                    required,
                    index: self.at,
                    line: self.line,
                    column: self.column,
                },
            );
        }
        Ok(())
    }
    fn unwind(&mut self, d: usize, column: isize) -> Result<(), ()> {
        self.calls.enter(d)?;
        if self.flow != 0 {
            return Ok(());
        }
        loop {
            self.calls.enter(d + 1)?;
            if self.indent <= column {
                break;
            }
            self.mark(d + 1)?;
            self.calls.enter(d + 1)?;
            self.indent = self.indents.pop().ok_or(())?;
            self.append(d, Token::BlockEnd)?;
        }
        Ok(())
    }
    fn add_indent(&mut self, d: usize, column: isize) -> Result<bool, ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        if self.indent < column {
            self.indents.push(self.indent);
            self.indent = column;
            Ok(true)
        } else {
            Ok(false)
        }
    }
    fn line_break(&mut self, d: usize) -> Result<bool, ()> {
        self.calls.enter(d)?;
        let c = self.peek(d + 1, 0)?;
        if c == '\r' {
            if self.prefix(d + 1, 2)? == "\r\n" {
                self.forward(d + 1, 2)?
            } else {
                self.forward(d + 1, 1)?
            }
            Ok(true)
        } else if breaks(c) {
            self.forward(d + 1, 1)?;
            Ok(true)
        } else {
            Ok(false)
        }
    }
    fn next(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        if self.at == 0 && self.peek(d + 1, 0)? == '\u{feff}' {
            self.forward(d + 1, 1)?;
        }
        loop {
            while self.peek(d + 1, 0)? == ' ' {
                self.forward(d + 1, 1)?;
            }
            if self.peek(d + 1, 0)? == '#' {
                while !breaks(self.peek(d + 1, 0)?) && self.peek(d + 1, 0)? != '\0' {
                    self.forward(d + 1, 1)?;
                }
            }
            if self.line_break(d + 1)? {
                if self.flow == 0 {
                    self.allow = true
                }
            } else {
                break;
            }
        }
        Ok(())
    }
    fn indicator(&self, d: usize, s: &str) -> Result<bool, ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        Ok(self.column == 0 && self.prefix(d + 1, 3)? == s && blank(self.peek(d + 1, 3)?))
    }
    fn emit_one(&mut self, d: usize, kind: Token, n: usize) -> Result<(), ()> {
        self.mark(d + 1)?;
        self.forward(d + 1, n)?;
        self.mark(d + 1)?;
        self.append(d, kind)
    }
    fn flow_start(&mut self, d: usize, kind: Token) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.save(d + 1)?;
        self.flow += 1;
        self.allow = true;
        self.emit_one(d, kind, 1)
    }
    fn flow_end(&mut self, d: usize, kind: Token) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.remove(d + 1)?;
        self.flow = self.flow.checked_sub(1).ok_or(())?;
        self.allow = false;
        self.emit_one(d, kind, 1)
    }
    fn block_entry(&mut self, d: usize, key: bool) -> Result<(), ()> {
        self.calls.enter(d)?;
        if self.flow == 0 {
            if !self.allow {
                return Err(());
            }
            if self.add_indent(d + 1, self.column)? {
                self.mark(d + 1)?;
                self.append(
                    d,
                    if key {
                        Token::BlockMappingStart
                    } else {
                        Token::BlockSequenceStart
                    },
                )?;
            }
        }
        self.allow = !key || self.flow == 0;
        self.remove(d + 1)?;
        self.emit_one(d, if key { Token::Key } else { Token::BlockEntry }, 1)
    }
    fn value(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        if let Some(key) = self.keys.remove(&self.flow) {
            self.calls.enter(d + 2)?;
            let position = key.number.checked_sub(self.taken).ok_or(())?;
            self.tokens.insert(position, Token::Key);
            if self.flow == 0 && self.add_indent(d + 1, key.column)? {
                self.calls.enter(d + 2)?;
                self.tokens.insert(position, Token::BlockMappingStart);
            }
            self.allow = false;
        } else {
            if self.flow == 0 {
                if !self.allow {
                    return Err(());
                }
                if self.add_indent(d + 1, self.column)? {
                    self.mark(d + 1)?;
                    self.append(d, Token::BlockMappingStart)?;
                }
            }
            self.allow = self.flow == 0;
            self.remove(d + 1)?;
        }
        self.emit_one(d, Token::Value, 1)
    }
    fn fetch(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.next(d + 1)?;
        self.stale(d + 1)?;
        self.unwind(d + 1, self.column)?;
        let c = self.peek(d + 1, 0)?;
        match c {
            '\0' => {
                self.calls.enter(d + 1)?;
                self.unwind(d + 2, -1)?;
                self.remove(d + 2)?;
                self.allow = false;
                self.keys.clear();
                self.mark(d + 2)?;
                self.append(d + 1, Token::StreamEnd)?;
                self.done = true;
                Ok(())
            }
            '[' => {
                self.calls.enter(d + 1)?;
                self.flow_start(d + 2, Token::FlowSequenceStart)
            }
            '{' => {
                self.calls.enter(d + 1)?;
                self.flow_start(d + 2, Token::FlowMappingStart)
            }
            ']' => {
                self.calls.enter(d + 1)?;
                self.flow_end(d + 2, Token::FlowSequenceEnd)
            }
            '}' => {
                self.calls.enter(d + 1)?;
                self.flow_end(d + 2, Token::FlowMappingEnd)
            }
            ',' => {
                self.calls.enter(d + 1)?;
                self.allow = true;
                self.remove(d + 2)?;
                self.emit_one(d + 1, Token::FlowEntry, 1)
            }
            '-' if self.indicator(d + 1, "---")? => self.document(d + 1, Token::DocumentStart),
            '.' if self.indicator(d + 1, "...")? => self.document(d + 1, Token::DocumentEnd),
            '-' if self.check_blank(d + 1, false)? => self.block_entry(d + 1, false),
            '?' if self.check_blank(d + 1, true)? => self.block_entry(d + 1, true),
            ':' if self.check_blank(d + 1, true)? => self.value(d + 1),
            '*' | '&' => self.anchor(d + 1, c == '*'),
            '!' => self.tag(d + 1),
            '"' | '\'' => {
                self.calls.enter(d + 1)?;
                self.flow_scalar(d + 2, c)
            }
            '|' | '>' if self.flow == 0 => {
                self.calls.enter(d + 1)?;
                self.block_scalar(d + 2)
            }
            '%' if self.column == 0 => self.directive(d + 1),
            _ => {
                self.calls.enter(d + 1)?;
                self.peek(d + 2, 0)?;
                self.plain(d + 1)
            }
        }
    }
    fn check_blank(&self, d: usize, flow: bool) -> Result<bool, ()> {
        self.calls.enter(d)?;
        if flow && self.flow != 0 {
            Ok(true)
        } else {
            Ok(blank(self.peek(d + 1, 1)?))
        }
    }
    fn document(&mut self, d: usize, kind: Token) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        self.unwind(d + 2, -1)?;
        self.remove(d + 2)?;
        self.allow = false;
        self.emit_one(d + 1, kind, 3)
    }
    fn anchor(&mut self, d: usize, alias: bool) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.save(d + 1)?;
        self.allow = false;
        self.calls.enter(d + 1)?;
        self.mark(d + 2)?;
        self.peek(d + 2, 0)?;
        self.forward(d + 2, 1)?;
        let mut n = 0;
        while matches!(self.peek(d+2,n)?,'a'..='z'|'A'..='Z'|'0'..='9'|'_'|'-') {
            n += 1;
        }
        if n == 0 {
            return Err(());
        }
        self.prefix(d + 2, n)?;
        self.forward(d + 2, n)?;
        self.peek(d + 2, 0)?;
        self.mark(d + 2)?;
        self.append(d + 1, if alias { Token::Alias } else { Token::Anchor })
    }
    fn tag(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.save(d + 1)?;
        self.allow = false;
        self.scan_tag(d + 1)
    }
    fn scan_tag(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        let mut ch = self.peek(d + 1, 1)?;
        self.calls.enter(d + 1)?;
        let nonspecific = if ch == '<' {
            self.forward(d + 1, 2)?;
            self.tag_uri(d + 1)?;
            if self.peek(d + 1, 0)? != '>' {
                return Err(());
            }
            self.forward(d + 1, 1)?;
            false
        } else if blank(ch) {
            self.forward(d + 1, 1)?;
            true
        } else {
            let mut length = 1;
            let mut handle = false;
            while !blank(ch) || ch == '\t' {
                self.calls.enter(d + 1)?;
                if ch == '!' {
                    handle = true;
                    break;
                }
                length += 1;
                ch = self.peek(d + 1, length)?;
            }
            if handle {
                self.tag_handle(d + 1)?;
            } else {
                self.forward(d + 1, 1)?;
            }
            self.tag_uri(d + 1)?;
            false
        };
        if !blank(self.peek(d + 1, 0)?) || self.peek(d + 1, 0)? == '\t' {
            return Err(());
        }
        self.mark(d + 1)?;
        self.append(d, Token::Tag(nonspecific))
    }
    fn tag_handle(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        if self.peek(d + 1, 0)? != '!' {
            return Err(());
        }
        let mut n = 1;
        let mut ch = self.peek(d + 1, n)?;
        if ch != ' ' {
            while ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_') {
                self.calls.enter(d + 1)?;
                n += 1;
                ch = self.peek(d + 1, n)?;
            }
            if ch != '!' {
                return Err(());
            }
            n += 1;
        }
        self.prefix(d + 1, n)?;
        self.forward(d + 1, n)
    }
    fn tag_uri(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        let mut n = 0;
        let mut chunks = false;
        let mut ch = self.peek(d + 1, n)?;
        while ch.is_ascii_alphanumeric() || "-;/?:@&=+$,_.!~*'()[]%".contains(ch) {
            self.calls.enter(d + 1)?;
            if ch == '%' {
                self.prefix(d + 1, n)?;
                self.forward(d + 1, n)?;
                n = 0;
                self.uri_escapes(d + 1)?;
                chunks = true;
            } else {
                n += 1;
            }
            ch = self.peek(d + 1, n)?;
        }
        if n != 0 {
            self.prefix(d + 1, n)?;
            self.forward(d + 1, n)?;
            chunks = true;
        }
        if !chunks {
            return Err(());
        }
        self.calls.enter(d + 1)
    }
    fn uri_escapes(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        while self.peek(d + 1, 0)? == '%' {
            self.forward(d + 1, 1)?;
            self.calls.enter(d + 1)?;
            for i in 0..2 {
                if !self.peek(d + 1, i)?.is_ascii_hexdigit() {
                    return Err(());
                }
            }
            self.prefix(d + 1, 2)?;
            self.calls.enter(d + 1)?;
            self.forward(d + 1, 2)?;
        }
        self.calls.enter(d + 1)
    }
    fn directive(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.unwind(d + 1, -1)?;
        self.remove(d + 1)?;
        self.allow = false;
        self.scan_directive(d + 1)
    }
    fn scan_directive(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        self.forward(d + 1, 1)?;
        let name = self.directive_name(d + 1)?;
        self.calls.enter(d + 1)?;
        match name.as_str() {
            "YAML" => {
                self.yaml_directive(d + 1)?;
                self.mark(d + 1)?;
            }
            "TAG" => {
                self.tag_directive(d + 1)?;
                self.mark(d + 1)?;
            }
            _ => {
                self.mark(d + 1)?;
                while self.peek(d + 1, 0)? != '\0' && !breaks(self.peek(d + 1, 0)?) {
                    self.forward(d + 1, 1)?;
                }
            }
        }
        self.block_ignored(d + 1)?;
        self.append(d, Token::Directive)
    }
    fn directive_name(&mut self, d: usize) -> Result<String, ()> {
        self.calls.enter(d)?;
        let mut n = 0;
        let mut ch = self.peek(d + 1, n)?;
        while ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_') {
            self.calls.enter(d + 1)?;
            n += 1;
            ch = self.peek(d + 1, n)?;
        }
        if n == 0 {
            return Err(());
        }
        let name = self.prefix(d + 1, n)?;
        self.forward(d + 1, n)?;
        if !blank(self.peek(d + 1, 0)?) {
            return Err(());
        }
        Ok(name)
    }
    fn yaml_directive(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        while self.peek(d + 1, 0)? == ' ' {
            self.forward(d + 1, 1)?;
        }
        self.yaml_number(d + 1)?;
        if self.peek(d + 1, 0)? != '.' {
            return Err(());
        }
        self.forward(d + 1, 1)?;
        self.yaml_number(d + 1)?;
        if !blank(self.peek(d + 1, 0)?) {
            return Err(());
        }
        Ok(())
    }
    fn yaml_number(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        let ch = self.peek(d + 1, 0)?;
        self.calls.enter(d + 1)?;
        if !ch.is_ascii_digit() {
            return Err(());
        }
        let mut n = 0;
        while self.peek(d + 1, n)?.is_ascii_digit() {
            self.calls.enter(d + 1)?;
            n += 1;
        }
        self.prefix(d + 1, n)?;
        self.calls.enter(d + 1)?;
        self.forward(d + 1, n)
    }
    fn tag_directive(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        while self.peek(d + 1, 0)? == ' ' {
            self.forward(d + 1, 1)?;
        }
        self.calls.enter(d + 1)?;
        self.tag_handle(d + 2)?;
        if self.peek(d + 2, 0)? != ' ' {
            return Err(());
        }
        while self.peek(d + 1, 0)? == ' ' {
            self.forward(d + 1, 1)?;
        }
        self.calls.enter(d + 1)?;
        self.tag_uri(d + 2)?;
        if !blank(self.peek(d + 2, 0)?) {
            return Err(());
        }
        Ok(())
    }
    fn plain(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.save(d + 1)?;
        self.allow = false;
        self.scan_plain(d + 1)
    }
    fn scan_plain(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        let indent = self.indent + 1;
        let start = self.at;
        loop {
            let mut n = 0;
            if self.peek(d + 1, 0)? == '#' {
                break;
            }
            loop {
                let c = self.peek(d + 1, n)?;
                if blank(c)
                    || (c == ':'
                        && (blank(self.peek(d + 1, n + 1)?)
                            || (self.flow != 0
                                && matches!(
                                    self.peek(d + 1, n + 1)?,
                                    ',' | '[' | ']' | '{' | '}'
                                ))))
                    || (self.flow != 0 && matches!(c, ',' | '?' | '[' | ']' | '{' | '}'))
                {
                    break;
                }
                n += 1;
            }
            self.calls.enter(d + 1)?;
            if n == 0 {
                break;
            }
            self.allow = false;
            self.prefix(d + 1, n)?;
            self.forward(d + 1, n)?;
            self.mark(d + 1)?;
            let spaces = self.plain_spaces(d + 1)?;
            if !spaces || self.peek(d + 1, 0)? == '#' {
                break;
            }
            if self.flow == 0 {
                self.calls.enter(d + 1)?;
                if self.column < indent {
                    break;
                }
            }
        }
        if self.at == start {
            return Err(());
        }
        self.calls.enter(d + 1)?;
        self.append(d, Token::Scalar(true))
    }
    fn plain_spaces(&mut self, d: usize) -> Result<bool, ()> {
        self.calls.enter(d)?;
        let mut n = 0;
        while self.peek(d + 1, n)? == ' ' {
            n += 1;
        }
        self.prefix(d + 1, n)?;
        self.forward(d + 1, n)?;
        let c = self.peek(d + 1, 0)?;
        if breaks(c) {
            self.line_break(d + 1)?;
            self.allow = true;
            let prefix = self.prefix(d + 1, 3)?;
            self.calls.enter(d + 1)?;
            if (prefix == "---" || prefix == "...") && blank(self.peek(d + 1, 3)?) {
                return Ok(false);
            }
            while self.peek(d + 1, 0)? == ' ' || breaks(self.peek(d + 1, 0)?) {
                if self.peek(d + 1, 0)? == ' ' {
                    self.forward(d + 1, 1)?;
                } else {
                    self.line_break(d + 1)?;
                    let prefix = self.prefix(d + 1, 3)?;
                    self.calls.enter(d + 1)?;
                    if (prefix == "---" || prefix == "...") && blank(self.peek(d + 1, 3)?) {
                        return Ok(false);
                    }
                }
            }
            Ok(true)
        } else {
            Ok(n != 0)
        }
    }
    fn flow_scalar(&mut self, d: usize, quote: char) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.save(d + 1)?;
        self.allow = false;
        self.scan_flow_scalar(d + 1, quote)
    }
    fn scan_flow_scalar(&mut self, d: usize, quote: char) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.calls.enter(d + 1)?;
        self.mark(d + 1)?;
        self.peek(d + 1, 0)?;
        self.forward(d + 1, 1)?;
        self.flow_nonspaces(d + 1, quote == '"')?;
        loop {
            self.calls.enter(d + 1)?;
            if self.peek(d + 1, 0)? == quote {
                break;
            }
            self.flow_spaces(d + 1, quote == '"')?;
            self.flow_nonspaces(d + 1, quote == '"')?;
        }
        self.forward(d + 1, 1)?;
        self.mark(d + 1)?;
        self.calls.enter(d + 1)?;
        self.append(d, Token::Scalar(false))
    }
    fn flow_nonspaces(&mut self, d: usize, double: bool) -> Result<(), ()> {
        self.calls.enter(d)?;
        loop {
            let mut n = 0;
            while !matches!(
                self.peek(d + 1, n)?,
                '\'' | '"'
                    | '\\'
                    | '\0'
                    | ' '
                    | '\t'
                    | '\r'
                    | '\n'
                    | '\u{85}'
                    | '\u{2028}'
                    | '\u{2029}'
            ) {
                n += 1;
            }
            if n != 0 {
                self.prefix(d + 1, n)?;
                self.forward(d + 1, n)?;
            }
            let c = self.peek(d + 1, 0)?;
            self.calls.enter(d + 1)?;
            if !double && c == '\'' && self.peek(d + 1, 1)? == '\'' {
                self.forward(d + 1, 2)?;
            } else if (double && c == '\'') || (!double && matches!(c, '"' | '\\')) {
                self.forward(d + 1, 1)?;
            } else if double && c == '\\' {
                self.forward(d + 1, 1)?;
                let c = self.peek(d + 1, 0)?;
                if "0abt\tnvfre \"/\\N_LP".contains(c) {
                    self.forward(d + 1, 1)?;
                } else if matches!(c, 'x' | 'u' | 'U') {
                    let n = match c {
                        'x' => 2,
                        'u' => 4,
                        _ => 8,
                    };
                    self.forward(d + 1, 1)?;
                    self.calls.enter(d + 1)?;
                    for i in 0..n {
                        if !self.peek(d + 1, i)?.is_ascii_hexdigit() {
                            return Err(());
                        }
                    }
                    self.prefix(d + 1, n)?;
                    self.calls.enter(d + 1)?;
                    self.forward(d + 1, n)?;
                } else if breaks(c) {
                    self.line_break(d + 1)?;
                    self.flow_breaks(d + 1)?;
                } else {
                    return Err(());
                }
            } else {
                return Ok(());
            }
        }
    }
    fn flow_spaces(&mut self, d: usize, _double: bool) -> Result<(), ()> {
        self.calls.enter(d)?;
        let mut n = 0;
        while matches!(self.peek(d + 1, n)?, ' ' | '\t') {
            n += 1;
        }
        self.prefix(d + 1, n)?;
        self.forward(d + 1, n)?;
        let c = self.peek(d + 1, 0)?;
        self.calls.enter(d + 1)?;
        if c == '\0' {
            return Err(());
        }
        if breaks(c) {
            self.line_break(d + 1)?;
            self.flow_breaks(d + 1)?;
        }
        Ok(())
    }
    fn flow_breaks(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        loop {
            let prefix = self.prefix(d + 1, 3)?;
            self.calls.enter(d + 1)?;
            if (prefix == "---" || prefix == "...") && blank(self.peek(d + 1, 3)?) {
                return Err(());
            }
            while matches!(self.peek(d + 1, 0)?, ' ' | '\t') {
                self.forward(d + 1, 1)?;
            }
            if breaks(self.peek(d + 1, 0)?) {
                self.line_break(d + 1)?;
            } else {
                return Ok(());
            }
        }
    }
    fn block_scalar(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.allow = true;
        self.remove(d + 1)?;
        self.scan_block_scalar(d + 1)
    }
    fn scan_block_scalar(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        self.forward(d + 1, 1)?;
        let increment = self.block_indicators(d + 1)?;
        self.block_ignored(d + 1)?;
        self.calls.enter(d + 1)?;
        let minimum = (self.indent + 1).max(1);
        let indent = if let Some(n) = increment {
            let indent = minimum + n - 1;
            self.block_breaks(d + 1, indent)?;
            indent
        } else {
            let indent = self.block_indentation(d + 1)?;
            self.calls.enter(d + 1)?;
            minimum.max(indent)
        };
        loop {
            self.calls.enter(d + 1)?;
            if self.column != indent || self.peek(d + 1, 0)? == '\0' {
                break;
            }
            self.peek(d + 1, 0)?;
            let mut n = 0;
            while !breaks(self.peek(d + 1, n)?) && self.peek(d + 1, n)? != '\0' {
                n += 1;
            }
            self.prefix(d + 1, n)?;
            self.forward(d + 1, n)?;
            self.line_break(d + 1)?;
            self.block_breaks(d + 1, indent)?;
        }
        self.calls.enter(d + 1)?;
        self.append(d, Token::Scalar(false))
    }
    fn block_indicators(&mut self, d: usize) -> Result<Option<isize>, ()> {
        self.calls.enter(d)?;
        let mut increment = None;
        let c = self.peek(d + 1, 0)?;
        if matches!(c, '+' | '-') {
            self.forward(d + 1, 1)?;
            let c = self.peek(d + 1, 0)?;
            if c.is_ascii_digit() {
                self.calls.enter(d + 1)?;
                increment = c.to_digit(10).map(|v| v as isize);
                self.calls.enter(d + 1)?;
                if increment == Some(0) {
                    return Err(());
                }
                self.forward(d + 1, 1)?;
            }
        } else if c.is_ascii_digit() {
            self.calls.enter(d + 1)?;
            increment = c.to_digit(10).map(|v| v as isize);
            self.calls.enter(d + 1)?;
            if increment == Some(0) {
                return Err(());
            }
            self.forward(d + 1, 1)?;
            if matches!(self.peek(d + 1, 0)?, '+' | '-') {
                self.forward(d + 1, 1)?;
            }
        }
        if !blank(self.peek(d + 1, 0)?) || self.peek(d + 1, 0)? == '\t' {
            return Err(());
        }
        Ok(increment)
    }
    fn block_ignored(&mut self, d: usize) -> Result<(), ()> {
        self.calls.enter(d)?;
        while self.peek(d + 1, 0)? == ' ' {
            self.forward(d + 1, 1)?;
        }
        if self.peek(d + 1, 0)? == '#' {
            while !breaks(self.peek(d + 1, 0)?) && self.peek(d + 1, 0)? != '\0' {
                self.forward(d + 1, 1)?;
            }
        }
        if !breaks(self.peek(d + 1, 0)?) && self.peek(d + 1, 0)? != '\0' {
            return Err(());
        }
        self.line_break(d + 1)?;
        Ok(())
    }
    fn block_indentation(&mut self, d: usize) -> Result<isize, ()> {
        self.calls.enter(d)?;
        let mut maximum = 0;
        self.mark(d + 1)?;
        while self.peek(d + 1, 0)? == ' ' || breaks(self.peek(d + 1, 0)?) {
            self.calls.enter(d + 1)?;
            if self.peek(d + 1, 0)? != ' ' {
                self.line_break(d + 1)?;
                self.mark(d + 1)?;
            } else {
                self.forward(d + 1, 1)?;
                self.calls.enter(d + 1)?;
                maximum = maximum.max(self.column);
            }
        }
        Ok(maximum)
    }
    fn block_breaks(&mut self, d: usize, indent: isize) -> Result<(), ()> {
        self.calls.enter(d)?;
        self.mark(d + 1)?;
        loop {
            self.calls.enter(d + 1)?;
            if self.column >= indent || self.peek(d + 1, 0)? != ' ' {
                break;
            }
            self.forward(d + 1, 1)?;
        }
        while breaks(self.peek(d + 1, 0)?) {
            self.line_break(d + 1)?;
            self.mark(d + 1)?;
            loop {
                self.calls.enter(d + 1)?;
                if self.column >= indent || self.peek(d + 1, 0)? != ' ' {
                    break;
                }
                self.forward(d + 1, 1)?;
            }
        }
        Ok(())
    }
}
