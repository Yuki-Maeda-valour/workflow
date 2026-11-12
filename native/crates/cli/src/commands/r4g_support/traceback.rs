//! 旧Pythonの呼出履歴(`Traceback (most recent call last):`)の形。
//!
//! 所在地の欄は固定のファイル名にする(2026-11-11 17:58決定・21:35拡張の利用者決定)。
//! 旧スクリプトはそのファイル名(例: `implementation-git.py`)、標準ライブラリは導入先からの
//! 相対パス(例: `subprocess.py`・`json/encoder.py`)。行番号・関数名・ソースの行は旧版に合わせ、
//! 標準ライブラリの行はCIで比べる本家のCPython 3.10.11の値を使う(2026-11-11 22:26の利用者決定)。

use super::python_io::TextStream;
use super::text::{PyText, from_str};

/// 呼出履歴の1段。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    /// 固定のファイル名。`<`で始まる名前(`<string>`など)は旧版でもそのまま。
    pub file: &'static str,
    pub line: u32,
    pub function: &'static str,
    /// ソースの行(旧版のファイルのその行。改行を除く)。無ければ`None`。
    pub source: Option<&'static str>,
}

/// 旧Python 3.10の`_Py_DisplaySourceLine`が削る行頭の字下げ(空白・タブ・改ページだけ)。行末は削らない。
fn strip_indent(line: &str) -> &str {
    line.trim_start_matches([' ', '\t', '\x0c'])
}

/// `traceback.print_exception`の形。`exception`は最後の行(`Type: message`か`Type`)。
pub fn render(frames: &[Frame], exception: &[u32]) -> PyText {
    let mut text = String::new();
    if !frames.is_empty() {
        text.push_str("Traceback (most recent call last):\n");
        for frame in frames {
            text.push_str(&format!(
                "  File \"{}\", line {}, in {}\n",
                frame.file, frame.line, frame.function
            ));
            if let Some(source) = frame.source {
                text.push_str(&format!("    {}\n", strip_indent(source)));
            }
        }
    }
    let mut out = from_str(&text);
    out.extend_from_slice(exception);
    out.push(10);
    out
}

/// 例外の最後の行。`message`が空なら型の名前だけ(`KeyboardInterrupt`など)。
pub fn exception(type_name: &str, message: &[u32]) -> PyText {
    let mut out = from_str(type_name);
    if !message.is_empty() {
        out.extend(from_str(": "));
        out.extend_from_slice(message);
    }
    out
}

/// 捕まえなかった例外の表示(旧Python 3.10の`PyErr_Display`)。標準エラーが`None`なら何も書かない。
///
/// 旧版と同じ区切りで`write`を呼ぶ: 見出し・各段の`File`の行・字下げ・ソースの行・改行・型の名前・
/// `": "`・文・改行。`File`の行までの失敗で残りの段と型の名前と文をやめ(ソースの行の失敗は無視)、
/// 最後の改行はいつも試し、終わりにflushを試す。書込みの失敗は旧版と同じく表に出さない。
pub fn print_exception(
    stream: &mut TextStream,
    frames: &[Frame],
    type_name: &str,
    message: &[u32],
) {
    let mut write = |text: &[u32]| stream.write(text).is_ok();
    let mut ok = true;
    if !frames.is_empty() {
        ok = write(&from_str("Traceback (most recent call last):\n"));
        for frame in frames {
            if !ok {
                break;
            }
            ok = write(&from_str(&format!(
                "  File \"{}\", line {}, in {}\n",
                frame.file, frame.line, frame.function
            )));
            if ok
                && let Some(source) = frame.source
                && write(&from_str("    "))
                && write(&from_str(strip_indent(source)))
            {
                write(&from_str("\n"));
            }
        }
    }
    if ok {
        ok = write(&from_str(type_name));
    }
    if ok {
        if !message.is_empty() {
            ok = write(&from_str(": "));
        }
        if ok {
            write(message);
        }
    }
    write(&from_str("\n"));
    let _ = stream.flush();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStrExt;

    /// 子のPythonが例外で止まったときの、本物の標準エラーと比べる(行頭の字下げだけを削り、行末を残す)。
    #[test]
    fn render_matches_the_stderr_of_a_python_child_that_raises() {
        let directory = std::env::temp_dir().join(format!("r4g-traceback-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir(&directory).unwrap();
        let script = directory.join("child.py");
        let source = "def inner():\n\x0c\t value = None.missing   \n\ndef outer():\n    return inner()\n\nouter()\n";
        std::fs::write(&script, source).unwrap();
        let output = std::process::Command::new("python3")
            .arg("-I")
            .arg(&script)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        let file: &'static str = Box::leak(
            String::from_utf8(script.as_os_str().as_bytes().to_vec())
                .unwrap()
                .into_boxed_str(),
        );
        let ours = render(
            &[
                Frame {
                    file,
                    line: 7,
                    function: "<module>",
                    source: Some("outer()"),
                },
                Frame {
                    file,
                    line: 5,
                    function: "outer",
                    source: Some("    return inner()"),
                },
                Frame {
                    file,
                    line: 2,
                    function: "inner",
                    source: Some("\x0c\t value = None.missing   "),
                },
            ],
            &exception(
                "AttributeError",
                &from_str("'NoneType' object has no attribute 'missing'"),
            ),
        );
        let ours: String = ours
            .iter()
            .map(|&code| char::from_u32(code).unwrap())
            .collect();
        assert_eq!(ours, String::from_utf8(output.stderr).unwrap());
        std::fs::remove_dir_all(&directory).unwrap();
    }
}
