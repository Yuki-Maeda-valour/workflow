//! 旧版の呼出履歴の定型。
//! - `File` 欄は固定名(旧スクリプトは `check_links.py`、標準ライブラリは `pathlib.py`・`posixpath.py`)。
//! - 行番号・関数名・ソースの行は、旧版と本家 CPython 3.10.11 の標準ライブラリの値。
//! - 書込みの区切りは CPython v3.10.11 の `Python/traceback.c`(`tb_displayline`・`_Py_DisplaySourceLine`・
//!   `tb_print_line_repeated`)・`Python/pythonrun.c`(`print_exception`・`print_exception_recursive`)・
//!   `Python/errors.c`(`write_unraisable_exc_file`)の `PyFile_WriteString`・`PyFile_WriteObject` の呼出しごとに分ける。

/// 呼出履歴の 1 フレーム。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Frame {
    pub file: &'static str,
    pub line: u32,
    pub name: &'static str,
    pub source: &'static str,
}

const fn frame(file: &'static str, line: u32, name: &'static str, source: &'static str) -> Frame {
    Frame {
        file,
        line,
        name,
        source,
    }
}

const LEGACY: &str = "check_links.py";
const PATHLIB: &str = "pathlib.py";
const POSIXPATH: &str = "posixpath.py";

pub const MODULE: Frame = frame(LEGACY, 93, "<module>", "sys.exit(main(sys.argv[1:]))");
pub const MAIN_ITER: Frame = frame(LEGACY, 51, "main", "for md in iter_md_files(argv):");
pub const MAIN_DOC: Frame = frame(LEGACY, 44, "main", "print(__doc__)");
pub const MAIN_READ: Frame = frame(
    LEGACY,
    52,
    "main",
    "text = md.read_text(encoding=\"utf-8\", errors=\"replace\")",
);
const RESOLVE_SOURCE: &str =
    "resolved = (base / target).resolve() if not target.startswith(\"/\") else Path(target)";
pub const MAIN_LINK_RESOLVE: Frame = frame(LEGACY, 64, "main", RESOLVE_SOURCE);
pub const MAIN_LINK_EXISTS: Frame = frame(LEGACY, 65, "main", "if not resolved.exists():");
pub const MAIN_CODE_RESOLVE: Frame = frame(LEGACY, 73, "main", RESOLVE_SOURCE);
pub const MAIN_CODE_ALT: Frame = frame(LEGACY, 77, "main", "if (up / target).exists():");
pub const MAIN_CODE_EXISTS: Frame = frame(
    LEGACY,
    80,
    "main",
    "if not resolved.exists() and alt is None:",
);
pub const MAIN_PRINT_COUNT: Frame =
    frame(LEGACY, 83, "main", "print(f\"検査ファイル数: {checked}\")");
pub const MAIN_PRINT_WARN: Frame = frame(LEGACY, 85, "main", "print(f\"WARN  {w}\")");
pub const MAIN_PRINT_ERROR: Frame = frame(LEGACY, 87, "main", "print(f\"ERROR {e}\")");
pub const MAIN_PRINT_RESULT: Frame = frame(
    LEGACY,
    88,
    "main",
    "print(f\"結果: ERROR {len(errors)} 件 / WARN {len(warns)} 件\")",
);
pub const ITER_IS_DIR: Frame = frame(LEGACY, 27, "iter_md_files", "if p.is_dir():");
pub const ITER_RGLOB: Frame = frame(
    LEGACY,
    28,
    "iter_md_files",
    "for f in sorted(p.rglob(\"*.md\")):",
);
pub const ITER_IS_FILE: Frame = frame(LEGACY, 31, "iter_md_files", "elif p.is_file():");
pub const ITER_WARN: Frame = frame(
    LEGACY,
    34,
    "iter_md_files",
    "print(f\"WARN: 対象が見つかりません: {t}\")",
);

pub const IS_DIR: Frame = frame(
    PATHLIB,
    1305,
    "is_dir",
    "return S_ISDIR(self.stat().st_mode)",
);
pub const IS_FILE: Frame = frame(
    PATHLIB,
    1322,
    "is_file",
    "return S_ISREG(self.stat().st_mode)",
);
pub const EXISTS: Frame = frame(PATHLIB, 1290, "exists", "self.stat()");
pub const STAT: Frame = frame(
    PATHLIB,
    1097,
    "stat",
    "return self._accessor.stat(self, follow_symlinks=follow_symlinks)",
);
pub const READ_TEXT_OPEN: Frame = frame(
    PATHLIB,
    1134,
    "read_text",
    "with self.open(mode='r', encoding=encoding, errors=errors) as f:",
);
pub const READ_TEXT_READ: Frame = frame(PATHLIB, 1135, "read_text", "return f.read()");
pub const OPEN: Frame = frame(
    PATHLIB,
    1119,
    "open",
    "return self._accessor.open(self, mode, buffering, encoding, errors,",
);
pub const RESOLVE_REALPATH: Frame = frame(
    PATHLIB,
    1077,
    "resolve",
    "s = self._accessor.realpath(self, strict=strict)",
);
pub const RESOLVE_STAT: Frame = frame(PATHLIB, 1087, "resolve", "p.stat()");
pub const RESOLVE_CHECK_ELOOP: Frame = frame(PATHLIB, 1089, "resolve", "check_eloop(e)");
pub const CHECK_ELOOP: Frame = frame(
    PATHLIB,
    1074,
    "check_eloop",
    "raise RuntimeError(\"Symlink loop from %r\" % e.filename)",
);
pub const RGLOB: Frame = frame(
    PATHLIB,
    1047,
    "rglob",
    "for p in selector.select_from(self):",
);
pub const SELECT_FROM_IS_DIR: Frame =
    frame(PATHLIB, 405, "select_from", "if not is_dir(parent_path):");
pub const RECURSIVE_ITERATE: Frame = frame(
    PATHLIB,
    492,
    "_select_from",
    "for starting_point in self._iterate_directories(parent_path, is_dir, scandir):",
);
pub const RECURSIVE_SUCCESSOR: Frame = frame(
    PATHLIB,
    493,
    "_select_from",
    "for p in successor_select(starting_point, is_dir, exists, scandir):",
);
pub const ITERATE_SCANDIR: Frame = frame(
    PATHLIB,
    471,
    "_iterate_directories",
    "with scandir(parent_path) as scandir_it:",
);
pub const ITERATE_ENTRY_IS_DIR: Frame = frame(
    PATHLIB,
    476,
    "_iterate_directories",
    "entry_is_dir = entry.is_dir()",
);
pub const ITERATE_ENTRY_IS_SYMLINK: Frame = frame(
    PATHLIB,
    480,
    "_iterate_directories",
    "if entry_is_dir and not entry.is_symlink():",
);
pub const ITERATE_RECURSE: Frame = frame(
    PATHLIB,
    482,
    "_iterate_directories",
    "for p in self._iterate_directories(path, is_dir, scandir):",
);
pub const WILDCARD_SCANDIR: Frame = frame(
    PATHLIB,
    440,
    "_select_from",
    "with scandir(parent_path) as scandir_it:",
);
pub const FSPATH: Frame = frame(PATHLIB, 632, "__fspath__", "return str(self)");
pub const REALPATH_JOIN: Frame = frame(
    POSIXPATH,
    396,
    "realpath",
    "path, ok = _joinrealpath(filename[:0], filename, strict, {})",
);
pub const REALPATH_ABSPATH: Frame = frame(POSIXPATH, 397, "realpath", "return abspath(path)");
pub const ABSPATH_GETCWD: Frame = frame(POSIXPATH, 384, "abspath", "cwd = os.getcwd()");
pub const JOINREALPATH_ISABS: Frame = frame(POSIXPATH, 411, "_joinrealpath", "if isabs(rest):");
pub const JOINREALPATH_LSTAT: Frame =
    frame(POSIXPATH, 431, "_joinrealpath", "st = os.lstat(newpath)");
pub const JOINREALPATH_RECURSE: Frame = frame(
    POSIXPATH,
    456,
    "_joinrealpath",
    "path, ok = _joinrealpath(path, os.readlink(newpath), strict, seen)",
);
pub const ISABS_SEP: Frame = frame(POSIXPATH, 63, "isabs", "sep = _get_sep(s)");
pub const GET_SEP: Frame = frame(POSIXPATH, 42, "_get_sep", "if isinstance(path, bytes):");

/// 旧版の main の 2 か所の `resolve()`(リンクは 64 行、コード中のパスは 73 行)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resolve {
    Link,
    Code,
}

impl Resolve {
    fn frame(self) -> Frame {
        match self {
            Resolve::Link => MAIN_LINK_RESOLVE,
            Resolve::Code => MAIN_CODE_RESOLVE,
        }
    }
}

/// 例外が起きる所。`depth` は、再帰で同じ行が続くフレーム(`_iterate_directories` の 482 行、
/// `_joinrealpath` の 456 行)の数。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Site {
    /// 対象の `is_dir`(stat)。
    TargetIsDir,
    /// 対象の `is_file`(stat)。
    TargetIsFile,
    /// 対象が見つからないときの WARN の print。
    TargetWarn,
    /// `rglob` の `select_from` の `is_dir`(stat)。
    RglobSelectIsDir,
    /// `*.md` を集める側の scandir。生成器は止まっているので深さに依らない。
    RglobWildcardScandir,
    /// 子へ進む側の scandir。
    RglobIterateScandir { depth: usize },
    /// 子へ進む側の `entry.is_dir()`。
    RglobEntryIsDir { depth: usize },
    /// 子へ進む側の `entry.is_symlink()`。
    RglobEntryIsSymlink { depth: usize },
    /// ディレクトリの深さが再帰の上限に当たったとき(scandir の `__fspath__`)。
    RglobRecursion { depth: usize },
    /// `read_text` の open。
    ReadOpen,
    /// `read_text` の read。
    ReadRead,
    /// `resolve` の realpath の lstat(NUL の ValueError)。
    ResolveLstat { call: Resolve, depth: usize },
    /// `resolve` の realpath の readlink(失敗した階層も 456 行)。
    ResolveReadlink { call: Resolve, depth: usize },
    /// `resolve` の realpath の abspath の getcwd。
    ResolveGetcwd(Resolve),
    /// `resolve` の後の stat の ELOOP(前の例外。呼出元のフレームを持たない)。
    ResolveStat,
    /// ELOOP から作る RuntimeError(後の例外)。
    ResolveLoop(Resolve),
    /// リンクを辿る深さが再帰の上限に当たったとき(isabs の `_get_sep` の isinstance)。
    ResolveRecursion { call: Resolve, depth: usize },
    /// リンクの `resolved.exists()`(65 行)。
    LinkExists,
    /// コード中のパスの祖先の `(up / target).exists()`(77 行)。
    CodeAltExists,
    /// コード中のパスの `resolved.exists()`(80 行)。
    CodeExists,
    /// 引数 0 個の docstring の print。
    PrintDoc,
    /// 検査ファイル数の print。
    PrintCount,
    /// WARN の行の print。
    PrintWarn,
    /// ERROR の行の print。
    PrintError,
    /// 結果の行の print。
    PrintResult,
}

impl Site {
    /// 呼出履歴のフレーム(外側から順)。
    pub fn frames(self) -> Vec<Frame> {
        let rglob = |depth: usize, last: &[Frame]| {
            let mut frames = vec![MODULE, MAIN_ITER, ITER_RGLOB, RGLOB, RECURSIVE_ITERATE];
            frames.extend(std::iter::repeat_n(ITERATE_RECURSE, depth));
            frames.extend_from_slice(last);
            frames
        };
        let realpath = |call: Resolve, depth: usize, last: &[Frame]| {
            let mut frames = vec![MODULE, call.frame(), RESOLVE_REALPATH, REALPATH_JOIN];
            frames.extend(std::iter::repeat_n(JOINREALPATH_RECURSE, depth));
            frames.extend_from_slice(last);
            frames
        };
        match self {
            Site::TargetIsDir => vec![MODULE, MAIN_ITER, ITER_IS_DIR, IS_DIR, STAT],
            Site::TargetIsFile => vec![MODULE, MAIN_ITER, ITER_IS_FILE, IS_FILE, STAT],
            Site::TargetWarn => vec![MODULE, MAIN_ITER, ITER_WARN],
            Site::RglobSelectIsDir => vec![
                MODULE,
                MAIN_ITER,
                ITER_RGLOB,
                RGLOB,
                SELECT_FROM_IS_DIR,
                IS_DIR,
                STAT,
            ],
            Site::RglobWildcardScandir => vec![
                MODULE,
                MAIN_ITER,
                ITER_RGLOB,
                RGLOB,
                RECURSIVE_SUCCESSOR,
                WILDCARD_SCANDIR,
            ],
            Site::RglobIterateScandir { depth } => rglob(depth, &[ITERATE_SCANDIR]),
            Site::RglobEntryIsDir { depth } => rglob(depth, &[ITERATE_ENTRY_IS_DIR]),
            Site::RglobEntryIsSymlink { depth } => rglob(depth, &[ITERATE_ENTRY_IS_SYMLINK]),
            Site::RglobRecursion { depth } => rglob(depth, &[ITERATE_SCANDIR, FSPATH]),
            Site::ReadOpen => vec![MODULE, MAIN_READ, READ_TEXT_OPEN, OPEN],
            Site::ReadRead => vec![MODULE, MAIN_READ, READ_TEXT_READ],
            Site::ResolveLstat { call, depth } => realpath(call, depth, &[JOINREALPATH_LSTAT]),
            Site::ResolveReadlink { call, depth } => realpath(call, depth, &[JOINREALPATH_RECURSE]),
            Site::ResolveGetcwd(call) => vec![
                MODULE,
                call.frame(),
                RESOLVE_REALPATH,
                REALPATH_ABSPATH,
                ABSPATH_GETCWD,
            ],
            Site::ResolveStat => vec![RESOLVE_STAT, STAT],
            Site::ResolveLoop(call) => vec![MODULE, call.frame(), RESOLVE_CHECK_ELOOP, CHECK_ELOOP],
            Site::ResolveRecursion { call, depth } => {
                realpath(call, depth, &[JOINREALPATH_ISABS, ISABS_SEP, GET_SEP])
            }
            Site::LinkExists => vec![MODULE, MAIN_LINK_EXISTS, EXISTS, STAT],
            Site::CodeAltExists => vec![MODULE, MAIN_CODE_ALT, EXISTS, STAT],
            Site::CodeExists => vec![MODULE, MAIN_CODE_EXISTS, EXISTS, STAT],
            Site::PrintDoc => vec![MODULE, MAIN_DOC],
            Site::PrintCount => vec![MODULE, MAIN_PRINT_COUNT],
            Site::PrintWarn => vec![MODULE, MAIN_PRINT_WARN],
            Site::PrintError => vec![MODULE, MAIN_PRINT_ERROR],
            Site::PrintResult => vec![MODULE, MAIN_PRINT_RESULT],
        }
    }
}

/// 例外の行の種類と str。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Exception {
    pub type_name: &'static str,
    pub value: String,
}

impl Exception {
    /// OS エラー。種類はこの実行物の OS の番号で errno から選び、文は呼出元が OS ごとに渡す。
    pub fn os(errno: i32, strerror: &str, filename: Option<&[u8]>) -> Self {
        Self::os_on(super::oserror::CURRENT, errno, strerror, filename)
    }

    /// OS エラー。種類は指定した OS の番号で選ぶ(別の OS の実測と照らすとき)。
    pub fn os_on(
        os: super::oserror::Os,
        errno: i32,
        strerror: &str,
        filename: Option<&[u8]>,
    ) -> Self {
        Self {
            type_name: super::oserror::exception_type_on(os, errno),
            value: super::oserror::os_error_text(errno, strerror, filename),
        }
    }

    /// lstat に NUL を含むパスを渡したとき。
    pub fn embedded_null() -> Self {
        Self {
            type_name: "ValueError",
            value: "embedded null byte".to_owned(),
        }
    }

    /// `resolve()` の `check_eloop`。
    pub fn symlink_loop(filename: &[u8]) -> Self {
        Self {
            type_name: "RuntimeError",
            value: format!("Symlink loop from {}", super::repr(filename)),
        }
    }

    /// 再帰の上限。C の関数の呼出しで当たったときは、本文に ` while calling a Python object` が付く。
    pub fn recursion(c_call: bool) -> Self {
        let suffix = if c_call {
            " while calling a Python object"
        } else {
            ""
        };
        Self {
            type_name: "RecursionError",
            value: format!("maximum recursion depth exceeded{suffix}"),
        }
    }

    /// SIGINT による中断。
    pub fn keyboard_interrupt() -> Self {
        Self {
            type_name: "KeyboardInterrupt",
            value: String::new(),
        }
    }
}

/// 1 段の呼出履歴。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Traceback {
    pub frames: Vec<Frame>,
    pub exception: Exception,
}

/// 書込み 1 回の部品の種類(Task 2.3 で、失敗と中断の後の扱いを部品ごとに決める)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PieceKind {
    /// `Traceback (most recent call last):\n`
    Header,
    /// 2 段の区切り(`\nDuring handling of the above exception, another exception occurred:\n\n`)。
    Separator,
    /// `  File "<固定名>", line <数>, in <名前>\n`
    FileLine,
    /// ソースの行の前の 4 個の空白。
    Indent,
    /// ソースの行(先頭の空白を除き、改行を含まない)。
    Source,
    /// ソースの行の後の改行。
    SourceEnd,
    /// `  [Previous line repeated <数> more times]\n`
    Repeated,
    /// 例外の種類の名前。
    TypeName,
    /// 種類と本文の間の `: `。
    Colon,
    /// 例外の str。
    Value,
    /// 例外の行の最後の改行。
    End,
    /// `Exception ignored in: `
    IgnoredIn,
    /// 書き出せなかった stdout の repr。
    IgnoredObject,
    /// `Exception ignored in: ...` の行の改行。
    IgnoredEnd,
}

/// 書込み 1 回分の部品。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Piece {
    pub kind: PieceKind,
    pub text: String,
}

fn piece(kind: PieceKind, text: impl Into<String>) -> Piece {
    Piece {
        kind,
        text: text.into(),
    }
}

/// `PyTraceBack_LIMIT`。これより古いフレームは出さない。
const LIMIT: usize = 1000;
/// `TB_RECURSIVE_CUTOFF`。同じフレームが続くとき、出すのはこの数まで。
const CUTOFF: usize = 3;
const CONTEXT_MESSAGE: &str =
    "\nDuring handling of the above exception, another exception occurred:\n\n";
/// `flush_std_files` が書き出せなかった stdout の repr(UTF-8 に固定した基準の環境)。
pub const STDOUT_REPR: &str = "<_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>";

/// `print_exception_recursive` の部品。`chain` は前の例外から順に並べる。
pub fn print_exception(chain: &[Traceback]) -> Vec<Piece> {
    let mut pieces = Vec::new();
    for (index, traceback) in chain.iter().enumerate() {
        if index > 0 {
            pieces.push(piece(PieceKind::Separator, CONTEXT_MESSAGE));
        }
        if !traceback.frames.is_empty() {
            pieces.push(piece(
                PieceKind::Header,
                "Traceback (most recent call last):\n",
            ));
            frame_pieces(&traceback.frames, &mut pieces);
        }
        let exception = &traceback.exception;
        pieces.push(piece(PieceKind::TypeName, exception.type_name));
        if !exception.value.is_empty() {
            pieces.push(piece(PieceKind::Colon, ": "));
        }
        pieces.push(piece(PieceKind::Value, exception.value.clone()));
        pieces.push(piece(PieceKind::End, "\n"));
    }
    pieces
}

/// `tb_printinternal`。同じフレームが 4 回以上続けば、3 回まで出して残りの数を書く。
fn frame_pieces(frames: &[Frame], pieces: &mut Vec<Piece>) {
    let frames = &frames[frames.len().saturating_sub(LIMIT)..];
    let mut last: Option<Frame> = None;
    let mut count = 0;
    for frame in frames {
        if last != Some(*frame) {
            if count > CUTOFF {
                pieces.push(repeated(count));
            }
            last = Some(*frame);
            count = 0;
        }
        count += 1;
        if count <= CUTOFF {
            pieces.push(piece(
                PieceKind::FileLine,
                format!(
                    "  File \"{}\", line {}, in {}\n",
                    frame.file, frame.line, frame.name
                ),
            ));
            pieces.push(piece(PieceKind::Indent, "    "));
            pieces.push(piece(PieceKind::Source, frame.source));
            pieces.push(piece(PieceKind::SourceEnd, "\n"));
        }
    }
    if count > CUTOFF {
        pieces.push(repeated(count));
    }
}

fn repeated(count: usize) -> Piece {
    let more = count - CUTOFF;
    let unit = if more > 1 { "times" } else { "time" };
    piece(
        PieceKind::Repeated,
        format!("  [Previous line repeated {more} more {unit}]\n"),
    )
}

/// `flush_std_files` が stdout を書き出せなかったときの `PyErr_WriteUnraisable` の部品。
/// 本文が空でも `: ` を書く(`KeyboardInterrupt: `)。
pub fn unraisable_stdout(exception: &Exception) -> Vec<Piece> {
    vec![
        piece(PieceKind::IgnoredIn, "Exception ignored in: "),
        piece(PieceKind::IgnoredObject, STDOUT_REPR),
        piece(PieceKind::IgnoredEnd, "\n"),
        piece(PieceKind::TypeName, exception.type_name),
        piece(PieceKind::Colon, ": "),
        piece(PieceKind::Value, exception.value.clone()),
        piece(PieceKind::End, "\n"),
    ]
}
