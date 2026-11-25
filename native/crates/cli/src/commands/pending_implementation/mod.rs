//! `pending-implementation.py`(旧版)のRust版。保護記録らしい候補を3か所の直下から列挙する。
//!
//! 分岐の正本は旧版のコード。引数の検査の順・OSの呼出しの順と回数・時計の確認点・例外の捕まえ方・
//! 結果のJSON・標準エラーの行・終了値を旧版と同じにする。子を起動しない(Gitも起動しない)。
//!
//! 製品の登録表にはまだ載せない。検査専用の入口(`examples/r4g_contract.rs`)だけが使う。

pub mod lines;
pub mod mark;
pub mod report;
pub mod scan;
pub mod system;

use super::r4g_support as support;
use report::{Acknowledged, Doc, Issue};
use support::python_io::PythonStdio;
use support::text;
use support::traceback::{self, Frame};
use system::{Exc, System};

pub const MAX_ENTRIES: usize = 4096;
pub const MAX_CANDIDATES: usize = 128;
pub const MAX_MARK: usize = 65536;
pub const MAX_POINTER: usize = 4096;
pub const MAX_DEPTH: usize = 128;
pub const ROOT_IDS: [&str; 3] = ["xdg_state", "home_state", "home_cache"];
pub const TEMP_DIRS: [&[u8]; 2] = [b"/tmp", b"/private/tmp"];

/// 旧版の`HELP`(`scan --help`の2語のときだけ標準出力へ出す)。
pub const HELP: &str = r#"使用方法:
  python3 pending-implementation.py scan --cwd <作業対象の管理ルート>
      [--acknowledged-list <64桁の一覧確認値>
       | --acknowledged-identities <64桁の実体確認値>]

保護記録らしい候補を3か所の直下から列挙します。候補の内部は読みません。
記録の外の所属の印を読み、別のプロジェクトの記録と確かめられた候補では止めません。
印が無い・壊れている・今回の作業と重なる・確かめられない候補は、人の確認が必要です。
確認値は「確認が要る」候補だけで計算し、人が全候補を具体的に確認した場合だけ渡してください。
  --acknowledged-list: 一覧確認値(list_sha256)。更新時刻・大きさ・権限の変化でも変わります。
  --acknowledged-identities: 実体確認値(identity_sha256)。全候補が今回と無関係と
      人が答えた場合だけ使います。同じ実体の更新時刻・大きさ・権限の変化では変わりません。
      重なり(overlap・overlap-unknown)のある候補があれば、値が一致しても続行しません。
2つの確認値は同時に渡せません(終了値2)。
保護記録の真正性や、処理の停止を証明する値ではありません。
終了値: 0=候補なし(none)/別プロジェクトの記録だけ(other-projects-only)/確認値一致、
        10=人の確認が必要、11=列挙不能、2=引数または作業対象の不正、20=想定外の失敗。
上限: 置き場ごと4096件、合計128候補、走査全体5秒。
"#;

/// 旧版の`ScanError`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScanError {
    pub code: &'static str,
    pub message: String,
    pub root_id: Option<&'static str>,
    pub path: Option<Vec<u8>>,
    pub exit_code: u8,
}

fn usage(message: &str) -> Exc {
    Exc::Scan(ScanError {
        code: "invalid-arguments",
        message: message.to_string(),
        root_id: None,
        path: None,
        exit_code: 2,
    })
}

fn invalid_cwd(message: &str) -> Exc {
    Exc::Scan(ScanError {
        code: "invalid-cwd",
        message: message.to_string(),
        root_id: None,
        path: None,
        exit_code: 2,
    })
}

/// 旧版の`parse_arguments`。引数は`sys.argv[1:]`(UTF-8と`surrogateescape`で読んだ値のバイト列)。
pub fn parse_arguments(argv: &[Vec<u8>]) -> Result<(Vec<u8>, Acknowledged), Exc> {
    if argv.first().map(Vec::as_slice) != Some(b"scan".as_slice()) {
        return Err(usage("scanと必須の--cwdを指定してください。"));
    }
    let names: [&[u8]; 3] = [
        b"--cwd",
        b"--acknowledged-list",
        b"--acknowledged-identities",
    ];
    let mut values: Vec<(&[u8], Vec<u8>)> = Vec::new();
    let mut args = argv[1..].iter();
    while let Some(token) = args.next() {
        let (name, value) = match token.iter().position(|byte| *byte == b'=') {
            Some(index) => (&token[..index], Some(token[index + 1..].to_vec())),
            None => (token.as_slice(), None),
        };
        let Some(name) = names.iter().copied().find(|known| *known == name) else {
            return Err(usage("不明または重複した引数があります。"));
        };
        if values.iter().any(|(seen, _)| *seen == name) {
            return Err(usage("不明または重複した引数があります。"));
        }
        let value = match value {
            Some(value) => value,
            None => match args.next() {
                Some(next) if !next.starts_with(b"--") => next.clone(),
                _ => return Err(usage("引数の値がありません。")),
            },
        };
        values.push((name, value));
    }
    let get = |name: &[u8]| {
        values
            .iter()
            .find(|(seen, _)| *seen == name)
            .map(|(_, value)| value.clone())
    };
    let Some(cwd) = get(b"--cwd") else {
        return Err(usage("--cwdは必須です。"));
    };
    let list = get(b"--acknowledged-list");
    let identities = get(b"--acknowledged-identities");
    if list.is_some() && identities.is_some() {
        return Err(usage("2つの確認値は同時に指定できません。"));
    }
    let check = |value: Vec<u8>| {
        if value.len() == 64 && value.iter().all(u8::is_ascii_hexdigit) {
            Ok(String::from_utf8(value)
                .expect("ASCII")
                .to_ascii_lowercase())
        } else {
            Err(usage("確認値は64桁のASCII hexで指定してください。"))
        }
    };
    let acknowledged = match (list, identities) {
        (Some(value), None) => Acknowledged::List(check(value)?),
        (None, Some(value)) => Acknowledged::Identity(check(value)?),
        _ => Acknowledged::None,
    };
    Ok((cwd, acknowledged))
}

/// 旧版の`run_detail`の、JSONと終了値と注記。`KeyboardInterrupt`だけを`Err`で返す。
pub fn run_detail(argv: &[Vec<u8>], sys: &mut dyn System) -> Result<(u8, Doc, Vec<String>), Exc> {
    let mut doc = Doc {
        result: "internal",
        ..Doc::default()
    };
    match run_detail_body(argv, sys, &mut doc) {
        Ok((code, notes)) => Ok((code, doc, notes)),
        Err(Exc::Scan(error)) => {
            doc.result = if error.exit_code == 2 {
                "usage"
            } else {
                "scan-unavailable"
            };
            doc.issues.push(Issue::of(&error));
            for root in &mut doc.roots {
                if Some(root.id) == error.root_id {
                    root.status = "error";
                    root.reason = Some(error.code);
                    root.identity = None;
                    root.canonical_id = None;
                }
            }
            finish_failure(&mut doc);
            Ok((error.exit_code, doc, Vec::new()))
        }
        Err(Exc::Interrupt) => Err(Exc::Interrupt),
        Err(other) => {
            let name = match other {
                Exc::Os(errno) => {
                    support::python_io::os_error_type(errno.raw_os_error()).to_string()
                }
                Exc::Unexpected(name) => name,
                Exc::Scan(_) | Exc::Interrupt => unreachable!("handled above"),
            };
            doc.result = "internal";
            doc.issues.push(Issue {
                code: "internal-error",
                root_id: None,
                path: None,
                message: format!("想定外の失敗が起きました: {name}"),
            });
            finish_failure(&mut doc);
            Ok((20, doc, Vec::new()))
        }
    }
}

fn finish_failure(doc: &mut Doc) {
    report::sort_candidates(doc);
    report::sort_issues(doc);
}

fn run_detail_body(
    argv: &[Vec<u8>],
    sys: &mut dyn System,
    doc: &mut Doc,
) -> Result<(u8, Vec<String>), Exc> {
    let (cwd, acknowledged) = parse_arguments(argv)?;
    // 旧版の check_features(Pythonの版・POSIX・osの機能の有無)は、Rustに同じ条件が無い。
    let budget = scan::Budget::new(sys)?;
    if cwd.is_empty() || cwd.contains(&0) {
        return Err(invalid_cwd("作業対象のディレクトリが不正です。"));
    }
    let resolved = match resolve_cwd(sys, cwd, &budget) {
        Ok(Some((path, _))) => path,
        Ok(None) => return Err(invalid_cwd("作業対象のディレクトリがありません。")),
        Err(Exc::Scan(error))
            if matches!(
                error.code,
                "resolution-failed" | "not-directory" | "unreadable"
            ) =>
        {
            return Err(invalid_cwd("作業対象の実体を確定できません。"));
        }
        Err(Exc::Os(_)) => return Err(invalid_cwd("作業対象の実体を確定できません。")),
        Err(other) => return Err(other),
    };
    doc.cwd = Some(resolved);
    scan::Scanner::new(doc, sys, &budget).scan()?;
    report::sort_candidates(doc);
    let digests = (report::list_digest(doc), report::identity_digest(doc));
    budget.check(sys, None, None)?;
    let (code, outcome, notes) = report::decide_outcome(doc, &acknowledged, &digests, sys)?;
    doc.result = outcome;
    doc.complete = true;
    doc.digests = Some(digests);
    Ok((code, notes))
}

/// `--cwd`を絶対パスにして解決する(相対なら`os.getcwdb()`と結ぶ)。
fn resolve_cwd(
    sys: &mut dyn System,
    cwd: Vec<u8>,
    budget: &scan::Budget,
) -> Result<Option<(Vec<u8>, dev_workflow_platform::r4g::fs::Stat)>, Exc> {
    let raw = if cwd.starts_with(b"/") {
        cwd
    } else {
        scan::join(&sys.getcwd()?, &cwd)
    };
    scan::resolve_directory(sys, &raw, budget, None)
}

/// 旧版の`main`と、それを呼ぶ`sys.exit(main())`の結果。
pub enum Ending {
    /// 終了値(`sys.exit`の値。捕まえなかった例外は1)。
    Exit(u8),
    /// 捕まえなかった`KeyboardInterrupt`(呼出履歴を書いた後、`SIGINT`で終える)。
    Interrupt,
}

fn frame(point: &str, function: &'static str) -> Frame {
    let line = lines::LINES.point(point);
    Frame {
        file: lines::SCRIPT_NAME,
        line: line.line,
        function,
        source: Some(line.source),
    }
}

/// 捕まえなかった例外の呼出履歴(`<module>`の`sys.exit(main())`と、`main`の行)。
fn uncaught(stdio: &mut PythonStdio, point: &str, type_name: &str, message: &[u32]) {
    traceback::print_exception(
        &mut stdio.stderr,
        &[frame("module", "<module>"), frame(point, "main")],
        type_name,
        message,
    );
}

/// `sys.<stream>.write(text)`。失敗は呼出履歴にして`Err`。
fn write_line(
    stdio: &mut PythonStdio,
    stderr_stream: bool,
    point: &str,
    text: &[u32],
) -> Result<(), Ending> {
    let stream = if stderr_stream {
        &mut stdio.stderr
    } else {
        &mut stdio.stdout
    };
    let error = match stream.write(text) {
        Ok(_) => None,
        Err(support::python_io::PyError::Interrupt) => return Err(Ending::Interrupt),
        Err(error) => {
            let line = support::python_io::exception_line(&error);
            let colon = line
                .windows(2)
                .position(|pair| pair == [58, 32])
                .unwrap_or(line.len());
            let type_name: String = line[..colon]
                .iter()
                .filter_map(|&code| char::from_u32(code))
                .collect();
            let message = line.get(colon + 2..).unwrap_or(&[]).to_vec();
            Some((type_name, message))
        }
    };
    match error {
        None => Ok(()),
        Some((type_name, message)) => {
            uncaught(stdio, point, &type_name, &message);
            Err(Ending::Exit(1))
        }
    }
}

/// 旧版の`main(argv)`。`stdio`は旧Pythonの`sys.stdout`・`sys.stderr`。
pub fn main(argv: &[Vec<u8>], sys: &mut dyn System, stdio: &mut PythonStdio) -> Ending {
    match main_body(argv, sys, stdio) {
        Ok(code) => Ending::Exit(code),
        Err(ending) => ending,
    }
}

fn main_body(
    argv: &[Vec<u8>],
    sys: &mut dyn System,
    stdio: &mut PythonStdio,
) -> Result<u8, Ending> {
    // 旧Pythonは、書込みの呼出しから戻った次の評価の合間でも信号を確かめる(部分書込みで戻った後など)。
    let checked = |sys: &mut dyn System| match sys.check_interrupt() {
        Ok(()) => Ok(()),
        Err(_) => Err(Ending::Interrupt),
    };
    if argv.len() == 2 && argv[0] == b"scan" && argv[1] == b"--help" {
        write_line(stdio, false, "help", &text::from_str(HELP))?;
        checked(sys)?;
        return Ok(0);
    }
    let (code, doc, notes) = match run_detail(argv, sys) {
        Ok(found) => found,
        Err(_) => return Err(Ending::Interrupt),
    };
    let mut line = report::result_json(&doc);
    line.push('\n');
    checked(sys)?;
    write_line(stdio, false, "result", &text::from_str(&line))?;
    checked(sys)?;
    if !doc.issues.is_empty() {
        // 診断は固定文とOSエラー番号だけ。候補の名前はJSONにだけ出す。
        for issue in &doc.issues {
            let text = format!("pending-implementation: {} {}\n", issue.code, issue.message);
            write_line(stdio, true, "issue", &text::from_str(&text))?;
            checked(sys)?;
        }
        return Ok(code);
    }
    for note in &notes {
        write_line(stdio, true, "note", &text::from_str(&format!("{note}\n")))?;
        checked(sys)?;
    }
    if code == 10 {
        write_line(
            stdio,
            true,
            "confirm",
            &text::from_str("保護記録候補の一覧について、人の確認が必要です。\n"),
        )?;
        checked(sys)?;
    }
    Ok(code)
}

/// 捕まえなかった`KeyboardInterrupt`の呼出履歴(途中の行は比べない決定。`<module>`と`main`の段だけ書く)。
pub fn print_keyboard_interrupt(stdio: &mut PythonStdio) {
    traceback::print_exception(
        &mut stdio.stderr,
        &[frame("module", "<module>"), frame("run", "main")],
        "KeyboardInterrupt",
        &[],
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<Vec<u8>> {
        values
            .iter()
            .map(|value| value.as_bytes().to_vec())
            .collect()
    }

    fn code_of(result: Result<(Vec<u8>, Acknowledged), Exc>) -> String {
        match result {
            Ok(_) => "ok".to_string(),
            Err(Exc::Scan(error)) => error.message,
            Err(other) => format!("{other:?}"),
        }
    }

    #[test]
    fn arguments_follow_the_old_order_of_checks() {
        let hex = "A".repeat(64);
        let cases: [(&[&str], &str); 13] = [
            (&[], "scanと必須の--cwdを指定してください。"),
            (&["--help"], "scanと必須の--cwdを指定してください。"),
            (&["scan"], "--cwdは必須です。"),
            (&["scan", "--help"], "不明または重複した引数があります。"),
            (&["scan", "--cwd"], "引数の値がありません。"),
            (&["scan", "--cwd", "--x"], "引数の値がありません。"),
            (
                &["scan", "--cwd=", "--cwd=x"],
                "不明または重複した引数があります。",
            ),
            (&["scan", "--cwd=--x"], "ok"),
            (
                &["scan", "--cwd", "x", "--acknowledged-list", "0"],
                "確認値は64桁のASCII hexで指定してください。",
            ),
            (
                &[
                    "scan",
                    "--acknowledged-list=",
                    "--acknowledged-identities=",
                    "--cwd=x",
                ],
                "2つの確認値は同時に指定できません。",
            ),
            (&["scan", "--cwd=x", "--acknowledged-list", &hex], "ok"),
            (
                &["scan", "--cwd=x", "--other"],
                "不明または重複した引数があります。",
            ),
            (&["scan", "--cwd", ""], "ok"),
        ];
        for (values, expected) in cases {
            assert_eq!(
                code_of(parse_arguments(&args(values))),
                expected,
                "{values:?}"
            );
        }
        let (_, acknowledged) =
            parse_arguments(&args(&["scan", "--cwd=x", "--acknowledged-list", &hex])).unwrap();
        assert_eq!(acknowledged, Acknowledged::List("a".repeat(64)));
    }

    /// 旧版の`HELP`と字面が同じこと(旧版のファイルの三重引用の中身と比べる)。
    #[test]
    fn help_is_the_old_text() {
        let source = std::fs::read_to_string(lines::old_script_path()).unwrap();
        let start = source.find("HELP = \"\"\"").unwrap() + "HELP = \"\"\"".len();
        let end = start + source[start..].find("\"\"\"").unwrap();
        assert_eq!(HELP, &source[start..end]);
    }
}
