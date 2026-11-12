//! `loop-task-text.py` の移植。親が保持した本文を `-c` で動かす 3 コマンド。
//!
//! フェンスの定義は `task_digest` のものを使う(複製しない)。

use crate::commands::r4a_rest_support::chars::{self, PyStr};
use crate::commands::r4a_rest_support::exception::{Frame, PyException};
use crate::commands::r4a_rest_support::helper::{self, Read, STRING_FILE};
use crate::commands::r4a_rest_support::stream::{Program, Raised};
use crate::commands::r4a_rest_support::text;
use crate::commands::task_digest::normalize::FencePolicy;

const fn frame(line: u32, func: &'static str) -> Frame {
    Frame {
        file: STRING_FILE,
        line,
        func,
        source: None,
    }
}
const MODULE_CALL: Frame = frame(99, "<module>");
const MODULE_PRINT: Frame = frame(103, "<module>");
const READ_TEXT: Frame = frame(28, "read_text");

fn digit(c: u32) -> bool {
    (48..=57).contains(&c)
}

/// `^> \*\*作成日\*\*: ([0-9]{4}-[0-9]{2}-[0-9]{2})` の先頭一致。
fn created(line: &[u32]) -> Option<PyStr> {
    const HEAD: &str = "> **作成日**: ";
    if !text::starts_with(line, HEAD) {
        return None;
    }
    let rest = &line[HEAD.chars().count()..];
    let shape = [
        digit(*rest.first()?),
        digit(*rest.get(1)?),
        digit(*rest.get(2)?),
        digit(*rest.get(3)?),
        *rest.get(4)? == 45,
        digit(*rest.get(5)?),
        digit(*rest.get(6)?),
        *rest.get(7)? == 45,
        digit(*rest.get(8)?),
        digit(*rest.get(9)?),
    ];
    shape.iter().all(|ok| *ok).then(|| rest[..10].to_vec())
}

fn meta(line: &[u32]) -> bool {
    const HEAD: &str = "> **無人実行**: 可";
    text::starts_with(line, HEAD)
        && line[HEAD.chars().count()..]
            .iter()
            .all(|c| matches!(c, 32 | 9 | 13 | 11 | 12))
}

/// `^- \*\*保留\*\*\(ship-task・日付\): ` の先頭一致。一致したら日付の後の位置を返す。
fn hold_end(line: &[u32]) -> Option<usize> {
    const HEAD: &str = "- **保留**(ship-task・";
    if !text::starts_with(line, HEAD) {
        return None;
    }
    let rest = &line[HEAD.chars().count()..];
    let ok = rest.len() >= 10 + 3
        && rest[..4].iter().all(|&c| digit(c))
        && rest[4] == 45
        && rest[5..7].iter().all(|&c| digit(c))
        && rest[7] == 45
        && rest[8..10].iter().all(|&c| digit(c))
        && text::starts_with(&rest[10..], "): ");
    ok.then(|| HEAD.chars().count() + 13)
}

fn hold_code(line: &[u32]) -> Option<PyStr> {
    let rest = &line[hold_end(line)?..];
    let first = *rest.first()?;
    if !matches!(u8::try_from(first), Ok(b'S' | b'D' | b'U' | b'G')) {
        return None;
    }
    let digits = rest[1..].iter().take_while(|&&c| digit(c)).count();
    (digits > 0).then(|| rest[..1 + digits].to_vec())
}

struct Document {
    lines: Vec<PyStr>,
    fenced: Vec<bool>,
}

fn document(body: &[u32]) -> Document {
    let lines = text::script_lines(body);
    let strings: Vec<String> = lines.iter().map(|l| text::to_string(l)).collect();
    let refs: Vec<&str> = strings.iter().map(String::as_str).collect();
    let fenced = crate::commands::task_digest::normalize::fence_flags(&refs, FencePolicy::Loose)
        .expect("the loose policy never fails");
    Document { lines, fenced }
}

impl Document {
    fn headings(&self) -> Vec<usize> {
        (0..self.lines.len())
            .filter(|&i| !self.fenced[i] && text::starts_with(&self.lines[i], "## "))
            .collect()
    }

    fn header(&self) -> Vec<&PyStr> {
        let end = self.headings().first().copied().unwrap_or(self.lines.len());
        (0..end)
            .filter(|&i| !self.fenced[i])
            .map(|i| &self.lines[i])
            .collect()
    }

    /// 追加修正記録の節の、フェンスの外の行。
    fn record_lines(&self) -> Vec<&PyStr> {
        let headings = self.headings();
        let mut out = Vec::new();
        for &start in headings
            .iter()
            .filter(|&&i| text::eq_str(&self.lines[i], "## 追加修正記録"))
        {
            let end = headings
                .iter()
                .copied()
                .find(|&i| i > start)
                .unwrap_or(self.lines.len());
            out.extend(
                (start + 1..end)
                    .filter(|&i| !self.fenced[i])
                    .map(|i| &self.lines[i]),
            );
        }
        out
    }
}

fn read_text(
    program: &mut Program,
    path: &PyStr,
    command: &'static str,
    call: Frame,
) -> Result<Document, PyException> {
    match helper::read_source(program, path, &[MODULE_CALL, call, READ_TEXT]) {
        Read::Bytes(bytes) => Ok(document(&chars::from_utf8_replace(&bytes))),
        Read::Error(error) => {
            let _ = command;
            Err(error)
        }
    }
}

fn run(program: &mut Program, argv: &[PyStr]) -> Result<(), PyException> {
    let Some(command) = argv.first() else {
        return Err(PyException::new(
            "IndexError",
            "list index out of range",
            &[MODULE_CALL],
        ));
    };
    let name: Option<(&'static str, Frame)> = if text::eq_str(command, "taskinfo") {
        Some(("cmd_taskinfo", frame(75, "cmd_taskinfo")))
    } else if text::eq_str(command, "holdcount") {
        Some(("cmd_holdcount", frame(84, "cmd_holdcount")))
    } else if text::eq_str(command, "holdcode") {
        Some(("cmd_holdcode", frame(90, "cmd_holdcode")))
    } else {
        None
    };
    let Some((function, call)) = name else {
        let mut e = PyException::new("KeyError", "", &[MODULE_CALL]);
        e.message = command.clone();
        return Err(e);
    };
    let given = argv.len() - 1;
    if given != 1 {
        return Err(helper::arity_error(function, 1, given, &[MODULE_CALL]));
    }
    let doc = read_text(program, &argv[1], function, call)?;
    let out = |program: &mut Program, body: PyStr, line: u32, func: &'static str| {
        program.print_stdout(&body, &[MODULE_CALL, frame(line, func)])
    };
    match function {
        "cmd_taskinfo" => {
            let head = doc.header();
            let has_meta = head.iter().any(|l| meta(l));
            let date = head.iter().find_map(|l| created(l)).unwrap_or_default();
            out(
                program,
                chars::from_str(&format!("meta={}", i32::from(has_meta))),
                79,
                function,
            )?;
            let mut line = chars::from_str("date=");
            line.extend(date);
            out(program, line, 80, function)
        }
        "cmd_holdcount" => {
            let count = doc
                .record_lines()
                .iter()
                .filter(|l| hold_end(l).is_some())
                .count();
            out(program, chars::from_str(&count.to_string()), 84, function)
        }
        _ => {
            let records = doc.record_lines();
            let holds: Vec<&&PyStr> = records.iter().filter(|l| hold_end(l).is_some()).collect();
            let code = holds.last().and_then(|l| hold_code(l)).unwrap_or_default();
            out(program, code, 92, function)
        }
    }
}

/// `argv` は `-c` の引数(`sys.argv[1:]`)。
pub fn main(program: &mut Program, argv: &[PyStr]) -> Result<i32, Raised> {
    match run(program, argv) {
        Ok(()) => Ok(0),
        Err(exception) => helper::helper_failure(program, exception, MODULE_PRINT),
    }
}
