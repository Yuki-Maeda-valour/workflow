//! `loop-child-output.py` の移植。親が保持した本文を `-c` で動かす 3 コマンド
//! (`result`・`permlog`・`origin-json`)。
//!
//! permlog の OS 操作は platform の `r4a_rest::read_permlog` を使う。量の上限は `Budget` で数える。

use crate::commands::r4a_platform::{PermlogError, PermlogHooks, read_permlog};
use crate::commands::r4a_rest_support::chars::{self, PyStr};
use crate::commands::r4a_rest_support::exception::{self, Frame, PyException};
use crate::commands::r4a_rest_support::helper::{self, STRING_FILE, one_line, string_frame};
use crate::commands::r4a_rest_support::json::{self, JsonFailure, JsonPolicy, PyJson};
use crate::commands::r4a_rest_support::stream::{Program, Raised, StdinRead};
use crate::commands::r4a_rest_support::text;
use dev_workflow_core::budget::{Budget, BudgetLimits};
use std::os::unix::ffi::OsStrExt;

const MODULE_CALL: Frame = string_frame(138, "<module>");
const MODULE_PRINT: Frame = string_frame(142, "<module>");

/// 呼出履歴の深さ(`<module>`→`cmd_*`→`loads`→`decode`→`raw_decode`)は store と同じ 5 つ。
pub const JSON_DEPTH: usize = 994;

fn frame(line: u32, func: &'static str) -> Frame {
    Frame {
        file: STRING_FILE,
        line,
        func,
        source: None,
    }
}

enum Flow {
    Done,
    /// `sys.exit(code)`。
    Exit(i32),
}

fn loads(text: &[u32], int_max_digits: usize) -> Result<PyJson, JsonFailure> {
    json::loads_text(
        text,
        JsonPolicy {
            int_max_digits,
            depth: JSON_DEPTH,
        },
    )
}

fn get<'a>(entries: &'a [(PyStr, PyJson)], key: &str) -> Option<&'a PyJson> {
    let key = chars::from_str(key);
    entries.iter().find(|(k, _)| *k == key).map(|(_, v)| v)
}

fn say(program: &mut Program, body: PyStr, site: Frame) -> Result<(), PyException> {
    program.print_stdout(&body, &[MODULE_CALL, site])
}

fn cat(prefix: &str, rest: &[u32]) -> PyStr {
    let mut out = chars::from_str(prefix);
    out.extend_from_slice(rest);
    out
}

// ---------------------------------------------------------------- result

const OUTCOMES: [&str; 5] = ["PR", "縮退", "保留", "失敗扱い", "候補なし"];

/// `^無人のイテレーションの結果: (PR|縮退|保留|失敗扱い|候補なし) — (.*)$`。
fn outcome(line: &[u32]) -> Option<(&'static str, PyStr)> {
    const HEAD: &str = "無人のイテレーションの結果: ";
    let mut end = line.len();
    while end > 0 && line[end - 1] == 13 {
        end -= 1;
    }
    let line = &line[..end];
    if !text::starts_with(line, HEAD) {
        return None;
    }
    let rest = &line[HEAD.chars().count()..];
    for name in OUTCOMES {
        if text::starts_with(rest, name) {
            let tail = &rest[name.chars().count()..];
            if text::starts_with(tail, " — ") {
                return Some((name, tail[3..].to_vec()));
            }
        }
    }
    None
}

fn cmd_result(program: &mut Program, path: &PyStr, digits: usize) -> Result<Flow, PyException> {
    let read_frames = [MODULE_CALL, frame(18, "cmd_result")];
    let bytes = helper::read_file(path, &read_frames)?;
    let raw = chars::from_utf8_replace(&bytes);
    let parsed = match loads(&raw, digits) {
        Ok(value) => value,
        Err(failure) if failure.is_value_error() => {
            say(
                program,
                chars::from_str("json=bad"),
                frame(22, "cmd_result"),
            )?;
            return Ok(Flow::Done);
        }
        Err(failure) => {
            return Err(crate::commands::r4a_rest_support::driver::json_exception(
                &failure,
                &[MODULE_CALL, frame(20, "cmd_result")],
            ));
        }
    };
    let is_result = |e: &PyJson| {
        matches!(e, PyJson::Object(entries)
            if matches!(get(entries, "type"), Some(PyJson::String(t)) if *t == chars::from_str("result")))
    };
    let results: Vec<&PyJson> = match &parsed {
        PyJson::Array(items) => items.iter().filter(|e| is_result(e)).collect(),
        other => vec![other],
    };
    let chosen: Option<&PyJson> = match &parsed {
        PyJson::Array(_) => results.last().copied(),
        other => Some(other),
    };
    let Some(PyJson::Object(object)) = chosen else {
        say(
            program,
            chars::from_str("json=bad"),
            frame(33, "cmd_result"),
        )?;
        return Ok(Flow::Done);
    };
    say(program, chars::from_str("json=ok"), frame(35, "cmd_result"))?;
    let body: PyStr = match get(object, "result") {
        Some(PyJson::String(s)) => s.clone(),
        _ => Vec::new(),
    };
    let mut last: Option<(&'static str, PyStr)> = None;
    for line in body.split(|&c| c == 10) {
        if let Some(found) = outcome(line) {
            last = Some(found);
        }
    }
    if let Some((name, detail)) = last {
        say(
            program,
            chars::from_str(&format!("outcome={name}")),
            frame(43, "cmd_result"),
        )?;
        say(
            program,
            cat("detail=", &one_line(&detail, 500)),
            frame(44, "cmd_result"),
        )?;
    }
    // 拒否の鍵("denial" を含む鍵)ごとに、値を要素の順に集める。
    let mut collected: Vec<(&PyStr, Vec<&PyJson>)> = Vec::new();
    for element in &results {
        let PyJson::Object(entries) = element else {
            // 辞書でない要素は `.items()` が無く AttributeError(単一の値のとき)。
            return Err(PyException::new(
                "AttributeError",
                &format!("'{}' object has no attribute 'items'", type_name(element)),
                &[MODULE_CALL, frame(54, "cmd_result")],
            ));
        };
        for (key, value) in entries {
            if has_denial(key) {
                match collected.iter_mut().find(|(k, _)| *k == key) {
                    Some((_, values)) => values.push(value),
                    None => collected.push((key, vec![value])),
                }
            }
        }
    }
    let mut denials: Vec<(PyStr, PyJson)> = Vec::new();
    for (key, values) in collected {
        let merged = if values.len() == 1 {
            values[0].clone()
        } else if values.iter().all(|v| matches!(v, PyJson::Array(_))) {
            PyJson::Array(
                values
                    .iter()
                    .flat_map(|v| match v {
                        PyJson::Array(items) => items.clone(),
                        _ => unreachable!(),
                    })
                    .collect(),
            )
        } else {
            PyJson::Array(values.into_iter().cloned().collect())
        };
        denials.push((key.clone(), merged));
    }
    if !denials.is_empty() {
        let dumped = json::dumps(&PyJson::Object(denials), false);
        say(
            program,
            cat("denials=", &one_line(&dumped, 2000)),
            frame(61, "cmd_result"),
        )?;
    }
    Ok(Flow::Done)
}

fn type_name(value: &PyJson) -> &'static str {
    match value {
        PyJson::Null => "NoneType",
        PyJson::Bool(_) => "bool",
        PyJson::Int { .. } => "int",
        PyJson::Float(_) => "float",
        PyJson::String(_) => "str",
        PyJson::Array(_) => "list",
        PyJson::Object(_) => "dict",
    }
}

/// `"denial" in str(k).lower()`。"denial" の各文字は ASCII なので、ASCII の小文字化で足りる。
fn has_denial(key: &[u32]) -> bool {
    let lowered: Vec<u32> = key
        .iter()
        .map(|&c| if (65..=90).contains(&c) { c + 32 } else { c })
        .collect();
    let needle = chars::from_str("denial");
    lowered
        .windows(needle.len())
        .any(|w| w == needle.as_slice())
}

// ---------------------------------------------------------------- permlog

/// `os.path.abspath(path).split('/')[1:]`。
fn abspath_components(path: &[u8]) -> Result<Vec<Vec<u8>>, PyException> {
    let mut joined: Vec<u8> = Vec::new();
    if path.first() == Some(&b'/') {
        joined.extend_from_slice(path);
    } else {
        let cwd = std::env::current_dir().map_err(|e| helper::io_exception(&e, &[]))?;
        joined.extend_from_slice(cwd.as_os_str().as_bytes());
        if joined.last() != Some(&b'/') {
            joined.push(b'/');
        }
        joined.extend_from_slice(path);
    }
    // posixpath.normpath
    let slashes = if joined.starts_with(b"//") && !joined.starts_with(b"///") {
        2
    } else {
        1
    };
    let mut comps: Vec<&[u8]> = Vec::new();
    for comp in joined.split(|b| *b == b'/') {
        if comp.is_empty() || comp == b"." {
            continue;
        }
        if comp != b".." || comps.last().is_some_and(|c| *c == b"..") {
            comps.push(comp);
        } else if !comps.is_empty() {
            comps.pop();
        }
    }
    let mut normalized = vec![b'/'; slashes];
    normalized.extend(comps.join(&b'/'));
    Ok(normalized
        .split(|b| *b == b'/')
        .skip(1)
        .map(<[u8]>::to_vec)
        .collect())
}

/// `str.splitlines()`。
fn splitlines(text: &[u32]) -> Vec<PyStr> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut at = 0;
    while at < text.len() {
        let c = text[at];
        if matches!(
            c,
            10 | 13 | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e | 0x85 | 0x2028 | 0x2029
        ) {
            lines.push(text[start..at].to_vec());
            if c == 13 && text.get(at + 1) == Some(&10) {
                at += 1;
            }
            start = at + 1;
        }
        at += 1;
    }
    if start < text.len() {
        lines.push(text[start..].to_vec());
    }
    lines
}

/// 参考表示の解析での失敗(`except (OSError, ValueError, UnicodeError)` が捕まるもの)。
fn permlog_issue(error: &PermlogError) -> PyStr {
    match error {
        PermlogError::Os { errno, name } => {
            let mut message =
                chars::from_str(&format!("[Errno {errno}] {}", exception::strerror(*errno)));
            if let Some(name) = name {
                message.extend(chars::from_str(": "));
                message.extend(chars::repr(&chars::from_os_bytes(name)));
            }
            message
        }
        PermlogError::NotRegularOrTooLarge => {
            chars::from_str("通常ファイルでないか読取上限を超える")
        }
        PermlogError::Truncated => chars::from_str("読取中に切り詰められた"),
        PermlogError::Changed => chars::from_str("読取中に変わった"),
    }
}

fn cmd_permlog(
    program: &mut Program<'_>,
    path: &PyStr,
    digits: usize,
    hooks: &mut dyn PermlogHooks,
) -> Result<Flow, PyException> {
    let mut allow = 0_usize;
    let mut deny = 0_usize;
    let mut kinds: Vec<(PyStr, usize)> = Vec::new();
    let mut lines: Vec<PyStr> = Vec::new();
    let mut issue: PyStr = Vec::new();
    let mut fatal: Option<PyException> = None;
    let path_bytes = chars::to_os_bytes(path).unwrap_or_default();
    let mut close_error = None;
    match abspath_components(&path_bytes) {
        // `os.getcwd()` の失敗の `str(exc)`(ファイル名なし)。errno は実際の値。
        Err(error) => issue = one_line(&error.message, 300),
        Ok(components) => {
            // 量は C0b の `Budget` で決める: 宣言サイズ 1 MiB 以下・行数 10000 以下(期限は使わない)。
            let mut budget = Budget::new(
                BudgetLimits {
                    items: 10_000,
                    total_bytes: u64::MAX,
                    file_bytes: 1_048_576,
                },
                std::time::Instant::now() + std::time::Duration::from_secs(365 * 24 * 3600),
            );
            let read = read_permlog(
                &components,
                &mut |size| budget.check_file_bytes(size).is_ok(),
                hooks,
            );
            close_error = read.close_error;
            let outcome: Result<(), PyStr> = match read.result {
                Err(error) => Err(permlog_issue(&error)),
                Ok(raw) => parse_permlog(
                    &raw,
                    digits,
                    &mut budget,
                    &mut allow,
                    &mut deny,
                    &mut kinds,
                    &mut lines,
                    &mut fatal,
                ),
            };
            if let Err(message) = outcome {
                issue = one_line(&message, 300);
            }
        }
    }
    if let Some(errno) = close_error {
        return Err(exception::os_error(
            errno,
            &[MODULE_CALL, frame(107, "cmd_permlog")],
        ));
    }
    if let Some(exception) = fatal {
        return Err(exception);
    }
    let site = |line| frame(line, "cmd_permlog");
    say(
        program,
        chars::from_str(&format!("allow={allow}")),
        site(109),
    )?;
    say(program, chars::from_str(&format!("deny={deny}")), site(110))?;
    kinds.sort_by(|a, b| a.0.cmp(&b.0));
    let mut kinds_line = chars::from_str("kinds=");
    for (n, (kind, count)) in kinds.iter().enumerate() {
        if n > 0 {
            kinds_line.push(u32::from(b','));
        }
        kinds_line.extend_from_slice(kind);
        kinds_line.extend(chars::from_str(&format!(":{count}")));
    }
    say(program, kinds_line, site(111))?;
    if !issue.is_empty() {
        say(program, cat("reference-error=", &issue), site(112))?;
    }
    for line in &lines {
        say(program, cat("line=", line), site(113))?;
    }
    Ok(Flow::Done)
}

#[allow(clippy::too_many_arguments)]
fn parse_permlog(
    raw: &[u8],
    digits: usize,
    budget: &mut Budget,
    allow: &mut usize,
    deny: &mut usize,
    kinds: &mut Vec<(PyStr, usize)>,
    lines: &mut Vec<PyStr>,
    fatal: &mut Option<PyException>,
) -> Result<(), PyStr> {
    if !raw.is_empty() && raw.last() != Some(&b'\n') {
        return Err(chars::from_str("末尾の行が途中で切れている"));
    }
    let decoded = text::decode_strict(raw).map_err(|e| chars::from_str(&e.message()))?;
    let records = splitlines(&decoded);
    if budget.add_items(records.len() as u64).is_err() {
        return Err(chars::from_str("行数上限を超える"));
    }
    for raw_line in &records {
        let entry = match loads(raw_line, digits) {
            Ok(value) => value,
            Err(failure) if failure.is_value_error() => {
                return Err(chars::from_str(&failure.message()));
            }
            Err(failure) => {
                *fatal = Some(crate::commands::r4a_rest_support::driver::json_exception(
                    &failure,
                    &[MODULE_CALL, frame(95, "cmd_permlog")],
                ));
                return Ok(());
            }
        };
        let PyJson::Object(object) = &entry else {
            return Err(chars::from_str("行の形式が違う"));
        };
        let decision = match get(object, "decision") {
            Some(PyJson::String(s)) if *s == chars::from_str("allow") => 0,
            Some(PyJson::String(s)) if *s == chars::from_str("deny") => 1,
            _ => return Err(chars::from_str("行の形式が違う")),
        };
        if decision == 0 {
            *allow += 1;
            continue;
        }
        *deny += 1;
        let kind = match get(object, "kind") {
            None => chars::from_str("other"),
            Some(PyJson::String(s)) => s.clone(),
            Some(_) => return Err(chars::from_str("kind が文字列でない")),
        };
        let kind = one_line(&kind, 100);
        match kinds.iter_mut().find(|(k, _)| *k == kind) {
            Some((_, count)) => *count += 1,
            None => kinds.push((kind.clone(), 1)),
        }
        if lines.len() < 100 {
            let field = |name: &str| {
                get(object, name).map_or_else(|| chars::from_str("None"), json::py_str)
            };
            let mut detail = field("tool_name");
            detail.extend(chars::from_str(" ["));
            detail.extend_from_slice(&kind);
            detail.extend(chars::from_str("] "));
            detail.extend(field("reason"));
            detail.extend(chars::from_str(" — "));
            detail.extend(field("subject"));
            lines.push(one_line(&detail, 600));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------- origin-json

fn cmd_origin_json(program: &mut Program, digits: usize) -> Result<Flow, PyException> {
    let frames = [MODULE_CALL, frame(119, "cmd_origin_json")];
    let bytes = match program.read_stdin() {
        StdinRead::Bytes(bytes) => bytes,
        StdinRead::Closed => {
            return Err(PyException::new(
                "AttributeError",
                "'NoneType' object has no attribute 'buffer'",
                &frames,
            ));
        }
        StdinRead::NoData => {
            return Err(PyException::new(
                "AttributeError",
                "'NoneType' object has no attribute 'decode'",
                &frames,
            ));
        }
        StdinRead::Interrupted => {
            return Err(crate::commands::r4a_rest_support::stream::keyboard_interrupt(&frames));
        }
        StdinRead::Failed(error) => return Err(helper::io_exception(&error, &frames)),
    };
    // 復号の失敗(UnicodeDecodeError)も ValueError として捕まる。
    let Ok(decoded) = text::decode_strict(&bytes) else {
        return Ok(Flow::Exit(1));
    };
    let data = match loads(&decoded, digits) {
        Ok(value) => value,
        Err(failure) if failure.is_value_error() => return Ok(Flow::Exit(1)),
        Err(failure) => {
            return Err(crate::commands::r4a_rest_support::driver::json_exception(
                &failure, &frames,
            ));
        }
    };
    let PyJson::Object(object) = &data else {
        return Ok(Flow::Exit(1));
    };
    let flag = |name: &str| match get(object, name) {
        Some(PyJson::Bool(b)) => Some(*b),
        _ => None,
    };
    let (Some(origin), Some(same), Some(vcs)) = (flag("origin"), flag("same"), flag("vcs")) else {
        return Ok(Flow::Exit(1));
    };
    let repo = match get(object, "repo") {
        None | Some(PyJson::Null) => None,
        Some(PyJson::String(s)) => Some(s.clone()),
        Some(_) => return Ok(Flow::Exit(1)),
    };
    let reason: PyStr = match get(object, "reason") {
        Some(PyJson::String(s)) => s.clone(),
        _ => Vec::new(),
    };
    let site = |line| frame(line, "cmd_origin_json");
    for (key, value) in [("origin", origin), ("same", same), ("vcs", vcs)] {
        say(
            program,
            chars::from_str(&format!("{key}={}", i32::from(value))),
            site(129),
        )?;
    }
    say(program, cat("reason=", &one_line(&reason, 300)), site(130))?;
    say(program, cat("repo=", &repo.unwrap_or_default()), site(131))?;
    Ok(Flow::Done)
}

// ---------------------------------------------------------------- 入口

fn run(
    program: &mut Program,
    argv: &[PyStr],
    digits: usize,
    hooks: &mut dyn PermlogHooks,
) -> Result<Flow, PyException> {
    let Some(command) = argv.first() else {
        return Err(PyException::new(
            "IndexError",
            "list index out of range",
            &[MODULE_CALL],
        ));
    };
    let (function, arity): (&str, usize) = if text::eq_str(command, "result") {
        ("cmd_result", 1)
    } else if text::eq_str(command, "permlog") {
        ("cmd_permlog", 1)
    } else if text::eq_str(command, "origin-json") {
        ("cmd_origin_json", 0)
    } else {
        let mut e = PyException::new("KeyError", "", &[MODULE_CALL]);
        e.message = command.clone();
        return Err(e);
    };
    let given = argv.len() - 1;
    if given != arity {
        return Err(helper::arity_error(function, arity, given, &[MODULE_CALL]));
    }
    match function {
        "cmd_result" => cmd_result(program, &argv[1], digits),
        "cmd_permlog" => cmd_permlog(program, &argv[1], digits, hooks),
        _ => cmd_origin_json(program, digits),
    }
}

/// `argv` は `-c` の引数(`sys.argv[1:]`)。`digits` は整数の文字列変換の上限。
pub fn main(
    program: &mut Program,
    argv: &[PyStr],
    digits: usize,
    hooks: &mut dyn PermlogHooks,
) -> Result<i32, Raised> {
    match run(program, argv, digits, hooks) {
        Ok(Flow::Done) => Ok(0),
        Ok(Flow::Exit(code)) => Err(Raised::Exit(code)),
        Err(exception) => helper::helper_failure(program, exception, MODULE_PRINT),
    }
}
