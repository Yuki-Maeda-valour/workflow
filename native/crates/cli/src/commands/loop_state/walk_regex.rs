// CPython 3.10 call-order adaptation; see CPYTHON-LICENSE in this directory.
//! Virtual calls for CPython 3.10's generated-glob regex subset. The actual
//! boolean matcher remains R4S. This follows sre_parse/sre_compile control
//! flow solely to preserve recursion exceptions and their evaluation order.
use super::{error::Result, walk_recursion::Calls};
#[derive(Clone)]
enum Node {
    Literal,
    NotLiteral,
    Any,
    At,
    Group(Vec<Node>),
    Branch(Vec<Vec<Node>>),
    Repeat(Vec<Node>),
}
fn frame<T>(
    result: Result<T>,
    module: &'static str,
    function: &'static str,
    line: u32,
    source: &'static str,
) -> Result<T> {
    result.map_err(|e| e.external(module, function, line, source))
}
fn p<T>(result: Result<T>, function: &'static str, line: u32, source: &'static str) -> Result<T> {
    frame(result, "sre_parse.py", function, line, source)
}
fn c<T>(result: Result<T>, function: &'static str, line: u32, source: &'static str) -> Result<T> {
    frame(result, "sre_compile.py", function, line, source)
}
fn instance(calls: Calls) -> Result<()> {
    // type_call's temporary entry precedes the Python __init__ entry.
    calls.builtin()?;
    Calls(calls.0 + 1).enter()?;
    Ok(())
}
fn append(calls: Calls) -> Result<()> {
    let calls = calls.enter()?;
    p(calls.builtin(), "append", 174, "self.data.append(code)")
}
fn length(calls: Calls, builtin: bool) -> Result<()> {
    let calls = if builtin {
        calls.builtin()?;
        Calls(calls.0 + 1)
    } else {
        calls
    };
    let calls = calls.enter()?;
    p(calls.builtin(), "__len__", 162, "return len(self.data)")
}
fn item(calls: Calls, slice: bool) -> Result<()> {
    let calls = calls.enter()?;
    p(
        calls.builtin(),
        "__getitem__",
        166,
        "if isinstance(index, slice):",
    )?;
    if slice {
        p(
            instance(calls),
            "__getitem__",
            167,
            "return SubPattern(self.state, self.data[index])",
        )?;
    }
    Ok(())
}
fn setitem(calls: Calls) -> Result<()> {
    calls.enter()?;
    Ok(())
}
struct Tokens<'a> {
    codes: &'a [u32],
    index: usize,
    next: Option<Vec<u32>>,
}
impl Tokens<'_> {
    fn advance(&mut self, parent: Calls) -> Result<()> {
        let calls = parent.enter()?;
        let Some(&ch) = self.codes.get(self.index) else {
            self.next = None;
            return Ok(());
        };
        p(
            calls.compare(),
            "__next",
            241,
            r#"if char == "\\": "#.trim_end(),
        )?;
        let mut token = vec![ch];
        self.index += 1;
        if ch == 92 {
            token.push(self.codes[self.index]);
            self.index += 1;
        }
        self.next = Some(token);
        Ok(())
    }
    fn get(&mut self, parent: Calls) -> Result<Option<Vec<u32>>> {
        let calls = parent.enter()?;
        let token = self.next.clone();
        p(self.advance(calls), "get", 257, "self.__next()")?;
        Ok(token)
    }
    fn matches(&mut self, parent: Calls, ch: u32) -> Result<bool> {
        let calls = parent.enter()?;
        p(calls.compare(), "match", 251, "if char == self.next:")?;
        if self.next.as_deref() == Some(&[ch]) {
            p(self.advance(calls), "match", 252, "self.__next()")?;
            Ok(true)
        } else {
            Ok(false)
        }
    }
    fn tell(&self, parent: Calls) -> Result<()> {
        let calls = parent.enter()?;
        p(
            calls.builtin(),
            "tell",
            288,
            "return self.index - len(self.next or '')",
        )
    }
}
fn sub(parent: Calls, tokens: &mut Tokens<'_>) -> Result<Vec<Node>> {
    let calls = parent.enter()?;
    p(
        tokens.tell(calls),
        "_parse_sub",
        442,
        "start = source.tell()",
    )?;
    let mut branches = Vec::new();
    loop {
        let nodes = p(
            parse(calls, tokens),
            "_parse_sub",
            444,
            "itemsappend(_parse(source, state, verbose, nested + 1,",
        )?;
        p(
            calls.builtin(),
            "_parse_sub",
            444,
            "itemsappend(_parse(source, state, verbose, nested + 1,",
        )?;
        branches.push(nodes);
        if !p(
            tokens.matches(calls, 124),
            "_parse_sub",
            446,
            r#"if not sourcematch("|"):"#,
        )? {
            break;
        }
    }
    p(
        calls.builtin().and_then(|_| calls.compare()),
        "_parse_sub",
        449,
        "if len(items) == 1:",
    )?;
    if branches.len() == 1 {
        return Ok(branches.pop().unwrap());
    }
    p(
        instance(calls),
        "_parse_sub",
        452,
        "subpattern = SubPattern(state)",
    )?;
    for (i, _) in branches.iter().enumerate() {
        p(length(calls, false), "_parse_sub", 458, "if not item:")?;
        p(item(calls, false), "_parse_sub", 461, "prefix = item[0]")?;
        if i > 0 {
            p(
                calls.compare().and_then(|_| Calls(calls.0 + 1).compare()),
                "_parse_sub",
                462,
                "elif item[0] != prefix:",
            )?;
            break;
        }
    }
    for nodes in &branches {
        p(
            length(calls, true).and_then(|_| calls.compare()),
            "_parse_sub",
            476,
            "if len(item) != 1:",
        )?;
        if nodes.len() != 1 {
            break;
        }
        p(item(calls, false), "_parse_sub", 478, "op, av = item[0]")?;
        if matches!(nodes[0], Node::At) {
            break;
        }
    }
    p(
        append(calls),
        "_parse_sub",
        491,
        "subpattern.append((BRANCH, (None, items)))",
    )?;
    Ok(vec![Node::Branch(branches)])
}
fn parse(parent: Calls, tokens: &mut Tokens<'_>) -> Result<Vec<Node>> {
    let calls = parent.enter()?;
    p(
        instance(calls),
        "_parse",
        496,
        "subpattern = SubPattern(state)",
    )?;
    let mut nodes = Vec::new();
    while let Some(this) = tokens.next.clone() {
        if this.len() == 1 && matches!(this[0], 124 | 41) {
            break;
        }
        p(tokens.get(calls), "_parse", 512, "sourceget()")?;
        let node = match this[0] {
            92 => {
                let escape = p(
                    calls.enter(),
                    "_parse",
                    526,
                    "code = _escape(source, this, state)",
                )?;
                p(
                    escape.builtin(),
                    "_escape",
                    358,
                    "code = CATEGORIES.get(escape)",
                )?;
                p(
                    escape.builtin(),
                    "_escape",
                    361,
                    "code = ESCAPES.get(escape)",
                )?;
                if this[1] != 92 {
                    p(
                        escape.builtin().and_then(|_| escape.compare()),
                        "_escape",
                        425,
                        "if len(escape) == 2:",
                    )?;
                    p(
                        escape.builtin(),
                        "_escape",
                        428,
                        "return LITERAL, ord(escape[1])",
                    )?;
                }
                Node::Literal
            }
            91 => {
                p(
                    tokens.tell(calls),
                    "_parse",
                    533,
                    "here = source.tell() - 1",
                )?;
                p(
                    tokens.matches(calls, 94),
                    "_parse",
                    545,
                    r#"negate = sourcematch("^")"#,
                )?;
                loop {
                    let token = p(tokens.get(calls), "_parse", 548, "this = sourceget()")?.unwrap();
                    if token == [93] {
                        break;
                    }
                    p(
                        calls.builtin(),
                        "_parse",
                        568,
                        "code1 = LITERAL, _ord(this)",
                    )?;
                    p(
                        tokens.matches(calls, 45),
                        "_parse",
                        569,
                        r#"if sourcematch("-"):"#,
                    )?;
                    p(calls.builtin(), "_parse", 604, "setappend(code1)")?;
                }
                let uniq = p(calls.enter(), "_parse", 606, "set = _uniq(set)")?;
                p(
                    uniq.builtin(),
                    "_uniq",
                    434,
                    "return list(dict.fromkeys(items))",
                )?;
                p(
                    calls.builtin().and_then(|_| calls.compare()),
                    "_parse",
                    608,
                    "if _len(set) == 1 and set[0][0] is LITERAL:",
                )?;
                Node::NotLiteral
            }
            42 | 63 => {
                p(tokens.tell(calls), "_parse", 623, "here = source.tell()")?;
                p(length(calls, false), "_parse", 664, "if subpattern:")?;
                p(item(calls, true), "_parse", 665, "item = subpattern[-1:]")?;
                p(
                    length(calls, false),
                    "_parse",
                    668,
                    "if not item or item[0][0] is AT:",
                )?;
                for line in [668, 671, 674] {
                    p(
                        item(calls, false),
                        "_parse",
                        line,
                        match line {
                            668 => "if not item or item[0][0] is AT:",
                            671 => "if item[0][0] in _REPEATCODES:",
                            _ => "if item[0][0] is SUBPATTERN:",
                        },
                    )?;
                }
                let previous = nodes.pop().unwrap();
                let body = if let Node::Group(body) = previous {
                    p(
                        item(calls, false),
                        "_parse",
                        675,
                        "group, add_flags, del_flags, p = item[0][1]",
                    )?;
                    body
                } else {
                    vec![previous]
                };
                p(
                    tokens.matches(calls, 63),
                    "_parse",
                    678,
                    r#"if sourcematch("?"):"#,
                )?;
                p(
                    setitem(calls),
                    "_parse",
                    681,
                    "subpattern[-1] = (MAX_REPEAT, (min, max, item))",
                )?;
                nodes.push(Node::Repeat(body));
                continue;
            }
            46 => Node::Any,
            40 => {
                p(
                    tokens.tell(calls),
                    "_parse",
                    687,
                    "start = source.tell() - 1",
                )?;
                p(
                    tokens.matches(calls, 63),
                    "_parse",
                    692,
                    r#"if sourcematch("?"):"#,
                )?;
                p(tokens.get(calls), "_parse", 694, "char = sourceget()")?;
                let body = p(
                    sub(calls, tokens),
                    "_parse",
                    841,
                    "p = _parse_sub(source, state, sub_verbose, nested + 1)",
                )?;
                p(
                    tokens.matches(calls, 41),
                    "_parse",
                    842,
                    r#"if not source.match(")"):"#,
                )?;
                Node::Group(body)
            }
            94 | 36 => Node::At,
            _ => {
                p(
                    calls.builtin(),
                    "_parse",
                    530,
                    "subpatternappend((LITERAL, _ord(this)))",
                )?;
                Node::Literal
            }
        };
        p(
            append(calls),
            "_parse",
            match this[0] {
                92 => 527,
                91 => 611,
                46 => 684,
                40 => 847,
                94 => 850,
                36 => 853,
                _ => 530,
            },
            match this[0] {
                92 => "subpatternappend(code)",
                91 => "subpatternappend((NOT_LITERAL, set[0][1]))",
                46 => "subpatternappend((ANY, None))",
                40 => "subpatternappend((SUBPATTERN, (group, add_flags, del_flags, p)))",
                94 => "subpatternappend((AT, AT_BEGINNING))",
                36 => "subpatternappend((AT, AT_END))",
                _ => "subpatternappend((LITERAL, _ord(this)))",
            },
        )?;
        nodes.push(node);
    }
    p(
        length(calls, true),
        "_parse",
        859,
        "for i in range(len(subpattern))[::-1]:",
    )?;
    for index in (0..nodes.len()).rev() {
        p(item(calls, false), "_parse", 860, "op, av = subpattern[i]")?;
        if let Node::Group(body) = nodes[index].clone() {
            p(setitem(calls), "_parse", 864, "subpattern[i: i+1] = p")?;
            nodes.splice(index..index + 1, body);
        }
    }
    Ok(nodes)
}
// The generated grammar contains no captures, lookarounds or general charsets.
// Traverse widths and emitted bodies in the same order as sre_compile.
fn width(parent: Calls, nodes: &[Node]) -> Result<usize> {
    let calls = parent.enter()?;
    let mut minimum = 0;
    for node in nodes {
        minimum += match node {
            Node::Literal | Node::NotLiteral | Node::Any => 1,
            Node::At => 0,
            Node::Group(body) => p(
                width(calls, body),
                "getwidth",
                195,
                "i, j = av[-1].getwidth()",
            )?,
            Node::Repeat(body) => {
                p(
                    width(calls, body),
                    "getwidth",
                    199,
                    "i, j = av[2].getwidth()",
                )?;
                0
            }
            Node::Branch(branches) => {
                let mut smallest = usize::MAX;
                for body in branches {
                    smallest = smallest.min(p(
                        width(calls, body),
                        "getwidth",
                        185,
                        "l, h = av.getwidth()",
                    )?);
                    p(calls.builtin(), "getwidth", 186, "i = min(i, l)")?;
                    p(calls.builtin(), "getwidth", 187, "j = max(j, h)")?;
                }
                smallest
            }
        };
    }
    p(
        calls.builtin(),
        "getwidth",
        221,
        "self.width = min(lo, MAXREPEAT - 1), min(hi, MAXREPEAT)",
    )?;
    p(
        calls.builtin(),
        "getwidth",
        221,
        "self.width = min(lo, MAXREPEAT - 1), min(hi, MAXREPEAT)",
    )?;
    Ok(minimum)
}
fn compile_nodes(parent: Calls, nodes: &[Node], subpattern: bool) -> Result<()> {
    let calls = parent.enter()?;
    for node in nodes {
        if subpattern {
            c(
                item(calls, false),
                "_compile",
                106,
                "for op, av in pattern:",
            )?;
        }
        match node {
            Node::Repeat(body) => {
                let simple = c(calls.enter(), "_compile", 156, "if _simple(av[2]):")?;
                c(length(simple, true), "_simple", 449, "if len(p) != 1:")?;
                if body.len() == 1 {
                    c(item(simple, false), "_simple", 451, "op, av = p[0]")?;
                }
                c(
                    compile_nodes(calls, body, true),
                    "_compile",
                    if body.len() == 1
                        && matches!(body[0], Node::Literal | Node::NotLiteral | Node::Any)
                    {
                        164
                    } else {
                        172
                    },
                    "_compile(code, av[2], flags)",
                )?;
            }
            Node::Branch(branches) => {
                for body in branches {
                    c(
                        compile_nodes(calls, body, true),
                        "_compile",
                        225,
                        "_compile(code, av, flags)",
                    )?;
                }
            }
            Node::Group(body) => {
                c(
                    compile_nodes(calls, body, true),
                    "_compile",
                    184,
                    "_compile(code, p, _combine_flags(flags, add_flags, del_flags))",
                )?;
            }
            Node::Literal | Node::NotLiteral => {
                c(calls.builtin(), "_compile", 114, "elif not iscased(av):")?;
                // tolower/fold emission only follows a successful iscased
                // call at this same depth. It cannot be the first overflow.
            }
            _ => {}
        }
    }
    if subpattern {
        c(
            item(calls, false),
            "_compile",
            106,
            "for op, av in pattern:",
        )?;
    }
    Ok(())
}
fn parse_regex(parent: Calls, regex: &[u32]) -> Result<Vec<Node>> {
    let calls = parent.enter()?;
    let mut tokens = Tokens {
        codes: regex,
        index: 0,
        next: None,
    };
    p(calls.builtin(), "parse", 947, "source = Tokenizer(str)")?;
    let init = p(
        Calls(calls.0 + 1).enter(),
        "parse",
        947,
        "source = Tokenizer(str)",
    )?;
    p(
        p(
            init.builtin(),
            "__init__",
            226,
            "self.istext = isinstance(string, str)",
        ),
        "parse",
        947,
        "source = Tokenizer(str)",
    )?;
    p(
        p(tokens.advance(init), "__init__", 233, "self.__next()"),
        "parse",
        947,
        "source = Tokenizer(str)",
    )?;
    p(instance(calls), "parse", 950, "state = State()")?;
    let nodes = p(
        sub(calls, &mut tokens),
        "parse",
        955,
        "p = _parse_sub(source, state, flags & SRE_FLAG_VERBOSE, 0)",
    )?;
    p(
        calls.enter()?.builtin(),
        "parse",
        965,
        "p.state.flags = fix_flags(str, p.state.flags)",
    )?;
    Ok(nodes)
}
fn code(parent: Calls, nodes: &[Node]) -> Result<()> {
    let calls = parent.enter()?;
    let info = || -> Result<()> {
        let info = calls.enter()?;
        let minimum = c(
            width(info, nodes),
            "_compile_info",
            564,
            "lo, hi = pattern.getwidth()",
        )?;
        if minimum > 0 {
            let literal = c(
                info.enter(),
                "_compile_info",
                576,
                "prefix, prefix_skip, got_all = _get_literal_prefix(pattern, flags)",
            )?;
            c(
                literal.enter(),
                "_get_literal_prefix",
                490,
                "iscased = _get_iscased(flags)",
            )?;
            // All generated patterns start with AT or BRANCH. Neither is a
            // literal prefix; this traverses pattern.data, a plain list.
            let charset = c(
                info.enter(),
                "_compile_info",
                579,
                "charset = _get_charset_prefix(pattern, flags)",
            )?;
            c(
                charset.enter(),
                "_get_charset_prefix",
                528,
                "iscased = _get_iscased(flags)",
            )?;
            if let Some(Node::Branch(branches)) = nodes.first() {
                let first = &branches[0];
                c(
                    length(charset, false),
                    "_get_charset_prefix",
                    537,
                    "if not p:",
                )?;
                if !first.is_empty() {
                    c(
                        item(charset, false),
                        "_get_charset_prefix",
                        539,
                        "op, av = p[0]",
                    )?;
                }
            }
        }
        Ok(())
    };
    c(info(), "_code", 628, "_compile_info(code, p, flags)")?;
    c(
        compile_nodes(calls, nodes, false),
        "_code",
        631,
        "_compile(code, p.data, flags)",
    )?;
    Ok(())
}
fn is_string(parent: Calls) -> Result<()> {
    let calls = parent.enter()?;
    c(
        calls
            .builtin()
            .and_then(|_| Calls(calls.0 + 1).check_instance()),
        "isstring",
        620,
        "return isinstance(obj, (str, bytes))",
    )
}
fn compiled(parent: Calls, regex: &[u32]) -> Result<()> {
    let calls = parent.enter()?;
    c(is_string(calls), "compile", 786, "if isstring(p):")?;

    let nodes = c(
        parse_regex(calls, regex),
        "compile",
        788,
        "p = sre_parse.parse(p, flags)",
    )?;
    c(
        code(calls, &nodes),
        "compile",
        792,
        "code = _code(p, flags)",
    )?;
    // State.groups is a C property descriptor calling its Python getter;
    // it does not introduce the Python __get__ used by RegexFlag.value.
    for (line, source) in [
        (800, "indexgroup = [None] * p.state.groups"),
        (806, "p.state.groups-1,"),
    ] {
        let groups = || -> Result<()> {
            let groups = calls.enter()?;
            p(
                groups.builtin(),
                "groups",
                84,
                "return len(self.groupwidths)",
            )
        };
        c(groups(), "compile", line, source)?;
        if line == 800 {
            c(
                calls.builtin(),
                "compile",
                801,
                "for k, i in groupindex.items():",
            )?;
        }
    }
    c(
        calls.builtin(),
        "compile",
        807,
        "groupindex, tuple(indexgroup)",
    )?;
    c(calls.builtin(), "compile", 804, "return _sre.compile(")?;
    Ok(())
}
pub fn miss(parent: Calls, regex: &[u32]) -> Result<()> {
    // parent is re._compile; isstring runs before sre_compile.compile.
    frame(
        is_string(parent),
        "re.py",
        "_compile",
        301,
        "if not sre_compile.isstring(pattern):",
    )?;
    frame(
        compiled(parent, regex),
        "re.py",
        "_compile",
        303,
        "p = sre_compile.compile(pattern, flags)",
    )?;
    // These two EnumMeta calls are sequential. Its Python heap metaclass
    // uses slot_tp_call: C guard +1, __call__ +1, __new__ +1.
    let and = frame(
        parent.enter(),
        "re.py",
        "_compile",
        304,
        "if not (flags & DEBUG):",
    )?;
    frame(
        frame(
            and.builtin()
                .and_then(|_| Calls(and.0 + 1).check_instance())
                .and_then(|_| Calls(and.0 + 2).check_instance())
                .and_then(|_| Calls(and.0 + 3).builtin()),
            "enum.py",
            "__and__",
            987,
            "if not isinstance(other, (self.__class__, int)):",
        ),
        "re.py",
        "_compile",
        304,
        "if not (flags & DEBUG):",
    )?;
    for _ in 0..2 {
        let call = || -> Result<()> {
            and.builtin()?;
            let meta = Calls(and.0 + 1).enter()?;
            // type(value) has no additional recursion entry.
            let new = meta.enter().map(|_| ());
            frame(
                new,
                "enum.py",
                "__call__",
                385,
                "return cls.__new__(cls, value)",
            )
        };
        frame(
            frame(
                call(),
                "enum.py",
                "__and__",
                989,
                "return self.__class__(self._value_ & self.__class__(other)._value_)",
            ),
            "re.py",
            "_compile",
            304,
            "if not (flags & DEBUG):",
        )?;
    }
    Ok(())
}
