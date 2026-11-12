//! 正常時の JSON。旧 `json.dumps(..., ensure_ascii=False)` と同じ区切り(`, ` と `: `)・同じ文字の扱い。
//! 文字列は生バイトのまま持つ。`"`・`\`・制御文字だけを書き換え、それ以外は元のバイトを保つ。
use super::bytes::json;
use dev_workflow_core::CommandOutput;

/// 旧版が返す JSON の値(警告に入る入れ子の辞書・リストと整数・文字列・真偽・null)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum PyJson {
    // `Null`・`Int` は #471 の警告の値で使う(接続時)。それまでは単体テストだけが作る。
    #[cfg_attr(not(test), allow(dead_code))]
    Null,
    Bool(bool),
    #[cfg_attr(not(test), allow(dead_code))]
    Int(i64),
    Text(Vec<u8>),
    List(Vec<PyJson>),
    Dict(Vec<(Vec<u8>, PyJson)>),
}

impl PyJson {
    pub(crate) fn text(value: impl AsRef<[u8]>) -> Self {
        Self::Text(value.as_ref().to_vec())
    }

    pub(crate) fn render(&self) -> Vec<u8> {
        match self {
            Self::Null => b"null".to_vec(),
            Self::Bool(true) => b"true".to_vec(),
            Self::Bool(false) => b"false".to_vec(),
            Self::Int(value) => value.to_string().into_bytes(),
            Self::Text(value) => json(value),
            Self::List(items) => {
                let items = items.iter().map(Self::render).collect::<Vec<_>>();
                [b"[".as_slice(), &items.join(&b", "[..]), b"]"].concat()
            }
            Self::Dict(items) => {
                let items = items
                    .iter()
                    .map(|(key, value)| [json(key), b": ".to_vec(), value.render()].concat())
                    .collect::<Vec<_>>();
                [b"{".as_slice(), &items.join(&b", "[..]), b"}"].concat()
            }
        }
    }
}

/// 正常時の出力: `path`・`tracked`・`sha256`・`warnings` の順の JSON と LF。終了 0。
pub(crate) fn success(
    path: &[u8],
    tracked: bool,
    sha256: &[u8],
    warnings: Vec<PyJson>,
) -> CommandOutput {
    let value = PyJson::Dict(vec![
        (b"path".to_vec(), PyJson::text(path)),
        (b"tracked".to_vec(), PyJson::Bool(tracked)),
        (b"sha256".to_vec(), PyJson::text(sha256)),
        (b"warnings".to_vec(), PyJson::List(warnings)),
    ]);
    let mut stdout = value.render();
    stdout.push(b'\n');
    CommandOutput::stdout(stdout)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn success_json_uses_the_old_key_order_and_default_separators() {
        let output = success(b"/r/\xe9\xa1\x8c\"\\\n\x01", true, b"ab", Vec::new());
        assert_eq!(
            output.stdout,
            b"{\"path\": \"/r/\xe9\xa1\x8c\\\"\\\\\\n\\u0001\", \"tracked\": true, \"sha256\": \"ab\", \"warnings\": []}\n"
        );
        assert_eq!(output.stderr, b"");
        assert_eq!(output.code.as_u8(), 0);
    }

    #[test]
    fn non_utf8_bytes_and_non_ascii_text_are_written_without_escaping() {
        let output = success("/r/進行中_x.md".as_bytes(), false, b"s", Vec::new());
        assert_eq!(
            output.stdout,
            "{\"path\": \"/r/進行中_x.md\", \"tracked\": false, \"sha256\": \"s\", \"warnings\": []}\n"
                .as_bytes()
        );
        let raw = success(b"/r/\xff\x7f", false, b"s", Vec::new());
        assert!(raw.stdout.windows(2).any(|pair| pair == b"\xff\x7f"));
    }

    #[test]
    fn warnings_keep_their_order_and_nested_separators() {
        let warning = |line: i64, code: &str| {
            PyJson::Dict(vec![
                (b"line".to_vec(), PyJson::Int(line)),
                (b"code".to_vec(), PyJson::text(code)),
                (
                    b"extra".to_vec(),
                    PyJson::List(vec![PyJson::Null, PyJson::Bool(false)]),
                ),
            ])
        };
        let output = success(b"/p", false, b"s", vec![warning(3, "b"), warning(1, "a")]);
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "{\"path\": \"/p\", \"tracked\": false, \"sha256\": \"s\", \"warnings\": \
[{\"line\": 3, \"code\": \"b\", \"extra\": [null, false]}, \
{\"line\": 1, \"code\": \"a\", \"extra\": [null, false]}]}\n"
        );
    }
}
