//! 記録の読み取り: `load_meta` :2281-2320、`set_taskmd_rel` :2321-2329、`verify_taskmd_body`
//! :2330-2346、`field_value` :492。bash の `read -r` と `IFS=$'\t'` の規則で読む。
use super::paths;
use super::shell::{Flow, Shell};
use super::sites::{self, Site};
use super::state::{fail_internal, fail_usage, file_sha256};
use super::text::{cunquote_all, cunquote_errexit, path_field};
use dev_workflow_platform::implement_guard as os;
use std::ffi::OsStr;
use std::fs;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

/// 読み取りの結果。
pub enum Lines {
    /// 改行で終わる行(NUL は bash の `read` と同じく落とす)と、途中の読み取りの誤り。
    Read(Vec<Vec<u8>>, Option<i32>),
    /// 開けなかった(番号)。
    Open(i32),
}

/// 読み取りの誤りの差し込み点(`fail:read:<ファイル名>`。検査では最初の read が失敗したことにする)。
pub fn read_fault(shell: &mut Shell, kind: &str, path: &[u8]) -> Option<i32> {
    let name = path.rsplit(|c| *c == b'/').next().unwrap_or_default();
    shell
        .point(&format!("fail:{kind}:{}", String::from_utf8_lossy(name)))
        .filter(|errno| *errno > 0)
}

/// ファイルを、bash の `while read` が本体を回す行に分ける(改行で終わらない最後の行は読まない)。
/// `fail` は差し込み点の番号(開けた後の最初の read が失敗したことにする)。
pub fn read_lines(path: &[u8], fail: Option<i32>) -> Lines {
    let mut file = match fs::File::open(Path::new(OsStr::from_bytes(path))) {
        Ok(file) => file,
        Err(error) => return Lines::Open(error.raw_os_error().unwrap_or(os::errno::EIO)),
    };
    if let Some(errno) = fail {
        return Lines::Read(Vec::new(), Some(errno));
    }
    let mut data = Vec::new();
    let mut buffer = vec![0u8; 65536];
    let mut failure = None;
    loop {
        match file.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => data.extend_from_slice(buffer.get(..n).unwrap_or_default()),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => {
                failure = Some(error.raw_os_error().unwrap_or(os::errno::EIO));
                break;
            }
        }
    }
    let mut lines = Vec::new();
    let mut current = Vec::new();
    for byte in data {
        match byte {
            0 => {}
            b'\n' => lines.push(std::mem::take(&mut current)),
            other => current.push(other),
        }
    }
    Lines::Read(lines, failure)
}

/// 最初の行(`read -r … <file`。改行で終わらなければ `read` は失敗する)。開けなければ `Err`。
/// 最初の行より前に読み取りの誤りがあれば、`read` の文を `line` の行で出して `Ok(None)`。
pub fn first_line(shell: &mut Shell, path: &[u8], line: u32) -> Result<Option<Vec<u8>>, i32> {
    let fail = read_fault(shell, "read", path);
    match read_lines(path, fail) {
        Lines::Open(errno) => Err(errno),
        Lines::Read(lines, failure) => {
            let first = lines.into_iter().next();
            if first.is_none()
                && let Some(errno) = failure
            {
                read_error(shell, line, errno);
            }
            Ok(first)
        }
    }
}

/// bash の `read` の読み取りの誤りの文(fd はリダイレクトの 0)。
fn read_error(shell: &mut Shell, line: u32, errno: i32) {
    let mut text = b"read error: 0: ".to_vec();
    text.extend_from_slice(super::strerror(errno).as_bytes());
    shell.redirect_error_text(line, b"read", &text);
}

/// `IFS=$'\t' read -r v1 … vn` の分け方(タブは IFS の空白: 前後を詰め、連続を 1 つの区切りにする。
/// 最後の変数は残りの全部から後ろのタブを除いたもの)。
pub fn fields(line: &[u8], count: usize) -> Vec<Vec<u8>> {
    let mut out = Vec::with_capacity(count);
    let mut rest = line;
    let skip = |s: &[u8]| -> usize { s.iter().position(|b| *b != b'\t').unwrap_or(s.len()) };
    rest = rest.get(skip(rest)..).unwrap_or_default();
    for index in 0..count {
        if index + 1 == count {
            let end = rest.iter().rposition(|b| *b != b'\t').map_or(0, |p| p + 1);
            out.push(rest.get(..end).unwrap_or_default().to_vec());
            break;
        }
        let end = rest.iter().position(|b| *b == b'\t').unwrap_or(rest.len());
        out.push(rest.get(..end).unwrap_or_default().to_vec());
        rest = rest.get(end..).unwrap_or_default();
        rest = rest.get(skip(rest)..).unwrap_or_default();
    }
    while out.len() < count {
        out.push(Vec::new());
    }
    out
}

/// `field_value`(errexit の働く所)。`"` で始まる値だけ C 風引用を戻す。`printf -v` の誤りで止まる。
pub fn field_value_errexit(shell: &mut Shell, value: &[u8]) -> Flow<Vec<u8>> {
    if value.first() != Some(&b'"') {
        return Ok(value.to_vec());
    }
    let unquoted = cunquote_errexit(value);
    report_notes(shell, &unquoted.notes)?;
    Ok(unquoted.value)
}

/// `field_value`(`if` の中。誤りの後も続ける)。
pub fn field_value_quiet(shell: &mut Shell, value: &[u8]) -> Vec<u8> {
    if value.first() != Some(&b'"') {
        return value.to_vec();
    }
    let unquoted = cunquote_all(value);
    for note in &unquoted.notes {
        shell.printf_note(sites::CUNQUOTE_PRINTF.line, &note.text);
    }
    unquoted.value
}

fn report_notes(shell: &mut Shell, notes: &[super::text::PrintfNote]) -> Flow<()> {
    for note in notes {
        shell.printf_note(sites::CUNQUOTE_PRINTF.line, &note.text);
        shell.after(sites::CUNQUOTE_PRINTF, !note.failed, 1)?;
    }
    Ok(())
}

/// 記録の値。
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub toplevel: Vec<u8>,
    pub cwd: Vec<u8>,
    pub exclude: Vec<u8>,
    pub hooks_path: Vec<u8>,
    pub config_path: Vec<u8>,
    pub config_worktree_path: Vec<u8>,
    pub selected: Vec<u8>,
    pub real: Vec<u8>,
    pub kind: Vec<u8>,
    /// 最後の `field_value` の結果(`UNQ`)。
    pub unq: Vec<u8>,
}

impl Meta {
    /// `set_taskmd_rel` の (`TASKMD_REL`, `TASKMD_SEL_REL`)。
    pub fn rel(&self) -> (Vec<u8>, Vec<u8>) {
        let rel_of = |p: &[u8]| -> Vec<u8> {
            let mut prefix = self.toplevel.clone();
            prefix.push(b'/');
            p.strip_prefix(prefix.as_slice())
                .map(<[u8]>::to_vec)
                .unwrap_or_default()
        };
        let mut real = rel_of(&self.real);
        let mut sel = rel_of(&self.selected);
        if real.is_empty() {
            real = vec![1];
        }
        if sel.is_empty() {
            sel = vec![1];
        }
        (real, sel)
    }
}

/// `while IFS=$'\t' read -r key val; do …; done <file` の 1 つ。
fn read_records(
    shell: &mut Shell,
    path: &[u8],
    open: Site,
    read_line: u32,
) -> Flow<Vec<(Vec<u8>, Vec<u8>)>> {
    let fail = read_fault(shell, "read", path);
    match read_lines(path, fail) {
        Lines::Open(errno) => {
            shell.redirect_error(open.line, path, errno);
            shell.after(open, false, 1)?;
            Ok(Vec::new())
        }
        Lines::Read(lines, failure) => {
            let records = lines
                .iter()
                .map(|line| {
                    let mut parts = fields(line, 2).into_iter();
                    (
                        parts.next().unwrap_or_default(),
                        parts.next().unwrap_or_default(),
                    )
                })
                .collect();
            if let Some(errno) = failure {
                // 読み取りの誤りでループが終わる(`read` の文。fd はループのリダイレクトの 0)。
                read_error(shell, read_line, errno);
            }
            Ok(records)
        }
    }
}

/// `load_meta`。
pub fn load_meta(shell: &mut Shell, state: &[u8], cwd_arg: &[u8]) -> Flow<Meta> {
    let mut meta = Meta::default();
    let path = [state, b"/snapshot/4-meta.txt"].concat();
    let records = read_records(shell, &path, sites::LOAD_META_OPEN_META, 2284)?;
    for (key, val) in records {
        let slot: Option<&mut Vec<u8>> = match key.as_slice() {
            b"toplevel" => Some(&mut meta.toplevel),
            b"cwd" => Some(&mut meta.cwd),
            b"exclude" => Some(&mut meta.exclude),
            b"hooks-path" => Some(&mut meta.hooks_path),
            b"config-path" => Some(&mut meta.config_path),
            b"config-worktree-path" => Some(&mut meta.config_worktree_path),
            _ => None,
        };
        if let Some(slot) = slot {
            let value = field_value_errexit(shell, &val)?;
            meta.unq.clone_from(&value);
            *slot = value;
        }
    }
    let path = [state, b"/snapshot/taskmd.txt"].concat();
    let records = read_records(shell, &path, sites::LOAD_META_OPEN_TASKMD, 2291)?;
    for (key, val) in records {
        match key.as_slice() {
            b"selected" | b"real" => {
                let value = field_value_errexit(shell, &val)?;
                meta.unq.clone_from(&value);
                if key == b"selected" {
                    meta.selected = value;
                } else {
                    meta.real = value;
                }
            }
            b"kind" => meta.kind = val,
            _ => {}
        }
    }
    if meta.toplevel.is_empty()
        || meta.cwd.is_empty()
        || meta.exclude.is_empty()
        || meta.hooks_path.is_empty()
        || meta.config_path.is_empty()
        || meta.config_worktree_path.is_empty()
        || meta.selected.is_empty()
        || meta.real.is_empty()
        || meta.kind.is_empty()
    {
        return Err(fail_internal(
            shell,
            "保護領域の記録に必要な項目が無い".as_bytes(),
        ));
    }
    // --cwd の取り違え。
    if paths::is_dir(cwd_arg) {
        let now = paths::realpath(cwd_arg).unwrap_or_default();
        shell.tool_point("realpath-cwd")?;
        if !now.is_empty() && now != meta.cwd {
            let mut text = "--cwd が take のときと違う(記録: ".as_bytes().to_vec();
            text.extend_from_slice(&path_field(&meta.cwd));
            text.extend_from_slice(" / 指定: ".as_bytes());
            text.extend_from_slice(&path_field(&now));
            text.extend_from_slice(")".as_bytes());
            return Err(fail_usage(shell, &text));
        }
    } else {
        let mut text = "NOTE: --cwd が存在しない(保護領域の記録 "
            .as_bytes()
            .to_vec();
        text.extend_from_slice(&path_field(&meta.cwd));
        text.extend_from_slice(" を使う)".as_bytes());
        shell.echo_err(sites::LOAD_META_NOTE, &text)?;
    }
    Ok(meta)
}

/// `verify_taskmd_body` の結果。
#[derive(Clone, Debug, Default)]
pub struct Body {
    pub ok: bool,
    pub mode: Vec<u8>,
    pub sha: Vec<u8>,
    pub path: Vec<u8>,
}

/// `verify_taskmd_body`(`if !` の中なので errexit は働かない)。
pub fn verify_taskmd_body(shell: &mut Shell, state: &[u8], real: &[u8]) -> Flow<Body> {
    let mut body = Body::default();
    let manifest = [state, b"/manifest.tsv"].concat();
    let line = match first_line(shell, &manifest, sites::VERIFY_TASKMD_READ.line) {
        Err(errno) => {
            shell.redirect_error(sites::VERIFY_TASKMD_READ.line, &manifest, errno);
            return Ok(body);
        }
        Ok(None) => return Ok(body),
        Ok(Some(line)) => line,
    };
    let parts = fields(&line, 4);
    let get = |i: usize| parts.get(i).cloned().unwrap_or_default();
    if get(0) != b"T" {
        return Ok(body);
    }
    let path = field_value_quiet(shell, &get(3));
    body.mode = get(1);
    body.sha = get(2);
    body.path = path;
    if body.path != real {
        return Ok(body);
    }
    let taskmd_body = [state, b"/taskmd-body"].concat();
    if paths::is_link(&taskmd_body) || !paths::is_file(&taskmd_body) {
        return Ok(body);
    }
    let mode = paths::mode_text(&taskmd_body);
    shell.tool_point("stat-mode-body")?;
    let Ok(mode) = mode else {
        return Ok(body);
    };
    if mode != body.mode {
        return Ok(body);
    }
    let digest = file_sha256(&taskmd_body);
    shell.tool_point("sha256-body")?;
    let Some(digest) = digest else {
        return Ok(body);
    };
    body.ok = digest.as_bytes() == body.sha.as_slice();
    Ok(body)
}
