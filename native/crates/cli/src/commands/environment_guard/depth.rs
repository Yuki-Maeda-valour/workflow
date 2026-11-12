//! 旧版の Python の再帰の深さの数え方(Python 3.10.12 の実測に合わせる)。
//!
//! Python 3.10 は、`_Py_EnterRecursiveCall` を次の所で呼び、深さが 1000 を超えれば `RecursionError` にする。
//! - Python の関数のフレームに入るとき(文面は `maximum recursion depth exceeded`)。
//! - C の関数・メソッドの呼出し(`os.stat`・`stat.S_ISDIR`・`sorted`・`hashlib.sha256` など。
//!   文面は `… while calling a Python object`)。入れ子の C の呼出し(`sorted` が呼ぶ `list.sort`)は、さらに 1 つ深い。
//! - 比較(`!=`・`>`。文面は `… in comparison`)。タプルの比較は、要素の比較がさらに 1 つ深い。
//!
//! 呼出元の深さを `Depth` で受け取り、旧版で呼出しが起きる所の順に確かめる。`None` は「数えない」(深さの基準が
//! 決まっていない呼出し。製品のコマンドは、次の段が各コマンドの値を入れる)。
//!
//! `with parent(path)` を入る所(`contextlib` と `pathlib` のフレームの連なり)は、旧版を実測した表
//! (`PARENT_ENTER`)で確かめる。`Depth` は、`with` 文を実行するフレームの深さ。
use super::error::PyException;
use super::pyjson::{Call, RECURSION_LIMIT, enter_call};

/// 呼出元のフレームの深さ。`None` は、数えない。
pub type Depth = Option<usize>;

/// フレームに入る(関数の本体の深さ `d`)。
pub fn frame(d: Depth) -> Result<(), PyException> {
    d.map_or(Ok(()), |d| enter_call(d, Call::Frame))
}

/// `d` のフレームから Python の関数を呼ぶ。戻り値は、呼ばれた関数の本体の深さ。
pub fn call(d: Depth) -> Result<Depth, PyException> {
    match d {
        Some(d) => enter_call(d + 1, Call::Frame).map(|()| Some(d + 1)),
        None => Ok(None),
    }
}

/// `d` のフレームから C の関数・メソッドを呼ぶ。
pub fn native(d: Depth) -> Result<(), PyException> {
    d.map_or(Ok(()), |d| enter_call(d + 1, Call::Native))
}

/// `d` のフレームでの比較(`!=`・`>` など)。
pub fn compare(d: Depth) -> Result<(), PyException> {
    d.map_or(Ok(()), |d| enter_call(d + 1, Call::Comparison))
}

/// `d` のフレームでの、タプルどうしの比較(要素の比較が 1 つ深い)。
pub fn compare_tuple(d: Depth) -> Result<(), PyException> {
    compare(d)?;
    d.map_or(Ok(()), |d| enter_call(d + 2, Call::Comparison))
}

/// `sorted(list)`: `sorted` の呼出しと、その中の `list.sort` の呼出し。
pub fn sorted(d: Depth) -> Result<(), PyException> {
    native(d)?;
    native(d.map(|d| d + 1))
}

/// 旧版の `with parent(path) as (fd, name):` に入る所(`contextmanager` の `helper`・`__enter__`・ジェネレータ・
/// `absolute()` の `pathlib`)。`d` は、`with` 文を実行するフレームの深さ。旧 Python 3.10.12 で、
/// `d` を 1 つずつ変えて、最初に落ちる検査の種類を測った表(`gen_cases.py` の area `depth` の固定期待が、
/// 表と旧版の一致を確かめる)。表より浅ければ通る。
pub fn parent_enter(d: Depth) -> Result<(), PyException> {
    parent_enter_with(d, PARENT_ENTER)
}

/// 旧版の `with parent(path)` に `Path` を渡す入口(`tree(root, 'plugin', …)` の `root`)。`absolute()` の `str(path)` が
/// `Path.__str__`(Python の関数)を呼ぶので、文字列を渡す入口と落ちる検査の並びが違う。
pub fn parent_enter_path(d: Depth) -> Result<(), PyException> {
    parent_enter_with(d, PARENT_ENTER_PATH)
}

fn parent_enter_with(d: Depth, table: &[u8]) -> Result<(), PyException> {
    let Some(d) = d else { return Ok(()) };
    let Some(index) = d.checked_sub(PARENT_ENTER_FROM) else {
        return Ok(());
    };
    let Some(kind) = table.get(index).copied() else {
        // 表の先(1000 を超える深さ)は、フレームの検査が先に落ちる。
        return Ok(());
    };
    enter_call(
        RECURSION_LIMIT + 1,
        match kind {
            b'C' => Call::Comparison,
            b'F' => Call::Frame,
            b'S' => Call::Str,
            _ => Call::Native,
        },
    )
}

/// `PARENT_ENTER` の最初の深さ。これより浅ければ通る。
pub const PARENT_ENTER_FROM: usize = 990;
/// 深さ `PARENT_ENTER_FROM` から 1000 まで、最初に落ちる検査の種類(`C` 比較・`N` C の呼出し・`F` フレーム)。
pub const PARENT_ENTER: &[u8] = b"CNNNCNNNFNF";
/// `Path` を渡す入口の表(`S` は `str()` の中の再帰。旧 Python 3.10.12 で、深さ 990 から 1000 まで測った)。
pub const PARENT_ENTER_PATH: &[u8] = b"CNNFFSNNFNF";

#[cfg(test)]
mod tests {
    use super::*;

    fn message(result: Result<(), PyException>) -> Option<String> {
        result
            .err()
            .map(|e| super::super::pystr::to_string(e.message.as_codepoints()))
    }

    #[test]
    fn checks_fail_just_above_the_limit_with_the_old_messages() {
        assert!(native(Some(999)).is_ok());
        assert_eq!(
            message(native(Some(1000))).unwrap(),
            "maximum recursion depth exceeded while calling a Python object"
        );
        assert!(compare(Some(999)).is_ok());
        assert_eq!(
            message(compare(Some(1000))).unwrap(),
            "maximum recursion depth exceeded in comparison"
        );
        assert!(compare_tuple(Some(998)).is_ok());
        assert!(
            message(compare_tuple(Some(999)))
                .unwrap()
                .ends_with("in comparison")
        );
        assert!(frame(Some(1000)).is_ok());
        assert_eq!(
            message(frame(Some(1001))).unwrap(),
            "maximum recursion depth exceeded"
        );
        assert!(sorted(Some(998)).is_ok());
        assert!(sorted(Some(999)).is_err());
        assert!(native(None).is_ok() && compare_tuple(None).is_ok() && sorted(None).is_ok());
        assert_eq!(call(Some(5)).unwrap(), Some(6));
        assert!(call(Some(1000)).is_err());
    }

    #[test]
    fn the_parent_table_covers_every_depth_up_to_the_limit() {
        assert_eq!(PARENT_ENTER_FROM + PARENT_ENTER.len() - 1, RECURSION_LIMIT);
        assert!(parent_enter(Some(PARENT_ENTER_FROM - 1)).is_ok());
        let kinds: Vec<Option<String>> = (PARENT_ENTER_FROM..=RECURSION_LIMIT)
            .map(|d| message(parent_enter(Some(d))))
            .collect();
        assert!(kinds.iter().all(Option::is_some));
        assert!(kinds[0].as_deref().unwrap().ends_with("in comparison"));
        assert_eq!(
            kinds[8].as_deref().unwrap(),
            "maximum recursion depth exceeded"
        );
    }
}
