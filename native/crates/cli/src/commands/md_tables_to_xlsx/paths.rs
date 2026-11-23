//! 出力先の事前検査(R4E Task 2.2)。
//!
//! 旧版の `output_paths`・`physical_directory` と、それが使う `pathlib`・`posixpath`(本家 CPython
//! 3.10.11)の関数を、同じ順・同じ OS 呼出しで再現する。
//!
//! - [`PyPath`]: `PurePosixPath` の字面の正規化(連続した `/`・`.`・末尾の `/`・先頭がちょうど `//`)。
//!   `..` は残す。パスは引数の元のバイトで持ち、文(`str(path)`・`repr`)は surrogateescape で文字にする。
//! - `posixpath.realpath`(`strict` と非 `strict`。リンクの環の検出と `seen`)・`Path.resolve`
//!   (`RuntimeError("Symlink loop from …")`)・`absolute`・`samefile`・`exists`・`is_dir`。
//! - [`check_output`]: `output_paths([out], inputs=…, source_roots=…)`。拒否は旧版の文、`OSError` と
//!   `RuntimeError` は `出力先または正本を解決できない: <str(例外)>`、ほかの例外(`Path([])` の
//!   `TypeError`)は呼出履歴つきで返す。
//! - [`make_parents`]: `Path.mkdir(parents=True, exist_ok=True)`(モード 0o777 と umask。作成の失敗の
//!   呼出履歴と、`During handling …` の連鎖)。
//!
//! # 再帰の上限
//!
//! Python の既定の上限(1000)で起きる `RecursionError` を、旧版と同じ深さ・同じ文にする。CPython 3.10
//! は、Python の関数の段に入るとき(文は `maximum recursion depth exceeded`)、C の関数・型・例外の型を
//! 呼ぶとき(` while calling a Python object`)、比較(` in comparison`)、`str()`(` while getting the
//! str of an object`)で深さを 1 つ足し、上限を超えた最初の所で例外にする。[`Depth`] がこの深さを
//! 数える。各関数は、旧版の呼出しの木のうち、その関数の中で初めて届く深さの呼出し(後の呼出しは先に
//! 届いた深さを超えないので数えなくても結果が同じ)を、旧版と同じ順に数える。数え方は、旧版の環境で
//! 上限を 1 つずつ変えて、上限を超えた最初の呼出しの種類を深さの順に並べた文字列で確かめた(試験の
//! `physical_directory_matches_the_measured_depth`・`output_paths_matches_the_measured_depth`)。
//! `F` は段、`C` は呼出し、`Q` は比較、`S` は `str()`、`R` は `repr()`。
//!
//! 深くなるのは `physical_directory` の無い親の再帰と、`_joinrealpath` のリンクの再帰だけ。旧版の実測
//! では、無い親の階層は 985 個で成功・986 個で失敗、リンクの連なりは 989 個で成功・990 個で失敗する
//! (`cases.json` の `measured`)。`Path.mkdir` の再帰は、同じ階層の `physical_directory` より 4 浅いので、
//! 旧版では先に上限に当たらない(試験で確かめる)。

use super::args::Value;
use super::errors::{ExcType, Filename, Frame, PyException, repr, types};
use super::pyio::PyStr;
use std::cell::Cell;
use std::collections::HashMap;
use std::ffi::OsStr;
use std::io;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::{DirBuilderExt, MetadataExt};

/// `sys.getrecursionlimit()` の既定。
pub const RECURSION_LIMIT: u32 = 1000;

/// `TypeError`。
pub const TYPE_ERROR: ExcType = ExcType {
    module: "builtins",
    name: "TypeError",
    bases: &["Exception", "BaseException"],
};

/// 旧スクリプトの `class OutputError(ValueError)`(スクリプトは `__main__` として動く)。
pub const OUTPUT_ERROR: ExcType = ExcType {
    module: "__main__",
    name: "OutputError",
    bases: &["ValueError", "Exception", "BaseException"],
};

const ENOENT: i32 = 2;
const EBADF: i32 = 9;
const ENOTDIR: i32 = 20;
#[cfg(target_os = "linux")]
const ELOOP: i32 = 40;
#[cfg(target_os = "macos")]
const ELOOP: i32 = 62;
const EINVAL: i32 = 22;

// ---------------------------------------------------------------- 呼出しの深さ

/// CPython 3.10 の `_Py_EnterRecursiveCall` が数える深さ(`tstate->recursion_depth`)。
#[derive(Debug)]
pub struct Depth {
    now: u32,
    limit: u32,
}

impl Depth {
    /// 今の段の深さ `now`(モジュールの段が 1)で、上限は既定の 1000。
    pub fn new(now: u32) -> Self {
        Self::with_limit(now, RECURSION_LIMIT)
    }

    /// 上限を変えた形(試験用)。
    pub fn with_limit(now: u32, limit: u32) -> Self {
        Self { now, limit }
    }

    /// 上限を超えたときの `RecursionError`。`event` は 1 文字の種類。
    fn exceeded(event: u8) -> PyException {
        let suffix = match event {
            b'F' => "",
            b'C' => " while calling a Python object",
            b'Q' => " in comparison",
            b'S' => " while getting the str of an object",
            b'R' => " while getting the repr of an object",
            _ => unreachable!("呼出しの種類は F・C・Q・S・R だけ"),
        };
        PyException::new(
            types::RECURSION_ERROR,
            format!("maximum recursion depth exceeded{suffix}"),
        )
    }

    /// 今の段から 1 つずつ深くなる呼出しの並び(`events[i]` が深さ `now + i + 1`)で、上限を超える
    /// 最初の位置。
    fn first_over(&self, events: &[u8]) -> Option<usize> {
        let room = self.limit.saturating_sub(self.now) as usize;
        (room < events.len()).then_some(room)
    }

    /// [`Self::first_over`] を例外にする。
    fn chain(&self, events: &[u8]) -> Result<(), PyException> {
        match self.first_over(events) {
            Some(index) => Err(Self::exceeded(events[index])),
            None => Ok(()),
        }
    }

    /// 1 つ深い呼出し(`event`)の中で `body` を動かす。入る所で上限を超えたら例外。
    fn within<T, E: From<PyException>>(
        &mut self,
        event: u8,
        body: impl FnOnce(&mut Self) -> Result<T, E>,
    ) -> Result<T, E> {
        if self.now >= self.limit {
            return Err(Self::exceeded(event).into());
        }
        self.now += 1;
        let result = body(self);
        self.now -= 1;
        result
    }

    /// Python の関数の段。
    fn frame<T, E: From<PyException>>(
        &mut self,
        body: impl FnOnce(&mut Self) -> Result<T, E>,
    ) -> Result<T, E> {
        self.within(b'F', body)
    }

    /// C の関数の呼出し(`any`・`list.extend` などで、中から生成器の段を動かすもの)。
    fn call<T, E: From<PyException>>(
        &mut self,
        body: impl FnOnce(&mut Self) -> Result<T, E>,
    ) -> Result<T, E> {
        self.within(b'C', body)
    }
}

/// `Path(x)`(`x` は str か Path): 型の呼出し → `__new__` → `os.name == 'nt'` → `object.__new__` →
/// `_parse_args`(`isinstance`)→ `parse_parts`(`reversed`)→ `splitroot`(`part[0] == sep`)。
const PATH_NEW: &[u8] = b"CFQCCCQ";
/// `Path(x)` で `x` が文字列でもパスでもない(`os.fspath` の `TypeError` まで)。
const PATH_NEW_TYPE_ERROR: &[u8] = b"CFQCC";
/// `path.parent`: property の段 → `len()` → `_from_parsed_parts`(`object.__new__`)。
const PARENT: &[u8] = b"FCC";
/// 根だけのパスの `path.parent`(`self` を返すので `_from_parsed_parts` を呼ばない)。
const PARENT_SELF: &[u8] = b"FC";
/// `path.name`: property の段 → `len()`。
const NAME: &[u8] = b"FC";
/// `a / name`: `__truediv__` → `_make_child` → `_parse_args`(`isinstance` など)→ `parse_parts`
/// (`reversed`)→ `splitroot` の比較(名前が空なら比較しない)。
const TRUEDIV: &[u8] = b"FFFCCQ";
const TRUEDIV_EMPTY: &[u8] = b"FFFCC";
/// `path.absolute()`(絶対パスは `is_absolute` だけ)。
const ABSOLUTE_ABSOLUTE: &[u8] = b"FF";
/// `path.absolute()`(相対パス: `os.getcwd()` → `_from_parts`)。
const ABSOLUTE_RELATIVE: &[u8] = b"FFCCCQ";
/// `os.fspath(path)` の中(`__fspath__` → `str()` → `PyObject_Str` → `__str__`)。`_str` が無ければ
/// `_format_parsed_parts` → `str.join` が続く。
const FSPATH_CACHED: &[u8] = b"FCSF";
const FSPATH_UNCACHED: &[u8] = b"FCSFFC";
/// `_joinrealpath` の段の中: `isinstance(path, bytes)` → `isabs(rest)` の `os.fspath` → `_get_sep` の
/// `isinstance`。同じ段のほかの呼出し(`join`・`os.lstat`・`os.readlink` など)はこれより浅い。
const JOINREALPATH_LEVEL: &[u8] = b"CCC";
/// `os.path.samestat(st, other_st)`: 段 → `st_ino ==`。
const SAMESTAT: &[u8] = b"FQ";

// ---------------------------------------------------------------- パス

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Root {
    None,
    /// `/`(1 つか、3 つ以上)。
    One,
    /// ちょうど `//`。
    Two,
}

/// `PurePosixPath`(`_root` と `_parts`)と、深さの数え方に効くキャッシュ(`_str`・`_cached_cparts`)。
///
/// 旧版の新しいオブジェクトは [`PyPath::parse`] などで作り、キャッシュは空。同じオブジェクトを
/// 渡すところは参照で渡す。
#[derive(Debug)]
pub struct PyPath {
    root: Root,
    parts: Vec<Vec<u8>>,
    str_cached: Cell<bool>,
    cparts_cached: Cell<bool>,
}

impl PyPath {
    fn new(root: Root, parts: Vec<Vec<u8>>) -> Self {
        Self {
            root,
            parts,
            str_cached: Cell::new(false),
            cparts_cached: Cell::new(false),
        }
    }

    /// `PurePosixPath(text)`(`parse_parts` と `splitroot`)。
    pub fn parse(text: &[u8]) -> Self {
        let slashes = text.iter().take_while(|&&byte| byte == b'/').count();
        let root = match slashes {
            0 => Root::None,
            2 => Root::Two,
            _ => Root::One,
        };
        let rest = &text[slashes..];
        let parts = rest
            .split(|&byte| byte == b'/')
            .filter(|part| !part.is_empty() && *part != b".")
            .map(<[u8]>::to_vec)
            .collect();
        Self::new(root, parts)
    }

    /// 同じ `_root`・`_parts` の新しいオブジェクト(`Path(path)`)。
    pub fn copy(&self) -> Self {
        Self::new(self.root, self.parts.clone())
    }

    /// `str(path)`(`_format_parsed_parts(...) or '.'`)のバイト。
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut text = match self.root {
            Root::None => Vec::new(),
            Root::One => b"/".to_vec(),
            Root::Two => b"//".to_vec(),
        };
        text.extend_from_slice(&self.parts.join(&b'/'));
        if text.is_empty() {
            text.push(b'.');
        }
        text
    }

    /// `str(path)`。
    pub fn to_py(&self) -> PyStr {
        PyStr::from_os_bytes(&self.to_bytes())
    }

    pub fn is_absolute(&self) -> bool {
        self.root != Root::None
    }

    /// `path.parent`(新しいオブジェクト)。根だけのパスは同じものを返すが、この版は根だけのパスの
    /// 親を、キャッシュの無い同じ字面のオブジェクトとして返す(呼出元で根を別に扱う)。
    fn parent_value(&self) -> Self {
        let mut parts = self.parts.clone();
        parts.pop();
        Self::new(self.root, parts)
    }

    /// 親が自分自身か(根だけのパス。`len(parts) == 1 and root`)。
    fn parent_is_self(&self) -> bool {
        self.root != Root::None && self.parts.is_empty()
    }

    /// `path.name`。
    fn name(&self) -> &[u8] {
        self.parts.last().map_or(&[][..], Vec::as_slice)
    }

    /// `self / name`(`_make_child`)。
    fn child(&self, name: &[u8]) -> Self {
        let other = Self::parse(name);
        if other.root != Root::None {
            return other;
        }
        let mut parts = self.parts.clone();
        parts.extend(other.parts);
        Self::new(self.root, parts)
    }

    /// `_cparts` の比較(`==`)。
    fn same_parts(&self, other: &Self) -> bool {
        self.root == other.root && self.parts == other.parts
    }

    /// `path.parents` の各要素(近い順)。
    fn parents(&self) -> Vec<Self> {
        (0..self.parts.len())
            .map(|index| {
                Self::new(
                    self.root,
                    self.parts[..self.parts.len() - 1 - index].to_vec(),
                )
            })
            .collect()
    }
}

// ---------------------------------------------------------------- 失敗

/// 検査の中の失敗: Python の例外か、`OutputError`(呼出履歴と連鎖つき)。
///
/// `OutputError` は `ValueError` なので、`output_paths` の `except (OSError, RuntimeError)` を素通り
/// する。分けて持つのは、その判定と、試験の表示のため。
#[derive(Debug)]
enum Fail {
    Raise(PyException),
    Output(PyException),
}

impl Fail {
    /// 例外に段を足す(どちらの種類でも)。
    fn called_from(self, stack: &[Frame]) -> Self {
        match self {
            Self::Raise(error) => Self::Raise(error.called_from(stack)),
            Self::Output(error) => Self::Output(error.called_from(stack)),
        }
    }

    /// `except` の中で起きたものとして、処理中の例外をつなぐ([`raised_within`])。
    fn raised_within(self, handled: PyException) -> Self {
        match self {
            Self::Raise(error) => Self::Raise(raised_within(error, handled)),
            Self::Output(error) => Self::Output(raised_within(error, handled)),
        }
    }
}

impl From<PyException> for Fail {
    fn from(error: PyException) -> Self {
        Self::Raise(error)
    }
}

/// [`check_output`] の結果の失敗。
#[derive(Debug)]
pub enum Rejected {
    /// `OutputError`(旧版の main は `except OutputError as exc:` で捕まえ、`ERROR 出力を拒否: <文>`
    /// と終了 2)。呼出履歴は `output_paths` から下(呼出元が `main`・`write_xlsx` の段を足す)。
    /// 正本・出力先を解決できないときは、元の `OSError`・`RuntimeError` を `__cause__` に持つ
    /// (`raise OutputError(…) from exc`)。
    Output(PyException),
    /// 捕まえられない例外(`output_paths` から下の呼出履歴つき。呼出元が外側の段を足す)。
    Raised(PyException),
}

/// `OutputError(文)`。
fn output_error(message: PyStr) -> PyException {
    PyException::new(OUTPUT_ERROR, message)
}

/// `OSError`(ファイル名つき)。errno の無い失敗(パスの中の NUL)は `ValueError`。
fn os_error(error: &io::Error, filename: &[u8]) -> PyException {
    match error.raw_os_error() {
        Some(number) => {
            PyException::os_with_filename(number, Filename::Str(PyStr::from_os_bytes(filename)))
        }
        None => PyException::new(types::VALUE_ERROR, "embedded null byte"),
    }
}

fn errno_of(error: &PyException) -> Option<i32> {
    error.errno
}

/// `pathlib._ignore_error`(ENOENT・ENOTDIR・EBADF・ELOOP)。
fn ignored(error: &PyException) -> bool {
    matches!(errno_of(error), Some(ENOENT | ENOTDIR | EBADF | ELOOP))
}

/// `except FileNotFoundError`。
fn is_file_not_found(error: &PyException) -> bool {
    error.kind == types::FILE_NOT_FOUND_ERROR
}

/// 処理中の例外 `handled` の中で新しく起きた例外の `__context__`(既にあれば変えない)。
fn raised_while(mut error: PyException, handled: &PyException) -> PyException {
    if error.context.is_none() && error.cause.is_none() && error != *handled {
        error.context = Some(handled.clone());
    }
    error
}

/// `except` の中(`handled` を処理している間)に呼んだ処理から出た例外の連鎖をつなぐ。
///
/// その間に最初に起きた例外(`__context__` を辿った最後)が、`handled` を `__context__` に持つ
/// (CPython は例外を起こす時点で、処理中の例外をつなぐ)。呼出元は、同じ例外に 1 回だけ使う。
fn raised_within(mut error: PyException, handled: PyException) -> PyException {
    let mut last = &mut error;
    while last.context.is_some() {
        last = last.context.as_mut().expect("直前で確かめた");
    }
    last.context = Some(handled);
    error
}

// ---------------------------------------------------------------- OS の呼出し

/// 現在のディレクトリを得る関数(`os.getcwd()`)。
pub type Getcwd<'a> = &'a dyn Fn() -> io::Result<Vec<u8>>;

/// 製品の `os.getcwd()`(長いパスでも glibc と同じに得る `dev_workflow_platform::r4e::current_dir`)。
pub fn system_getcwd() -> io::Result<Vec<u8>> {
    dev_workflow_platform::r4e::current_dir().map(|path| path.into_os_string().into_vec())
}

fn sys_stat(path: &[u8], follow: bool) -> io::Result<std::fs::Metadata> {
    let path = std::path::Path::new(OsStr::from_bytes(path));
    if follow {
        std::fs::metadata(path)
    } else {
        std::fs::symlink_metadata(path)
    }
}

/// `os.getcwd()` の `OSError`(ファイル名なし)。
fn getcwd(cwd: Getcwd) -> Result<Vec<u8>, PyException> {
    cwd().map_err(|error| PyException::os(error.raw_os_error().unwrap_or(EINVAL)))
}

// ---------------------------------------------------------------- pathlib

/// `os.fspath(path)` の中の呼出し(`_str` を作る)。
fn fspath(depth: &Depth, path: &PyPath) -> Result<(), PyException> {
    let events = fspath_events(path);
    if let Some(index) = depth.first_over(events) {
        return Err(fspath_exceeded(path, index));
    }
    path.str_cached.set(true);
    Ok(())
}

fn fspath_events(path: &PyPath) -> &'static [u8] {
    if path.str_cached.get() {
        FSPATH_CACHED
    } else {
        FSPATH_UNCACHED
    }
}

/// `os.fspath(path)` の中の `index` 番目の呼出しで上限を超えた `RecursionError`。
///
/// 入った段(`__fspath__` → `__str__` → `_format_parsed_parts`)を呼出履歴に足す。`_str` がまだ無い
/// ときの `_format_parsed_parts` の段の所は `__str__` の `except AttributeError:` の中なので、その
/// `AttributeError`(625 行)を `__context__` に持つ(旧版の実測)。
fn fspath_exceeded(path: &PyPath, index: usize) -> PyException {
    let events = fspath_events(path);
    let format = if path.is_absolute() {
        frames::PATHLIB_FORMAT_ROOTED
    } else {
        frames::PATHLIB_FORMAT_RELATIVE
    };
    let entered: &[Frame] = match index {
        0 => &[],
        1..=3 => &[frames::PATHLIB_FSPATH],
        4 => &[frames::PATHLIB_FSPATH, frames::PATHLIB_STR],
        _ => &[frames::PATHLIB_FSPATH, frames::PATHLIB_STR, format],
    };
    let error = Depth::exceeded(events[index]);
    let error = if index >= 4 {
        let missing = PyException::new(
            types::ATTRIBUTE_ERROR,
            "'PosixPath' object has no attribute '_str'",
        )
        .called_from(&[frames::PATHLIB_STR_CACHED]);
        error.with_context(missing)
    } else {
        error
    };
    error.called_from(entered)
}

/// `_joinrealpath` の段に入った直後の呼出し([`JOINREALPATH_LEVEL`])。上限を超えたら、その所までの
/// 段(`isinstance` の 402 行、`isabs` の 411 行 → `os.fspath` の 62 行、`_get_sep` の 63 行 → 42 行)
/// を呼出履歴に足す。
fn joinrealpath_entry(depth: &Depth) -> Result<(), PyException> {
    let Some(index) = depth.first_over(JOINREALPATH_LEVEL) else {
        return Ok(());
    };
    let entered: &[Frame] = match index {
        0 => &[frames::JOINREALPATH_ISINSTANCE],
        1 => &[frames::JOINREALPATH_ISABS, frames::ISABS_FSPATH],
        _ => &[
            frames::JOINREALPATH_ISABS,
            frames::ISABS_GET_SEP,
            frames::GET_SEP_ISINSTANCE,
        ],
    };
    Err(Depth::exceeded(JOINREALPATH_LEVEL[index]).called_from(entered))
}

/// Python の関数の段に入り、中から出た例外にだけ、その段の行を足す(入る所で上限を超えた
/// `RecursionError` は呼出元の段で起きるので足さない)。
fn in_frame<T>(
    depth: &mut Depth,
    line: Frame,
    body: impl FnOnce(&mut Depth) -> Result<T, PyException>,
) -> Result<T, PyException> {
    depth.frame(|depth| body(depth).map_err(|error| error.called_from(&[line])))
}

/// `Path.stat(follow_symlinks=…)`(段 → `os.stat` → `os.fspath`)。失敗は `stat` の段(1097 行)から。
fn path_stat(
    depth: &mut Depth,
    path: &PyPath,
    follow: bool,
) -> Result<std::fs::Metadata, PyException> {
    in_frame(depth, frames::PATHLIB_STAT, |depth| {
        depth.call(|depth| {
            fspath(depth, path)?;
            let bytes = path.to_bytes();
            sys_stat(&bytes, follow).map_err(|error| os_error(&error, &bytes))
        })
    })
}

/// `Path.lstat()`(`return self.stat(follow_symlinks=False)`)。
fn path_lstat(depth: &mut Depth, path: &PyPath) -> Result<std::fs::Metadata, PyException> {
    in_frame(depth, frames::PATHLIB_LSTAT, |depth| {
        path_stat(depth, path, false)
    })
}

/// `Path.is_dir()`(無い・リンクの環などは偽、ほかの誤りは例外)。
fn is_dir(depth: &mut Depth, path: &PyPath) -> Result<bool, PyException> {
    in_frame(
        depth,
        frames::PATHLIB_IS_DIR_STAT,
        |depth| match path_stat(depth, path, true) {
            Ok(info) => Ok(info.is_dir()),
            Err(error) if ignored(&error) || error.kind == types::VALUE_ERROR => Ok(false),
            Err(error) => Err(error),
        },
    )
}

/// `Path.exists()`。
fn exists(depth: &mut Depth, path: &PyPath) -> Result<bool, PyException> {
    in_frame(
        depth,
        frames::PATHLIB_EXISTS_STAT,
        |depth| match path_stat(depth, path, true) {
            Ok(_) => Ok(true),
            Err(error) if ignored(&error) || error.kind == types::VALUE_ERROR => Ok(false),
            Err(error) => Err(error),
        },
    )
}

/// `Path.samefile(other)`(`os.path.samestat`)。
fn samefile(depth: &mut Depth, path: &PyPath, other: &PyPath) -> Result<bool, PyException> {
    depth.frame(|depth| {
        let first = path_stat(depth, path, true)
            .map_err(|error| error.called_from(&[frames::PATHLIB_SAMEFILE_STAT]))?;
        let second = path_stat(depth, other, true)
            .map_err(|error| error.called_from(&[frames::PATHLIB_SAMEFILE_OTHER]))?;
        // samestat の段に入る所(F)は samefile の段、中の比較(Q)は samestat の段で起きる。
        if let Some(index) = depth.first_over(SAMESTAT) {
            let stack: &[Frame] = if index == 0 {
                &[frames::PATHLIB_SAMEFILE_SAMESTAT]
            } else {
                &[
                    frames::PATHLIB_SAMEFILE_SAMESTAT,
                    frames::GENERICPATH_SAMESTAT,
                ]
            };
            return Err(Depth::exceeded(SAMESTAT[index]).called_from(stack));
        }
        Ok(first.ino() == second.ino() && first.dev() == second.dev())
    })
}

/// `path.parent`(新しいオブジェクト)。
fn parent(depth: &Depth, path: &PyPath) -> Result<PyPath, PyException> {
    depth.chain(PARENT)?;
    Ok(path.parent_value())
}

/// `a == b`(`PurePath.__eq__`: `_cparts` を作って比べる)。
fn equals(depth: &Depth, left: &PyPath, right: &PyPath) -> Result<bool, PyException> {
    // 比較 → __eq__ の段 → isinstance(→ _cparts の property の段)。4 つ目は、まだ作っていない
    // _cparts の casefold_parts の段、なければ同じ長さの列の、同じでない要素の比較。
    let fourth = if !left.cparts_cached.get() || !right.cparts_cached.get() {
        Some(b'F')
    } else if left.parts.len() == right.parts.len()
        && (left.root == Root::Two && right.root == Root::Two || !left.same_parts(right))
    {
        Some(b'Q')
    } else {
        None
    };
    let mut events = b"QFC".to_vec();
    events.extend(fourth);
    depth.chain(&events)?;
    left.cparts_cached.set(true);
    right.cparts_cached.set(true);
    Ok(left.same_parts(right))
}

/// `path.name`。
fn name<'a>(depth: &Depth, path: &'a PyPath) -> Result<&'a [u8], PyException> {
    depth.chain(NAME)?;
    Ok(path.name())
}

/// `base / name`。
fn truediv(depth: &Depth, base: &PyPath, name: &[u8]) -> Result<PyPath, PyException> {
    depth.chain(if name.is_empty() {
        TRUEDIV_EMPTY
    } else {
        TRUEDIV
    })?;
    Ok(base.child(name))
}

/// `Path(text)`。
fn path_new(depth: &Depth, text: &[u8]) -> Result<PyPath, PyException> {
    depth.chain(PATH_NEW)?;
    Ok(PyPath::parse(text))
}

/// `path.absolute()`。
fn absolute(depth: &Depth, path: PyPath, cwd: Getcwd) -> Result<PyPath, PyException> {
    if path.is_absolute() {
        depth.chain(ABSOLUTE_ABSOLUTE)?;
        return Ok(path);
    }
    depth.chain(ABSOLUTE_RELATIVE)?;
    let base = PyPath::parse(
        &getcwd(cwd).map_err(|error| error.called_from(&[frames::PATHLIB_ABSOLUTE_GETCWD]))?,
    );
    let mut parts = base.parts;
    parts.extend(path.parts);
    Ok(PyPath::new(base.root, parts))
}

/// `Path.resolve(strict)`。リンクの環は `RuntimeError("Symlink loop from <repr>")`。
fn resolve(
    depth: &mut Depth,
    path: &PyPath,
    strict: bool,
    cwd: Getcwd,
) -> Result<PyPath, PyException> {
    depth.frame(|depth| {
        let real = depth
            .frame(|depth| realpath(depth, path, strict, cwd))
            .map_err(|failure| {
                check_eloop(
                    failure.called_from(&[frames::PATHLIB_RESOLVE_REALPATH]),
                    frames::PATHLIB_RESOLVE_CHECK_ELOOP,
                )
            })?;
        let resolved = PyPath::parse(&real);
        if !strict {
            // 環は realpath が `os.stat` で見ないので、ここで確かめる。ほかの OSError は捨てる。
            match path_stat(depth, &resolved, true) {
                Err(error) if error.errno == Some(ELOOP) => {
                    return Err(check_eloop(
                        OsFailure {
                            error: error.called_from(&[frames::PATHLIB_RESOLVE_STAT]),
                            filename: Some(resolved.to_bytes()),
                        },
                        frames::PATHLIB_RESOLVE_STAT_CHECK_ELOOP,
                    ));
                }
                Err(error) if !error.kind.is("OSError") => {
                    return Err(error.called_from(&[frames::PATHLIB_RESOLVE_STAT]));
                }
                _ => {}
            }
        }
        Ok(resolved)
    })
}

/// `realpath` の中の失敗。`OSError` なら `e.filename` も持つ(`check_eloop` が使う)。
#[derive(Debug)]
struct OsFailure {
    error: PyException,
    filename: Option<Vec<u8>>,
}

impl From<PyException> for OsFailure {
    fn from(error: PyException) -> Self {
        Self {
            error,
            filename: None,
        }
    }
}

impl OsFailure {
    /// OS の呼出しの失敗(`OSError(errno, 文, filename)`)。
    fn os(error: &io::Error, filename: &[u8]) -> Self {
        Self {
            error: os_error(error, filename),
            filename: Some(filename.to_vec()),
        }
    }

    fn called_from(mut self, stack: &[Frame]) -> Self {
        self.error = self.error.called_from(stack);
        self
    }
}

/// `check_eloop(e)`: `ELOOP` の `OSError` を `RuntimeError` にする(ほかはそのまま。呼出元の
/// `raise` がそのまま投げ直す)。`RuntimeError` は `resolve` の `call`(呼ぶ行)→ `check_eloop` の
/// `raise` の段で、処理中の `OSError` を `__context__` に持つ。
fn check_eloop(failure: OsFailure, call: Frame) -> PyException {
    let OsFailure { error, filename } = failure;
    let Some(filename) = filename.filter(|_| error.errno == Some(ELOOP)) else {
        return error;
    };
    let mut message = PyStr::from("Symlink loop from ");
    message.push_py(&repr(&PyStr::from_os_bytes(&filename)));
    raised_while(PyException::new(types::RUNTIME_ERROR, message), &error)
        .called_from(&[call, frames::PATHLIB_CHECK_ELOOP_RAISES])
}

// ---------------------------------------------------------------- posixpath

fn join(base: &[u8], name: &[u8]) -> Vec<u8> {
    if name.first() == Some(&b'/') {
        name.to_vec()
    } else if base.is_empty() || base.last() == Some(&b'/') {
        [base, name].concat()
    } else {
        [base, b"/", name].concat()
    }
}

/// `posixpath.split`。
fn split(path: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let index = path
        .iter()
        .rposition(|&byte| byte == b'/')
        .map_or(0, |i| i + 1);
    let (head, tail) = path.split_at(index);
    let mut head = head.to_vec();
    if !head.is_empty() && head.iter().any(|&byte| byte != b'/') {
        while head.last() == Some(&b'/') {
            head.pop();
        }
    }
    (head, tail.to_vec())
}

/// `posixpath.normpath`。
fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.first() == Some(&b'/'));
    if path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|&byte| byte == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part != b".." || initial == 0 && parts.is_empty() || parts.last() == Some(&&b".."[..]) {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let mut result = b"/".repeat(initial);
    result.extend_from_slice(&parts.join(&b'/'));
    if result.is_empty() {
        b".".to_vec()
    } else {
        result
    }
}

/// `posixpath.abspath`。
fn abspath(path: &[u8], cwd: Getcwd) -> Result<Vec<u8>, PyException> {
    if path.first() == Some(&b'/') {
        return Ok(normpath(path));
    }
    Ok(normpath(&join(&getcwd(cwd)?, path)))
}

/// `posixpath.realpath(filename, strict=…)` の段の中。
fn realpath(
    depth: &mut Depth,
    filename: &PyPath,
    strict: bool,
    cwd: Getcwd,
) -> Result<Vec<u8>, OsFailure> {
    depth
        .call(|depth| fspath(depth, filename))
        .map_err(|error| error.called_from(&[frames::REALPATH_FSPATH]))?;
    let text = filename.to_bytes();
    let mut seen = HashMap::new();
    let (path, _) = depth
        .frame(|depth| joinrealpath(depth, Some(Vec::new()), &text, strict, &mut seen))
        .map_err(|failure| failure.called_from(&[frames::REALPATH_JOINS]))?;
    match path {
        Some(path) => Ok(abspath(&path, cwd).map_err(|error| {
            error.called_from(&[frames::REALPATH_ABSPATH, frames::ABSPATH_GETCWD])
        })?),
        None => Err(none_type_error()
            .called_from(&[frames::REALPATH_ABSPATH, frames::ABSPATH_FSPATH])
            .into()),
    }
}

/// `os.fspath(None)` の `TypeError`。
fn none_type_error() -> PyException {
    PyException::new(
        TYPE_ERROR,
        "expected str, bytes or os.PathLike object, not NoneType",
    )
}

/// `seen`(リンクのパス → 解決したパス。解決の途中は `None`)。
type Seen = HashMap<Vec<u8>, Option<Vec<u8>>>;

/// `_joinrealpath(path, rest, strict, seen)` の段の中(段には呼出元が入っている)。
///
/// 旧版はリンクを読むたびに同じ関数を呼ぶ。ここでは、待っている段(残りの `rest` と、解決中の
/// `newpath`)を積んで繰り返しで行い、段の深さだけを [`Depth`] で数える。`path` の `None` は、旧版の
/// `seen[newpath]` の `None`(環を見つけても `os.stat` が通ったとき)。
fn joinrealpath(
    depth: &mut Depth,
    path: Option<Vec<u8>>,
    rest: &[u8],
    strict: bool,
    seen: &mut Seen,
) -> Result<(Option<Vec<u8>>, bool), OsFailure> {
    let mut waiting: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    let result = join_levels(depth, path, rest, strict, seen, &mut waiting);
    // 例外のときは、待っている段から出る(旧版の各段の 456 行目の呼出しを通って外へ出る)。
    let nested = waiting.len();
    depth.now -= nested as u32;
    result.map_err(|failure| failure.called_from(&vec![frames::JOINREALPATH_RECURSES; nested]))
}

/// `if isabs(rest): rest = rest[1:]; path = sep`。
fn start_level(path: &mut Option<Vec<u8>>, rest: &mut Vec<u8>) {
    if rest.first() == Some(&b'/') {
        rest.remove(0);
        *path = Some(b"/".to_vec());
    }
}

/// [`joinrealpath`] の繰り返し。`waiting` は外側の段(残りの `rest` と、その段が解決中のリンク)。
fn join_levels(
    depth: &mut Depth,
    mut path: Option<Vec<u8>>,
    rest: &[u8],
    strict: bool,
    seen: &mut Seen,
    waiting: &mut Vec<(Vec<u8>, Vec<u8>)>,
) -> Result<(Option<Vec<u8>>, bool), OsFailure> {
    joinrealpath_entry(depth)?;
    let mut rest = rest.to_vec();
    start_level(&mut path, &mut rest);
    loop {
        if rest.is_empty() {
            // この段は (path, True) を返す。外側の段は seen[newpath] = path として続ける。
            let Some((outer_rest, newpath)) = waiting.pop() else {
                return Ok((path, true));
            };
            depth.now -= 1;
            seen.insert(newpath, path.clone());
            rest = outer_rest;
            continue;
        }
        let (name, after) = match rest.iter().position(|&byte| byte == b'/') {
            Some(index) => (rest[..index].to_vec(), rest[index + 1..].to_vec()),
            None => (std::mem::take(&mut rest), Vec::new()),
        };
        rest = after;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            path = Some(match &path {
                Some(current) if !current.is_empty() => {
                    let (head, tail) = split(current);
                    if tail == b".." {
                        join(&join(&head, b".."), b"..")
                    } else {
                        head
                    }
                }
                _ => b"..".to_vec(),
            });
            continue;
        }
        let Some(current) = &path else {
            return Err(none_type_error()
                .called_from(&[frames::JOINREALPATH_JOINS, frames::JOIN_FSPATH])
                .into());
        };
        let newpath = join(current, &name);
        let is_link = match sys_stat(&newpath, false) {
            Ok(info) => info.file_type().is_symlink(),
            Err(error) if strict => {
                return Err(
                    OsFailure::os(&error, &newpath).called_from(&[frames::JOINREALPATH_LSTAT])
                );
            }
            Err(_) => false,
        };
        if !is_link {
            path = Some(newpath);
            continue;
        }
        if let Some(cached) = seen.get(&newpath) {
            path = cached.clone();
            if path.is_some() {
                continue;
            }
            if !strict {
                // return join(newpath, rest), False。外側の段も join(path, rest), False で返す。
                let mut joined = join(&newpath, &rest);
                while let Some((outer_rest, _)) = waiting.pop() {
                    depth.now -= 1;
                    joined = join(&joined, &outer_rest);
                }
                return Ok((Some(joined), false));
            }
            if let Err(error) = sys_stat(&newpath, true) {
                return Err(
                    OsFailure::os(&error, &newpath).called_from(&[frames::JOINREALPATH_STAT])
                );
            }
        }
        seen.insert(newpath.clone(), None);
        let target = std::fs::read_link(OsStr::from_bytes(&newpath))
            .map_err(|error| {
                OsFailure::os(&error, &newpath).called_from(&[frames::JOINREALPATH_RECURSES])
            })?
            .into_os_string()
            .into_vec();
        // 次の段に入る(入れなければ、この段の 456 行目で起きる)。
        if depth.now >= depth.limit {
            return Err(OsFailure::from(Depth::exceeded(b'F'))
                .called_from(&[frames::JOINREALPATH_RECURSES]));
        }
        depth.now += 1;
        waiting.push((std::mem::replace(&mut rest, target), newpath));
        joinrealpath_entry(depth)?;
        start_level(&mut path, &mut rest);
    }
}

// ---------------------------------------------------------------- 旧スクリプト

/// `physical_directory(path)`(`path` は新しいオブジェクト。段に入るのもここで行う)。
///
/// 旧版は無い親ごとに同じ関数を呼ぶ。ここでは、`except FileNotFoundError:` の中の段(その段の名前と
/// 例外)を積んで下り、残る段から順に `/ path.name` と `.resolve()` をして上る。段の深さだけを
/// [`Depth`] で数える。
///
/// 例外(`OutputError` も)は旧版と同じ連鎖にする。下の段の `lstat` の `FileNotFoundError` は、上の
/// 段が処理中の `FileNotFoundError` の中で起きるので、それを `__context__` に持つ。ある段の中で
/// 起きた例外は、その段より上の段が処理中の例外の連鎖につながり、上の各段の 111 行を通って出る。
/// 処理中の例外の連鎖は、例外が起きたときにだけ作る(深い再帰で連鎖を写し続けないため)。
fn physical_directory(depth: &mut Depth, path: &PyPath, cwd: Getcwd) -> Result<PyPath, Fail> {
    let mut handlers: Vec<(Vec<u8>, PyException)> = Vec::new();
    let mut owned: Option<PyPath> = None;
    let mut outcome = loop {
        let target = owned.as_ref().unwrap_or(path);
        if depth.now >= depth.limit {
            // 段に入れない(呼出元の段の中で起きる)。
            break Err(Fail::Raise(Depth::exceeded(b'F')));
        }
        depth.now += 1;
        match physical_level(depth, target, cwd) {
            Level::Missing {
                name,
                handled,
                parent,
            } => {
                handlers.push((name, handled));
                owned = Some(parent);
            }
            Level::Done(result) => {
                depth.now -= 1;
                break result;
            }
        }
    };
    let mut linked = false;
    while let Some((name, handled)) = handlers.pop() {
        // return (physical_directory(path.parent) / path.name).resolve()
        outcome = outcome.and_then(|physical| {
            depth.chain(NAME)?;
            let child = truediv(depth, &physical, &name)?;
            Ok(resolve(depth, &child, false, cwd)?)
        });
        outcome = outcome.map_err(|fail| {
            let fail = if linked {
                fail
            } else {
                linked = true;
                fail.raised_within(handled_chain(&handlers, handled))
            };
            fail.called_from(&[frames::PHYSICAL_RECURSES])
        });
        depth.now -= 1;
    }
    outcome
}

/// 処理中の `FileNotFoundError` の連鎖: 一番内側の `innermost` から、上の段(`outer` の後ろから前)の
/// 例外へ `__context__` でつなぐ。
fn handled_chain(outer: &[(Vec<u8>, PyException)], mut innermost: PyException) -> PyException {
    let mut chain: Option<PyException> = None;
    for (_, handled) in outer {
        let mut handled = handled.clone();
        handled.context = chain;
        chain = Some(handled);
    }
    innermost.context = chain;
    innermost
}

/// `physical_directory` の 1 つの段の結果。
enum Level {
    /// 無い親(`except FileNotFoundError:` の中で、親の段を呼ぶ所まで)。
    Missing {
        name: Vec<u8>,
        handled: PyException,
        parent: PyPath,
    },
    Done(Result<PyPath, Fail>),
}

/// `physical_directory` の 1 つの段(段には入っている)。
fn physical_level(depth: &mut Depth, path: &PyPath, cwd: Getcwd) -> Level {
    let info = match path_lstat(depth, path) {
        Ok(info) => info,
        Err(error) if is_file_not_found(&error) => {
            let handled = error.called_from(&[frames::PHYSICAL_LSTAT]);
            return match missing_parent(depth, path) {
                Ok(Some(parent)) => Level::Missing {
                    name: path.name().to_vec(),
                    handled,
                    parent,
                },
                Ok(None) => {
                    let mut message = PyStr::from("出力先の親を解決できない: ");
                    message.push_py(&path.to_py());
                    Level::Done(Err(Fail::Output(
                        raised_while(output_error(message), &handled)
                            .called_from(&[frames::PHYSICAL_RAISES_NO_PARENT]),
                    )))
                }
                Err(error) => Level::Done(Err(Fail::Raise(
                    raised_while(error, &handled).called_from(&[frames::PHYSICAL_CHECKS_PARENT]),
                ))),
            };
        }
        Err(error) => {
            return Level::Done(Err(error.called_from(&[frames::PHYSICAL_LSTAT]).into()));
        }
    };
    let kind = info.file_type();
    if !(kind.is_dir() || kind.is_symlink()) {
        return Level::Done(Err(not_directory(path, frames::PHYSICAL_RAISES_NOT_DIR)));
    }
    let resolved = match resolve(depth, path, true, cwd) {
        Ok(resolved) => resolved,
        Err(error) => {
            return Level::Done(Err(error.called_from(&[frames::PHYSICAL_RESOLVES]).into()));
        }
    };
    match is_dir(depth, &resolved) {
        Ok(true) => Level::Done(Ok(resolved)),
        Ok(false) => Level::Done(Err(not_directory(
            path,
            frames::PHYSICAL_RAISES_NOT_DIR_RESOLVED,
        ))),
        Err(error) => Level::Done(Err(error.called_from(&[frames::PHYSICAL_IS_DIR]).into())),
    }
}

/// `if path == path.parent:` と、再帰に渡す `path.parent`。根(親が自分)なら `None`。
fn missing_parent(depth: &mut Depth, path: &PyPath) -> Result<Option<PyPath>, PyException> {
    let same = if path.parent_is_self() {
        depth.chain(PARENT_SELF)?;
        equals(depth, path, path)?
    } else {
        let parent_object = parent(depth, path)?;
        equals(depth, path, &parent_object)?
    };
    if same {
        return Ok(None);
    }
    Ok(Some(parent(depth, path)?))
}

/// `raise OutputError(f"出力先の親がディレクトリでない: {path}")`(113 行か 116 行)。
fn not_directory(path: &PyPath, line: Frame) -> Fail {
    rejected("出力先の親がディレクトリでない: ", path, line)
}

/// `raise OutputError(f"<prefix>{path}")`(`line` はその行)。
fn rejected(prefix: &str, path: &PyPath, line: Frame) -> Fail {
    let mut message = PyStr::from(prefix);
    message.push_py(&path.to_py());
    Fail::Output(output_error(message).called_from(&[line]))
}

/// `output_paths([out], inputs=inputs, source_roots=source_roots)` を、旧版の `write_xlsx` の段
/// (深さ 3)から呼ぶ。成功なら物理パス(旧版の `checked_out`)。
///
/// `inputs` は旧版の main が読めた入力の引数(`Path(p)`)、`source_roots` は `args.source_root`。
pub fn check_output(
    out: &PyPath,
    inputs: &[PyPath],
    source_roots: &[Value],
) -> Result<PyPath, Rejected> {
    output_paths(
        &mut Depth::new(3),
        out,
        inputs,
        source_roots,
        &system_getcwd,
    )
}

/// [`check_output`] の深さと `os.getcwd()` を渡す形。`depth` は呼出元の段。
pub fn output_paths(
    depth: &mut Depth,
    out: &PyPath,
    inputs: &[PyPath],
    source_roots: &[Value],
    cwd: Getcwd,
) -> Result<PyPath, Rejected> {
    let result = depth.frame(|depth| output_paths_body(depth, out, inputs, source_roots, cwd));
    match result {
        Ok(dest) => Ok(dest),
        Err(Fail::Output(error)) => Err(Rejected::Output(error)),
        Err(Fail::Raise(error)) if error.kind.is("OSError") || error.kind.is("RuntimeError") => {
            // except (OSError, RuntimeError) as exc:
            //     raise OutputError(f"出力先または正本を解決できない: {exc}") from exc
            let mut message = PyStr::from("出力先または正本を解決できない: ");
            message.push_py(&error.message);
            Err(Rejected::Output(
                output_error(message)
                    .with_cause(error)
                    .called_from(&[frames::OUTPUT_PATHS_RAISES_FROM]),
            ))
        }
        Err(Fail::Raise(error)) => Err(Rejected::Raised(error)),
    }
}

/// `output_paths` の `try:` の中。
fn output_paths_body(
    depth: &mut Depth,
    out: &PyPath,
    inputs: &[PyPath],
    source_roots: &[Value],
    cwd: Getcwd,
) -> Result<PyPath, Fail> {
    // input_paths = [Path(p) for p in inputs]
    let input_paths = if inputs.is_empty() {
        Vec::new()
    } else {
        depth
            .frame(|depth| {
                inputs
                    .iter()
                    .map(|input| {
                        depth.chain(PATH_NEW)?;
                        Ok(input.copy())
                    })
                    .collect::<Result<Vec<_>, PyException>>()
                    .map_err(|error| error.called_from(&[frames::INPUT_PATHS_LISTCOMP]))
            })
            .map_err(|error| error.called_from(&[frames::OUTPUT_PATHS_INPUT_PATHS]))?
    };
    // inputs = [p.resolve(strict=True) for p in input_paths]
    let resolved_inputs = if input_paths.is_empty() {
        Vec::new()
    } else {
        depth
            .frame(|depth| {
                input_paths
                    .iter()
                    .map(|input| resolve(depth, input, true, cwd))
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(|error| error.called_from(&[frames::INPUTS_LISTCOMP]))
            })
            .map_err(|error| error.called_from(&[frames::OUTPUT_PATHS_INPUTS]))?
    };
    // roots = [Path(p).resolve(strict=True) for p in source_roots]
    let mut roots = if source_roots.is_empty() {
        Vec::new()
    } else {
        depth
            .frame(|depth| {
                let mut roots = Vec::new();
                for root in source_roots {
                    let Value::Str(text) = root else {
                        depth.chain(PATH_NEW_TYPE_ERROR)?;
                        return Err(PyException::new(
                            TYPE_ERROR,
                            "expected str, bytes or os.PathLike object, not list",
                        )
                        .called_from(&[
                            frames::ROOTS_LISTCOMP,
                            frames::PATHLIB_NEW,
                            frames::PATHLIB_FROM_PARTS,
                            frames::PATHLIB_PARSE_ARGS,
                        ]));
                    };
                    let path = path_new(depth, text.as_bytes())
                        .map_err(|error| error.called_from(&[frames::ROOTS_LISTCOMP]))?;
                    roots.push(
                        resolve(depth, &path, true, cwd)
                            .map_err(|error| error.called_from(&[frames::ROOTS_LISTCOMP]))?,
                    );
                }
                Ok(roots)
            })
            .map_err(|error: PyException| error.called_from(&[frames::OUTPUT_PATHS_ROOTS]))?
    };
    if roots.is_empty() {
        // roots = [p.parent.resolve(strict=True) for p in input_paths]
        if !input_paths.is_empty() {
            roots = depth
                .frame(|depth| {
                    input_paths
                        .iter()
                        .map(|input| {
                            let parent_object = parent(depth, input)?;
                            resolve(depth, &parent_object, true, cwd)
                        })
                        .collect::<Result<Vec<_>, _>>()
                        .map_err(|error| error.called_from(&[frames::PARENTS_LISTCOMP]))
                })
                .map_err(|error| error.called_from(&[frames::OUTPUT_PATHS_PARENTS]))?;
        }
        // roots.extend(p.parent for p in inputs)
        if !resolved_inputs.is_empty() {
            let extra = generator(
                depth,
                (frames::OUTPUT_PATHS_EXTEND, frames::EXTEND_GENEXPR),
                |depth| {
                    resolved_inputs
                        .iter()
                        .map(|input| parent(depth, input))
                        .collect::<Result<Vec<_>, _>>()
                },
            )?;
            roots.extend(extra);
        }
    }
    // if any(not root.is_dir() for root in roots)
    if !roots.is_empty() {
        let any_not_dir = generator(
            depth,
            (
                frames::OUTPUT_PATHS_ROOTS_ARE_DIRS,
                frames::ROOTS_ARE_DIRS_GENEXPR,
            ),
            |depth| {
                for root in &roots {
                    if !is_dir(depth, root)? {
                        return Ok(true);
                    }
                }
                Ok(false)
            },
        )?;
        if any_not_dir {
            return Err(Fail::Output(
                output_error(PyStr::from("正本ディレクトリが通常のディレクトリでない"))
                    .called_from(&[frames::OUTPUT_PATHS_RAISES_ROOT_NOT_DIR]),
            ));
        }
    }
    // path = Path(path).absolute()
    depth
        .chain(PATH_NEW)
        .map_err(|error| error.called_from(&[frames::OUTPUT_PATHS_ABSOLUTE]))?;
    let path = absolute(depth, out.copy(), cwd)
        .map_err(|error| error.called_from(&[frames::OUTPUT_PATHS_ABSOLUTE]))?;
    // dest = physical_directory(path.parent) / path.name
    let parent_object = parent(depth, &path)?;
    let physical = physical_directory(depth, &parent_object, cwd)
        .map_err(|fail| fail.called_from(&[frames::OUTPUT_PATHS_PHYSICAL]))?;
    let dest = truediv(depth, &physical, name(depth, &path)?)?;
    let info = match path_lstat(depth, &dest) {
        Ok(info) => Some(info),
        Err(error) if is_file_not_found(&error) => None,
        Err(error) => return Err(error.called_from(&[frames::OUTPUT_PATHS_DEST_LSTAT]).into()),
    };
    if let Some(info) = info {
        if !info.file_type().is_file() {
            return Err(rejected(
                "出力先が通常ファイルでない（リンクも拒否）: ",
                &path,
                frames::OUTPUT_PATHS_RAISES_NOT_REGULAR,
            ));
        }
        if info.nlink() > 1 {
            return Err(rejected(
                "出力先に複数のhardlinkがある: ",
                &path,
                frames::OUTPUT_PATHS_RAISES_HARDLINK,
            ));
        }
        // any(dest.samefile(p) for p in inputs)
        let same = generator(
            depth,
            (frames::OUTPUT_PATHS_SAME_INPUT, frames::SAME_INPUT_GENEXPR),
            |depth| {
                for input in &resolved_inputs {
                    if samefile(depth, &dest, input)? {
                        return Ok(true);
                    }
                }
                Ok(false)
            },
        )?;
        if same {
            return Err(rejected(
                "出力先が入力ファイルと同じ: ",
                &path,
                frames::OUTPUT_PATHS_RAISES_SAME_INPUT,
            ));
        }
    }
    for ancestor in dest.parents() {
        // any(parent == root or (parent.exists() and parent.samefile(root)) for root in roots)
        let inside = generator(
            depth,
            (
                frames::OUTPUT_PATHS_INSIDE_ROOT,
                frames::INSIDE_ROOT_GENEXPR,
            ),
            |depth| {
                for root in &roots {
                    if equals(depth, &ancestor, root)?
                        || exists(depth, &ancestor)? && samefile(depth, &ancestor, root)?
                    {
                        return Ok(true);
                    }
                }
                Ok(false)
            },
        )?;
        if inside {
            return Err(rejected(
                "出力先が正本ディレクトリ内にある: ",
                &path,
                frames::OUTPUT_PATHS_RAISES_INSIDE,
            ));
        }
    }
    Ok(dest)
}

/// `any(<生成器>)`・`list.extend(<生成器>)` のように、C の関数が生成器の段を動かす所。例外は
/// `output_paths` の `line`(呼ぶ行)と、中から出たものには生成器の段 `genexpr`(同じ行)を通る。
fn generator<T>(
    depth: &mut Depth,
    (line, genexpr): (Frame, Frame),
    body: impl FnOnce(&mut Depth) -> Result<T, PyException>,
) -> Result<T, PyException> {
    depth
        .call(|depth| in_frame(depth, genexpr, body))
        .map_err(|error| error.called_from(&[line]))
}

/// 旧版の main の `out = Path(args.output)`。値が空のリスト(`-o=--`)なら `TypeError`
/// (`pathlib` の段つき。呼出元が `<module>` と `main` の段を足す)。
pub fn output_argument(value: &Value) -> Result<PyPath, PyException> {
    match value {
        Value::Str(text) => Ok(PyPath::parse(text.as_bytes())),
        Value::EmptyList => Err(PyException::new(
            TYPE_ERROR,
            "expected str, bytes or os.PathLike object, not list",
        )
        .called_from(&[
            frames::PATHLIB_NEW,
            frames::PATHLIB_FROM_PARTS,
            frames::PATHLIB_PARSE_ARGS,
        ])),
    }
}

// ---------------------------------------------------------------- Path.mkdir

/// `checked_out.parent.mkdir(parents=True, exist_ok=True)` を、旧版の `write_xlsx` の段(深さ 3)から
/// 呼ぶ。`dest` は [`check_output`] の結果。失敗は `pathlib` の段からの呼出履歴(連鎖を含む。呼出元が
/// `<module>`・`main`・`write_xlsx` の段を足す)。
pub fn make_parents(dest: &PyPath) -> Result<(), PyException> {
    make_parents_at(&mut Depth::new(3), dest)
}

/// [`make_parents`] の深さを渡す形。
pub fn make_parents_at(depth: &mut Depth, dest: &PyPath) -> Result<(), PyException> {
    let directory = parent(depth, dest)?;
    mkdir_parents(depth, directory)
}

/// `os.mkdir(path, 0o777)` を、`os.fspath` の呼出しの深さとともに。上限を超えた所に応じて、
/// `__fspath__`・`__str__`・`_format_parsed_parts` の段を呼出履歴に足す。
fn accessor_mkdir(depth: &Depth, path: &PyPath) -> Result<(), PyException> {
    let events = [&b"C"[..], fspath_events(path)].concat();
    if let Some(index) = depth.first_over(&events) {
        return Err(match index {
            0 => Depth::exceeded(b'C'),
            _ => fspath_exceeded(path, index - 1),
        });
    }
    path.str_cached.set(true);
    let bytes = path.to_bytes();
    std::fs::DirBuilder::new()
        .mode(0o777)
        .create(OsStr::from_bytes(&bytes))
        .map_err(|error| os_error(&error, &bytes))
}

/// `Path.mkdir` の 1 回の段の結果。
enum Attempt {
    Done(Result<(), PyException>),
    /// `except FileNotFoundError:` で親を作りに行く(その例外)。
    Missing(PyException),
}

/// `Path.mkdir(mode, parents, exist_ok)` の段の中(段には入っている)。`handling` は呼出元が処理中の
/// 例外(ここで新しく起きた例外の `__context__`)。親を作りに行く所で止める。
fn mkdir_attempt(
    depth: &mut Depth,
    path: &PyPath,
    parents: bool,
    exist_ok: bool,
    handling: Option<&PyException>,
) -> Attempt {
    let error = match accessor_mkdir(depth, path) {
        Ok(()) => return Attempt::Done(Ok(())),
        Err(error) => {
            // os.fspath の中の RecursionError は、`__str__` の AttributeError を __context__ に
            // 持つことがある。処理中の例外は、その連鎖の最後につなぐ。
            let error = match handling {
                Some(handled) => raised_within(error, handled.clone()),
                None => error,
            };
            error.called_from(&[frames::PATHLIB_MKDIR_ACCESSOR])
        }
    };
    if is_file_not_found(&error) {
        if !parents {
            return Attempt::Done(Err(error));
        }
        // if not parents or self.parent == self: raise
        let same = if path.parent_is_self() {
            depth
                .chain(PARENT_SELF)
                .and_then(|()| equals(depth, path, path))
        } else {
            parent(depth, path).and_then(|parent_object| equals(depth, path, &parent_object))
        };
        return match same {
            Ok(true) => Attempt::Done(Err(error)),
            Ok(false) => Attempt::Missing(error),
            Err(inner) => Attempt::Done(Err(
                raised_while(inner, &error).called_from(&[frames::PATHLIB_MKDIR_CHECKS_PARENT])
            )),
        };
    }
    // except OSError:(RecursionError などは当たらない)
    if error.errno.is_none() || !exist_ok {
        return Attempt::Done(Err(error));
    }
    Attempt::Done(match is_dir(depth, path) {
        Ok(true) => Ok(()),
        Ok(false) => Err(error),
        // is_dir の段(1305 行)と stat の段(1097 行)は is_dir が足す。
        Err(inner) => Err(raised_while(inner, &error).called_from(&[frames::PATHLIB_MKDIR_IS_DIR])),
    })
}

/// 1 つ深い段で `Path.mkdir` を始める(入れなければ、呼出元の段で起きる)。
fn enter(depth: &mut Depth) -> Result<(), PyException> {
    if depth.now >= depth.limit {
        return Err(Depth::exceeded(b'F'));
    }
    depth.now += 1;
    Ok(())
}

/// `top.mkdir(parents=True, exist_ok=True)`。
///
/// 旧版は無い親ごとに `self.parent.mkdir(parents=True, exist_ok=True)` を呼び、戻ってから
/// `self.mkdir(mode, parents=False, exist_ok=exist_ok)` を呼ぶ。ここでは `except FileNotFoundError:` の
/// 中の段(そのパスと例外)を積んで下り、上りながら 2 回目の作成をする。段の深さだけを [`Depth`] で数える。
fn mkdir_parents(depth: &mut Depth, top: PyPath) -> Result<(), PyException> {
    let mut handlers: Vec<(PyPath, PyException)> = Vec::new();
    let mut current = top;
    let mut outcome = loop {
        if let Err(error) = enter(depth) {
            break Err(error);
        }
        let handling = handlers.last().map(|(_, handled)| handled);
        match mkdir_attempt(depth, &current, true, true, handling) {
            Attempt::Done(result) => {
                depth.now -= 1;
                break result;
            }
            Attempt::Missing(handled) => match parent(depth, &current) {
                Ok(parent_object) => {
                    handlers.push((std::mem::replace(&mut current, parent_object), handled));
                }
                Err(inner) => {
                    depth.now -= 1;
                    break Err(
                        raised_while(inner, &handled).called_from(&[frames::PATHLIB_MKDIR_PARENT])
                    );
                }
            },
        }
    };
    while let Some((path, handled)) = handlers.pop() {
        // 親の作成(1179 行目)が終わったら、self.mkdir(mode, parents=False, exist_ok=True)(1180 行目)。
        outcome = match outcome {
            Err(error) => {
                Err(raised_while(error, &handled).called_from(&[frames::PATHLIB_MKDIR_PARENT]))
            }
            Ok(()) => {
                let second = enter(depth).and_then(|()| {
                    let result = match mkdir_attempt(depth, &path, false, true, Some(&handled)) {
                        Attempt::Done(result) => result,
                        Attempt::Missing(error) => Err(error),
                    };
                    depth.now -= 1;
                    result
                });
                second.map_err(|error| {
                    raised_while(error, &handled).called_from(&[frames::PATHLIB_MKDIR_SELF])
                })
            }
        };
        depth.now -= 1;
    }
    outcome
}

// ---------------------------------------------------------------- 呼出履歴の段

/// 旧スクリプト・`pathlib.py`・`posixpath.py`(本家 CPython 3.10.11)の段。
pub mod frames {
    use super::super::errors::{Frame, frames::SCRIPT};

    const PATHLIB: &str = "pathlib.py";
    const POSIXPATH: &str = "posixpath.py";
    const GENERICPATH: &str = "genericpath.py";

    // ---- 再帰の上限の RecursionError の段(`os.fspath`・`_joinrealpath` の入口)
    pub const PATHLIB_STR_CACHED: Frame =
        Frame::new(PATHLIB, 625, "__str__", "            return self._str");
    pub const REALPATH_FSPATH: Frame = Frame::new(
        POSIXPATH,
        395,
        "realpath",
        "    filename = os.fspath(filename)",
    );
    pub const JOINREALPATH_ISINSTANCE: Frame = Frame::new(
        POSIXPATH,
        402,
        "_joinrealpath",
        "    if isinstance(path, bytes):",
    );
    pub const JOINREALPATH_ISABS: Frame =
        Frame::new(POSIXPATH, 411, "_joinrealpath", "    if isabs(rest):");
    pub const ISABS_FSPATH: Frame = Frame::new(POSIXPATH, 62, "isabs", "    s = os.fspath(s)");
    pub const ISABS_GET_SEP: Frame = Frame::new(POSIXPATH, 63, "isabs", "    sep = _get_sep(s)");
    pub const GET_SEP_ISINSTANCE: Frame =
        Frame::new(POSIXPATH, 42, "_get_sep", "    if isinstance(path, bytes):");

    // ---- 拒否の例外(OutputError)と、その元の例外の段
    pub const PHYSICAL_RAISES_NO_PARENT: Frame = Frame::new(
        SCRIPT,
        110,
        "physical_directory",
        "            raise OutputError(f\"出力先の親を解決できない: {path}\")",
    );
    pub const PHYSICAL_RAISES_NOT_DIR: Frame = Frame::new(
        SCRIPT,
        113,
        "physical_directory",
        "        raise OutputError(f\"出力先の親がディレクトリでない: {path}\")",
    );
    pub const PHYSICAL_RAISES_NOT_DIR_RESOLVED: Frame = Frame::new(
        SCRIPT,
        116,
        "physical_directory",
        "        raise OutputError(f\"出力先の親がディレクトリでない: {path}\")",
    );
    pub const OUTPUT_PATHS_INPUT_PATHS: Frame = Frame::new(
        SCRIPT,
        123,
        "output_paths",
        "        input_paths = [Path(p) for p in inputs]",
    );
    pub const INPUT_PATHS_LISTCOMP: Frame = Frame::new(
        SCRIPT,
        123,
        "<listcomp>",
        "        input_paths = [Path(p) for p in inputs]",
    );
    pub const OUTPUT_PATHS_EXTEND: Frame = Frame::new(
        SCRIPT,
        128,
        "output_paths",
        "            roots.extend(p.parent for p in inputs)",
    );
    pub const EXTEND_GENEXPR: Frame = Frame::new(
        SCRIPT,
        128,
        "<genexpr>",
        "            roots.extend(p.parent for p in inputs)",
    );
    pub const OUTPUT_PATHS_ROOTS_ARE_DIRS: Frame = Frame::new(
        SCRIPT,
        129,
        "output_paths",
        "        if any(not root.is_dir() for root in roots):",
    );
    pub const ROOTS_ARE_DIRS_GENEXPR: Frame = Frame::new(
        SCRIPT,
        129,
        "<genexpr>",
        "        if any(not root.is_dir() for root in roots):",
    );
    pub const OUTPUT_PATHS_RAISES_ROOT_NOT_DIR: Frame = Frame::new(
        SCRIPT,
        130,
        "output_paths",
        "            raise OutputError(\"正本ディレクトリが通常のディレクトリでない\")",
    );
    pub const OUTPUT_PATHS_ABSOLUTE: Frame = Frame::new(
        SCRIPT,
        133,
        "output_paths",
        "            path = Path(path).absolute()",
    );
    pub const OUTPUT_PATHS_DEST_LSTAT: Frame = Frame::new(
        SCRIPT,
        136,
        "output_paths",
        "                info = dest.lstat()",
    );
    pub const OUTPUT_PATHS_RAISES_NOT_REGULAR: Frame = Frame::new(
        SCRIPT,
        141,
        "output_paths",
        "                    raise OutputError(f\"出力先が通常ファイルでない（リンクも拒否）: {path}\")",
    );
    pub const OUTPUT_PATHS_RAISES_HARDLINK: Frame = Frame::new(
        SCRIPT,
        143,
        "output_paths",
        "                    raise OutputError(f\"出力先に複数のhardlinkがある: {path}\")",
    );
    pub const OUTPUT_PATHS_SAME_INPUT: Frame = Frame::new(
        SCRIPT,
        144,
        "output_paths",
        "                if any(dest.samefile(p) for p in inputs):",
    );
    pub const SAME_INPUT_GENEXPR: Frame = Frame::new(
        SCRIPT,
        144,
        "<genexpr>",
        "                if any(dest.samefile(p) for p in inputs):",
    );
    pub const OUTPUT_PATHS_RAISES_SAME_INPUT: Frame = Frame::new(
        SCRIPT,
        145,
        "output_paths",
        "                    raise OutputError(f\"出力先が入力ファイルと同じ: {path}\")",
    );
    pub const OUTPUT_PATHS_INSIDE_ROOT: Frame = Frame::new(
        SCRIPT,
        148,
        "output_paths",
        "                if any(parent == root or (parent.exists() and parent.samefile(root)) for root in roots):",
    );
    pub const INSIDE_ROOT_GENEXPR: Frame = Frame::new(
        SCRIPT,
        148,
        "<genexpr>",
        "                if any(parent == root or (parent.exists() and parent.samefile(root)) for root in roots):",
    );
    pub const OUTPUT_PATHS_RAISES_INSIDE: Frame = Frame::new(
        SCRIPT,
        149,
        "output_paths",
        "                    raise OutputError(f\"出力先が正本ディレクトリ内にある: {path}\")",
    );
    pub const OUTPUT_PATHS_RAISES_FROM: Frame = Frame::new(
        SCRIPT,
        153,
        "output_paths",
        "        raise OutputError(f\"出力先または正本を解決できない: {exc}\") from exc",
    );
    pub const PATHLIB_SAMEFILE_STAT: Frame =
        Frame::new(PATHLIB, 1006, "samefile", "        st = self.stat()");
    pub const PATHLIB_SAMEFILE_OTHER: Frame = Frame::new(
        PATHLIB,
        1008,
        "samefile",
        "            other_st = other_path.stat()",
    );
    pub const PATHLIB_SAMEFILE_SAMESTAT: Frame = Frame::new(
        PATHLIB,
        1011,
        "samefile",
        "        return os.path.samestat(st, other_st)",
    );
    pub const PATHLIB_ABSOLUTE_GETCWD: Frame = Frame::new(
        PATHLIB,
        1062,
        "absolute",
        "        return self._from_parts([self._accessor.getcwd()] + self._parts)",
    );
    pub const PATHLIB_CHECK_ELOOP_RAISES: Frame = Frame::new(
        PATHLIB,
        1074,
        "check_eloop",
        "                raise RuntimeError(\"Symlink loop from %r\" % e.filename)",
    );
    pub const PATHLIB_RESOLVE_CHECK_ELOOP: Frame =
        Frame::new(PATHLIB, 1079, "resolve", "            check_eloop(e)");
    pub const PATHLIB_RESOLVE_STAT: Frame =
        Frame::new(PATHLIB, 1087, "resolve", "                p.stat()");
    pub const PATHLIB_RESOLVE_STAT_CHECK_ELOOP: Frame =
        Frame::new(PATHLIB, 1089, "resolve", "                check_eloop(e)");
    pub const PATHLIB_LSTAT: Frame = Frame::new(
        PATHLIB,
        1222,
        "lstat",
        "        return self.stat(follow_symlinks=False)",
    );
    pub const PATHLIB_EXISTS_STAT: Frame =
        Frame::new(PATHLIB, 1290, "exists", "            self.stat()");
    pub const ABSPATH_GETCWD: Frame =
        Frame::new(POSIXPATH, 384, "abspath", "            cwd = os.getcwd()");
    pub const JOINREALPATH_LSTAT: Frame = Frame::new(
        POSIXPATH,
        431,
        "_joinrealpath",
        "            st = os.lstat(newpath)",
    );
    pub const JOINREALPATH_STAT: Frame = Frame::new(
        POSIXPATH,
        451,
        "_joinrealpath",
        "                os.stat(newpath)",
    );
    pub const GENERICPATH_SAMESTAT: Frame = Frame::new(
        GENERICPATH,
        89,
        "samestat",
        "    return (s1.st_ino == s2.st_ino and",
    );

    pub const WRITE_XLSX_CHECKS: Frame = Frame::new(
        SCRIPT,
        172,
        "write_xlsx",
        "    checked_out, = output_paths([out], inputs=inputs, source_roots=source_roots)",
    );
    pub const WRITE_XLSX_MKDIR: Frame = Frame::new(
        SCRIPT,
        195,
        "write_xlsx",
        "    checked_out.parent.mkdir(parents=True, exist_ok=True)",
    );
    pub const MAIN_PATH_OUTPUT: Frame =
        Frame::new(SCRIPT, 226, "main", "    out = Path(args.output)");
    pub const PHYSICAL_LSTAT: Frame = Frame::new(
        SCRIPT,
        107,
        "physical_directory",
        "        info = path.lstat()",
    );
    pub const PHYSICAL_RECURSES: Frame = Frame::new(
        SCRIPT,
        111,
        "physical_directory",
        "        return (physical_directory(path.parent) / path.name).resolve()",
    );
    pub const PHYSICAL_CHECKS_PARENT: Frame = Frame::new(
        SCRIPT,
        109,
        "physical_directory",
        "        if path == path.parent:",
    );
    pub const PHYSICAL_IS_DIR: Frame = Frame::new(
        SCRIPT,
        115,
        "physical_directory",
        "    if not resolved.is_dir():",
    );
    pub const PHYSICAL_RESOLVES: Frame = Frame::new(
        SCRIPT,
        114,
        "physical_directory",
        "    resolved = path.resolve(strict=True)",
    );
    pub const OUTPUT_PATHS_INPUTS: Frame = Frame::new(
        SCRIPT,
        124,
        "output_paths",
        "        inputs = [p.resolve(strict=True) for p in input_paths]",
    );
    pub const INPUTS_LISTCOMP: Frame = Frame::new(
        SCRIPT,
        124,
        "<listcomp>",
        "        inputs = [p.resolve(strict=True) for p in input_paths]",
    );
    pub const OUTPUT_PATHS_ROOTS: Frame = Frame::new(
        SCRIPT,
        125,
        "output_paths",
        "        roots = [Path(p).resolve(strict=True) for p in source_roots]",
    );
    pub const ROOTS_LISTCOMP: Frame = Frame::new(
        SCRIPT,
        125,
        "<listcomp>",
        "        roots = [Path(p).resolve(strict=True) for p in source_roots]",
    );
    pub const OUTPUT_PATHS_PARENTS: Frame = Frame::new(
        SCRIPT,
        127,
        "output_paths",
        "            roots = [p.parent.resolve(strict=True) for p in input_paths]",
    );
    pub const PARENTS_LISTCOMP: Frame = Frame::new(
        SCRIPT,
        127,
        "<listcomp>",
        "            roots = [p.parent.resolve(strict=True) for p in input_paths]",
    );
    pub const OUTPUT_PATHS_PHYSICAL: Frame = Frame::new(
        SCRIPT,
        134,
        "output_paths",
        "            dest = physical_directory(path.parent) / path.name",
    );
    pub const PATHLIB_NEW: Frame = Frame::new(
        PATHLIB,
        960,
        "__new__",
        "        self = cls._from_parts(args)",
    );
    pub const PATHLIB_FROM_PARTS: Frame = Frame::new(
        PATHLIB,
        594,
        "_from_parts",
        "        drv, root, parts = self._parse_args(args)",
    );
    pub const PATHLIB_PARSE_ARGS: Frame = Frame::new(
        PATHLIB,
        578,
        "_parse_args",
        "                a = os.fspath(a)",
    );
    pub const PATHLIB_FSPATH: Frame =
        Frame::new(PATHLIB, 632, "__fspath__", "        return str(self)");
    pub const PATHLIB_STR: Frame = Frame::new(
        PATHLIB,
        627,
        "__str__",
        "            self._str = self._format_parsed_parts(self._drv, self._root,",
    );
    pub const PATHLIB_FORMAT_ROOTED: Frame = Frame::new(
        PATHLIB,
        611,
        "_format_parsed_parts",
        "            return drv + root + cls._flavour.join(parts[1:])",
    );
    pub const PATHLIB_FORMAT_RELATIVE: Frame = Frame::new(
        PATHLIB,
        613,
        "_format_parsed_parts",
        "            return cls._flavour.join(parts)",
    );
    pub const PATHLIB_RESOLVE_REALPATH: Frame = Frame::new(
        PATHLIB,
        1077,
        "resolve",
        "            s = self._accessor.realpath(self, strict=strict)",
    );
    pub const PATHLIB_STAT: Frame = Frame::new(
        PATHLIB,
        1097,
        "stat",
        "        return self._accessor.stat(self, follow_symlinks=follow_symlinks)",
    );
    pub const PATHLIB_MKDIR_ACCESSOR: Frame = Frame::new(
        PATHLIB,
        1175,
        "mkdir",
        "            self._accessor.mkdir(self, mode)",
    );
    pub const PATHLIB_MKDIR_CHECKS_PARENT: Frame = Frame::new(
        PATHLIB,
        1177,
        "mkdir",
        "            if not parents or self.parent == self:",
    );
    pub const PATHLIB_MKDIR_PARENT: Frame = Frame::new(
        PATHLIB,
        1179,
        "mkdir",
        "            self.parent.mkdir(parents=True, exist_ok=True)",
    );
    pub const PATHLIB_MKDIR_SELF: Frame = Frame::new(
        PATHLIB,
        1180,
        "mkdir",
        "            self.mkdir(mode, parents=False, exist_ok=exist_ok)",
    );
    pub const PATHLIB_MKDIR_IS_DIR: Frame = Frame::new(
        PATHLIB,
        1184,
        "mkdir",
        "            if not exist_ok or not self.is_dir():",
    );
    pub const PATHLIB_IS_DIR_STAT: Frame = Frame::new(
        PATHLIB,
        1305,
        "is_dir",
        "            return S_ISDIR(self.stat().st_mode)",
    );
    pub const JOIN_FSPATH: Frame = Frame::new(POSIXPATH, 76, "join", "    a = os.fspath(a)");
    pub const ABSPATH_FSPATH: Frame =
        Frame::new(POSIXPATH, 379, "abspath", "    path = os.fspath(path)");
    pub const REALPATH_JOINS: Frame = Frame::new(
        POSIXPATH,
        396,
        "realpath",
        "    path, ok = _joinrealpath(filename[:0], filename, strict, {})",
    );
    pub const REALPATH_ABSPATH: Frame =
        Frame::new(POSIXPATH, 397, "realpath", "    return abspath(path)");
    pub const JOINREALPATH_JOINS: Frame = Frame::new(
        POSIXPATH,
        429,
        "_joinrealpath",
        "        newpath = join(path, name)",
    );
    pub const JOINREALPATH_RECURSES: Frame = Frame::new(
        POSIXPATH,
        456,
        "_joinrealpath",
        "        path, ok = _joinrealpath(path, os.readlink(newpath), strict, seen)",
    );

    /// この単位の段のすべて(実物と照らす対象)。
    pub const ALL: &[Frame] = &[
        WRITE_XLSX_CHECKS,
        WRITE_XLSX_MKDIR,
        MAIN_PATH_OUTPUT,
        PHYSICAL_LSTAT,
        PHYSICAL_RECURSES,
        PHYSICAL_CHECKS_PARENT,
        PHYSICAL_IS_DIR,
        PHYSICAL_RESOLVES,
        OUTPUT_PATHS_INPUTS,
        INPUTS_LISTCOMP,
        OUTPUT_PATHS_ROOTS,
        ROOTS_LISTCOMP,
        OUTPUT_PATHS_PARENTS,
        PARENTS_LISTCOMP,
        OUTPUT_PATHS_PHYSICAL,
        PATHLIB_NEW,
        PATHLIB_FROM_PARTS,
        PATHLIB_PARSE_ARGS,
        PATHLIB_FSPATH,
        PATHLIB_STR,
        PATHLIB_FORMAT_ROOTED,
        PATHLIB_FORMAT_RELATIVE,
        PATHLIB_RESOLVE_REALPATH,
        PATHLIB_STAT,
        PATHLIB_MKDIR_ACCESSOR,
        PATHLIB_MKDIR_CHECKS_PARENT,
        PATHLIB_MKDIR_PARENT,
        PATHLIB_MKDIR_SELF,
        PATHLIB_MKDIR_IS_DIR,
        PATHLIB_IS_DIR_STAT,
        JOIN_FSPATH,
        ABSPATH_FSPATH,
        REALPATH_JOINS,
        REALPATH_ABSPATH,
        JOINREALPATH_JOINS,
        JOINREALPATH_RECURSES,
        PHYSICAL_RAISES_NO_PARENT,
        PHYSICAL_RAISES_NOT_DIR,
        PHYSICAL_RAISES_NOT_DIR_RESOLVED,
        OUTPUT_PATHS_INPUT_PATHS,
        INPUT_PATHS_LISTCOMP,
        OUTPUT_PATHS_EXTEND,
        EXTEND_GENEXPR,
        OUTPUT_PATHS_ROOTS_ARE_DIRS,
        ROOTS_ARE_DIRS_GENEXPR,
        OUTPUT_PATHS_RAISES_ROOT_NOT_DIR,
        OUTPUT_PATHS_ABSOLUTE,
        OUTPUT_PATHS_DEST_LSTAT,
        OUTPUT_PATHS_RAISES_NOT_REGULAR,
        OUTPUT_PATHS_RAISES_HARDLINK,
        OUTPUT_PATHS_SAME_INPUT,
        SAME_INPUT_GENEXPR,
        OUTPUT_PATHS_RAISES_SAME_INPUT,
        OUTPUT_PATHS_INSIDE_ROOT,
        INSIDE_ROOT_GENEXPR,
        OUTPUT_PATHS_RAISES_INSIDE,
        OUTPUT_PATHS_RAISES_FROM,
        PATHLIB_SAMEFILE_STAT,
        PATHLIB_SAMEFILE_OTHER,
        PATHLIB_SAMEFILE_SAMESTAT,
        PATHLIB_ABSOLUTE_GETCWD,
        PATHLIB_CHECK_ELOOP_RAISES,
        PATHLIB_RESOLVE_CHECK_ELOOP,
        PATHLIB_RESOLVE_STAT,
        PATHLIB_RESOLVE_STAT_CHECK_ELOOP,
        PATHLIB_LSTAT,
        PATHLIB_EXISTS_STAT,
        ABSPATH_GETCWD,
        JOINREALPATH_LSTAT,
        JOINREALPATH_STAT,
        GENERICPATH_SAMESTAT,
        PATHLIB_STR_CACHED,
        REALPATH_FSPATH,
        JOINREALPATH_ISINSTANCE,
        JOINREALPATH_ISABS,
        ISABS_FSPATH,
        ISABS_GET_SEP,
        GET_SEP_ISINSTANCE,
    ];
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    /// 使い捨ての置き場(realpath したパス)。終わったら権限を戻して消す。
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4e-paths-{}-{}-{name}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }

        fn bytes(&self) -> Vec<u8> {
            self.0.as_os_str().as_bytes().to_vec()
        }

        fn join(&self, relative: &str) -> Vec<u8> {
            if relative.is_empty() {
                return self.bytes();
            }
            [self.bytes(), b"/".to_vec(), relative.as_bytes().to_vec()].concat()
        }

        fn show(&self, text: &[u8]) -> String {
            String::from_utf8_lossy(text).replace(&*self.0.to_string_lossy(), "@W")
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ =
                std::fs::set_permissions(self.0.join("ro"), std::fs::Permissions::from_mode(0o755));
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    /// 旧版の深さの測定に使った置き場と同じ形(`dir`・`doc`・リンクの連なり `l0`〜`l5` など)。
    fn measured_layout() -> Scratch {
        let scratch = Scratch::new("layout");
        let root = &scratch.0;
        for dir in ["dir/sub", "doc", "dir2", "other"] {
            std::fs::create_dir_all(root.join(dir)).unwrap();
        }
        for file in ["file", "doc/in.md", "exist.xlsx", "dir/in.md"] {
            std::fs::write(root.join(file), b"").unwrap();
        }
        let missing = root.join("missing");
        let mut links = vec![
            ("link", PathBuf::from("dir")),
            ("loop", PathBuf::from("loop")),
            ("broken", missing),
            ("lf", PathBuf::from("file")),
            ("ldoc", PathBuf::from("doc")),
            ("l0", PathBuf::from("dir")),
        ];
        let names = (1..6)
            .map(|i| (format!("l{i}"), format!("l{}", i - 1)))
            .collect::<Vec<_>>();
        for (name, target) in &names {
            links.push((name.as_str(), PathBuf::from(target)));
        }
        for (name, target) in links {
            symlink(target, root.join(name)).unwrap();
        }
        scratch
    }

    fn no_cwd() -> io::Result<Vec<u8>> {
        Err(io::Error::from_raw_os_error(ENOENT))
    }

    /// 上限を 1 つずつ上げて、初めて届く深さの種類の文字列と、通る最小の上限を得る。
    fn profile<T>(now: u32, mut run: impl FnMut(&mut Depth) -> Result<T, String>) -> (u32, String) {
        let mut kinds = String::new();
        for limit in now..now + 200 {
            match run(&mut Depth::with_limit(now, limit)) {
                Ok(_) => return (limit - now, kinds),
                Err(message) => {
                    let kind = match message.strip_prefix("maximum recursion depth exceeded") {
                        Some("") => 'F',
                        Some(" while calling a Python object") => 'C',
                        Some(" in comparison") => 'Q',
                        Some(" while getting the str of an object") => 'S',
                        Some(" while getting the repr of an object") => 'R',
                        _ => panic!("上限で止まらなかった: {message}"),
                    };
                    kinds.push(kind);
                }
            }
        }
        panic!("上限を上げても通らない");
    }

    fn describe_fail(scratch: &Scratch, fail: &Fail) -> String {
        match fail {
            Fail::Output(error) => format!(
                "OutputError: {}",
                scratch.show(error.message.to_string_lossy().as_bytes())
            ),
            Fail::Raise(error) => scratch.show(error.type_line().to_string_lossy().as_bytes()),
        }
    }

    /// `physical_directory(Path(p))` の深さと結果は、旧版で上限を 1 つずつ変えて測った値と同じ。
    #[test]
    fn physical_directory_matches_the_measured_depth() {
        let scratch = measured_layout();
        let eloop = strerror_line(ELOOP);
        let cases: &[(&str, u32, &str, &str)] = &[
            ("dir", 10, "FFFCFCSFFC", "OK @W/dir"),
            ("link", 10, "FFFCFCSFFC", "OK @W/dir"),
            ("l3", 11, "FFFCFCSFFCC", "OK @W/dir"),
            ("l4", 12, "FFFCFCSFFCCC", "OK @W/dir"),
            ("l5", 13, "FFFCFCSFFCCCC", "OK @W/dir"),
            ("dir/m1", 11, "FFFCFCSFFCC", "OK @W/dir/m1"),
            ("dir/m1/m2/m3", 13, "FFFCFCSFFCCCC", "OK @W/dir/m1/m2/m3"),
            ("l2/m1/m2", 12, "FFFCFCSFFCCC", "OK @W/dir/m1/m2"),
            ("l4/m1", 13, "FFFCFCSFFCCCC", "OK @W/dir/m1"),
            (
                "file",
                10,
                "FFFCFCSFFC",
                "OutputError: 出力先の親がディレクトリでない: @W/file",
            ),
            (
                "broken/m1",
                11,
                "FFFCFCSFFCC",
                "FileNotFoundError: [Errno 2] No such file or directory: '@W/missing'",
            ),
            (
                "loop/m1",
                10,
                "FFFCFCSFFC",
                &format!("OSError: {eloop}: '@W/loop/m1'"),
            ),
            (
                "file/m1",
                10,
                "FFFCFCSFFC",
                "NotADirectoryError: [Errno 20] Not a directory: '@W/file/m1'",
            ),
            ("dir/../dir/m1", 11, "FFFCFCSFFCC", "OK @W/dir/m1"),
            (
                "lf",
                10,
                "FFFCFCSFFC",
                "OutputError: 出力先の親がディレクトリでない: @W/lf",
            ),
            (
                "loop",
                10,
                "FFFCFCSFFC",
                "RuntimeError: Symlink loop from '@W/loop'",
            ),
            ("", 10, "FFFCFCSFFC", "OK @W"),
        ];
        for (relative, peak, kinds, result) in cases {
            let target = scratch.join(relative);
            let run =
                |depth: &mut Depth| physical_directory(depth, &PyPath::parse(&target), &no_cwd);
            let measured = profile(0, |depth| match run(depth) {
                Err(Fail::Raise(error)) if error.kind == types::RECURSION_ERROR => {
                    Err(error.message.to_string_lossy())
                }
                _ => Ok(()),
            });
            assert_eq!(measured, (*peak, (*kinds).to_owned()), "{relative}");
            let actual = match run(&mut Depth::new(0)) {
                Ok(path) => format!("OK {}", scratch.show(&path.to_bytes())),
                Err(fail) => describe_fail(&scratch, &fail),
            };
            assert_eq!(actual, *result, "{relative}");
        }
    }

    fn strerror_line(number: i32) -> String {
        format!(
            "[Errno {number}] {}",
            super::super::errors::strerror(number)
        )
    }

    /// `output_paths([Path(o)], inputs=…, source_roots=…)` の深さと結果は、旧版の測定と同じ。
    ///
    /// 旧版の測定は `call_output` の段(深さ 1)から呼んだ。`output_paths` の `except` の中の書式
    /// (上限のすぐ近くで別の RecursionError になる)が混ざる深さ 3・4 は比べない。
    #[test]
    fn output_paths_matches_the_measured_depth() {
        let scratch = measured_layout();
        type Case<'a> = (&'a str, &'a [&'a str], &'a [&'a str], u32, &'a str, &'a str);
        let cases: &[Case] = &[
            (
                "out/book.xlsx",
                &["doc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OK @W/out/book.xlsx",
            ),
            (
                "l3/book.xlsx",
                &["doc/in.md"],
                &["l4"],
                14,
                "FFCSFQCCCQFCCC",
                "OutputError: 出力先が正本ディレクトリ内にある: @W/l3/book.xlsx",
            ),
            (
                "doc/new.xlsx",
                &["doc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 出力先が正本ディレクトリ内にある: @W/doc/new.xlsx",
            ),
            (
                "dir/m1/m2/book.xlsx",
                &["doc/in.md"],
                &["l4", "dir2"],
                14,
                "FFCSFQCCCQFCCC",
                "OutputError: 出力先が正本ディレクトリ内にある: @W/dir/m1/m2/book.xlsx",
            ),
            (
                "exist.xlsx",
                &["doc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OK @W/exist.xlsx",
            ),
            (
                "out.xlsx",
                &["doc/in.md"],
                &["loop"],
                12,
                "FFCSFQCCCQFC",
                "OutputError: 出力先または正本を解決できない: Symlink loop from '@W/loop'",
            ),
            (
                "doc/in.md",
                &["doc/in.md"],
                &["other"],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 出力先が入力ファイルと同じ: @W/doc/in.md",
            ),
            (
                "l4/m1/m2/book.xlsx",
                &["doc/in.md"],
                &[],
                16,
                "FFCSFQCCCQFCCCCC",
                "OK @W/dir/m1/m2/book.xlsx",
            ),
            (
                "out/book.xlsx",
                &["ldoc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OK @W/out/book.xlsx",
            ),
            (
                "ldoc/new/book.xlsx",
                &["doc/in.md"],
                &["ldoc"],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 出力先が正本ディレクトリ内にある: @W/ldoc/new/book.xlsx",
            ),
            (
                "l5/book.xlsx",
                &["doc/in.md"],
                &["l5"],
                15,
                "FFCSFQCCCQFCCCC",
                "OutputError: 出力先が正本ディレクトリ内にある: @W/l5/book.xlsx",
            ),
            (
                "dir",
                &["doc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 出力先が通常ファイルでない（リンクも拒否）: @W/dir",
            ),
            (
                "lf",
                &["doc/in.md"],
                &[],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 出力先が通常ファイルでない（リンクも拒否）: @W/lf",
            ),
            (
                "x.xlsx",
                &["doc/in.md"],
                &["missing"],
                12,
                "FFCSFQCCCQFC",
                "OutputError: 出力先または正本を解決できない: [Errno 2] No such file or directory: '@W/missing'",
            ),
            (
                "x.xlsx",
                &["doc/in.md"],
                &["file"],
                13,
                "FFCSFQCCCQFCC",
                "OutputError: 正本ディレクトリが通常のディレクトリでない",
            ),
            (
                "x.xlsx",
                &["l4/in.md"],
                &["dir", "l5"],
                15,
                "FFCSFQCCCQFCCCC",
                "OK @W/x.xlsx",
            ),
        ];
        for (out, inputs, roots, peak, kinds, result) in cases {
            let out = PyPath::parse(&scratch.join(out));
            let inputs = inputs
                .iter()
                .map(|input| PyPath::parse(&scratch.join(input)))
                .collect::<Vec<_>>();
            let roots = roots
                .iter()
                .map(|root| Value::Str(OsStr::from_bytes(&scratch.join(root)).to_owned()))
                .collect::<Vec<_>>();
            let run = |depth: &mut Depth| output_paths(depth, &out, &inputs, &roots, &no_cwd);
            let (measured_peak, measured_kinds) = profile(1, |depth| match run(depth) {
                Err(Rejected::Output(error))
                    if error
                        .message
                        .to_string_lossy()
                        .contains("maximum recursion depth exceeded") =>
                {
                    Err(error
                        .message
                        .to_string_lossy()
                        .trim_start_matches("出力先または正本を解決できない: ")
                        .to_owned())
                }
                _ => Ok(()),
            });
            assert_eq!(
                measured_peak + 1,
                *peak,
                "{}",
                scratch.show(&out.to_bytes())
            );
            // 旧版の文字列は深さ 1 から。Rust は深さ 2(上限 1)から。深さ 5 から比べる。
            assert_eq!(
                &measured_kinds[3..],
                &kinds[4..],
                "{}",
                scratch.show(&out.to_bytes())
            );
            let actual = match run(&mut Depth::new(1)) {
                Ok(path) => format!("OK {}", scratch.show(&path.to_bytes())),
                Err(Rejected::Output(error)) => {
                    format!(
                        "OutputError: {}",
                        scratch.show(error.message.to_string_lossy().as_bytes())
                    )
                }
                Err(Rejected::Raised(error)) => {
                    scratch.show(error.type_line().to_string_lossy().as_bytes())
                }
            };
            assert_eq!(actual, *result);
        }
    }

    /// `PurePosixPath` の字面の正規化と `parent`・`name`・`parents`・`/`。
    #[test]
    fn pure_paths_normalize_like_pathlib() {
        for (text, expected) in [
            ("", "."),
            (".", "."),
            ("a//b", "a/b"),
            ("a/./b/", "a/b"),
            ("./a", "a"),
            ("../a", "../a"),
            ("a/..", "a/.."),
            ("/", "/"),
            ("//", "//"),
            ("///", "/"),
            ("//a//b/", "//a/b"),
            ("///a", "/a"),
            ("////a", "/a"),
            ("/a/b/", "/a/b"),
        ] {
            assert_eq!(
                String::from_utf8(PyPath::parse(text.as_bytes()).to_bytes()).unwrap(),
                expected,
                "{text}"
            );
        }
        let path = PyPath::parse(b"/a/b/c");
        assert_eq!(path.parent_value().to_bytes(), b"/a/b");
        assert_eq!(path.name(), b"c");
        let parents = path
            .parents()
            .iter()
            .map(PyPath::to_bytes)
            .collect::<Vec<_>>();
        assert_eq!(parents, [b"/a/b".to_vec(), b"/a".to_vec(), b"/".to_vec()]);
        let relative = PyPath::parse(b"a/b");
        let parents = relative
            .parents()
            .iter()
            .map(PyPath::to_bytes)
            .collect::<Vec<_>>();
        assert_eq!(parents, [b"a".to_vec(), b".".to_vec()]);
        assert!(PyPath::parse(b"/").parent_is_self());
        assert_eq!(PyPath::parse(b"/").name(), b"");
        assert_eq!(PyPath::parse(b"").name(), b"");
        assert_eq!(path.child(b"d").to_bytes(), b"/a/b/c/d");
        assert_eq!(path.child(b"").to_bytes(), b"/a/b/c");
        assert_eq!(path.child(b"..").to_bytes(), b"/a/b/c/..");
        assert_eq!(path.child(b"/x").to_bytes(), b"/x");
        assert_eq!(PyPath::parse(b"//x").child(b"y").to_bytes(), b"//x/y");
        assert!(PyPath::parse(b"//a").same_parts(&PyPath::parse(b"//a")));
        assert!(!PyPath::parse(b"//a").same_parts(&PyPath::parse(b"/a")));
        // UTF-8 でない名前の文は surrogateescape。
        assert_eq!(
            PyPath::parse(b"/x\xff").to_py().as_code_points(),
            &[0x2f, 0x78, 0xdcff]
        );
    }

    #[test]
    fn posixpath_helpers_match_python() {
        for (path, expected) in [
            ("", "."),
            ("/", "/"),
            ("//", "//"),
            ("///", "/"),
            ("//a/../b", "//b"),
            ("/a/../../b", "/b"),
            ("a/../../b", "../b"),
            ("../..", "../.."),
            ("./a/./b/", "a/b"),
            ("a/..", "."),
        ] {
            assert_eq!(normpath(path.as_bytes()), expected.as_bytes(), "{path}");
        }
        for (path, head, tail) in [
            ("/a/b", "/a", "b"),
            ("/a", "/", "a"),
            ("//a", "//", "a"),
            ("a//b", "a", "b"),
            ("a", "", "a"),
            ("a/", "a", ""),
        ] {
            assert_eq!(
                split(path.as_bytes()),
                (head.as_bytes().to_vec(), tail.as_bytes().to_vec()),
                "{path}"
            );
        }
        assert_eq!(join(b"", b"a"), b"a");
        assert_eq!(join(b"/", b"a"), b"/a");
        assert_eq!(join(b"x", b"a"), b"x/a");
        assert_eq!(join(b"x", b"/a"), b"/a");
    }

    /// `realpath` と `resolve`(相対・`..`・リンクの環・無い末尾)。
    #[test]
    fn resolve_follows_posixpath_realpath() {
        let scratch = measured_layout();
        let base = scratch.bytes();
        let cwd = move || Ok(base.clone());
        let resolve_text = |text: &str, strict: bool| -> String {
            let path = PyPath::parse(text.as_bytes());
            match resolve(&mut Depth::new(10), &path, strict, &cwd) {
                Ok(path) => scratch.show(&path.to_bytes()),
                Err(error) => scratch.show(error.type_line().to_string_lossy().as_bytes()),
            }
        };
        let w = scratch.0.to_string_lossy().into_owned();
        assert_eq!(resolve_text(&format!("{w}/l2/x"), false), "@W/dir/x");
        assert_eq!(
            resolve_text(&format!("{w}/l2/x"), true),
            "FileNotFoundError: [Errno 2] No such file or directory: '@W/dir/x'"
        );
        assert_eq!(
            resolve_text(&format!("{w}/loop"), true),
            "RuntimeError: Symlink loop from '@W/loop'"
        );
        assert_eq!(
            resolve_text(&format!("{w}/loop"), false),
            "RuntimeError: Symlink loop from '@W/loop'"
        );
        assert_eq!(
            resolve_text(&format!("{w}/loop/x"), false),
            "RuntimeError: Symlink loop from '@W/loop/x'"
        );
        assert_eq!(
            resolve_text(&format!("{w}/dir/../link/sub"), true),
            "@W/dir/sub"
        );
        assert_eq!(resolve_text(&format!("//{}/dir", &w[1..]), true), "@W/dir");
        // 現在のディレクトリが取れない(相対パス)は OSError(ファイル名なし)。
        let path = PyPath::parse(b".");
        let error = resolve(&mut Depth::new(10), &path, true, &no_cwd).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "FileNotFoundError: [Errno 2] No such file or directory"
        );
    }

    fn write_xlsx_check(
        scratch: &Scratch,
        out: &[u8],
        roots: &[Value],
    ) -> Result<PyPath, Rejected> {
        let input = PyPath::parse(&scratch.join("doc/in.md"));
        output_paths(
            &mut Depth::new(3),
            &PyPath::parse(out),
            &[input],
            roots,
            &no_cwd,
        )
    }

    fn recursion_message(result: Result<PyPath, Rejected>) -> String {
        match result {
            Err(Rejected::Output(error)) => error.message.to_string_lossy(),
            other => panic!("拒否にならなかった: {other:?}"),
        }
    }

    const LIMIT_MESSAGE: &str = "出力先または正本を解決できない: maximum recursion depth exceeded while calling a Python object";

    /// 旧版の実測(`cases.json` の `measured`): 無い親の階層は `out` と `d` を 985 個で成功、986 個・
    /// 987 個で失敗(境界の前・等しい・後)。
    ///
    /// Mac はパスの長さの上限(1024 バイト)が先に当たる(旧版も `File name too long`)ので Linux だけ。
    #[cfg(target_os = "linux")]
    #[test]
    fn missing_parents_hit_the_legacy_recursion_limit() {
        let scratch = measured_layout();
        for (count, ok) in [(985, true), (986, false), (987, false)] {
            let out = scratch.join(&format!("out/{}book.xlsx", "d/".repeat(count)));
            let result = write_xlsx_check(&scratch, &out, &[]);
            if ok {
                assert_eq!(result.unwrap().to_bytes(), out, "{count}");
            } else {
                assert_eq!(recursion_message(result), LIMIT_MESSAGE, "{count}");
            }
        }
    }

    /// 旧版の実測: リンクの連なり(`l<k-1>` → … → `l0` → 実体)は 989 個で成功、990 個・991 個で失敗。
    #[test]
    fn link_chains_hit_the_legacy_recursion_limit() {
        for (count, ok) in [(989, true), (990, false), (991, false)] {
            let scratch = Scratch::new("chain");
            std::fs::create_dir(scratch.0.join("real")).unwrap();
            std::fs::create_dir(scratch.0.join("doc")).unwrap();
            std::fs::write(scratch.0.join("doc/in.md"), b"").unwrap();
            symlink("real", scratch.0.join("l0")).unwrap();
            for index in 1..count {
                symlink(
                    format!("l{}", index - 1),
                    scratch.0.join(format!("l{index}")),
                )
                .unwrap();
            }
            let out = scratch.join(&format!("l{}/book.xlsx", count - 1));
            let result = write_xlsx_check(&scratch, &out, &[]);
            if ok {
                assert_eq!(
                    result.unwrap().to_bytes(),
                    scratch.join("real/book.xlsx"),
                    "{count}"
                );
            } else {
                assert_eq!(recursion_message(result), LIMIT_MESSAGE, "{count}");
            }
        }
    }

    /// 正本のリンクの連なりの境界の前・等しい・後(旧版の実測: 989 個で成功、990 個・991 個で失敗。
    /// 一覧の段 5 → resolve 6 → realpath 7 → _joinrealpath 8 から 1 つずつ深くなり、最後の段の
    /// isabs → _get_sep → isinstance が 3 つ深い。出力先の親の連なりと同じ深さ)。
    #[test]
    fn source_root_chains_hit_the_legacy_recursion_limit() {
        let scratch = Scratch::new("roots");
        std::fs::create_dir(scratch.0.join("real")).unwrap();
        std::fs::create_dir(scratch.0.join("doc")).unwrap();
        std::fs::write(scratch.0.join("doc/in.md"), b"").unwrap();
        symlink("real", scratch.0.join("l0")).unwrap();
        for index in 1..992 {
            symlink(
                format!("l{}", index - 1),
                scratch.0.join(format!("l{index}")),
            )
            .unwrap();
        }
        for (links, ok) in [(989, true), (990, false), (991, false)] {
            let root =
                Value::Str(OsStr::from_bytes(&scratch.join(&format!("l{}", links - 1))).to_owned());
            let result = write_xlsx_check(&scratch, &scratch.join("out.xlsx"), &[root]);
            if ok {
                assert!(result.is_ok(), "{links}");
            } else {
                assert_eq!(recursion_message(result), LIMIT_MESSAGE, "{links}");
            }
        }
    }

    /// 拒否は `OutputError` の例外(呼出履歴と連鎖つき)。段と連鎖は、旧版で拒否の行の print を
    /// SIGINT で例外にして出した全文と同じ(`__main__.OutputError` の呼出履歴・`from exc` の元・
    /// 無い親の `FileNotFoundError` の連鎖・リンクの環の `RuntimeError` と `OSError`)。
    #[test]
    fn rejections_carry_the_legacy_output_error_chain() {
        let scratch = measured_layout();
        let roots: &[Value] = &[];
        let rejected = |out: &str, roots: &[Value]| match write_xlsx_check(
            &scratch,
            &scratch.join(out),
            roots,
        ) {
            Err(Rejected::Output(error)) => error,
            other => panic!("拒否にならなかった: {other:?}"),
        };
        let lines = |error: &PyException| {
            error
                .traceback
                .iter()
                .map(|frame| (frame.file, frame.line))
                .collect::<Vec<_>>()
        };
        const SCRIPT: &str = "md_tables_to_xlsx.py";
        // 正本の中: 149 行の raise。連鎖なし。
        let error = rejected("doc/x.xlsx", roots);
        assert_eq!(error.kind.display_name(), "__main__.OutputError");
        assert_eq!(lines(&error), [(SCRIPT, 149)]);
        assert!(error.cause.is_none() && error.context.is_none());
        // 親がファイル: physical_directory の 113 行(output_paths の 134 行から)。
        let error = rejected("file/x.xlsx", roots);
        assert_eq!(lines(&error), [(SCRIPT, 134), (SCRIPT, 113)]);
        // 親がファイルへのリンク: 116 行。
        let error = rejected("lf/x.xlsx", roots);
        assert_eq!(lines(&error), [(SCRIPT, 134), (SCRIPT, 116)]);
        // 壊れたリンクの下の無い親: 元の FileNotFoundError(resolve)が、処理中の
        // FileNotFoundError(lstat)を __context__ に持ち、OutputError の __cause__ になる。
        let error = rejected("broken/sub/x.xlsx", roots);
        assert_eq!(lines(&error), [(SCRIPT, 153)]);
        assert!(error.suppress_context);
        let cause = error.cause.as_ref().expect("from exc");
        assert_eq!(
            lines(cause),
            [
                (SCRIPT, 134),
                (SCRIPT, 111),
                (SCRIPT, 114),
                ("pathlib.py", 1077),
                ("posixpath.py", 396),
                ("posixpath.py", 456),
                ("posixpath.py", 431),
            ]
        );
        let handled = cause.context.as_ref().expect("lstat の FileNotFoundError");
        assert_eq!(
            scratch.show(handled.type_line().to_string_lossy().as_bytes()),
            "FileNotFoundError: [Errno 2] No such file or directory: '@W/broken/sub'"
        );
        assert_eq!(
            lines(handled),
            [(SCRIPT, 107), ("pathlib.py", 1222), ("pathlib.py", 1097)]
        );
        assert!(handled.context.is_none());
        // リンクの環の正本: RuntimeError(check_eloop)が OSError(ELOOP)を __context__ に持つ。
        let root = [Value::Str(
            OsStr::from_bytes(&scratch.join("loop")).to_owned(),
        )];
        let error = rejected("out/x.xlsx", &root);
        let cause = error.cause.as_ref().expect("from exc");
        assert_eq!(cause.kind, types::RUNTIME_ERROR);
        assert_eq!(
            lines(cause),
            [
                (SCRIPT, 125),
                (SCRIPT, 125),
                ("pathlib.py", 1079),
                ("pathlib.py", 1074),
            ]
        );
        let eloop = cause.context.as_ref().expect("OSError(ELOOP)");
        assert_eq!(eloop.errno, Some(ELOOP));
        assert_eq!(
            lines(eloop),
            [
                ("pathlib.py", 1077),
                ("posixpath.py", 396),
                ("posixpath.py", 456),
                ("posixpath.py", 451),
            ]
        );
    }

    /// `--source-root=--`(空のリスト)は `Path([])` の `TypeError` で、捕まえられずに呼出履歴になる。
    #[test]
    fn an_empty_list_root_is_a_type_error() {
        let scratch = measured_layout();
        let roots = [
            Value::Str(OsStr::from_bytes(&scratch.join("missing")).to_owned()),
            Value::EmptyList,
        ];
        // 先の正本の OSError が先に当たる。
        assert!(matches!(
            write_xlsx_check(&scratch, &scratch.join("x.xlsx"), &roots),
            Err(Rejected::Output(_))
        ));
        let roots = [
            Value::Str(OsStr::from_bytes(&scratch.join("dir")).to_owned()),
            Value::EmptyList,
        ];
        let Err(Rejected::Raised(error)) =
            write_xlsx_check(&scratch, &scratch.join("x.xlsx"), &roots)
        else {
            panic!("TypeError にならなかった");
        };
        assert_eq!(
            error.type_line().to_string_lossy(),
            "TypeError: expected str, bytes or os.PathLike object, not list"
        );
        let places = error
            .traceback
            .iter()
            .map(|frame| (frame.file, frame.line, frame.function))
            .collect::<Vec<_>>();
        assert_eq!(
            places,
            [
                ("md_tables_to_xlsx.py", 125, "output_paths"),
                ("md_tables_to_xlsx.py", 125, "<listcomp>"),
                ("pathlib.py", 960, "__new__"),
                ("pathlib.py", 594, "_from_parts"),
                ("pathlib.py", 578, "_parse_args"),
            ]
        );
        let error = output_argument(&Value::EmptyList).unwrap_err();
        assert_eq!(error.traceback.len(), 3);
        assert_eq!(
            output_argument(&Value::Str("a//b/".into()))
                .unwrap()
                .to_bytes(),
            b"a/b"
        );
    }

    fn places(error: &PyException) -> Vec<(&'static str, u32)> {
        error
            .traceback
            .iter()
            .map(|frame| (frame.file, frame.line))
            .collect()
    }

    /// 読み取り専用のディレクトリの下の無い親(旧版の実測: 2 階層以上は `FileNotFoundError` の連鎖)。
    #[test]
    fn mkdir_failures_chain_like_pathlib() {
        let scratch = Scratch::new("mkdir");
        let ro = scratch.0.join("ro");
        std::fs::create_dir(&ro).unwrap();
        std::fs::set_permissions(&ro, std::fs::Permissions::from_mode(0o555)).unwrap();
        assert!(
            std::fs::write(ro.join("probe"), b"").is_err(),
            "root では読み取り専用のディレクトリを確かめられない(通常の利用者で流す)"
        );
        for missing in 1..=3 {
            let names = ["a", "b", "c"][..missing].join("/");
            let dest = PyPath::parse(&scratch.join(&format!("ro/{names}/book.xlsx")));
            let error = make_parents(&dest).unwrap_err();
            assert_eq!(
                scratch.show(error.type_line().to_string_lossy().as_bytes()),
                "PermissionError: [Errno 13] Permission denied: '@W/ro/a'"
            );
            let mut expected = vec![("pathlib.py", 1179); missing - 1];
            expected.push(("pathlib.py", 1175));
            assert_eq!(places(&error), expected, "{missing}");
            // 連鎖: 深い方の FileNotFoundError から順に。
            let mut context = error.context.as_ref();
            for level in 1..missing {
                let inner = context.expect("FileNotFoundError が連鎖する");
                let name = ["a", "b", "c"][..=level].join("/");
                assert_eq!(
                    scratch.show(inner.type_line().to_string_lossy().as_bytes()),
                    format!(
                        "FileNotFoundError: [Errno 2] No such file or directory: '@W/ro/{name}'"
                    )
                );
                assert_eq!(places(inner), [("pathlib.py", 1175)]);
                context = inner.context.as_ref();
            }
            assert!(context.is_none());
        }
        // 既にあるディレクトリ(exist_ok)と、親の作成(モード 0o777 から umask を除いたもの)。
        let dest = PyPath::parse(&scratch.join("new/x/y/book.xlsx"));
        make_parents(&dest).unwrap();
        make_parents(&dest).unwrap();
        assert!(scratch.0.join("new/x/y").is_dir());
        // 親がファイルなら NotADirectoryError(FileNotFoundError ではないので連鎖しない)。
        std::fs::write(scratch.0.join("plain"), b"").unwrap();
        let error = make_parents(&PyPath::parse(&scratch.join("plain/x/book.xlsx"))).unwrap_err();
        assert_eq!(error.kind, types::NOT_A_DIRECTORY_ERROR);
        assert!(error.context.is_none());
        // 既にあるファイルの名前で作る: FileExistsError で、is_dir が偽なのでそのまま。
        let error = make_parents(&PyPath::parse(&scratch.join("plain/book.xlsx"))).unwrap_err();
        assert_eq!(error.kind, types::FILE_EXISTS_ERROR);
        assert_eq!(places(&error), [("pathlib.py", 1175)]);
    }

    /// `Path.mkdir` の再帰の上限の前・等しい・後(write_xlsx の段 3 から)。同じ階層の数では
    /// `physical_directory` が先に上限に当たる(旧版でも mkdir の上限には届かない)。
    ///
    /// Mac はパスの長さの上限(1024 バイト)が先に当たるので Linux だけ。
    #[cfg(target_os = "linux")]
    #[test]
    fn mkdir_recursion_is_shallower_than_the_preflight() {
        let scratch = Scratch::new("mkdir-depth");
        for (count, ok) in [(990, true), (991, false), (992, false)] {
            let dest = PyPath::parse(
                &scratch.join(&format!("m{count}/{}book.xlsx", "d/".repeat(count - 1))),
            );
            let result = make_parents(&dest);
            if ok {
                assert!(result.is_ok(), "{count}");
                continue;
            }
            let error = result.unwrap_err();
            assert_eq!(
                error.type_line().to_string_lossy(),
                "RecursionError: maximum recursion depth exceeded while calling a Python object",
                "{count}"
            );
            // 最も深い段は _format_parsed_parts(str.join の呼出し)。`__str__` の
            // `except AttributeError:` の中なので、その AttributeError を __context__ に持つ。
            assert_eq!(
                error.traceback.last().map(|frame| (frame.file, frame.line)),
                Some(("pathlib.py", 611))
            );
            let missing = error.context.as_ref().expect("AttributeError");
            assert_eq!(
                missing.type_line().to_string_lossy(),
                "AttributeError: 'PosixPath' object has no attribute '_str'"
            );
            assert_eq!(places(missing), [("pathlib.py", 625)]);
            let mut chain = 0;
            let mut context = missing.context.as_ref();
            while let Some(inner) = context {
                assert_eq!(inner.kind, types::FILE_NOT_FOUND_ERROR);
                chain += 1;
                context = inner.context.as_ref();
            }
            assert_eq!(chain, 990, "{count}");
        }
        // 事前検査は 987 個の無い階層(out と d を 986 個)で止まるので、mkdir の上限(991 個)には届かない。
        let input = PyPath::parse(&scratch.join("in.md"));
        std::fs::write(scratch.0.join("in.md"), b"").unwrap();
        let out = PyPath::parse(&scratch.join(&format!("out/{}book.xlsx", "d/".repeat(986))));
        assert!(output_paths(&mut Depth::new(3), &out, &[input], &[], &no_cwd).is_err());
    }

    /// 旧スクリプトの段は、リポジトリの旧スクリプトの行と同じ(標準ライブラリの段は検査入口の
    /// `R4E_CONTRACT_FRAMES` で旧版の環境の実物と照らす)。
    #[test]
    fn script_frames_match_the_legacy_script() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../plugins/dev-workflow/skills/export-doc/scripts/md_tables_to_xlsx.py");
        let source = std::fs::read_to_string(path).unwrap();
        let lines = source.split('\n').collect::<Vec<_>>();
        let mut checked = 0;
        for frame in frames::ALL
            .iter()
            .filter(|frame| frame.file == "md_tables_to_xlsx.py")
        {
            assert_eq!(lines[frame.line as usize - 1], frame.source, "{frame:?}");
            checked += 1;
        }
        assert_eq!(checked, 36);
    }
}
