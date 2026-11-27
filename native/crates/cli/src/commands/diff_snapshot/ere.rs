use super::bytes::{joined, os};
use super::{Failure, Result};
use dev_workflow_platform::diff_snapshot::process;
use std::process::Command;
const REPRESENTATIVE: [&[u8]; 5] = [b"x", b"a/b/c.ts", b".claude/x", b"src/app.ts", b"README.md"];
pub fn matches(path: &[u8], ere: &[u8]) -> Result<bool> {
    // `printf '%s' PATH | LC_ALL=C grep -Eiqz -- ERE` (372): the shell forks the
    // pipeline there, and grep's own assignment gives it that line.
    process::fork(372);
    let mut grep = Command::new("grep");
    grep.args(["-Eiqz", "--"]).arg(os(ere)).env("LC_ALL", "C");
    grep.envs(process::own_line(372));
    process::pipeline_element(&mut grep);
    Ok(super::foreground(&mut grep, path)?.is_ok_and(|o| o.code == 0))
}
fn validate(ere: &[u8], line: u32) -> Result<()> {
    // `printf '' | grep -Eqz -- ERE 2>/dev/null` (519 for one element, 556 for
    // the combined ERE).
    process::fork(line);
    let mut grep = Command::new("grep");
    grep.args(["-Eqz", "--"]).arg(os(ere));
    process::pipeline_element(&mut grep);
    match super::foreground(&mut grep, b"")? {
        Ok(o) if o.code < 2 => Ok(()),
        _ => Err(Failure::usage("除外の正規表現が不正")),
    }
}
pub fn validate_glob(value: &[u8]) -> Result<()> {
    if value.is_empty() {
        return Err(Failure::usage("--exclude-glob に空の値は指定できない"));
    }
    if value.windows(2).any(|s| s == b"//") {
        return Err(Failure::usage_bytes(joined(&[
            "--exclude-glob に '//' は使えない: ".as_bytes(),
            value,
        ])));
    }
    if value.iter().any(|c| b"'\"`$[]{}".contains(c)) {
        return Err(Failure::usage_bytes(joined(&["--exclude-glob に引用符・バッククォート・$・ブラケット・ブレースは使えない(黙って非マッチになるため): ".as_bytes(),value])));
    }
    if value.iter().any(|c| *c < 32 || *c == 127) {
        return Err(Failure::usage(
            "--exclude-glob に改行・制御文字は使えません",
        ));
    }
    let v = value.strip_suffix(b"/").unwrap_or(value);
    let v = v.strip_prefix(b"/").unwrap_or(v);
    if v.is_empty() {
        return Err(Failure::usage_bytes(joined(&[
            "--exclude-glob の要素が空になる: ".as_bytes(),
            value,
        ])));
    }
    Ok(())
}
pub fn glob(value: &[u8]) -> Vec<u8> {
    let value = value.strip_suffix(b"/").unwrap_or(value);
    let (mut out, v) = if let Some(v) = value.strip_prefix(b"/") {
        (b"^".to_vec(), v)
    } else {
        (b"(^|/)".to_vec(), value)
    };
    let mut i = 0;
    while i < v.len() {
        if v[i..].starts_with(b"**/") {
            out.extend(b"(.*/)?");
            i += 3;
        } else if v[i..].starts_with(b"**") {
            out.extend(b".*");
            i += 2;
        } else {
            match v[i] {
                b'*' => out.extend(b"[^/]*"),
                b'?' => out.extend(b"[^/]"),
                c if b"\\.^$+()[]{}|".contains(&c) => {
                    out.push(b'\\');
                    out.push(c);
                }
                c => out.push(c),
            }
            i += 1;
        }
    }
    out.extend(b"($|/)");
    out
}
fn all(ere: &[u8]) -> Result<bool> {
    for path in REPRESENTATIVE {
        if !matches(path, ere)? {
            return Ok(false);
        }
    }
    Ok(true)
}
pub fn build(globs: &[Vec<u8>], eres: &[Vec<u8>]) -> Result<Vec<u8>> {
    for g in globs {
        validate_glob(g)?;
    }
    let mut parts = Vec::new();
    for g in globs {
        // one="$(glob_to_ere "$g")" (536).
        process::fork(536);
        let one = glob(g);
        validate(&one, 519)?;
        if all(&one)? {
            return Err(Failure::usage_bytes(joined(&[
                "secret_paths が全パスに当たる: ".as_bytes(),
                g,
            ])));
        }
        parts.push(one);
    }
    for e in eres {
        if e.is_empty() {
            return Err(Failure::usage("--exclude に空の値は指定できない"));
        }
        validate(e, 519)?;
        if all(e)? {
            return Err(Failure::usage_bytes(joined(&[
                "secret_paths が全パスに当たる: ".as_bytes(),
                e,
            ])));
        }
        parts.push(e.clone());
    }
    let combined = parts.join(&b'|');
    validate(&combined, 556)?;
    if all(&combined)? {
        return Err(Failure::usage_bytes(joined(&[
            "secret_paths が全パスに当たる: ".as_bytes(),
            &combined,
        ])));
    }
    Ok(combined)
}
