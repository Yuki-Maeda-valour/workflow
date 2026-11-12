//! `candidate-keys.py` の移植。収集(`--task-dir`)と検査(`--check`)。
//!
//! 状態名は `resolve_task_dir::state_file`、コードフェンスは `task_digest` の `normalize::fence_flags`(緩い方針)を使う(複製しない)。

use crate::commands::r4a_rest_support::args::{self, Action, Env, Kind, NArgs, Parser, Value};
use crate::commands::r4a_rest_support::chars::{self, PyStr};
use crate::commands::r4a_rest_support::driver;
use crate::commands::r4a_rest_support::exception::{self, Frame, PyException};
use crate::commands::r4a_rest_support::frames::*;
use crate::commands::r4a_rest_support::json::{self, PyJson};
use crate::commands::r4a_rest_support::nfc;
use crate::commands::r4a_rest_support::stream::{Program, Raised, StdinRead};
use crate::commands::r4a_rest_support::text;
use crate::commands::task_digest::normalize::FencePolicy;
use std::os::unix::ffi::OsStrExt;

pub const PROG: &str = "candidate-keys.py";
const SOURCES: [&str; 4] = [
    "data-audit",
    "refactor",
    "stack-research",
    "reflect-decisions",
];

const fn frame(line: u32, func: &'static str, source: &'static str) -> Frame {
    Frame {
        file: PROG,
        line,
        func,
        source: Some(source),
    }
}
const MODULE: Frame = frame(232, "<module>", "sys.exit(main())");
const MAIN_PARSE: Frame = frame(208, "main", "args = parser.parse_args()");
const MAIN_USAGE_CHECK: Frame = frame(
    212,
    "main",
    "print(\"使い方: candidate-keys.py --check [--source=<発見元>] <パス | ->\", file=sys.stderr)",
);
const MAIN_USAGE_SOURCE: Frame = frame(
    215,
    "main",
    "print(f\"使い方: --source は {' / '.join(SOURCES)} のどれか\", file=sys.stderr)",
);
const MAIN_CHECK: Frame = frame(217, "main", "code, payload = check(args.path, args.source)");
const MAIN_USAGE_DIR: Frame = frame(
    220,
    "main",
    "print(\"使い方: candidate-keys.py --task-dir=<ディレクトリ>\", file=sys.stderr)",
);
const MAIN_PATH: Frame = frame(222, "main", "task_dir = Path(args.task_dir)");
const MAIN_EXISTS: Frame = frame(
    223,
    "main",
    "if (task_dir.exists() or task_dir.is_symlink()) and not task_dir.is_dir():",
);
const MAIN_NOT_DIR: Frame = frame(
    224,
    "main",
    "print(f\"使い方: --task-dir がディレクトリでない: {args.task_dir}\", file=sys.stderr)",
);
const MAIN_COLLECT: Frame = frame(226, "main", "code, payload = collect(task_dir)");
const MAIN_PRINT: Frame = frame(
    227,
    "main",
    "print(json.dumps(payload, ensure_ascii=False, sort_keys=True))",
);
const COLLECT_EXISTS: Frame = frame(
    118,
    "collect",
    "if not task_dir.exists() and not task_dir.is_symlink():",
);
const COLLECT_SCANDIR: Frame = frame(122, "collect", "with os.scandir(task_dir) as iterator:");
const COLLECT_PRINT: Frame = frame(
    125,
    "collect",
    "print(f\"収集できない: ディレクトリを読めない: {exc}\", file=sys.stderr)",
);
const CHECK_READ: Frame = frame(196, "check", "return check_text(_read(path), expected)");
const READ_STDIN: Frame = frame(
    110,
    "_read",
    "data = sys.stdin.buffer.read() if path == \"-\" else Path(path).read_bytes()",
);
const READ_DECODE: Frame = frame(111, "_read", "return data.decode(\"utf-8\")");

pub fn parser() -> Parser {
    Parser {
        prog: PROG,
        description: None,
        actions: vec![
            Action {
                option_strings: &["--task-dir"],
                dest: "task_dir",
                nargs: NArgs::One,
                kind: Kind::Store,
                help: None,
            },
            Action {
                option_strings: &["--check"],
                dest: "check",
                nargs: NArgs::Zero,
                kind: Kind::StoreTrue,
                help: None,
            },
            Action {
                option_strings: &["--source"],
                dest: "source",
                nargs: NArgs::One,
                kind: Kind::Store,
                help: None,
            },
            Action {
                option_strings: &[],
                dest: "path",
                nargs: NArgs::Optional,
                kind: Kind::Store,
                help: Some("--check で検査する候補のパス。`-` なら stdin を読む"),
            },
        ],
    }
}

// ---------------------------------------------------------------- 本文の解析

/// `re.sub(r"\s+", " ", NFC(value)).strip()`。
pub fn normalize_key(value: &[u32]) -> PyStr {
    let composed = nfc::nfc(value);
    let mut out: PyStr = Vec::with_capacity(composed.len());
    let mut in_space = false;
    for &c in &composed {
        if chars::is_space(c) {
            if !in_space {
                out.push(32);
            }
            in_space = true;
        } else {
            out.push(c);
            in_space = false;
        }
    }
    let start = out
        .iter()
        .position(|&c| !chars::is_space(c))
        .unwrap_or(out.len());
    let end = out
        .iter()
        .rposition(|&c| !chars::is_space(c))
        .map_or(start, |p| p + 1);
    out[start..end].to_vec()
}

struct Parsed {
    source_lines: usize,
    sources: Vec<&'static str>,
    key_lines: usize,
    keys: Vec<PyStr>,
    skipped: Vec<PyStr>,
    malformed_skips: usize,
    unattended: bool,
    record: bool,
    checkboxes: Vec<usize>,
}

fn match_source(line: &[u32]) -> Option<&'static str> {
    const HEAD: &str = "> **発見元**: ";
    if !text::starts_with(line, HEAD) {
        return None;
    }
    let rest = &line[HEAD.chars().count()..];
    SOURCES.iter().copied().find(|name| {
        text::starts_with(rest, name) && {
            let tail = &rest[name.len()..];
            tail.is_empty() || tail[0] == 32 || tail[0] == 40
        }
    })
}

fn match_skip(line: &[u32]) -> Option<PyStr> {
    const HEAD: &str = "> **見送り**: ";
    if !text::starts_with(line, HEAD) {
        return None;
    }
    let rest = &line[HEAD.chars().count()..];
    let digit = |c: u32| (48..=57).contains(&c);
    let shape: [fn(u32) -> bool; 10] = [
        |c| (48..=57).contains(&c),
        |c| (48..=57).contains(&c),
        |c| (48..=57).contains(&c),
        |c| (48..=57).contains(&c),
        |c| c == 45,
        |c| (48..=57).contains(&c),
        |c| (48..=57).contains(&c),
        |c| c == 45,
        |c| (48..=57).contains(&c),
        |c| (48..=57).contains(&c),
    ];
    let _ = digit;
    if rest.len() < 10 + 3 + 1 || !shape.iter().zip(rest).all(|(f, &c)| f(c)) {
        return None;
    }
    let after = &rest[10..];
    // " — " の後に 1 文字以上(`.+` は改行以外。行に改行は無い)
    (text::starts_with(after, " — ") && after.len() > 3).then(|| rest.to_vec())
}

fn is_checkbox(line: &[u32]) -> bool {
    let rest = &line[line
        .iter()
        .position(|&c| !chars::is_space(c))
        .unwrap_or(line.len())..];
    text::starts_with(rest, "- [")
        && matches!(rest.get(3), Some(&32) | Some(&120) | Some(&88))
        && rest.get(4) == Some(&93)
}

fn parse(body: &[u32]) -> Parsed {
    let lines: Vec<PyStr> = text::script_lines(body)
        .iter()
        .map(|l| text::rstrip_space_tab(l))
        .collect();
    let strings: Vec<String> = lines.iter().map(|l| text::to_string(l)).collect();
    let refs: Vec<&str> = strings.iter().map(String::as_str).collect();
    let fenced = crate::commands::task_digest::normalize::fence_flags(&refs, FencePolicy::Loose)
        .expect("the loose policy never fails");
    let mut header: Vec<&PyStr> = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if fenced[i] {
            continue;
        }
        if text::starts_with(line, "## ") {
            break;
        }
        header.push(line);
    }
    let source_lines: Vec<&&PyStr> = header
        .iter()
        .filter(|l| text::starts_with(l, "> **発見元**:"))
        .collect();
    let sources: Vec<&'static str> = source_lines
        .iter()
        .filter_map(|l| match_source(l))
        .collect();
    let key_lines: Vec<&&PyStr> = header
        .iter()
        .filter(|l| text::starts_with(l, "> **指摘キー**:"))
        .collect();
    let mut keys: Vec<PyStr> = Vec::new();
    for line in &key_lines {
        const HEAD: &str = "> **指摘キー**: ";
        let key = if text::starts_with(line, HEAD) && line.len() > HEAD.chars().count() {
            normalize_key(&line[HEAD.chars().count()..])
        } else {
            Vec::new()
        };
        if !key.is_empty() && !keys.contains(&key) {
            keys.push(key);
        }
    }
    let skip_lines: Vec<&&PyStr> = header
        .iter()
        .filter(|l| text::starts_with(l, "> **見送り**:"))
        .collect();
    let skipped: Vec<PyStr> = skip_lines.iter().filter_map(|l| match_skip(l)).collect();
    let malformed_skips = skip_lines
        .iter()
        .filter(|l| match_skip(l).is_none())
        .count();
    let unattended = header
        .iter()
        .any(|l| text::starts_with(l, "> **無人実行**:"));
    let record = lines
        .iter()
        .enumerate()
        .any(|(i, l)| !fenced[i] && text::starts_with(l, "## 追加修正記録"));
    let checkboxes = lines
        .iter()
        .enumerate()
        .filter(|(_, l)| is_checkbox(l))
        .map(|(i, _)| i + 1)
        .collect();
    Parsed {
        source_lines: source_lines.len(),
        sources,
        key_lines: key_lines.len(),
        keys,
        skipped,
        malformed_skips,
        unattended,
        record,
        checkboxes,
    }
}

fn problem(code: &str, detail: &str) -> PyJson {
    PyJson::object(vec![
        ("code", PyJson::str(code)),
        ("detail", PyJson::str(detail)),
    ])
}

fn malformed_problem(count: usize) -> PyJson {
    problem(
        "skipped-malformed",
        &format!(
            "見送りの書式違いの行が {count} 行ある(`> **見送り**: YYYY-MM-DD — <理由>`。区切りは em ダッシュ `—` だけ)"
        ),
    )
}

/// `check_text`。戻り値は (終了値, ペイロード)。
pub fn check_text(body: &[u32], expected: Option<&[u32]>) -> (i32, PyJson) {
    let parsed = parse(body);
    let mut problems: Vec<PyJson> = Vec::new();
    let source = (parsed.source_lines == 1 && parsed.sources.len() == 1).then(|| parsed.sources[0]);
    match source {
        None => problems.push(problem(
            "source-count",
            &format!(
                "ヘッダの発見元の行が {} 行(照合パターンに合うもの {} 行)。照合パターンに合う行がちょうど 1 行必要",
                parsed.source_lines,
                parsed.sources.len()
            ),
        )),
        Some(found) => {
            if let Some(want) = expected
                && chars::from_str(found) != want
            {
                let mut detail = chars::from_str(&format!("発見元が {found}(期待は "));
                detail.extend_from_slice(want);
                detail.push(u32::from(b')'));
                problems.push(PyJson::Object(vec![
                    (chars::from_str("code"), PyJson::str("source-mismatch")),
                    (chars::from_str("detail"), PyJson::String(detail)),
                ]));
            }
        }
    }
    if parsed.key_lines != 1 || parsed.keys.len() != 1 {
        problems.push(problem(
            "key-count",
            &format!(
                "ヘッダの指摘キーの行が {} 行(値のあるもの {} 個)。ちょうど 1 行が必要",
                parsed.key_lines,
                parsed.keys.len()
            ),
        ));
    }
    if parsed.unattended {
        problems.push(problem(
            "unattended-meta",
            "ヘッダに無人実行のメタ行がある(候補には書かない)",
        ));
    }
    if !parsed.skipped.is_empty() {
        problems.push(problem("skipped", "ヘッダに見送りの行がある"));
    }
    if parsed.malformed_skips > 0 {
        problems.push(malformed_problem(parsed.malformed_skips));
    }
    if parsed.record {
        problems.push(problem(
            "record-section",
            "`## 追加修正記録` の見出しがある(候補には書かない)",
        ));
    }
    if !parsed.checkboxes.is_empty() {
        let lines: Vec<String> = parsed.checkboxes.iter().map(usize::to_string).collect();
        problems.push(problem(
            "checkbox",
            &format!(
                "チェックボックス形の行がある(フェンスの中も数える。行 {})",
                lines.join(", ")
            ),
        ));
    }
    let ok = problems.is_empty();
    let payload = PyJson::object(vec![
        ("ok", PyJson::Bool(ok)),
        ("source", source.map_or(PyJson::Null, PyJson::str)),
        (
            "keys",
            PyJson::Array(parsed.keys.into_iter().map(PyJson::String).collect()),
        ),
        ("problems", PyJson::Array(problems)),
    ]);
    (i32::from(!ok), payload)
}

// ---------------------------------------------------------------- 入出力

fn path_repr(path: &[u8]) -> String {
    chars::repr(&chars::from_os_bytes(path))
        .iter()
        .map(|&c| char::from_u32(c).unwrap_or('?'))
        .collect()
}

/// `str(OSError)`(ファイル名付き)。
fn os_message(error: &std::io::Error, path: &[u8]) -> PyStr {
    let mut out = match error.raw_os_error() {
        Some(errno) => chars::from_str(&format!("[Errno {errno}] {}", exception::strerror(errno))),
        None => chars::from_str("I/O error"),
    };
    out.extend(chars::from_str(": "));
    out.extend(chars::from_str(&path_repr(path)));
    out
}

/// `pathlib.PurePosixPath` の字面の正規化(連続スラッシュと `.` の除去・先頭 `//` の保持)。
pub fn pathlib_normalize(path: &[u32]) -> PyStr {
    let leading = path.iter().take_while(|&&c| c == 47).count();
    let root: PyStr = match leading {
        0 => vec![],
        2 => vec![47, 47],
        _ => vec![47],
    };
    let parts: Vec<&[u32]> = path
        .split(|&c| c == 47)
        .filter(|p| !p.is_empty() && **p != [46])
        .collect();
    if root.is_empty() && parts.is_empty() {
        return vec![46];
    }
    let mut out = root;
    for (i, part) in parts.iter().enumerate() {
        if i > 0 {
            out.push(47);
        }
        out.extend_from_slice(part);
    }
    out
}

fn os_bytes(path: &[u32]) -> Vec<u8> {
    chars::to_os_bytes(path).unwrap_or_default()
}

/// pathlib の `_IGNORED_ERROS`(ENOENT・ENOTDIR・EBADF・ELOOP)。値は OS ごとの定数。
fn ignorable(errno: Option<i32>) -> bool {
    matches!(
        errno,
        Some(exception::ENOENT | exception::ENOTDIR | exception::EBADF | exception::ELOOP)
    )
}

#[derive(Clone, Copy)]
enum PathCheck {
    Exists,
    IsSymlink,
    IsDir,
}

/// `Path.exists()`・`is_symlink()`・`is_dir()`。無視する errno 以外は、pathlib の呼出履歴つきの例外にする。
fn path_check(kind: PathCheck, path: &[u32], caller: &[Frame]) -> Result<bool, PyException> {
    let bytes = os_bytes(path);
    let os = std::ffi::OsStr::from_bytes(&bytes);
    let result = match kind {
        PathCheck::IsSymlink => std::fs::symlink_metadata(os).map(|m| m.file_type().is_symlink()),
        PathCheck::IsDir => std::fs::metadata(os).map(|m| m.is_dir()),
        PathCheck::Exists => std::fs::metadata(os).map(|_| true),
    };
    match result {
        Ok(value) => Ok(value),
        Err(error) if ignorable(error.raw_os_error()) => Ok(false),
        Err(error) => {
            let mut frames = caller.to_vec();
            match kind {
                PathCheck::Exists => frames.extend([PATHLIB_EXISTS, PATHLIB_STAT]),
                PathCheck::IsDir => frames.extend([PATHLIB_IS_DIR, PATHLIB_STAT]),
                PathCheck::IsSymlink => {
                    frames.extend([PATHLIB_IS_SYMLINK, PATHLIB_LSTAT, PATHLIB_STAT]);
                }
            }
            let errno = error.raw_os_error().unwrap_or(0);
            let mut e = PyException::new(exception::os_error_kind(errno), "", &frames);
            e.message = os_message(&error, &bytes);
            e.repr_args = None;
            Err(e)
        }
    }
}

struct Entry {
    name: PyStr,
    path: PyStr,
}

/// `collect()`。戻り値は (終了値, ペイロード)。stderr の診断はここで出す。
fn collect(program: &mut Program, task_dir: &PyStr) -> Result<(i32, PyJson), PyException> {
    let empty = |code| {
        (
            code,
            PyJson::object(vec![
                ("files", PyJson::Array(vec![])),
                ("names", PyJson::Array(vec![])),
            ]),
        )
    };
    let caller = [MODULE, MAIN_COLLECT, COLLECT_EXISTS];
    if !path_check(PathCheck::Exists, task_dir, &caller)?
        && !path_check(PathCheck::IsSymlink, task_dir, &caller)?
    {
        return Ok(empty(0));
    }
    let dir_bytes = os_bytes(task_dir);
    let listing = std::fs::read_dir(std::ffi::OsStr::from_bytes(&dir_bytes))
        .and_then(|it| it.collect::<Result<Vec<_>, _>>());
    let entries = match listing {
        Ok(entries) => entries,
        Err(error) => {
            let mut message = chars::from_str("収集できない: ディレクトリを読めない: ");
            message.extend(os_message(&error, &dir_bytes));
            // print が失敗したら、捕まえた OSError(`collect` の `os.scandir`)が文脈として残る。
            let errno = error.raw_os_error().unwrap_or(0);
            let mut handled =
                PyException::new(exception::os_error_kind(errno), "", &[COLLECT_SCANDIR]);
            handled.message = os_message(&error, &dir_bytes);
            handled.repr_args = None;
            program
                .print_stderr(&message, &[MODULE, MAIN_COLLECT, COLLECT_PRINT])
                .map_err(|mut failure| {
                    failure.context = Some(Box::new(handled));
                    failure
                })?;
            return Ok(empty(1));
        }
    };
    let mut named: Vec<Entry> = entries
        .iter()
        .map(|entry| {
            let name = chars::from_os_bytes(entry.file_name().as_bytes());
            let mut path = task_dir.clone();
            if path.last() != Some(&47) {
                path.push(47);
            }
            path.extend_from_slice(&name);
            (entry, name, path)
        })
        .map(|(_, name, path)| Entry { name, path })
        .collect();
    let types: Vec<_> = entries.iter().map(|e| e.file_type()).collect();
    let mut indexed: Vec<(usize, Entry)> = named.drain(..).enumerate().collect();
    indexed.sort_by(|a, b| a.1.name.cmp(&b.1.name));
    let mut files: Vec<PyJson> = Vec::new();
    let mut names: Vec<PyStr> = Vec::new();
    let mut code = 0;
    for (index, entry) in indexed {
        let Ok(kind) = &types[index] else { continue };
        if kind.is_symlink() || !kind.is_file() {
            continue;
        }
        if !crate::commands::resolve_task_dir::state_file(&os_bytes(&entry.name)) {
            continue;
        }
        let underscore = entry
            .name
            .iter()
            .position(|&c| c == 95)
            .expect("a state name has `_`");
        let state = entry.name[..underscore].to_vec();
        let rest = &entry.name[underscore + 1..];
        let name = rest[..rest.len() - 3].to_vec();
        if !names.contains(&name) {
            names.push(name.clone());
        }
        let mut item_keys = PyJson::Array(vec![]);
        let mut skipped = PyJson::Null;
        let mut problems: Vec<PyJson> = Vec::new();
        let path_bytes = os_bytes(&entry.path);
        let read = crate::commands::r4a_rest_support::helper::fs_read(&path_bytes);
        if let Err(error) = &read
            && error.raw_os_error() == Some(4)
        {
            return Err(crate::commands::r4a_rest_support::stream::keyboard_interrupt(&caller));
        }
        let decoded = read
            .map_err(|e| os_message(&e, &path_bytes))
            .and_then(|bytes| {
                text::decode_strict(&bytes).map_err(|e| chars::from_str(&e.message()))
            });
        match decoded {
            Err(message) => {
                let mut detail = chars::from_str("読めない: ");
                detail.extend(message);
                problems.push(PyJson::Object(vec![
                    (chars::from_str("code"), PyJson::str("unreadable")),
                    (chars::from_str("detail"), PyJson::String(detail)),
                ]));
                code = 1;
            }
            Ok(body) => {
                let parsed = parse(&body);
                item_keys = PyJson::Array(parsed.keys.into_iter().map(PyJson::String).collect());
                if let Some(first) = parsed.skipped.into_iter().next() {
                    skipped = PyJson::String(first);
                }
                if parsed.malformed_skips > 0 {
                    problems.push(malformed_problem(parsed.malformed_skips));
                }
            }
        }
        files.push(PyJson::Object(vec![
            (chars::from_str("file"), PyJson::String(entry.name.clone())),
            (chars::from_str("keys"), item_keys),
            (chars::from_str("name"), PyJson::String(name)),
            (chars::from_str("problems"), PyJson::Array(problems)),
            (chars::from_str("skipped"), skipped),
            (chars::from_str("state"), PyJson::String(state)),
        ]));
    }
    names.sort();
    Ok((
        code,
        PyJson::object(vec![
            ("files", PyJson::Array(files)),
            (
                "names",
                PyJson::Array(names.into_iter().map(PyJson::String).collect()),
            ),
        ]),
    ))
}

/// `check()`。読めない場合は `unreadable` の問題にする。
fn check(
    program: &mut Program,
    path: &PyStr,
    expected: Option<&[u32]>,
) -> Result<(i32, PyJson), PyException> {
    let unreadable = |message: PyStr| {
        let mut detail = chars::from_str("読めない: ");
        detail.extend(message);
        let problem = PyJson::Object(vec![
            (chars::from_str("code"), PyJson::str("unreadable")),
            (chars::from_str("detail"), PyJson::String(detail)),
        ]);
        (
            1,
            PyJson::object(vec![
                ("ok", PyJson::Bool(false)),
                ("source", PyJson::Null),
                ("keys", PyJson::Array(vec![])),
                ("problems", PyJson::Array(vec![problem])),
            ]),
        )
    };
    let data = if *path == [45] {
        match program.read_stdin() {
            StdinRead::Bytes(bytes) => bytes,
            StdinRead::Closed => {
                return Err(PyException::new(
                    "AttributeError",
                    "'NoneType' object has no attribute 'buffer'",
                    &[MODULE, MAIN_CHECK, CHECK_READ, READ_STDIN],
                ));
            }
            StdinRead::NoData => {
                return Err(PyException::new(
                    "AttributeError",
                    "'NoneType' object has no attribute 'decode'",
                    &[MODULE, MAIN_CHECK, CHECK_READ, READ_DECODE],
                ));
            }
            StdinRead::Interrupted => {
                return Err(
                    crate::commands::r4a_rest_support::stream::keyboard_interrupt(&[
                        MODULE, MAIN_CHECK, CHECK_READ, READ_STDIN,
                    ]),
                );
            }
            StdinRead::Failed(error) => {
                return Ok(unreadable(match error.raw_os_error() {
                    Some(errno) => {
                        chars::from_str(&format!("[Errno {errno}] {}", exception::strerror(errno)))
                    }
                    None => chars::from_str("I/O error"),
                }));
            }
        }
    } else {
        // `Path(path).read_bytes()`: 字面は pathlib で正規化される。
        let normalized = pathlib_normalize(path);
        let bytes = os_bytes(&normalized);
        match crate::commands::r4a_rest_support::helper::fs_read(&bytes) {
            Ok(data) => data,
            Err(error) if error.raw_os_error() == Some(4) => {
                return Err(
                    crate::commands::r4a_rest_support::stream::keyboard_interrupt(&[
                        MODULE, MAIN_CHECK, CHECK_READ,
                    ]),
                );
            }
            Err(error) => return Ok(unreadable(os_message(&error, &bytes))),
        }
    };
    match text::decode_strict(&data) {
        Ok(body) => Ok(check_text(&body, expected)),
        Err(error) => Ok(unreadable(chars::from_str(&error.message()))),
    }
}

fn print_usage(program: &mut Program, message: &[u32], site: Frame) -> Result<i32, Raised> {
    program
        .print_stderr(message, &[MODULE, site])
        .map_err(Raised::Exception)?;
    Ok(2)
}

/// `main()`。
pub fn main(program: &mut Program, env: &Env, argv: &[PyStr]) -> Result<i32, Raised> {
    let namespace = match parser().parse(env, argv) {
        Ok(namespace) => namespace,
        Err(exit) => return Err(driver::handle_exit(program, exit, &[MODULE, MAIN_PARSE])),
    };
    let check_mode = matches!(args::get(&namespace, "check"), Value::Bool(true));
    let task_dir = args::get(&namespace, "task_dir").clone();
    let source = args::get(&namespace, "source").clone();
    let path = args::get(&namespace, "path").clone();
    let payload = if check_mode {
        if task_dir != Value::None || path == Value::None {
            return print_usage(
                program,
                &chars::from_str(
                    "使い方: candidate-keys.py --check [--source=<発見元>] <パス | ->",
                ),
                MAIN_USAGE_CHECK,
            );
        }
        let expected: Option<&[u32]> = match &source {
            Value::None => None,
            Value::Str(s) if SOURCES.iter().any(|n| chars::from_str(n) == *s) => Some(s),
            _ => {
                return print_usage(
                    program,
                    &chars::from_str(&format!(
                        "使い方: --source は {} のどれか",
                        SOURCES.join(" / ")
                    )),
                    MAIN_USAGE_SOURCE,
                );
            }
        };
        let Value::Str(path) = &path else {
            unreachable!("checked above")
        };
        check(program, path, expected).map_err(Raised::Exception)?
    } else {
        if task_dir == Value::None || path != Value::None || source != Value::None {
            return print_usage(
                program,
                &chars::from_str("使い方: candidate-keys.py --task-dir=<ディレクトリ>"),
                MAIN_USAGE_DIR,
            );
        }
        let raw = match &task_dir {
            Value::Str(s) => s.clone(),
            other => {
                let kind = match other {
                    Value::List(_) => "list",
                    _ => "NoneType",
                };
                return Err(Raised::Exception(PyException::new(
                    "TypeError",
                    &format!("expected str, bytes or os.PathLike object, not {kind}"),
                    &[
                        MODULE,
                        MAIN_PATH,
                        PATHLIB_NEW,
                        PATHLIB_FROM_PARTS,
                        PATHLIB_PARSE_ARGS,
                    ],
                )));
            }
        };
        let normalized = pathlib_normalize(&raw);
        let caller = [MODULE, MAIN_EXISTS];
        // `(exists() or is_symlink()) and not is_dir()`: 短絡評価の順で、無視する errno 以外は例外。
        let present = path_check(PathCheck::Exists, &normalized, &caller)
            .map_err(Raised::Exception)?
            || path_check(PathCheck::IsSymlink, &normalized, &caller).map_err(Raised::Exception)?;
        if present
            && !path_check(PathCheck::IsDir, &normalized, &caller).map_err(Raised::Exception)?
        {
            let mut message = chars::from_str("使い方: --task-dir がディレクトリでない: ");
            message.extend_from_slice(&raw);
            return print_usage(program, &message, MAIN_NOT_DIR);
        }
        collect(program, &normalized).map_err(Raised::Exception)?
    };
    let (code, document) = payload;
    program
        .print_stdout(&json::dumps(&document, true), &[MODULE, MAIN_PRINT])
        .map_err(Raised::Exception)?;
    Ok(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(text: &str) -> PyStr {
        chars::from_str(text)
    }

    #[test]
    fn keys_are_nfc_and_space_normalized() {
        assert_eq!(normalize_key(&s("  a\u{301}\t\u{3000}b  ")), s("\u{e1} b"));
    }

    #[test]
    fn pathlib_normalization() {
        assert_eq!(pathlib_normalize(&s("")), s("."));
        assert_eq!(pathlib_normalize(&s("a//b/./c/")), s("a/b/c"));
        assert_eq!(pathlib_normalize(&s("//x")), s("//x"));
        assert_eq!(pathlib_normalize(&s("///x")), s("/x"));
        assert_eq!(pathlib_normalize(&s("./")), s("."));
        assert_eq!(pathlib_normalize(&s("../a")), s("../a"));
    }

    #[test]
    fn check_reports_every_problem_in_order() {
        let body = s("> **発見元**: nope\n> **見送り**: bad\n```\n- [ ] x\n```\n## 追加修正記録\n");
        let (code, _) = check_text(&body, None);
        assert_eq!(code, 1);
    }
}
