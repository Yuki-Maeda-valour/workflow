//! R4U(#480)の専用の処理。旧版 update-doc/scripts/check_links.py と同じ結果を返す部品を集める。
//! 製品 CLI の登録表(commands/mod.rs)には接続しない(未登録・未配布)。
//! この段は文字列だけの部品(パスの整え方・照合・読取りの復号・repr・OS の文・呼出履歴の定型)である。
#![forbid(unsafe_code)]

pub mod matcher;
pub mod oserror;
pub mod path;
pub mod posix;
pub mod text;
pub mod traceback;

#[path = "../resolve_task_dir/bytes.rs"]
// surrogateescape の符号位置と repr だけを使う。JSON の表記などの関数は使わない。
#[allow(dead_code)]
mod bytes;
#[path = "../resolve_task_dir/unicode.rs"]
// str.isprintable の表だけを使う。decimal は使わない。
#[allow(dead_code)]
mod unicode;

/// Python の文字列(ファイル名は surrogateescape で復号したもの)の符号位置の列。
pub fn code_points(raw: &[u8]) -> Vec<u32> {
    bytes::characters(raw)
}

/// Python 3.10 の repr(str)。引数は surrogateescape で符号化したバイト列。
pub fn repr(raw: &[u8]) -> String {
    String::from_utf8(bytes::repr(raw))
        .expect("repr は surrogate を \\udcXX にするので UTF-8 になる")
}

/// Python 3.10(Unicode 13.0.0)の str.isprintable。
pub fn printable(code: u32) -> bool {
    unicode::printable(code)
}
