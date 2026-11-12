//! R4Q(#494)の専用の処理のうち、旧版(check-process.py・loop-supervisor.py)が使う Python 3.10.11 の
//! 値と文を再現する純粋な部品を集める。OS を呼ばない(OS の部品は platform の `r4q_process`)。
//! 製品 CLI の登録表(commands/mod.rs)には接続しない(未登録・未配布)。読み込む側は `#[path]` で読む。
//!
//! - `value`: Python の値の形(順序付きの辞書・大きな整数・surrogate を含む文字列・bytes)と repr。
//! - `number`: `int()`(`PYTHONINTMAXSTRDIGITS` の桁の上限を含む)・`float()`・float の repr・int と float の
//!   区別を持つ数(`PyNumber`)。
//! - `codec`: `json.loads(bytes)` の文字コードの判定と、surrogatepass の復号と誤りの文。
//! - `json`: `json.dumps`(既定の区切り・`sort_keys`・`ensure_ascii`)と `json.loads(bytes)`。
//! - `argparse`: 旧版の 2 本の parser(argparse 3.10.11)の解釈・usage・help・誤りの文と、`COLUMNS` と
//!   端末の幅による幅の決め方。
//! - `exception`: `OSError` などの `str(exc)`・`TimeoutExpired` の文・pathlib の `str(directory / name)`・
//!   strerror の表(Linux は R4U の表、Mac は Apple の公開の Libc の表)。
//! - `legacy`: 旧版の docstring と呼出履歴の定型(到達する経路だけ)。
#![forbid(unsafe_code)]

pub mod argparse;
pub mod codec;
pub mod exception;
pub mod json;
pub mod legacy;
pub mod number;
pub mod value;

#[path = "../check_links/oserror.rs"]
// errno から例外の種類を選ぶ表と、glibc の strerror の固定表(R4U)。読む側の `repr` を使う。
#[allow(dead_code)]
pub mod oserror;
#[path = "../check_links/path.rs"]
// pathlib の `PurePosixPath` の整え方と連結だけを使う。
#[allow(dead_code)]
pub mod path;
#[path = "../check_links/traceback.rs"]
// 呼出履歴の書き方(`print_exception`)と `Frame` だけを使う。R4U の定数のフレームは使わない。
#[allow(dead_code)]
pub mod traceback;

#[path = "../resolve_task_dir/bytes.rs"]
// surrogateescape の符号位置だけを使う。harness=false では走らない既存の単体テストの import は使わない。
#[allow(dead_code, unused_imports)]
mod bytes;
#[path = "../resolve_task_dir/unicode.rs"]
// str.isprintable と Unicode の 10 進数字の表。
#[allow(dead_code)]
mod unicode;

/// Python の文字列(surrogateescape で復号したもの)の符号位置の列。
pub fn code_points(raw: &[u8]) -> Vec<u32> {
    bytes::characters(raw)
}

/// Python 3.10 の repr(str)。引数は surrogateescape で符号化したバイト列。
pub fn repr(raw: &[u8]) -> String {
    value::text(&value::str_repr(&code_points(raw)))
}

/// Python 3.10(Unicode 13.0.0)の str.isprintable。
pub fn printable(code: u32) -> bool {
    unicode::printable(code)
}

/// Python 3.10(Unicode 13.0.0)の 10 進数字の値(`Py_UNICODE_TODECIMAL`)。
pub fn decimal(code: u32) -> Option<u8> {
    unicode::decimal(code)
}
