//! Python の値(bool・任意精度の int・float・str・bytes・list・dict・None)と等しさ。
use super::error::PyException;
use dev_workflow_core::loop_state::CodepointString;
use std::collections::HashMap;

/// 任意精度の整数。10 進の数字列で持つので、桁数の上限(`PYTHONINTMAXSTRDIGITS`)も扱える。
#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct PyInt {
    negative: bool,
    digits: String,
}

impl PyInt {
    /// 数字だけの列から作る。先頭の 0 は落とし、-0 は 0 にする。
    pub fn from_decimal(negative: bool, digits: &str) -> Self {
        let trimmed = digits.trim_start_matches('0');
        if trimmed.is_empty() {
            return Self {
                negative: false,
                digits: "0".to_owned(),
            };
        }
        Self {
            negative,
            digits: trimmed.to_owned(),
        }
    }

    pub fn from_i64(value: i64) -> Self {
        Self::from_decimal(value < 0, &value.unsigned_abs().to_string())
    }

    pub fn is_negative(&self) -> bool {
        self.negative
    }

    pub fn is_zero(&self) -> bool {
        self.digits == "0"
    }

    /// 符号を除いた 10 進の桁数。
    pub fn digit_count(&self) -> usize {
        self.digits.len()
    }

    /// `str(int)` / `repr(int)`。桁数の上限(0 は無制限)を超えると旧版と同じ `ValueError`。
    pub fn to_decimal(&self, max_digits: usize) -> Result<String, PyException> {
        if max_digits > 0 && self.digits.len() > max_digits {
            return Err(PyException::value_error(&format!(
                "Exceeds the limit ({max_digits}) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit"
            )));
        }
        Ok(self.to_decimal_unchecked())
    }

    fn to_decimal_unchecked(&self) -> String {
        if self.negative {
            format!("-{}", self.digits)
        } else {
            self.digits.clone()
        }
    }

    /// `float(int)`。大きすぎると旧版と同じ `OverflowError`。
    pub fn to_f64(&self) -> Result<f64, PyException> {
        let value: f64 = self.digits.parse().expect("数字列");
        if value.is_infinite() {
            return Err(PyException::new(
                "OverflowError",
                "int too large to convert to float",
            ));
        }
        Ok(if self.negative { -value } else { value })
    }

    pub fn to_i64(&self) -> Option<i64> {
        self.to_decimal_unchecked().parse().ok()
    }

    /// 負は 0、大きすぎる値は `usize::MAX` にする。
    pub fn to_usize_saturating(&self) -> usize {
        if self.negative {
            return 0;
        }
        self.digits.parse().unwrap_or(usize::MAX)
    }
}

/// `float` が整数の値なら、その 10 進の表記(符号と数字列)。
fn integral_decimal(value: f64) -> Option<(bool, String)> {
    if !value.is_finite() || value.fract() != 0.0 {
        return None;
    }
    let digits = format!("{:.0}", value.abs());
    Some((value.is_sign_negative() && digits != "0", digits))
}

/// `repr(float)`。最短の桁で、`1e+16` 以上と `1e-05` 未満は指数表記にする。
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    let sci = format!("{:e}", value.abs());
    let (mantissa, exponent) = sci.split_once('e').expect("指数表記");
    let exponent: i32 = exponent.parse().expect("指数");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let sign = if value.is_sign_negative() { "-" } else { "" };
    let body = if (-4..16).contains(&exponent) {
        if exponent >= 0 {
            let split = exponent as usize + 1;
            let (whole, fraction) = if digits.len() > split {
                (digits[..split].to_owned(), digits[split..].to_owned())
            } else {
                (
                    format!("{digits}{}", "0".repeat(split - digits.len())),
                    "0".to_owned(),
                )
            };
            format!("{whole}.{fraction}")
        } else {
            format!("0.{}{digits}", "0".repeat((-exponent - 1) as usize))
        }
    } else {
        let head = &digits[..1];
        let rest = &digits[1..];
        let sign_e = if exponent < 0 { '-' } else { '+' };
        let mantissa = if rest.is_empty() {
            head.to_owned()
        } else {
            format!("{head}.{rest}")
        };
        format!("{mantissa}e{sign_e}{:02}", exponent.abs())
    };
    format!("{sign}{body}")
}

#[derive(Clone, Debug)]
pub enum PyValue {
    None,
    Bool(bool),
    Int(PyInt),
    Float(f64),
    Str(CodepointString),
    Bytes(Vec<u8>),
    List(Vec<PyValue>),
    Dict(PyDict),
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
enum HashKey {
    None,
    Int(bool, String),
    FloatBits(u64),
    Str(Vec<u32>),
    Bytes(Vec<u8>),
}

fn hash_key(value: &PyValue) -> Result<HashKey, PyException> {
    Ok(match value {
        PyValue::None => HashKey::None,
        PyValue::Bool(b) => HashKey::Int(false, if *b { "1" } else { "0" }.to_owned()),
        PyValue::Int(i) => HashKey::Int(i.negative, i.digits.clone()),
        PyValue::Float(f) => match integral_decimal(*f) {
            Some((negative, digits)) => HashKey::Int(negative, digits),
            None => HashKey::FloatBits(f.to_bits()),
        },
        PyValue::Str(s) => HashKey::Str(s.as_codepoints().to_vec()),
        PyValue::Bytes(b) => HashKey::Bytes(b.clone()),
        PyValue::List(_) => return Err(unhashable("list")),
        PyValue::Dict(_) => return Err(unhashable("dict")),
    })
}

fn unhashable(name: &str) -> PyException {
    PyException::type_error(&format!("unhashable type: '{name}'"))
}

/// 挿入順を保つ辞書。同じキー(`1 == 1.0 == True`)への代入は、最初の位置とキーを保つ。
#[derive(Clone, Debug, Default)]
pub struct PyDict {
    entries: Vec<(PyValue, PyValue)>,
    index: HashMap<HashKey, usize>,
}

impl PyDict {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn insert(&mut self, key: PyValue, value: PyValue) -> Result<(), PyException> {
        let hash = hash_key(&key)?;
        match self.index.get(&hash) {
            Some(&position) => self.entries[position].1 = value,
            None => {
                self.index.insert(hash, self.entries.len());
                self.entries.push((key, value));
            }
        }
        Ok(())
    }

    pub fn insert_str(&mut self, key: &str, value: PyValue) {
        self.insert(PyValue::from(key), value)
            .expect("文字列は hash できる");
    }

    pub fn get(&self, key: &PyValue) -> Result<Option<&PyValue>, PyException> {
        let hash = hash_key(key)?;
        Ok(self
            .index
            .get(&hash)
            .map(|&position| &self.entries[position].1))
    }

    pub fn get_str(&self, key: &str) -> Option<&PyValue> {
        self.get(&PyValue::from(key)).ok().flatten()
    }

    pub fn contains_str(&self, key: &str) -> bool {
        self.get_str(key).is_some()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&PyValue, &PyValue)> {
        self.entries.iter().map(|(k, v)| (k, v))
    }

    pub fn keys(&self) -> impl Iterator<Item = &PyValue> {
        self.entries.iter().map(|(k, _)| k)
    }

    pub fn values(&self) -> impl Iterator<Item = &PyValue> {
        self.entries.iter().map(|(_, v)| v)
    }
}

impl From<&str> for PyValue {
    fn from(value: &str) -> Self {
        PyValue::Str(CodepointString::from(value))
    }
}

impl From<bool> for PyValue {
    fn from(value: bool) -> Self {
        PyValue::Bool(value)
    }
}

impl From<i64> for PyValue {
    fn from(value: i64) -> Self {
        PyValue::Int(PyInt::from_i64(value))
    }
}

impl PyValue {
    pub fn type_name(&self) -> &'static str {
        match self {
            PyValue::None => "NoneType",
            PyValue::Bool(_) => "bool",
            PyValue::Int(_) => "int",
            PyValue::Float(_) => "float",
            PyValue::Str(_) => "str",
            PyValue::Bytes(_) => "bytes",
            PyValue::List(_) => "list",
            PyValue::Dict(_) => "dict",
        }
    }

    pub fn is_str(&self) -> bool {
        matches!(self, PyValue::Str(_))
    }

    pub fn as_str(&self) -> Option<&CodepointString> {
        match self {
            PyValue::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_dict(&self) -> Option<&PyDict> {
        match self {
            PyValue::Dict(d) => Some(d),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&[PyValue]> {
        match self {
            PyValue::List(l) => Some(l),
            _ => None,
        }
    }

    pub fn truthy(&self) -> bool {
        match self {
            PyValue::None => false,
            PyValue::Bool(b) => *b,
            PyValue::Int(i) => !i.is_zero(),
            PyValue::Float(f) => *f != 0.0,
            PyValue::Str(s) => !s.as_codepoints().is_empty(),
            PyValue::Bytes(b) => !b.is_empty(),
            PyValue::List(l) => !l.is_empty(),
            PyValue::Dict(d) => !d.is_empty(),
        }
    }

    /// `==`。NaN は自分自身とも等しくない。
    pub fn py_eq(&self, other: &PyValue) -> bool {
        self.eq_with(other, false)
    }

    /// `!=`。
    pub fn py_ne(&self, other: &PyValue) -> bool {
        !self.py_eq(other)
    }

    /// list・dict の中身、`in`、`dict` の値の比較。同じ物(`json` の NaN の定数)は等しい。
    pub fn eq_in_container(&self, other: &PyValue) -> bool {
        self.eq_with(other, true)
    }

    fn eq_with(&self, other: &PyValue, same_nan: bool) -> bool {
        use PyValue::*;
        match (self, other) {
            (None, None) => true,
            (Str(a), Str(b)) => a == b,
            (Bytes(a), Bytes(b)) => a == b,
            (List(a), List(b)) => {
                a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x.eq_with(y, true))
            }
            (Dict(a), Dict(b)) => {
                a.len() == b.len()
                    && a.iter().all(|(key, value)| match b.get(key) {
                        Ok(Option::Some(found)) => value.eq_with(found, true),
                        _ => false,
                    })
            }
            (Float(a), Float(b)) => {
                if a.is_nan() && b.is_nan() {
                    same_nan
                } else {
                    a == b
                }
            }
            _ => match (self.number(), other.number()) {
                (Option::Some(a), Option::Some(b)) => a.equals(&b),
                _ => false,
            },
        }
    }

    fn number(&self) -> Option<Number> {
        match self {
            PyValue::Bool(b) => Some(Number::Int(PyInt::from_i64(i64::from(*b)))),
            PyValue::Int(i) => Some(Number::Int(i.clone())),
            PyValue::Float(f) => Some(Number::Float(*f)),
            _ => None,
        }
    }
}

enum Number {
    Int(PyInt),
    Float(f64),
}

impl Number {
    fn equals(&self, other: &Number) -> bool {
        match (self, other) {
            (Number::Int(a), Number::Int(b)) => a == b,
            (Number::Float(a), Number::Float(b)) => a == b,
            (Number::Int(i), Number::Float(f)) | (Number::Float(f), Number::Int(i)) => {
                match integral_decimal(*f) {
                    Some((negative, digits)) => PyInt::from_decimal(negative, &digits) == *i,
                    None => false,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn int(text: &str) -> PyValue {
        let (negative, digits) = text.strip_prefix('-').map_or((false, text), |d| (true, d));
        PyValue::Int(PyInt::from_decimal(negative, digits))
    }

    #[test]
    fn numbers_compare_across_types() {
        assert!(PyValue::Bool(true).py_eq(&int("1")));
        assert!(int("1").py_eq(&PyValue::Float(1.0)));
        assert!(PyValue::Bool(true).py_eq(&PyValue::Float(1.0)));
        assert!(PyValue::Bool(false).py_eq(&PyValue::Float(-0.0)));
        assert!(int("0").py_eq(&PyValue::Float(-0.0)));
        assert!(!int("1").py_eq(&PyValue::Float(1.5)));
        assert!(int("100000000000000000000").py_eq(&PyValue::Float(1e20)));
        assert!(!int("100000000000000000001").py_eq(&PyValue::Float(1e20)));
        assert!(int("-9007199254740993").py_ne(&PyValue::Float(-9007199254740992.0)));
        assert!(!PyValue::Float(f64::NAN).py_eq(&PyValue::Float(f64::NAN)));
        assert!(
            PyValue::List(vec![PyValue::Float(f64::NAN)])
                .py_eq(&PyValue::List(vec![PyValue::Float(f64::NAN)]))
        );
        assert!(!PyValue::None.py_eq(&PyValue::Bool(false)));
        assert!(!PyValue::from("1").py_eq(&int("1")));
        assert!(!PyValue::Bytes(b"a".to_vec()).py_eq(&PyValue::from("a")));
    }

    #[test]
    fn dict_keeps_insertion_order_and_first_key() {
        let mut dict = PyDict::new();
        dict.insert(int("1"), PyValue::from("a")).unwrap();
        dict.insert(PyValue::from("b"), PyValue::from("b")).unwrap();
        dict.insert(PyValue::Float(1.0), PyValue::from("c"))
            .unwrap();
        dict.insert(PyValue::Bool(true), PyValue::from("d"))
            .unwrap();
        assert_eq!(dict.len(), 2);
        let keys: Vec<&PyValue> = dict.keys().collect();
        assert!(matches!(keys[0], PyValue::Int(_)));
        assert!(
            dict.get(&PyValue::Bool(true))
                .unwrap()
                .unwrap()
                .py_eq(&PyValue::from("d"))
        );
        assert_eq!(
            dict.insert(PyValue::List(vec![]), PyValue::None)
                .unwrap_err()
                .message,
            CodepointString::from("unhashable type: 'list'")
        );
        let mut other = PyDict::new();
        other.insert_str("b", PyValue::from("b"));
        other
            .insert(PyValue::Float(1.0), PyValue::from("d"))
            .unwrap();
        assert!(PyValue::Dict(dict).py_eq(&PyValue::Dict(other)));
    }

    #[test]
    fn float_repr_matches_python_layout() {
        for (value, expect) in [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (123456789012345680.0, "1.2345678901234568e+17"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1.2345e-7, "1.2345e-07"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (1e22, "1e+22"),
            (1e100, "1e+100"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (f64::INFINITY, "inf"),
            (-2.5, "-2.5"),
        ] {
            assert_eq!(float_repr(value), expect, "{value:e}");
        }
    }

    #[test]
    fn int_conversions_follow_python_limits() {
        let big = PyInt::from_decimal(false, &"9".repeat(5000));
        assert!(big.to_f64().unwrap_err().is_instance("OverflowError"));
        assert!(big.to_decimal(4300).unwrap_err().is_instance("ValueError"));
        assert!(big.to_decimal(0).is_ok());
        assert_eq!(
            PyInt::from_decimal(true, "0007").to_decimal(4300).unwrap(),
            "-7"
        );
        assert_eq!(PyInt::from_decimal(true, "000"), PyInt::from_i64(0));
        assert_eq!(
            PyInt::from_i64(i64::MIN).to_decimal(0).unwrap(),
            "-9223372036854775808"
        );
        assert_eq!(PyInt::from_decimal(false, "10").to_usize_saturating(), 10);
        assert_eq!(big.to_usize_saturating(), usize::MAX);
    }

    #[test]
    fn float_cases_match_python_3_10() {
        use super::super::testing::fixture;
        for case in fixture!("float_cases.json").as_array().unwrap() {
            let bits = u64::from_str_radix(case["bits"].as_str().unwrap(), 16).unwrap();
            let value = f64::from_bits(bits);
            assert_eq!(float_repr(value), case["repr"].as_str().unwrap(), "{case}");
            // 整数の値の float は同じ値の int と等しく、10 進の表記も同じ。
            let integral = value.fract() == 0.0;
            assert_eq!(integral, case["integral"].as_bool().unwrap(), "{case}");
            if integral {
                let text = case["int_text"].as_str().unwrap();
                let (negative, digits) =
                    text.strip_prefix('-').map_or((false, text), |d| (true, d));
                assert!(
                    PyValue::Float(value)
                        .py_eq(&PyValue::Int(PyInt::from_decimal(negative, digits))),
                    "{case}"
                );
                assert!(
                    PyValue::Float(value).py_ne(&PyValue::Int(PyInt::from_decimal(
                        negative,
                        &format!("{digits}1")
                    ))),
                    "{case}"
                );
            }
        }
    }
}
