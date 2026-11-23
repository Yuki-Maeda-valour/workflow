//! 旧版の `main`・`iter_md_files`(check_links.py:24-89)を同じ順で行う。
use super::fsops::{self, Raised};
use super::matcher;
use super::path::{self, PurePath};
use super::pyio::{IoError, Py};
use super::traceback::{Resolve, Site};

/// 旧版の docstring(`print(__doc__)` で出す。PYTHONOPTIMIZE が 2 以上なら None)。
pub const DOC: &str = "Markdown の相対リンク・記載パスの存在チェック(汎用・依存なし・読み取り専用)。\n\n使い方:\n    python3 check_links.py <ファイルまたはディレクトリ>...\n\nディレクトリを渡すと配下の *.md を再帰走査する(.git / node_modules 等は除外)。\n検査対象:\n  1. [text](relative/path) 形式の相対リンク(http(s):, mailto:, #アンカーのみは対象外)\n  2. `path/to/file.ext` 形式のインラインコード中の相対パスらしき文字列(存在すれば OK、\n     無ければ WARN — コード片の可能性があるため error にはしない)\n終了コード: リンク切れ(ERROR)が 1 件以上あれば 1、なければ 0。\n";

/// 書込みの例外を、その print の所の例外にする。
fn printed(site: Site, error: IoError) -> Raised {
    Raised::single(site, super::display::io_exception(error))
}

fn print(py: &mut Py, site: Site, body: &[u8]) -> Result<(), Raised> {
    py.print(body).map_err(|error| printed(site, error))
}

/// 集めた WARN と ERROR と検査ファイル数。
#[derive(Default)]
struct Report {
    errors: Vec<Vec<u8>>,
    warns: Vec<Vec<u8>>,
    checked: usize,
}

/// 旧版の `main(argv)`。戻り値は `sys.exit` に渡す値。
pub fn main(py: &mut Py, argv: &[Vec<u8>], doc: Option<&str>) -> Result<u8, Raised> {
    if argv.is_empty() {
        print(py, Site::PrintDoc, doc.unwrap_or("None").as_bytes())?;
        return Ok(2);
    }
    let mut report = Report::default();
    for target in argv {
        let path = PurePath::new(target);
        if fsops::is_dir(&path, Site::TargetIsDir)? {
            let mut files = fsops::rglob(&path)?;
            path::sort(&mut files);
            for file in files {
                if !file.has_excluded_part() {
                    check(&file, &mut report)?;
                }
            }
        } else if fsops::is_file(&path, Site::TargetIsFile)? {
            check(&path, &mut report)?;
        } else {
            let mut line = "WARN: 対象が見つかりません: ".as_bytes().to_vec();
            line.extend_from_slice(target);
            print(py, Site::TargetWarn, &line)?;
        }
    }
    print(
        py,
        Site::PrintCount,
        format!("検査ファイル数: {}", report.checked).as_bytes(),
    )?;
    for warn in &report.warns {
        print(py, Site::PrintWarn, &[b"WARN  ", warn.as_slice()].concat())?;
    }
    for error in &report.errors {
        print(
            py,
            Site::PrintError,
            &[b"ERROR ", error.as_slice()].concat(),
        )?;
    }
    print(
        py,
        Site::PrintResult,
        format!(
            "結果: ERROR {} 件 / WARN {} 件",
            report.errors.len(),
            report.warns.len()
        )
        .as_bytes(),
    )?;
    Ok(u8::from(!report.errors.is_empty()))
}

/// `<ファイル>:<行>: <文> -> <拾った文字列>`。
fn message(md: &[u8], line: usize, text: &str, found: &str) -> Vec<u8> {
    let mut message = md.to_vec();
    message.extend_from_slice(format!(":{line}: {text} -> {found}").as_bytes());
    message
}

/// 相対なら親と連結して resolve、`/` で始まれば整えた絶対パスのまま(旧版の 64 行と 73 行)。
fn resolved(base: &PurePath, target: &str, call: Resolve) -> Result<PurePath, Raised> {
    if target.starts_with('/') {
        Ok(PurePath::new(target.as_bytes()))
    } else {
        fsops::resolve(&base.join(target.as_bytes()), call)
    }
}

/// 1 ファイルの読取りと 2 種類の照合(旧版の 52-81 行)。
fn check(md: &PurePath, report: &mut Report) -> Result<(), Raised> {
    let name = md.to_bytes();
    let text = super::text::decode(&fsops::read_bytes(&name)?);
    let base = md.parent();
    report.checked += 1;
    for found in matcher::links(&text) {
        let raw = found.group;
        let Some(target) = matcher::link_target(raw) else {
            continue;
        };
        if target.is_empty() {
            continue;
        }
        let line = matcher::line_number(&text, found.start);
        let resolved = resolved(&base, target, Resolve::Link)?;
        if !fsops::exists(&resolved, Site::LinkExists)? {
            report.errors.push(message(&name, line, "リンク切れ", raw));
        }
    }
    for found in matcher::code_paths(&text) {
        let target = found.group;
        if matcher::code_skipped(target) {
            continue;
        }
        let line = matcher::line_number(&text, found.start);
        let resolved = resolved(&base, target, Resolve::Code)?;
        let mut alt = false;
        for up in base.parents().iter().take(3) {
            if fsops::exists(&up.join(target.as_bytes()), Site::CodeAltExists)? {
                alt = true;
                break;
            }
        }
        if !fsops::exists(&resolved, Site::CodeExists)? && !alt {
            report.warns.push(message(
                &name,
                line,
                "記載パスが見つからない(コード片なら無視可)",
                target,
            ));
        }
    }
    Ok(())
}
