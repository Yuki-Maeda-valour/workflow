//! Python 3.10 `json.loads(str)` 用の R4A AST と parser。

#[derive(Clone, Debug, PartialEq)]
pub enum PyJson {
    Null,
    Bool(bool),
    Int { negative: bool, digits: Vec<u8> },
    Float(f64),
    String(Vec<u32>),
    Array(Vec<PyJson>),
    Object(Vec<(Vec<u32>, PyJson)>),
}
#[derive(Clone, Copy, Debug)]
pub struct JsonPolicy {
    pub int_max_digits: usize,
    pub depth: usize,
}
#[derive(Clone, Debug, PartialEq)]
pub enum JsonFailure {
    Decode {
        message: &'static str,
        position: usize,
        line: usize,
        column: usize,
    },
    IntDigits {
        limit: usize,
        count: usize,
    },
    Recursion {
        operation: &'static str,
    },
}
impl Default for JsonPolicy {
    fn default() -> Self {
        Self {
            int_max_digits: 4300,
            depth: 994,
        }
    }
}

struct P<'a> {
    s: &'a [u32],
    at: usize,
    policy: JsonPolicy,
    containers: usize,
}
impl<'a> P<'a> {
    fn err<T>(&self, m: &'static str, p: usize) -> Result<T, JsonFailure> {
        let (mut l, mut c) = (1, 1);
        for x in &self.s[..p] {
            if *x == 10 {
                l += 1;
                c = 1
            } else {
                c += 1
            }
        }
        Err(JsonFailure::Decode {
            message: m,
            position: p,
            line: l,
            column: c,
        })
    }
    fn ws(&mut self) {
        while matches!(self.s.get(self.at), Some(32 | 9 | 10 | 13)) {
            self.at += 1
        }
    }
    fn value(&mut self) -> Result<PyJson, JsonFailure> {
        self.ws();
        match self.s.get(self.at).copied() {
            Some(34) => self.string().map(PyJson::String),
            Some(91) => self.array(),
            Some(123) => self.object(),
            Some(116) if self.word(b"true") => Ok(PyJson::Bool(true)),
            Some(102) if self.word(b"false") => Ok(PyJson::Bool(false)),
            Some(110) if self.word(b"null") => Ok(PyJson::Null),
            Some(78) if self.word(b"NaN") => self.constant(f64::NAN),
            Some(73) if self.word(b"Infinity") => self.constant(f64::INFINITY),
            Some(45) if self.s.get(self.at + 1) == Some(&73) && self.word(b"-Infinity") => {
                self.constant(f64::NEG_INFINITY)
            }
            Some(45) | Some(48..=57) => self.number(),
            _ => self.err("Expecting value", self.at),
        }
    }
    fn word(&mut self, w: &[u8]) -> bool {
        if self.s[self.at..]
            .iter()
            .zip(w)
            .all(|(a, b)| *a == u32::from(*b))
            && self.s.len() - self.at >= w.len()
        {
            self.at += w.len();
            true
        } else {
            false
        }
    }
    fn constant(&self, value: f64) -> Result<PyJson, JsonFailure> {
        // CPython calls the parse_constant hook for these spellings.  Its
        // recursion guard therefore also applies to a constant at the end of
        // an otherwise valid 994-container input.
        if self.containers >= self.policy.depth {
            return Err(JsonFailure::Recursion {
                operation: "calling a Python object",
            });
        }
        Ok(PyJson::Float(value))
    }
    fn string(&mut self) -> Result<Vec<u32>, JsonFailure> {
        let start = self.at;
        self.at += 1;
        let mut v = vec![];
        while let Some(&x) = self.s.get(self.at) {
            self.at += 1;
            if x == 34 {
                return Ok(v);
            }
            if x < 32 {
                return self.err("Invalid control character at", self.at - 1);
            }
            if x != 92 {
                v.push(x);
                continue;
            }
            let p = self.at - 1;
            let Some(&e) = self.s.get(self.at) else {
                return self.err("Unterminated string starting at", start);
            };
            self.at += 1;
            match e {
                34 | 92 | 47 => v.push(e),
                98 => v.push(8),
                102 => v.push(12),
                110 => v.push(10),
                114 => v.push(13),
                116 => v.push(9),
                117 => {
                    let a = self.at;
                    if a + 4 > self.s.len()
                        || self.s[a..a + 4]
                            .iter()
                            .any(|x| !matches!(x, 48..=57 | 65..=70 | 97..=102))
                    {
                        return self.err("Invalid \\uXXXX escape", a - 1);
                    }
                    let n = self.s[a..a + 4].iter().fold(0, |n, x| {
                        n * 16
                            + match x {
                                48..=57 => x - 48,
                                65..=70 => x - 55,
                                _ => x - 87,
                            }
                    });
                    self.at += 4;
                    if (0xd800..=0xdbff).contains(&n)
                        && self.s.get(self.at) == Some(&92)
                        && self.s.get(self.at + 1) == Some(&117)
                        && self.at + 6 <= self.s.len()
                        && self.s[self.at + 2..self.at + 6]
                            .iter()
                            .all(|x| matches!(x, 48..=57 | 65..=70 | 97..=102))
                    {
                        let m = self.s[self.at + 2..self.at + 6].iter().fold(0, |n, x| {
                            n * 16
                                + match x {
                                    48..=57 => x - 48,
                                    65..=70 => x - 55,
                                    _ => x - 87,
                                }
                        });
                        if (0xdc00..=0xdfff).contains(&m) {
                            self.at += 6;
                            v.push(0x10000 + ((n - 0xd800) << 10) + m - 0xdc00);
                            if self.at == self.s.len() {
                                return self.err("Invalid \\uXXXX escape", a + 5);
                            }
                        } else {
                            v.push(n)
                        }
                    } else {
                        v.push(n)
                    }
                    if self.at == self.s.len() {
                        return self.err("Invalid \\uXXXX escape", a - 1);
                    }
                }
                _ => return self.err("Invalid \\escape", p),
            }
        }
        self.err("Unterminated string starting at", start)
    }
    fn number(&mut self) -> Result<PyJson, JsonFailure> {
        let start = self.at;
        let neg = self.s.get(self.at) == Some(&45);
        if neg {
            self.at += 1
        }
        if !matches!(self.s.get(self.at), Some(48..=57)) {
            return self.err("Expecting value", start);
        }
        if self.s.get(self.at) == Some(&48) {
            self.at += 1;
        } else {
            while matches!(self.s.get(self.at), Some(48..=57)) {
                self.at += 1
            }
        }
        let integer_end = self.at;
        let digits: Vec<u8> = self.s[start + usize::from(neg)..integer_end]
            .iter()
            .map(|x| *x as u8)
            .collect();
        let mut float = false;
        if self.s.get(self.at) == Some(&46) && matches!(self.s.get(self.at + 1), Some(48..=57)) {
            float = true;
            self.at += 1;
            while matches!(self.s.get(self.at), Some(48..=57)) {
                self.at += 1;
            }
        }
        if matches!(self.s.get(self.at), Some(101 | 69)) {
            let exponent_start = self.at;
            let mut exponent = self.at + 1;
            if matches!(self.s.get(exponent), Some(43 | 45)) {
                exponent += 1;
            }
            if matches!(self.s.get(exponent), Some(48..=57)) {
                float = true;
                self.at = exponent + 1;
                while matches!(self.s.get(self.at), Some(48..=57)) {
                    self.at += 1;
                }
            } else {
                self.at = exponent_start;
            }
        }
        if float {
            let text: String = self.s[start..self.at]
                .iter()
                .filter_map(|x| char::from_u32(*x))
                .collect();
            return Ok(PyJson::Float(
                text.parse::<f64>().expect("validated JSON number"),
            ));
        }
        if self.policy.int_max_digits != 0 && digits.len() > self.policy.int_max_digits {
            return Err(JsonFailure::IntDigits {
                limit: self.policy.int_max_digits,
                count: digits.len(),
            });
        }
        Ok(PyJson::Int {
            negative: neg && digits != b"0",
            digits,
        })
    }
    fn array(&mut self) -> Result<PyJson, JsonFailure> {
        self.containers += 1;
        if self.containers > self.policy.depth {
            return Err(JsonFailure::Recursion { operation: "array" });
        }
        self.at += 1;
        let mut v = vec![];
        self.ws();
        if self.s.get(self.at) == Some(&93) {
            self.at += 1;
            self.containers -= 1;
            return Ok(PyJson::Array(v));
        }
        loop {
            v.push(self.value()?);
            self.ws();
            match self.s.get(self.at) {
                Some(44) => self.at += 1,
                Some(93) => {
                    self.at += 1;
                    break;
                }
                _ => return self.err("Expecting ',' delimiter", self.at),
            }
        }
        self.containers -= 1;
        Ok(PyJson::Array(v))
    }
    fn object(&mut self) -> Result<PyJson, JsonFailure> {
        self.containers += 1;
        if self.containers > self.policy.depth {
            return Err(JsonFailure::Recursion {
                operation: "object",
            });
        }
        self.at += 1;
        let mut v = vec![];
        self.ws();
        if self.s.get(self.at) == Some(&125) {
            self.at += 1;
            self.containers -= 1;
            return Ok(PyJson::Object(v));
        }
        loop {
            self.ws();
            if self.s.get(self.at) != Some(&34) {
                return self.err("Expecting property name enclosed in double quotes", self.at);
            }
            let k = self.string()?;
            self.ws();
            if self.s.get(self.at) != Some(&58) {
                return self.err("Expecting ':' delimiter", self.at);
            }
            self.at += 1;
            let x = self.value()?;
            if let Some((_, old)) = v.iter_mut().find(|(a, _)| *a == k) {
                *old = x
            } else {
                v.push((k, x))
            };
            self.ws();
            match self.s.get(self.at) {
                Some(44) => self.at += 1,
                Some(125) => {
                    self.at += 1;
                    break;
                }
                _ => return self.err("Expecting ',' delimiter", self.at),
            }
        }
        self.containers -= 1;
        Ok(PyJson::Object(v))
    }
}
/// Python の `repr(value)`(JSON から作った値)。
pub fn py_repr(value: &PyJson) -> Vec<u32> {
    let mut out = Vec::new();
    repr_into(&mut out, value);
    out
}

/// Python の `str(value)`(文字列はそのまま・ほかは `repr`)。
pub fn py_str(value: &PyJson) -> Vec<u32> {
    match value {
        PyJson::String(text) => text.clone(),
        other => py_repr(other),
    }
}

fn repr_into(out: &mut Vec<u32>, value: &PyJson) {
    match value {
        PyJson::Null => push_ascii(out, "None"),
        PyJson::Bool(true) => push_ascii(out, "True"),
        PyJson::Bool(false) => push_ascii(out, "False"),
        PyJson::Int { negative, digits } => {
            if *negative {
                out.push(45);
            }
            out.extend(digits.iter().map(|d| u32::from(*d)));
        }
        PyJson::Float(f) => push_ascii(out, &float_repr(*f)),
        PyJson::String(text) => out.extend(super::chars::repr(text)),
        PyJson::Array(items) => {
            out.push(91);
            for (at, item) in items.iter().enumerate() {
                if at > 0 {
                    push_ascii(out, ", ");
                }
                repr_into(out, item);
            }
            out.push(93);
        }
        PyJson::Object(entries) => {
            out.push(123);
            for (at, (key, item)) in entries.iter().enumerate() {
                if at > 0 {
                    push_ascii(out, ", ");
                }
                out.extend(super::chars::repr(key));
                push_ascii(out, ": ");
                repr_into(out, item);
            }
            out.push(125);
        }
    }
}

impl PyJson {
    pub fn str(text: &str) -> Self {
        Self::String(text.chars().map(u32::from).collect())
    }
    pub fn int(value: usize) -> Self {
        Self::Int {
            negative: false,
            digits: value.to_string().into_bytes(),
        }
    }
    pub fn object(entries: Vec<(&str, PyJson)>) -> Self {
        Self::Object(
            entries
                .into_iter()
                .map(|(key, value)| (key.chars().map(u32::from).collect(), value))
                .collect(),
        )
    }
}

impl JsonFailure {
    /// Python の例外の種類名。
    pub fn exception_name(&self) -> &'static str {
        match self {
            Self::Decode { .. } => "json.decoder.JSONDecodeError",
            Self::IntDigits { .. } => "ValueError",
            Self::Recursion { .. } => "RecursionError",
        }
    }

    /// `str(exception)`。
    pub fn message(&self) -> String {
        match self {
            Self::Decode {
                message,
                position,
                line,
                column,
            } => format!("{message}: line {line} column {column} (char {position})"),
            Self::IntDigits { limit, count } => format!(
                "Exceeds the limit ({limit}) for integer string conversion: value has {count} digits; use sys.set_int_max_str_digits() to increase the limit"
            ),
            Self::Recursion { operation } => match *operation {
                "array" => "maximum recursion depth exceeded while decoding a JSON array from a unicode string".into(),
                "object" => "maximum recursion depth exceeded while decoding a JSON object from a unicode string".into(),
                other => format!("maximum recursion depth exceeded while {other}"),
            },
        }
    }

    /// `ValueError`(JSONDecodeError を含む)として捕まるか。
    pub fn is_value_error(&self) -> bool {
        !matches!(self, Self::Recursion { .. })
    }
}

/// Python の `float.__repr__`。
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.into();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if value == 0.0 {
        return format!("{sign}0.0");
    }
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific.split_once('e').expect("exponent");
    let exponent: i32 = exponent.parse().expect("exponent number");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let decimal_point = exponent + 1;
    if -4 < decimal_point && decimal_point <= 16 {
        if decimal_point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat((-decimal_point) as usize))
        } else if digits.len() as i32 <= decimal_point {
            format!(
                "{sign}{digits}{}.0",
                "0".repeat((decimal_point - digits.len() as i32) as usize)
            )
        } else {
            let (head, tail) = digits.split_at(decimal_point as usize);
            format!("{sign}{head}.{tail}")
        }
    } else {
        let (head, tail) = digits.split_at(1);
        let fraction = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        format!(
            "{sign}{head}{fraction}e{}{:02}",
            if exponent < 0 { '-' } else { '+' },
            exponent.abs()
        )
    }
}

fn push_ascii(out: &mut Vec<u32>, text: &str) {
    out.extend(text.bytes().map(u32::from));
}

fn dump_string(out: &mut Vec<u32>, text: &[u32]) {
    out.push(34);
    for &code in text {
        match code {
            34 => push_ascii(out, "\\\""),
            92 => push_ascii(out, "\\\\"),
            10 => push_ascii(out, "\\n"),
            13 => push_ascii(out, "\\r"),
            9 => push_ascii(out, "\\t"),
            8 => push_ascii(out, "\\b"),
            12 => push_ascii(out, "\\f"),
            0..=31 => push_ascii(out, &format!("\\u{code:04x}")),
            _ => out.push(code),
        }
    }
    out.push(34);
}

/// `json.dumps(value, ensure_ascii=False[, sort_keys=True])`。区切りは `", "` と `": "`。
pub fn dumps(value: &PyJson, sort_keys: bool) -> Vec<u32> {
    let mut out = Vec::new();
    dump_value(&mut out, value, sort_keys);
    out
}

fn dump_value(out: &mut Vec<u32>, value: &PyJson, sort_keys: bool) {
    match value {
        PyJson::Null => push_ascii(out, "null"),
        PyJson::Bool(true) => push_ascii(out, "true"),
        PyJson::Bool(false) => push_ascii(out, "false"),
        PyJson::Int { negative, digits } => {
            if *negative {
                out.push(45);
            }
            out.extend(digits.iter().map(|d| u32::from(*d)));
        }
        PyJson::Float(f) => {
            if f.is_nan() {
                push_ascii(out, "NaN");
            } else if f.is_infinite() {
                push_ascii(out, if *f < 0.0 { "-Infinity" } else { "Infinity" });
            } else {
                push_ascii(out, &float_repr(*f));
            }
        }
        PyJson::String(text) => dump_string(out, text),
        PyJson::Array(items) => {
            out.push(91);
            for (at, item) in items.iter().enumerate() {
                if at > 0 {
                    push_ascii(out, ", ");
                }
                dump_value(out, item, sort_keys);
            }
            out.push(93);
        }
        PyJson::Object(entries) => {
            out.push(123);
            let mut order: Vec<&(Vec<u32>, PyJson)> = entries.iter().collect();
            if sort_keys {
                order.sort_by(|a, b| a.0.cmp(&b.0));
            }
            for (at, (key, item)) in order.into_iter().enumerate() {
                if at > 0 {
                    push_ascii(out, ", ");
                }
                dump_string(out, key);
                push_ascii(out, ": ");
                dump_value(out, item, sort_keys);
            }
            out.push(125);
        }
    }
}

pub fn loads_text(input: &[u32], policy: JsonPolicy) -> Result<PyJson, JsonFailure> {
    if input.first() == Some(&0xfeff) {
        return Err(JsonFailure::Decode {
            message: "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            position: 0,
            line: 1,
            column: 1,
        });
    }
    let mut p = P {
        s: input,
        at: 0,
        policy,
        containers: 0,
    };
    let v = p.value()?;
    p.ws();
    if p.at != input.len() {
        return p.err("Extra data", p.at);
    }
    Ok(v)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ast_and_duplicate() {
        let x = loads_text(
            &"{\"z\":1,\"a\":2,\"z\":3}"
                .chars()
                .map(u32::from)
                .collect::<Vec<_>>(),
            JsonPolicy::default(),
        )
        .unwrap();
        assert_eq!(
            x,
            PyJson::Object(vec![
                (
                    vec![122],
                    PyJson::Int {
                        negative: false,
                        digits: b"3".to_vec()
                    }
                ),
                (
                    vec![97],
                    PyJson::Int {
                        negative: false,
                        digits: b"2".to_vec()
                    }
                )
            ])
        );
    }
    #[test]
    fn positions() {
        assert!(matches!(
            loads_text(&[91, 63, 93], JsonPolicy::default()),
            Err(JsonFailure::Decode { position: 1, .. })
        ));
    }
    #[test]
    fn depth_994_accepts_and_995_rejects() {
        let ok = ["[".repeat(994), "null".into(), "]".repeat(994)].concat();
        assert!(
            loads_text(
                &ok.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default()
            )
            .is_ok()
        );
        let bad = ["[".repeat(995), "null".into(), "]".repeat(995)].concat();
        assert!(matches!(
            loads_text(
                &bad.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default()
            ),
            Err(JsonFailure::Recursion { .. })
        ));
    }
    #[test]
    fn number_prefixes_and_integer_limits_match_python() {
        let policy = JsonPolicy::default();
        for text in ["1.5", "1e2", "-Infinity"] {
            assert!(loads_text(&text.chars().map(u32::from).collect::<Vec<_>>(), policy).is_ok());
        }
        for (text, position) in [
            ("-", 0),
            ("-NaN", 0),
            ("--1", 0),
            ("01", 1),
            ("1.", 1),
            ("1e+", 1),
            ("1.2.3", 3),
            ("1e2e3", 3),
            ("-1.", 2),
        ] {
            assert!(
                matches!(
                    loads_text(&text.chars().map(u32::from).collect::<Vec<_>>(), policy),
                    Err(JsonFailure::Decode { position: got, .. }) if got == position
                ),
                "{text:?}"
            );
        }
        let digits = "9".repeat(5_000);
        assert!(
            loads_text(
                &digits.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy {
                    int_max_digits: 0,
                    ..policy
                }
            )
            .is_ok()
        );
        let decimal = format!("{}.0", "9".repeat(641));
        assert!(
            loads_text(
                &decimal.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy {
                    int_max_digits: 640,
                    ..policy
                }
            )
            .is_ok()
        );
    }
    #[test]
    fn unicode_and_container_boundaries_match_python() {
        assert_eq!(
            loads_text(
                &r#""\ud800\udc00""#.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default(),
            ),
            Ok(PyJson::String(vec![0x1_0000]))
        );
        assert!(matches!(
            loads_text(
                &r#""\ud802\udc0""#.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default(),
            ),
            Err(JsonFailure::Decode {
                message: "Invalid \\uXXXX escape",
                position: 8,
                ..
            })
        ));
        let siblings = format!("[{}]", vec!["[]"; 995].join(","));
        assert!(
            loads_text(
                &siblings.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default(),
            )
            .is_ok()
        );
        let deep_nan = ["[".repeat(994), "NaN".into(), "]".repeat(994)].concat();
        assert!(matches!(
            loads_text(
                &deep_nan.chars().map(u32::from).collect::<Vec<_>>(),
                JsonPolicy::default(),
            ),
            Err(JsonFailure::Recursion {
                operation: "calling a Python object"
            })
        ));
    }
}
