//! `open(path, "r", encoding="utf-8", errors="replace").read()` と同じ本文を作る。
//! 読めないバイトは Python 3.10 の UTF-8 の復号と同じ範囲ごとに U+FFFD にし、
//! CRLF と CR を LF に揃える(テキストの読取りの既定の newline=None)。BOM は残す。

/// 読んだバイト列を、旧版が照合に使う本文にする。
pub fn decode(data: &[u8]) -> String {
    String::from_utf8_lossy(data)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}
