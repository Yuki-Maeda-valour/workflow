//! 入力の読取りと表の取り出し(旧 `md_tables_to_xlsx.py` の `main` の 211〜224 行と、
//! `clean_cell`・`split_row`・`is_separator`・`extract_tables`)。
//!
//! - 文字列は Python の str と同じく文字の番号の列([`PyStr`])で扱う。
//! - 空白の判定(`str.isspace`・`str.strip`・`re` の `\s`)と大文字小文字を区別しない照合(`re.I`)は、
//!   CPython 3.10.11(Unicode 13.0.0)の表([`unicode13`])で行う。Rust の Unicode の版に依る関数
//!   (`char::is_whitespace`・`char::to_lowercase` など)は使わない。
//! - 正規表現は Rust の regex の crate を使わない。旧版の式を、Python の `re` と同じ意味の手書きの
//!   照合にする(`.` は `\n` 以外・太字は最短一致・否定の文字の組は `\n` も含む・`<br>` は `re.I`)。
//! - 入力のパスは `Path(p)` の字面(`PurePosixPath` の正規化)で `stat` し、読む。`WARN` の行と
//!   出力先の検査に渡す入力は、引数の字面のまま持つ。

use super::errors::{self, Filename, PyException, frames};
use super::pyio::{PyStr, Runtime, Stream};
use super::unicode13;
use dev_workflow_platform::r4e;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Read};
use std::os::unix::ffi::OsStrExt;

const NEWLINE: u32 = 0x0a;
const CARRIAGE_RETURN: u32 = 0x0d;
const SPACE: u32 = 0x20;
const PIPE: u32 = 0x7c;
const BACKSLASH: u32 = 0x5c;
const STAR: u32 = 0x2a;
const BACKTICK: u32 = 0x60;
const HASH: u32 = 0x23;
const COLON: u32 = 0x3a;
const DASH: u32 = 0x2d;
const DOT: u32 = 0x2e;

/// `Path.is_file()` が「無い」と扱う errno(`pathlib._IGNORED_ERROS`: ENOENT・ENOTDIR・EBADF・ELOOP)。
const IGNORED_ERRNOS: [i32; 4] = [2, 20, 9, ELOOP];
#[cfg(target_os = "linux")]
const ELOOP: i32 = 40;
#[cfg(target_os = "macos")]
const ELOOP: i32 = 62;

/// 取り出した 1 つの表。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Table {
    /// `f"{doc_name} {heading}".strip()`(シート名の元)。
    pub name: PyStr,
    /// 見出しの行とデータの行。各行は `split_row` の結果(1 つ以上のセル)。
    pub rows: Vec<Vec<PyStr>>,
}

/// 表のある入力を読み終えた結果。
#[derive(Debug)]
pub struct Loaded {
    /// すべての入力の表(引数の順)。
    pub tables: Vec<Table>,
    /// 読んだ入力(旧版の `inputs`。引数の字面。出力先の事前検査に渡す)。
    pub inputs: Vec<OsString>,
}

/// 旧 `main` の入力の段の結果。
#[derive(Debug)]
pub enum Collected {
    /// 表がある。この後に出力へ進む。
    Tables(Loaded),
    /// `main` が返す終了値(表なしで無い入力があれば 2、表なしは 1)。
    Exit(i32),
}

/// 旧 `main` の 211〜224 行: 入力を引数の順に `is_file()` → `read_text()` → `extract_tables()`。
/// 無い入力の `WARN` は、全部を読んだ後に標準エラーへ出す(途中の入力で例外になれば出さない)。
/// 表が無ければ、無い入力があれば終了 2、無ければ標準出力へ `表が見つからなかった(出力なし)` と終了 1。
pub fn collect(py: &mut Runtime, args: &[OsString]) -> Result<Collected, PyException> {
    let mut tables = Vec::new();
    let mut missing = Vec::new();
    let mut inputs = Vec::new();
    for arg in args {
        py.check_signals()
            .map_err(|error| error.called_from(&[frames::MODULE, frames::MAIN_INPUT_LOOP]))?;
        let path = path_str(arg.as_bytes());
        // is_file の誤りは、is_file の except OSError の後始末に入る所(is_file の段)で信号を確かめる。
        let exists = match is_file(&path) {
            Ok(exists) => exists,
            Err(error) => {
                let stack = [
                    frames::MODULE,
                    frames::MAIN_CHECKS_IS_FILE,
                    frames::PATHLIB_IS_FILE,
                ];
                return Err(match py.enter_handler(error, 2, &stack) {
                    Ok(error) | Err(error) => error,
                });
            }
        };
        if !exists {
            missing.push(arg);
            continue;
        }
        // is_file() から戻った所(呼出しの切れ目)。
        py.check_signals()
            .map_err(|error| error.called_from(&[frames::MODULE, frames::MAIN_CHECKS_IS_FILE]))?;
        let text = read_text(py, &path)?;
        tables.extend(extract_tables(&text, &stem(&path)));
        // extract_tables(旧版は Python の繰り返し)の中で届いた SIGINT。
        py.check_signals()
            .map_err(|error| error.called_from(&[frames::MODULE, frames::MAIN_READS_INPUT]))?;
        inputs.push(arg.clone());
    }
    for arg in &missing {
        // for p in missing: の繰り返しの切れ目(print の前)。
        py.check_signals()
            .map_err(|error| error.called_from(frames::PRINT_WARN))?;
        let mut line = PyStr::from("WARN 入力が存在しない: ");
        line.push_py(&PyStr::from_os_bytes(arg.as_bytes()));
        py.print(Some(Stream::Stderr), &line)
            .map_err(|error| error.called_from(frames::PRINT_WARN))?;
    }
    if !missing.is_empty() && tables.is_empty() {
        return Ok(Collected::Exit(2));
    }
    if tables.is_empty() {
        py.check_signals()
            .map_err(|error| error.called_from(frames::PRINT_NO_TABLES))?;
        py.print(None, &PyStr::from("表が見つからなかった(出力なし)"))
            .map_err(|error| error.called_from(frames::PRINT_NO_TABLES))?;
        return Ok(Collected::Exit(1));
    }
    Ok(Collected::Tables(Loaded { tables, inputs }))
}

// ---------------------------------------------------------------- パス

/// `str(PurePosixPath(p))`: 連続した `/` と `.` の成分を除き、末尾の `/` を落とす。先頭がちょうど
/// `//` なら残し、3 つ以上は `/` にする。空になれば `.`。`..` は残す(リンクは辿らない)。
pub fn path_str(arg: &[u8]) -> Vec<u8> {
    let (root, rest): (&[u8], &[u8]) = if arg.first() == Some(&b'/') {
        let start = arg
            .iter()
            .position(|&byte| byte != b'/')
            .unwrap_or(arg.len());
        (if start == 2 { b"//" } else { b"/" }, &arg[start..])
    } else {
        (b"", arg)
    };
    let parts = rest
        .split(|&byte| byte == b'/')
        .filter(|part| !part.is_empty() && *part != b".")
        .collect::<Vec<_>>();
    if root.is_empty() && parts.is_empty() {
        return b".".to_vec();
    }
    let mut text = root.to_vec();
    text.extend_from_slice(&parts.join(&b'/'));
    text
}

/// `Path(p).stem`(`path` は [`path_str`] の結果)。名前(最後の成分)の最後の `.` が先頭でも末尾でも
/// なければ、その前まで。根だけ(`/`・`//`)と `.` の名前は空。
pub fn stem(path: &[u8]) -> PyStr {
    let name = match path {
        b"." | b"/" | b"//" => &[][..],
        _ => path.rsplit(|&byte| byte == b'/').next().unwrap_or(path),
    };
    let name = PyStr::from_os_bytes(name);
    let points = name.as_code_points();
    match points.iter().rposition(|&code| code == DOT) {
        Some(index) if index > 0 && index + 1 < points.len() => {
            PyStr::from_code_points(points[..index].to_vec())
        }
        _ => name,
    }
}

/// `Path.is_file()`: リンクを辿って `stat` し、通常ファイルか。ENOENT・ENOTDIR・EBADF・ELOOP は偽、
/// ほかの誤りは `OSError` の呼出履歴(`main` の 213 行から)。
pub fn is_file(path: &[u8]) -> Result<bool, PyException> {
    if path.contains(&0) {
        // `os.stat` の ValueError(埋め込まれた NUL)。`is_file` は偽を返す(引数からは来ない)。
        return Ok(false);
    }
    match std::fs::metadata(OsStr::from_bytes(path)) {
        Ok(info) => Ok(info.file_type().is_file()),
        Err(error) => {
            let number = error.raw_os_error().unwrap_or(errors::EIO);
            if IGNORED_ERRNOS.contains(&number) {
                return Ok(false);
            }
            Err(os_error_with_path(number, path).called_from(&[
                frames::MODULE,
                frames::MAIN_CHECKS_IS_FILE,
                frames::PATHLIB_IS_FILE,
                frames::PATHLIB_STAT,
            ]))
        }
    }
}

/// `Path.read_text(encoding="utf-8")`: 開いて全部を読み、UTF-8 として厳密に読む(BOM は残す)。
/// 改行は `\r\n` と `\r` を `\n` へ変える(`newline=None`)。
pub fn read_text(py: &mut Runtime, path: &[u8]) -> Result<PyStr, PyException> {
    read_text_closing(py, path, r4e::close_checked)
}

/// [`read_text`] の閉じ方(`close(2)`)を差し替えられる形。試験は閉じるときの誤りを作る。
///
/// `with self.open(…) as f: return f.read()` のとおり、読み(と UTF-8 の解読)の後に、成否に
/// かかわらず閉じる。閉じるときの誤りは `with` の行(1134 行)からの `OSError` で、読みの例外が
/// あれば、それを `__context__` に持つ(旧版の実測)。
fn read_text_closing(
    py: &mut Runtime,
    path: &[u8],
    close: impl FnOnce(File) -> io::Result<()>,
) -> Result<PyStr, PyException> {
    const STACK: &[errors::Frame] = &[frames::MODULE, frames::MAIN_READS_INPUT];
    let mut file = File::open(OsStr::from_bytes(path)).map_err(|error| {
        let number = error.raw_os_error().unwrap_or(errors::EIO);
        os_error_with_path(number, path)
            .called_from(&[frames::PATHLIB_READ_TEXT_OPENS, frames::PATHLIB_OPEN])
            .called_from(STACK)
    })?;
    let mut data = Vec::new();
    let read = file
        .read_to_end(&mut data)
        .map_err(|error| {
            let number = error.raw_os_error().unwrap_or(errors::EIO);
            PyException::os(number).called_from(&[frames::PATHLIB_READ_TEXT_READS])
        })
        .and_then(|_| {
            // 読み終えた後、decode の段に入る所(319 行)。読取りの間に届いた SIGINT は、解読より先に
            // KeyboardInterrupt になる(解読できない入力でも)。
            py.check_signals().map_err(|error| {
                error.called_from(&[frames::PATHLIB_READ_TEXT_READS, frames::CODECS_DECODE_ENTRY])
            })?;
            let text = errors::decode_utf8(&data).map_err(|error| {
                error.called_from(&[frames::PATHLIB_READ_TEXT_READS, frames::CODECS_DECODE])
            })?;
            // 解読(_buffer_decode の呼出し)から戻った所(322 行)。
            py.check_signals().map_err(|error| {
                error.called_from(&[frames::PATHLIB_READ_TEXT_READS, frames::CODECS_DECODE])
            })?;
            Ok(text)
        });
    let read = match read {
        Ok(text) => Ok(text),
        // 読取りか解読の例外は、with の後始末に入る所(1135 行)で信号を確かめる。届いていれば
        // KeyboardInterrupt になり、__exit__ は呼ばれない(後で捨てられるときに閉じ、誤りは出さない)。
        Err(error) => match py.enter_handler(error, 0, &[frames::PATHLIB_READ_TEXT_READS]) {
            Ok(error) => Err(error),
            Err(interrupt) => {
                drop(file);
                return Err(interrupt.called_from(STACK));
            }
        },
    };
    if let Err(error) = close(file) {
        let error = py.os_error(error.raw_os_error().unwrap_or(errors::EIO));
        let error = match read {
            Err(previous) => error.with_context(previous),
            Ok(_) => error,
        };
        return Err(error
            .called_from(&[frames::PATHLIB_READ_TEXT_OPENS])
            .called_from(STACK));
    }
    let text = read.map_err(|error| error.called_from(STACK))?;
    // with を出る __exit__ の呼出しから戻った所(1134 行)。
    py.check_signals().map_err(|error| {
        error
            .called_from(&[frames::PATHLIB_READ_TEXT_OPENS])
            .called_from(STACK)
    })?;
    Ok(PyStr::from_code_points(translate_newlines(text)))
}

/// `OSError(errno, strerror, filename)`(ファイル名は `str(path)`)。
fn os_error_with_path(number: i32, path: &[u8]) -> PyException {
    PyException::os_with_filename(number, Filename::Str(PyStr::from_os_bytes(path)))
}

/// 読んだ文字の `\r\n` と `\r` を `\n` にした、文字の番号の列。
fn translate_newlines(text: &str) -> Vec<u32> {
    let mut points = Vec::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if u32::from(c) == CARRIAGE_RETURN {
            if chars.peek() == Some(&'\n') {
                chars.next();
            }
            points.push(NEWLINE);
        } else {
            points.push(u32::from(c));
        }
    }
    points
}

// ---------------------------------------------------------------- str の操作

fn is_space(code: u32) -> bool {
    unicode13::is_space(code)
}

/// `str.strip()`(Unicode の空白)。
pub fn strip(text: &[u32]) -> &[u32] {
    lstrip(rstrip(text))
}

/// `str.lstrip()`。
pub fn lstrip(text: &[u32]) -> &[u32] {
    let start = text
        .iter()
        .position(|&code| !is_space(code))
        .unwrap_or(text.len());
    &text[start..]
}

fn rstrip(text: &[u32]) -> &[u32] {
    let end = text
        .iter()
        .rposition(|&code| !is_space(code))
        .map_or(0, |index| index + 1);
    &text[..end]
}

/// `str.strip(chars)`(1 文字だけの集合)。
fn strip_char(text: &[u32], char: u32) -> &[u32] {
    let start = text
        .iter()
        .position(|&code| code != char)
        .unwrap_or(text.len());
    let text = &text[start..];
    let end = text
        .iter()
        .rposition(|&code| code != char)
        .map_or(0, |index| index + 1);
    &text[..end]
}

/// `str.splitlines()` の改行か(`\n`・`\r`・`\v`・`\f`・`\x1c`〜`\x1e`・`\x85`・` `・` `)。
fn is_line_break(code: u32) -> bool {
    matches!(
        code,
        0x0a..=0x0d | 0x1c..=0x1e | 0x85 | 0x2028 | 0x2029
    )
}

/// `str.splitlines()`(行末を含めない。`\r\n` は 1 つの改行。最後の改行の後の空の行は作らない)。
pub fn splitlines(text: &[u32]) -> Vec<&[u32]> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < text.len() {
        let code = text[index];
        if is_line_break(code) {
            lines.push(&text[start..index]);
            index += if code == CARRIAGE_RETURN && text.get(index + 1) == Some(&NEWLINE) {
                2
            } else {
                1
            };
            start = index;
        } else {
            index += 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn starts_with(text: &[u32], prefix: &str) -> bool {
    let mut points = text.iter();
    prefix.chars().all(|c| points.next() == Some(&u32::from(c)))
}

// ---------------------------------------------------------------- re の照合

/// `re.I`(Unicode)の 1 文字の照合(CPython 3.10.11 の `sre_compile._compile` と `_sre` の
/// `LITERAL_UNI_IGNORE`・`IN_UNI_IGNORE`)。
///
/// 式の文字が大文字小文字を持たなければ、同じ文字だけに一致する。持てば、式の文字の単純な小文字化を
/// `lo` として、文字列の文字の単純な小文字化が `lo`(`lo` が同じと見なす組にあれば、その組のどれか)に
/// 一致する。
pub fn sre_ignorecase_eq(pattern: u32, text: u32) -> bool {
    if !unicode13::sre_iscased(pattern) {
        return text == pattern;
    }
    let lower = unicode13::simple_lower(pattern);
    let folded = unicode13::simple_lower(text);
    match unicode13::sre_equivalence(lower) {
        Some(group) => group.contains(&folded),
        None => folded == lower,
    }
}

/// `pattern.sub(repl, text)` の枠: 先頭から照合し、一致したら置き換えて一致の終わりから続け、
/// 一致しなければ 1 文字進める。`matcher` は位置 `start` で一致すれば
/// `(置き換えの文字の範囲, 一致の終わり)` を返す。置き換えの範囲が `None` なら固定の文字。
fn substitute(
    text: &[u32],
    replacement: &[u32],
    matcher: impl Fn(&[u32], usize) -> Option<(Option<(usize, usize)>, usize)>,
) -> Vec<u32> {
    let mut result = Vec::with_capacity(text.len());
    let mut index = 0;
    while index < text.len() {
        match matcher(text, index) {
            Some((group, end)) => {
                match group {
                    Some((from, to)) => result.extend_from_slice(&text[from..to]),
                    None => result.extend_from_slice(replacement),
                }
                index = end;
            }
            None => {
                result.push(text[index]);
                index += 1;
            }
        }
    }
    result
}

/// `from` から後で最初に `code` がある位置。
fn find_from(text: &[u32], from: usize, code: u32) -> Option<usize> {
    let rest = text.get(from..)?;
    rest.iter()
        .position(|&found| found == code)
        .map(|offset| from + offset)
}

/// `\*\*(.+?)\*\*`(`.` は `\n` 以外。最短一致)。
fn match_bold(text: &[u32], start: usize) -> Option<(Option<(usize, usize)>, usize)> {
    if text.get(start) != Some(&STAR) || text.get(start + 1) != Some(&STAR) {
        return None;
    }
    let mut end = start + 2;
    loop {
        // `.+?` が 1 文字ずつ伸びる(`\n` と文字列の終わりで失敗)。
        if text.get(end).is_none_or(|&code| code == NEWLINE) {
            return None;
        }
        end += 1;
        if text.get(end) == Some(&STAR) && text.get(end + 1) == Some(&STAR) {
            return Some((Some((start + 2, end)), end + 2));
        }
    }
}

/// `` `([^`]*)` ``(否定の組は `\n` も含む)。
fn match_code(text: &[u32], start: usize) -> Option<(Option<(usize, usize)>, usize)> {
    if text.get(start) != Some(&BACKTICK) {
        return None;
    }
    let close = find_from(text, start + 1, BACKTICK)?;
    Some((Some((start + 1, close)), close + 1))
}

/// `\[([^\]]*)\]\([^)]*\)`。
fn match_link(text: &[u32], start: usize) -> Option<(Option<(usize, usize)>, usize)> {
    if text.get(start) != Some(&u32::from('[')) {
        return None;
    }
    let close = find_from(text, start + 1, u32::from(']'))?;
    if text.get(close + 1) != Some(&u32::from('(')) {
        return None;
    }
    let paren = find_from(text, close + 2, u32::from(')'))?;
    Some((Some((start + 1, close)), paren + 1))
}

/// `<br\s*/?>`(`re.IGNORECASE`)。`\s*` の後の `/` は、その後に `>` があるときだけ一致に入る。
fn match_br(text: &[u32], start: usize) -> Option<(Option<(usize, usize)>, usize)> {
    if text.get(start) != Some(&u32::from('<')) {
        return None;
    }
    let letters = [u32::from('b'), u32::from('r')];
    for (offset, letter) in letters.iter().enumerate() {
        if !text
            .get(start + 1 + offset)
            .is_some_and(|&code| sre_ignorecase_eq(*letter, code))
        {
            return None;
        }
    }
    let mut end = start + 3;
    while text.get(end).is_some_and(|&code| is_space(code)) {
        end += 1;
    }
    if text.get(end) == Some(&u32::from('/')) && text.get(end + 1) == Some(&u32::from('>')) {
        return Some((None, end + 2));
    }
    (text.get(end) == Some(&u32::from('>'))).then_some((None, end + 1))
}

// ---------------------------------------------------------------- 表の取り出し

/// 旧 `clean_cell`: 前後の空白を除き、太字・コード・リンク・`<br>` の順に置き換える。
pub fn clean_cell(text: &[u32]) -> Vec<u32> {
    let text = strip(text);
    let text = substitute(text, &[], match_bold);
    let text = substitute(&text, &[], match_code);
    let text = substitute(&text, &[], match_link);
    substitute(&text, &[NEWLINE], match_br)
}

/// 旧 `split_row`: 前後の空白を除き、先頭と末尾の `|` を 1 つずつ外して、前が `\` でない `|` で分ける。
/// 各セルは `\|` を `|` にしてから [`clean_cell`]。
pub fn split_row(line: &[u32]) -> Vec<PyStr> {
    let mut line = strip(line);
    if line.first() == Some(&PIPE) {
        line = &line[1..];
    }
    if line.last() == Some(&PIPE) {
        line = &line[..line.len() - 1];
    }
    let mut cells = Vec::new();
    let mut start = 0;
    for index in 0..line.len() {
        if line[index] == PIPE && (index == 0 || line[index - 1] != BACKSLASH) {
            cells.push(&line[start..index]);
            start = index + 1;
        }
    }
    cells.push(&line[start..]);
    cells
        .into_iter()
        .map(|cell| PyStr::from_code_points(clean_cell(&unescape_pipes(cell))))
        .collect()
}

/// `str.replace("\\|", "|")`。
fn unescape_pipes(cell: &[u32]) -> Vec<u32> {
    let mut result = Vec::with_capacity(cell.len());
    let mut index = 0;
    while index < cell.len() {
        if cell[index] == BACKSLASH && cell.get(index + 1) == Some(&PIPE) {
            result.push(PIPE);
            index += 2;
        } else {
            result.push(cell[index]);
            index += 1;
        }
    }
    result
}

/// 旧 `is_separator`: 前後の空白と前後の `|` を除いて `|` で分け、空でないセルが 1 つ以上あり、
/// すべてが `:?-{3,}:?` に全体で一致する。
pub fn is_separator(line: &[u32]) -> bool {
    let body = strip_char(strip(line), PIPE);
    let mut filled = 0;
    for cell in body.split(|&code| code == PIPE) {
        let cell = strip(cell);
        if cell.is_empty() {
            continue;
        }
        if !is_dash_cell(cell) {
            return false;
        }
        filled += 1;
    }
    filled > 0
}

/// `re.fullmatch(r":?-{3,}:?", cell)`。
fn is_dash_cell(cell: &[u32]) -> bool {
    let cell = cell.strip_prefix(&[COLON]).unwrap_or(cell);
    let cell = cell.strip_suffix(&[COLON]).unwrap_or(cell);
    cell.len() >= 3 && cell.iter().all(|&code| code == DASH)
}

/// `re.match(r"^(#{1,6})\s+(.*)", line)` が一致すれば、`m.group(2).strip()`。
fn heading(line: &[u32]) -> Option<&[u32]> {
    let hashes = line.iter().take_while(|&&code| code == HASH).count();
    if !(1..=6).contains(&hashes) || !line.get(hashes).is_some_and(|&code| is_space(code)) {
        return None;
    }
    // `\s+` が空白を全部取り、`.*` が残り(行に `\n` は無い)を取る。
    let rest = lstrip(&line[hashes..]);
    let end = rest
        .iter()
        .position(|&code| code == NEWLINE)
        .unwrap_or(rest.len());
    Some(strip(&rest[..end]))
}

/// 旧 `extract_tables`: 行に分け、``` のフェンスの中を飛ばし、見出しを覚え、`|` で始まる行の次が
/// 区切り行なら表にする(続く `|` で始まる行がデータの行)。
pub fn extract_tables(text: &PyStr, doc_name: &PyStr) -> Vec<Table> {
    let lines = splitlines(text.as_code_points());
    let mut tables = Vec::new();
    let mut current_heading: &[u32] = &[];
    let mut in_code = false;
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        if starts_with(lstrip(line), "```") {
            in_code = !in_code;
            index += 1;
            continue;
        }
        if in_code {
            index += 1;
            continue;
        }
        if let Some(found) = heading(line) {
            current_heading = found;
            index += 1;
            continue;
        }
        if lstrip(line).first() == Some(&PIPE)
            && index + 1 < lines.len()
            && is_separator(lines[index + 1])
        {
            let mut rows = vec![split_row(line)];
            index += 2;
            while index < lines.len() && lstrip(lines[index]).first() == Some(&PIPE) {
                rows.push(split_row(lines[index]));
                index += 1;
            }
            tables.push(Table {
                name: table_name(doc_name, current_heading),
                rows,
            });
            continue;
        }
        index += 1;
    }
    tables
}

/// `f"{doc_name} {heading}".strip()`。
fn table_name(doc_name: &PyStr, heading: &[u32]) -> PyStr {
    let mut name = doc_name.as_code_points().to_vec();
    name.push(SPACE);
    name.extend_from_slice(heading);
    PyStr::from_code_points(strip(&name).to_vec())
}

#[cfg(test)]
mod tests {
    use super::super::pyio::fake::FakeOs;
    use super::*;

    fn s(text: &str) -> Vec<u32> {
        text.chars().map(u32::from).collect()
    }

    fn text(points: &[u32]) -> String {
        points
            .iter()
            .map(|&code| char::from_u32(code).unwrap())
            .collect()
    }

    fn clean(input: &str) -> String {
        text(&clean_cell(&s(input)))
    }

    #[test]
    fn splitlines_matches_python() {
        let cases: [(&str, &[&str]); 9] = [
            ("", &[]),
            ("\n", &[""]),
            ("a\nb", &["a", "b"]),
            ("a\n\nb\n", &["a", "", "b"]),
            ("a\r\nb\rc", &["a", "b", "c"]),
            (
                "a\x0bb\x0cc\x1cd\x1de\x1ef",
                &["a", "b", "c", "d", "e", "f"],
            ),
            ("a\u{85}b\u{2028}c\u{2029}d", &["a", "b", "c", "d"]),
            // \x1f は改行ではない(空白ではある)。
            ("a\x1fb", &["a\x1fb"]),
            ("\r\r\n", &["", ""]),
        ];
        for (input, expected) in cases {
            let points = s(input);
            let lines = splitlines(&points)
                .into_iter()
                .map(text)
                .collect::<Vec<_>>();
            assert_eq!(lines, expected, "{input:?}");
        }
    }

    #[test]
    fn strip_uses_the_python_space_set() {
        for (input, expected) in [
            ("  a  ", "a"),
            ("\u{3000}a\u{a0}", "a"),
            ("\x1c\x1d\x1e\x1fa\u{85}", "a"),
            ("\u{200b}a\u{200b}", "\u{200b}a\u{200b}"),
            ("\u{feff}a", "\u{feff}a"),
            ("\u{180e}a", "\u{180e}a"),
            ("\t\x0b\x0c\ra", "a"),
        ] {
            assert_eq!(text(strip(&s(input))), expected, "{input:?}");
        }
    }

    #[test]
    fn newlines_are_translated_like_universal_newlines() {
        assert_eq!(text(&translate_newlines("a\r\nb\rc\n\r")), "a\nb\nc\n\n");
        assert_eq!(text(&translate_newlines("\u{feff}x\r")), "\u{feff}x\n");
    }

    #[test]
    fn bold_is_the_shortest_match_and_dot_excludes_only_newline() {
        for (input, expected) in [
            ("**b**", "b"),
            ("**a **b** c**", "a b c"),
            ("****", "****"),
            ("*****", "*"),
            ("**unclosed", "**unclosed"),
            ("**  sp  **", "  sp  "),
            ("**`x`**", "x"),
            // `.` は \n 以外(\r や   も一致する)。\n は <br> の置き換えで後から入る。
            ("**a\u{2028}b**", "a\u{2028}b"),
            ("**a\x0bb**", "a\x0bb"),
        ] {
            assert_eq!(clean(input), expected, "{input:?}");
        }
        // `.` は \n に一致しない(clean_cell の前の値では \n は来ないので、照合だけを確かめる)。
        let points = s("**a\nb**");
        assert_eq!(match_bold(&points, 0), None);
    }

    #[test]
    fn code_link_and_br_follow_the_python_patterns() {
        for (input, expected) in [
            ("`c`", "c"),
            ("` code `", " code "),
            ("`a", "`a"),
            ("``", ""),
            ("[l](u)", "l"),
            ("[x](y", "[x](y"),
            ("[**bold link**](h)", "bold link"),
            ("[a] (b)", "[a] (b)"),
            ("[a](b)c)", "ac)"),
            ("a<br>b", "a\nb"),
            ("x<BR/>", "x\n"),
            ("<br />y", "\ny"),
            ("p<br\u{3000}>q", "p\nq"),
            ("m<Br  />n", "m\nn"),
            ("<br><br>", "\n\n"),
            ("r<br/ >s", "r<br/ >s"),
            ("u<brx>v", "u<brx>v"),
            ("<br\t/>", "\n"),
            ("<br//>", "<br//>"),
            ("a<BR>b<bR />c", "a\nb\nc"),
        ] {
            assert_eq!(clean(input), expected, "{input:?}");
        }
        // 前後の空白は置き換えの前に除くので、<br> から来た前後の改行は残る。
        assert_eq!(clean(" x<br> "), "x\n");
    }

    #[test]
    fn ignorecase_follows_sre() {
        // 大文字小文字を持たない文字は同じ文字だけ。
        assert!(sre_ignorecase_eq(u32::from('1'), u32::from('1')));
        assert!(!sre_ignorecase_eq(u32::from('1'), u32::from('١')));
        assert!(sre_ignorecase_eq(u32::from('b'), u32::from('B')));
        assert!(!sre_ignorecase_eq(u32::from('b'), u32::from('c')));
        // 同じと見なす組(i と ı、s と ſ、σ と ς)と、単純な小文字化(K と k、İ と i)。
        for (pattern, text) in [
            ('i', 'ı'),
            ('I', 'ı'),
            ('ı', 'I'),
            ('İ', 'i'),
            ('İ', 'ı'),
            ('s', 'ſ'),
            ('S', 'ſ'),
            ('k', '\u{212a}'),
            ('\u{212a}', 'K'),
            ('Σ', 'ς'),
            ('ς', 'σ'),
        ] {
            assert!(
                sre_ignorecase_eq(u32::from(pattern), u32::from(text)),
                "{pattern} {text}"
            );
        }
        // ẞ(U+1E9E)の単純な小文字化は ß。SS には一致しない。
        assert!(sre_ignorecase_eq(u32::from('ẞ'), u32::from('ß')));
        assert!(!sre_ignorecase_eq(u32::from('ß'), u32::from('s')));
        // Unicode 14 で大文字小文字を得た文字(Ⱟ U+2C2F)は Unicode 13 では持たない。
        assert!(!sre_ignorecase_eq(0x2c2f, 0x2c5f));
    }

    #[test]
    fn split_row_handles_pipes_and_escapes() {
        let row = |input: &str| {
            split_row(&s(input))
                .iter()
                .map(|cell| cell.to_string_lossy())
                .collect::<Vec<_>>()
        };
        assert_eq!(row("| a | b |"), ["a", "b"]);
        assert_eq!(row("| x \\| y | z |"), ["x | y", "z"]);
        assert_eq!(row("| \\\\| w | v\\| |"), ["\\| w", "v|"]);
        assert_eq!(row("| \\| | end\\|"), ["|", "end\\"]);
        assert_eq!(row("|"), [""]);
        assert_eq!(row("||"), [""]);
        assert_eq!(row("|||"), ["", ""]);
        assert_eq!(row("  | a |  "), ["a"]);
        assert_eq!(row("| 1 | 2 | 3 | 4 | 5 |"), ["1", "2", "3", "4", "5"]);
    }

    #[test]
    fn separators_follow_the_legacy_rule() {
        for (input, expected) in [
            ("|---|---|", true),
            ("| :---: | ---: |", true),
            ("| --- | |", true),
            ("---", true),
            (":--- | ---:", true),
            ("|--|--|", false),
            ("|:-:|", false),
            ("| :----- |", true),
            ("||", false),
            ("|   |", false),
            ("|-:--|", false),
            ("|::---|", false),
            ("|---|x|", false),
            ("\u{3000}|---|\u{3000}", true),
        ] {
            assert_eq!(is_separator(&s(input)), expected, "{input:?}");
        }
    }

    #[test]
    fn headings_follow_the_legacy_pattern() {
        for (input, expected) in [
            ("# a", Some("a")),
            ("###### six", Some("six")),
            ("####### seven", None),
            ("#見出し", None),
            ("# ", Some("")),
            ("#", None),
            ("#\t", Some("")),
            ("#\u{3000}全角", Some("全角")),
            ("## a ##", Some("a ##")),
            ("  # 字下げ", None),
            ("#  a \u{3000}", Some("a")),
            ("#\x1fx", Some("x")),
        ] {
            assert_eq!(
                heading(&s(input)).map(text),
                expected.map(str::to_owned),
                "{input:?}"
            );
        }
    }

    #[test]
    fn tables_are_extracted_like_the_legacy_script() {
        let doc = PyStr::from("doc");
        let tables = extract_tables(
            &PyStr::from(
                "# 概要\n| a | b |\n|---|---|\n| 1 | 2 |\n```\n| x |\n|---|\n```\n~~~\n| t |\n|---|\n~~~\n# \n| e |\n---\n|---|\n",
            ),
            &doc,
        );
        let summary = tables
            .iter()
            .map(|table| {
                (
                    table.name.to_string_lossy(),
                    table
                        .rows
                        .iter()
                        .map(|row| row.iter().map(PyStr::to_string_lossy).collect::<Vec<_>>())
                        .collect::<Vec<_>>(),
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(
            summary,
            [
                ("doc 概要".to_owned(), vec![vec!["a", "b"], vec!["1", "2"]]),
                ("doc 概要".to_owned(), vec![vec!["t"]]),
                ("doc".to_owned(), vec![vec!["e"], vec!["---"]]),
            ]
            .map(|(name, rows)| (
                name,
                rows.into_iter()
                    .map(|row| row.into_iter().map(str::to_owned).collect::<Vec<_>>())
                    .collect::<Vec<_>>()
            ))
        );
        // | で始まらない行は表にしない。データの行が無い表も 1 つの表。
        assert!(extract_tables(&PyStr::from("a | b\n---|---\n"), &doc).is_empty());
        assert_eq!(
            extract_tables(&PyStr::from("| a |\n|---|"), &doc)[0].rows,
            vec![vec![PyStr::from("a")]]
        );
        // BOM の直後の | は表にならない。
        assert!(extract_tables(&PyStr::from("\u{feff}| a |\n|---|\n"), &doc).is_empty());
    }

    #[test]
    fn path_text_and_stem_follow_pathlib() {
        for (input, expected) in [
            (&b""[..], &b"."[..]),
            (b".", b"."),
            (b"./a.md", b"a.md"),
            (b"x//y.md", b"x/y.md"),
            (b"a.md/", b"a.md"),
            (b"/", b"/"),
            (b"//", b"//"),
            (b"///a//b/./", b"/a/b"),
            (b"//a", b"//a"),
            (b"../a/./.", b"../a"),
        ] {
            assert_eq!(path_str(input), expected, "{input:?}");
        }
        for (input, expected) in [
            (&b".md"[..], ".md"),
            (b"a.", "a."),
            (b"a.b.md", "a.b"),
            (b"x/y.md", "y"),
            (b"..", ".."),
            (b".", ""),
            (b"/", ""),
            (b"dir/.hidden.md", ".hidden"),
        ] {
            assert_eq!(stem(input).to_string_lossy(), expected, "{input:?}");
        }
        // UTF-8 でない名前はサロゲートになる。
        assert_eq!(stem(b"doc/\xff\xfe.md").as_code_points(), &[0xdcff, 0xdcfe]);
    }

    fn runtime() -> (Runtime, FakeOs) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        let runtime = Runtime::start_with(&state, &|_| None, Box::new(os.clone())).unwrap();
        (runtime, os)
    }

    #[test]
    fn reading_reports_python_errors() {
        let (mut py, _os) = runtime();
        let py = &mut py;
        let dir = std::env::temp_dir().join(format!("r4e-markdown-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = |name: &str| dir.join(name).into_os_string().into_vec();
        use std::os::unix::ffi::OsStringExt;
        std::fs::write(dir.join("ok.md"), b"\xef\xbb\xbf# a\r\n|x|\r|---|\n").unwrap();
        assert_eq!(
            read_text(py, &path("ok.md")).unwrap().to_string_lossy(),
            "\u{feff}# a\n|x|\n|---|\n"
        );
        assert!(is_file(&path("ok.md")).unwrap());
        assert!(!is_file(&path("none.md")).unwrap());
        assert!(!is_file(&path("ok.md/x")).unwrap());
        assert!(!is_file(&dir.clone().into_os_string().into_vec()).unwrap());
        std::fs::write(dir.join("bad.md"), b"ab\x80").unwrap();
        let error = read_text(py, &path("bad.md")).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "UnicodeDecodeError: 'utf-8' codec can't decode byte 0x80 in position 2: invalid start byte"
        );
        assert_eq!(
            error
                .traceback
                .iter()
                .map(|frame| (frame.file, frame.line))
                .collect::<Vec<_>>(),
            [
                ("md_tables_to_xlsx.py", 241),
                ("md_tables_to_xlsx.py", 216),
                ("pathlib.py", 1135),
                ("codecs.py", 322),
            ]
        );
        let error = read_text(py, &path("none.md")).unwrap_err();
        assert_eq!(error.errno, Some(2));
        assert_eq!(lines(&error), [241, 216, 1134, 1119]);

        // 閉じるとき(close(2))の誤りは、with の行からの OSError(旧版の実測)。
        let failing = |errno: i32| {
            move |file: File| {
                drop(file);
                Err(io::Error::from_raw_os_error(errno))
            }
        };
        let error = read_text_closing(py, &path("ok.md"), failing(5)).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "OSError: [Errno 5] Input/output error"
        );
        assert_eq!(lines(&error), [241, 216, 1134]);
        assert!(error.context.is_none());
        // 読みの例外の後に閉じるときも失敗したら、読みの例外を __context__ に持つ。
        let error = read_text_closing(py, &path("bad.md"), failing(28)).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "OSError: [Errno 28] No space left on device"
        );
        assert_eq!(lines(&error), [241, 216, 1134]);
        let context = error.context.as_ref().expect("読みの例外");
        assert!(
            context
                .type_line()
                .to_string_lossy()
                .starts_with("UnicodeDecodeError: ")
        );
        assert_eq!(lines(context), [1135, 322]);
        // EINTR は、届いた SIGINT があれば KeyboardInterrupt、無ければ InterruptedError。
        let error = read_text_closing(py, &path("ok.md"), failing(4)).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "InterruptedError: [Errno 4] Interrupted system call"
        );
        // SIGINT が閉じる間に届いた(decode の段に入る所と解読の後の 2 回の確かめの直後)。
        let (mut interrupted, os) = runtime();
        os.0.borrow_mut().sigint_after_checks = Some(2);
        let error = read_text_closing(&mut interrupted, &path("ok.md"), failing(4)).unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), "KeyboardInterrupt");
        assert_eq!(lines(&error), [241, 216, 1134]);
        assert!(error.context.is_none());
        // 読取りの間に届いた SIGINT は、decode の段に入る所(319 行)で KeyboardInterrupt。with の
        // 後始末は __exit__ を呼び、閉じるときの InterruptedError は、それを __context__ に持つ。
        let (mut interrupted, os) = runtime();
        os.0.borrow_mut().pending = true;
        let error = read_text_closing(&mut interrupted, &path("ok.md"), failing(4)).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "InterruptedError: [Errno 4] Interrupted system call"
        );
        let context = error.context.as_ref().expect("KeyboardInterrupt");
        assert_eq!(context.type_line().to_string_lossy(), "KeyboardInterrupt");
        assert_eq!(lines(context), [1135, 319]);
        // 解読できない入力でも、読取りの間に届いた SIGINT は解読より先に KeyboardInterrupt
        // (旧版の実測: 600 MiB の入力に 0.15 秒後に送る)。
        let closed = std::cell::Cell::new(0);
        let counting = |file: File| {
            closed.set(closed.get() + 1);
            drop(file);
            Ok(())
        };
        let (mut interrupted, os) = runtime();
        os.0.borrow_mut().pending = true;
        let error = read_text_closing(&mut interrupted, &path("bad.md"), counting).unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), "KeyboardInterrupt");
        assert_eq!(lines(&error), [241, 216, 1135, 319]);
        assert!(error.context.is_none());
        assert_eq!(closed.get(), 1);
        // 解読の間に届いた SIGINT(decode の段に入る所の確かめの後): 解読の誤りの後、with の
        // 後始末に入る所(1135 行)で KeyboardInterrupt になり、解読の誤りを __context__ に持つ。
        // __exit__ は呼ばれない(閉じない)。旧版の実測: 0.3〜0.6 秒後に送る。
        let (mut interrupted, os) = runtime();
        os.0.borrow_mut().sigint_after_checks = Some(1);
        let error = read_text_closing(&mut interrupted, &path("bad.md"), |_: File| {
            panic!("__exit__ は呼ばれない")
        })
        .unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), "KeyboardInterrupt");
        assert_eq!(lines(&error), [241, 216, 1135]);
        let context = error.context.as_ref().expect("解読の誤り");
        assert!(
            context
                .type_line()
                .to_string_lossy()
                .starts_with("UnicodeDecodeError: ")
        );
        assert_eq!(lines(context), [1135, 322]);
        // 読取りの誤り(ディレクトリを読む EISDIR)の前に届いた SIGINT も、with の後始末に入る所。
        let directory = dir.join("sub");
        std::fs::create_dir(&directory).unwrap();
        let (mut interrupted, os) = runtime();
        os.0.borrow_mut().pending = true;
        let error = read_text_closing(
            &mut interrupted,
            &directory.clone().into_os_string().into_vec(),
            |_: File| panic!("__exit__ は呼ばれない"),
        )
        .unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), "KeyboardInterrupt");
        assert_eq!(lines(&error), [241, 216, 1135]);
        let context = error.context.as_ref().expect("読取りの誤り");
        assert_eq!(
            context.type_line().to_string_lossy(),
            "IsADirectoryError: [Errno 21] Is a directory"
        );
        assert_eq!(lines(context), [1135]);
        // 届いていなければ、読取りの誤りのまま(__exit__ で閉じる)。
        closed.set(0);
        let error =
            read_text_closing(py, &directory.into_os_string().into_vec(), counting).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "IsADirectoryError: [Errno 21] Is a directory"
        );
        assert_eq!(lines(&error), [241, 216, 1135]);
        assert_eq!(closed.get(), 1);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    fn lines(error: &PyException) -> Vec<u32> {
        error.traceback.iter().map(|frame| frame.line).collect()
    }
}
