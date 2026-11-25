//! `taskmd-diff` :2928-2975。
use super::meta::{load_meta, read_fault, verify_taskmd_body};
use super::paths;
use super::shell::{Flow, Part, Shell};
use super::sites;
use super::state::{make_work, open_state};
use super::text::{path_field, quoteaf};
use super::{Context, prelude};
use dev_workflow_platform::implement_guard as os;
use std::ffi::OsStr;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

pub fn run(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    prelude(shell, context)?;
    let checked = open_state(
        shell,
        &context.args.state,
        &context.places,
        &context.excluded,
        &context.args.manifest_sha256,
        &context.args.snapshot_sha256,
    )?;
    let state = checked.state.clone();
    context.checked = Some(checked);
    make_work(shell, &state)?;
    let meta = load_meta(shell, &state, &context.args.cwd)?;
    let body = verify_taskmd_body(shell, &state, &meta.real)?;
    let dod = path_field(&[state.as_slice(), b"/taskmd-body"].concat());
    if !body.ok {
        shell.echo_err(
            sites::TASKMD_BODY_ECHO,
            "ERROR [taskmd-body] 退避したタスク MD が記録と一致しない(完了条件の基準を信頼できない)".as_bytes(),
        )?;
        shell.printf(
            sites::TASKMD_BODY_PRINTF,
            &[Part::Text(b"REASON=taskmd-body\n")],
        )?;
        return Err(shell.finish(33));
    }
    // 選択パス → 実体パスの対応。
    let kind_now: &[u8] = if paths::is_link(&meta.selected) {
        b"symlink"
    } else {
        b"file"
    };
    let mut real_now = Vec::new();
    if paths::exists(&meta.selected) {
        let resolved = paths::realpath(&meta.selected);
        shell.tool_point("realpath-selected")?;
        if let Some(resolved) = resolved.filter(|r| !r.is_empty()) {
            real_now = resolved;
        }
    }
    if kind_now != meta.kind.as_slice()
        || real_now.is_empty()
        || real_now != meta.real
        || paths::is_link(&real_now)
        || !paths::is_file(&real_now)
    {
        let mut text = "NOTE: タスク MD の選択パスと実体の対応が変わった(記録: "
            .as_bytes()
            .to_vec();
        text.extend_from_slice(&meta.kind);
        text.extend_from_slice(" → ".as_bytes());
        text.extend_from_slice(&path_field(&meta.real));
        text.extend_from_slice(" / 現在: ".as_bytes());
        text.extend_from_slice(kind_now);
        text.extend_from_slice(" → ".as_bytes());
        if real_now.is_empty() {
            text.extend_from_slice(&path_field("(確定できない)".as_bytes()));
        } else {
            text.extend_from_slice(&path_field(&real_now));
        }
        text.extend_from_slice(")".as_bytes());
        shell.echo_err(sites::MAPPING_ECHO, &text)?;
        shell.printf(
            sites::MAPPING_PRINTF,
            &[
                Part::Text(b"TASKMD=mapping-changed\nDOD="),
                Part::Arg(&dod),
                Part::Text(b"\n"),
            ],
        )?;
        return Err(shell.finish(35));
    }
    let body_path = [state.as_slice(), b"/taskmd-body"].concat();
    let readable = paths::readable(&real_now);
    if readable {
        let faults = [
            read_fault(shell, "read-cmp", &body_path),
            read_fault(shell, "read-cmp", &real_now),
        ];
        let same = match same_bytes(&body_path, &real_now, faults) {
            Compared::Same => true,
            Compared::Differ => false,
            Compared::Trouble(path, errno) => {
                // GNU cmp(diffutils)の読み取りの誤り: `cmp: <パス>: <strerror>`、終了値 2(`if` の中)。
                shell.tool_error(b"cmp", &path, Some(errno));
                false
            }
        };
        shell.tool_point("cmp-same")?;
        if same {
            shell.printf(
                sites::SAME_PRINTF,
                &[
                    Part::Text(b"TASKMD=same\nDOD="),
                    Part::Arg(&dod),
                    Part::Text(b"\n"),
                ],
            )?;
            return Ok(());
        }
    }
    if readable {
        let s1 = stat_size(shell, &body_path);
        shell.tool_point("stat-size-before")?;
        let s2 = stat_size(shell, &real_now);
        shell.tool_point("stat-size-after")?;
        let before = normalized_lines(shell, &body_path, sites::SED_BEFORE)?;
        shell.tool_point("sed-before")?;
        let after = normalized_lines(shell, &real_now, sites::SED_AFTER)?;
        shell.tool_point("sed-after")?;
        let equal = s1.is_some() && s1 == s2 && before == after;
        shell.tool_point("cmp-norm")?;
        if equal {
            shell.printf(
                sites::CHECKS_ONLY_PRINTF,
                &[
                    Part::Text(b"TASKMD=checks-only\nDOD="),
                    Part::Arg(&dod),
                    Part::Text(b"\n"),
                ],
            )?;
            return Ok(());
        }
    }
    shell.echo_err(
        sites::BODY_CHANGED_ECHO,
        "NOTE: タスク MD の要件本文が変わっている(チェック状態の更新だけではない)".as_bytes(),
    )?;
    shell.printf(
        sites::BODY_CHANGED_PRINTF,
        &[
            Part::Text(b"TASKMD=body-changed\nDOD="),
            Part::Arg(&dod),
            Part::Text(b"\n"),
        ],
    )?;
    Err(shell.finish(34))
}

fn open(path: &[u8]) -> std::io::Result<fs::File> {
    fs::File::open(Path::new(OsStr::from_bytes(path)))
}

/// `cmp -s a b` の結果。
enum Compared {
    Same,
    Differ,
    /// 開けないか読めない(cmp が文を出して終了値 2)。パスと番号。
    Trouble(Vec<u8>, i32),
}

/// `cmp -s a b`。違いを見つけた所で止める。`faults` は差し込み点の番号(a・b の最初の read)。
fn same_bytes(a_path: &[u8], b_path: &[u8], faults: [Option<i32>; 2]) -> Compared {
    let errno_of = |error: std::io::Error| error.raw_os_error().unwrap_or(os::errno::EIO);
    let a = match open(a_path) {
        Ok(file) => file,
        Err(error) => return Compared::Trouble(a_path.to_vec(), errno_of(error)),
    };
    let b = match open(b_path) {
        Ok(file) => file,
        Err(error) => return Compared::Trouble(b_path.to_vec(), errno_of(error)),
    };
    let mut a = BufReader::new(a);
    let mut b = BufReader::new(b);
    let mut left = [0u8; 8192];
    let mut right = [0u8; 8192];
    loop {
        let n = match faults[0] {
            Some(errno) => return Compared::Trouble(a_path.to_vec(), errno),
            None => match read_full(&mut a, &mut left) {
                Ok(n) => n,
                Err(error) => return Compared::Trouble(a_path.to_vec(), errno_of(error)),
            },
        };
        let m = match faults[1] {
            Some(errno) => return Compared::Trouble(b_path.to_vec(), errno),
            None => match read_full(&mut b, &mut right) {
                Ok(m) => m,
                Err(error) => return Compared::Trouble(b_path.to_vec(), errno_of(error)),
            },
        };
        if n != m || left.get(..n) != right.get(..m) {
            return Compared::Differ;
        }
        if n == 0 {
            return Compared::Same;
        }
    }
}

fn read_full(reader: &mut impl Read, buffer: &mut [u8]) -> std::io::Result<usize> {
    let mut filled = 0usize;
    while filled < buffer.len() {
        match reader.read(buffer.get_mut(filled..).unwrap_or_default()) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(filled)
}

/// `stat -c %s -- <path>`(失敗は GNU stat 9.4 の文を出して `None`)。
fn stat_size(shell: &mut Shell, path: &[u8]) -> Option<u64> {
    match paths::size_of(path) {
        Ok(size) => Some(size),
        Err(errno) => {
            let mut text = b"cannot statx ".to_vec();
            text.extend_from_slice(&quoteaf(path));
            shell.tool_error(b"stat", &text, Some(errno));
            None
        }
    }
}

/// C の地域設定の `[[:space:]]`。
fn space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 11 | 12 | b'\r')
}

/// `NORM_SED` を 1 行に当てる: 行頭の `[[:space:]]*[-*+][[:space:]]+\[` の後ろの `x`・`X` を空白に
/// し、その後ろが `]` のときだけ置き換える(1 行に 1 か所)。
pub fn normalize_line(line: &mut [u8]) {
    let mut i = 0usize;
    while line.get(i).is_some_and(|b| space(*b)) {
        i += 1;
    }
    if !matches!(line.get(i), Some(b'-' | b'*' | b'+')) {
        return;
    }
    i += 1;
    let start = i;
    while line.get(i).is_some_and(|b| space(*b)) {
        i += 1;
    }
    if i == start || line.get(i) != Some(&b'[') {
        return;
    }
    i += 1;
    if matches!(line.get(i), Some(b'x' | b'X'))
        && line.get(i + 1) == Some(&b']')
        && let Some(slot) = line.get_mut(i)
    {
        *slot = b' ';
    }
}

/// `sed -E "$NORM_SED" <path` の出力の sha256 の代わりに、正規化した行の列の digest を返す
/// (Rust の中で持つ中間の値。00:35 の決定)。全部を読み、読めなければ旧版の地点の文で止める。
fn normalized_lines(shell: &mut Shell, path: &[u8], site: sites::Site) -> Flow<Vec<u8>> {
    use sha2::{Digest, Sha256};
    let file = match open(path) {
        Ok(file) => file,
        Err(error) => {
            let errno = error
                .raw_os_error()
                .unwrap_or(dev_workflow_platform::implement_guard::errno::EIO);
            shell.redirect_error(site.line, path, errno);
            shell.after(site, false, 1)?;
            return Ok(Vec::new());
        }
    };
    let fault = read_fault(shell, "read-sed", path);
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut line = Vec::new();
    loop {
        line.clear();
        let read = match fault {
            Some(errno) => Err(std::io::Error::from_raw_os_error(errno)),
            None => reader.read_until(b'\n', &mut line),
        };
        match read {
            Ok(0) => break,
            Ok(_) => {
                normalize_line(&mut line);
                hasher.update(&line);
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => {
                let errno = error
                    .raw_os_error()
                    .unwrap_or(dev_workflow_platform::implement_guard::errno::EIO);
                shell.tool_error(b"sed", b"read error on stdin", Some(errno));
                shell.after(site, false, 4)?;
                return Ok(Vec::new());
            }
        }
    }
    Ok(hasher.finalize().to_vec())
}
