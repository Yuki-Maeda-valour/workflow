//! パスの解決: `cd -P`・`pwd -P`(`pwd_of` :502)、GNU `realpath`(coreutils 9.4 の
//! `canonicalize_filename_mode` の `CAN_ALL_BUT_LAST`)、`split_path` :503-509、`norm_rest` :571-590、
//! `resolve_future_path` :591-614、bash の `command -v`。道具は起動しない。
use dev_workflow_platform::implement_guard as os;
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

fn path(bytes: &[u8]) -> &Path {
    Path::new(OsStr::from_bytes(bytes))
}

/// リンクを辿る種類(`[ -d ]`・`[ -f ]`・`[ -e ]`)。
pub fn is_dir(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok_and(|m| m.is_dir())
}

pub fn is_file(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok_and(|m| m.is_file())
}

pub fn exists(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok()
}

/// リンクか(`[ -L ]`)。
pub fn is_link(p: &[u8]) -> bool {
    !p.is_empty() && fs::symlink_metadata(path(p)).is_ok_and(|m| m.file_type().is_symlink())
}

/// `[ -r ]`・`[ -x ]`(実効の uid)。
pub fn readable(p: &[u8]) -> bool {
    !p.is_empty() && os::effective_readable(p)
}

pub fn executable(p: &[u8]) -> bool {
    !p.is_empty() && os::effective_executable(p)
}

/// 現在のフォルダの物理パス(`getcwd`)。
pub fn getcwd() -> Option<Vec<u8>> {
    std::env::current_dir()
        .ok()
        .map(|p| p.as_os_str().as_bytes().to_vec())
}

/// 起動時の `OLDPWD`(bash は起動時にフォルダでなければ値の無い変数にする。`OLDPWD_CHECK_DIRECTORY`)。
static OLDPWD: std::sync::OnceLock<Option<Vec<u8>>> = std::sync::OnceLock::new();

/// 起動時に 1 回だけ、受け継いだ `OLDPWD` を覚える(フォルダのときだけ値を持つ)。
pub fn remember_oldpwd(inherited: Option<&[u8]>) {
    let value = inherited.filter(|value| is_dir(value)).map(<[u8]>::to_vec);
    let _ = OLDPWD.set(value);
}

/// `pwd_of`(サブシェルの `CDPATH= cd -P -- <値> && pwd -P`)。在るフォルダで、検索の権限があれば
/// 物理パス。値がちょうど `-` なら、bash の `cd -` と同じく `OLDPWD` へ移る(値が無ければ失敗)。
pub fn phys_dir(p: &[u8]) -> Option<Vec<u8>> {
    if p == b"-" {
        let oldpwd = OLDPWD.get().cloned().flatten()?;
        return phys_dir_of(&oldpwd);
    }
    phys_dir_of(p)
}

fn phys_dir_of(p: &[u8]) -> Option<Vec<u8>> {
    if p.is_empty() {
        return None;
    }
    let resolved = fs::canonicalize(path(p)).ok()?;
    let bytes = resolved.as_os_str().as_bytes().to_vec();
    if !is_dir(&bytes) || !executable(&bytes) {
        return None;
    }
    Some(bytes)
}

/// `split_path`: (親, 末尾の要素)。
pub fn split_path(p: &[u8]) -> (Vec<u8>, Vec<u8>) {
    if p == b"/" {
        return (b"/".to_vec(), Vec::new());
    }
    // `*/?*`: `/` があり、その後ろに 1 文字以上ある。
    if let Some(slash) = p.iter().rposition(|b| *b == b'/')
        && slash + 1 < p.len()
    {
        let base = p.get(slash + 1..).unwrap_or_default().to_vec();
        let mut dir = p.get(..slash).unwrap_or_default().to_vec();
        if dir.is_empty() {
            dir = b"/".to_vec();
        }
        return (dir, base);
    }
    // `*/?*` に当たらない(`/` が無いか、末尾が `/`): `*)` の枝。ただし `*/?*` は最長一致なので、
    // 途中に `/` があり末尾が `/` の値も `*/?*` に当たる。
    if let Some(first) = p.iter().position(|b| *b == b'/')
        && first + 1 < p.len()
    {
        // 末尾が `/` の値: `${1##*/}` は空、`${1%/*}` は最後の `/` より前。
        let dir_end = p.iter().rposition(|b| *b == b'/').unwrap_or(0);
        let mut dir = p.get(..dir_end).unwrap_or_default().to_vec();
        if dir.is_empty() {
            dir = b"/".to_vec();
        }
        return (dir, Vec::new());
    }
    (b".".to_vec(), p.to_vec())
}

/// `norm_rest`: まだ無い相対部分を語彙的に正規化する。余った `..` は失敗。
pub fn norm_rest(rest: &[u8]) -> Option<Vec<u8>> {
    let mut out: Vec<Vec<u8>> = Vec::new();
    for segment in rest.split(|b| *b == b'/') {
        match segment {
            b"" | b"." => {}
            b".." => {
                out.pop()?;
            }
            other => out.push(other.to_vec()),
        }
    }
    Some(out.join(&b'/'))
}

/// `resolve_future_path`。在る最も深い親を物理パスにし、残りを語彙的に正規化して継ぐ。
/// `NREST`(最後の `norm_rest` の結果)も返す(子の環境の変数の値)。
pub fn resolve_future_path(p: &[u8], nrest: &mut Vec<u8>) -> Option<Vec<u8>> {
    let mut current = p.to_vec();
    let mut rest: Vec<u8> = Vec::new();
    loop {
        if is_dir(&current) {
            let mut resolved = phys_dir(&current)?;
            if !rest.is_empty() {
                nrest.clear();
                let normalized = norm_rest(&rest)?;
                nrest.clone_from(&normalized);
                if !normalized.is_empty() {
                    resolved.push(b'/');
                    resolved.extend_from_slice(&normalized);
                }
            }
            return Some(resolved);
        }
        if exists(&current) || is_link(&current) {
            return None;
        }
        if current == b"/" || current == b"." || current.is_empty() {
            return None;
        }
        let (dir, base) = split_path(&current);
        current = dir;
        if rest.is_empty() {
            rest = base;
        } else {
            let mut joined = base;
            joined.push(b'/');
            joined.extend_from_slice(&rest);
            rest = joined;
        }
    }
}

/// GNU `realpath`(既定の `CAN_ALL_BUT_LAST`: 最後の要素のほかは在ること)。
pub fn realpath(p: &[u8]) -> Option<Vec<u8>> {
    if p.is_empty() {
        return None;
    }
    let mut pending: Vec<u8> = if p.first() == Some(&b'/') {
        p.to_vec()
    } else {
        let mut cwd = getcwd()?;
        cwd.push(b'/');
        cwd.extend_from_slice(p);
        cwd
    };
    let mut resolved: Vec<u8> = Vec::new();
    let mut links = 0usize;
    loop {
        // 次の要素を取り出す。
        let trimmed_start = pending
            .iter()
            .position(|b| *b != b'/')
            .unwrap_or(pending.len());
        let rest = pending.get(trimmed_start..).unwrap_or_default().to_vec();
        if rest.is_empty() {
            break;
        }
        let end = rest.iter().position(|b| *b == b'/').unwrap_or(rest.len());
        let component = rest.get(..end).unwrap_or_default().to_vec();
        let after = rest.get(end..).unwrap_or_default().to_vec();
        pending = after.clone();
        match component.as_slice() {
            b"." => continue,
            b".." => {
                if let Some(slash) = resolved.iter().rposition(|b| *b == b'/') {
                    resolved.truncate(slash);
                }
                continue;
            }
            _ => {}
        }
        let mut candidate = resolved.clone();
        candidate.push(b'/');
        candidate.extend_from_slice(&component);
        let more = after.iter().any(|b| *b != b'/');
        let trailing_slash = !after.is_empty();
        match fs::symlink_metadata(path(&candidate)) {
            Err(error) => {
                if error.kind() == std::io::ErrorKind::NotFound && !more {
                    resolved = candidate;
                    // 最後の要素は無くてよい(末尾の `/` があっても同じ)。
                    break;
                }
                return None;
            }
            Ok(meta) if meta.file_type().is_symlink() => {
                links += 1;
                if links > 40 {
                    return None;
                }
                let target = fs::read_link(path(&candidate)).ok()?;
                let target = target.as_os_str().as_bytes();
                let mut next = target.to_vec();
                next.extend_from_slice(&after);
                if target.first() == Some(&b'/') {
                    resolved.clear();
                }
                pending = next;
            }
            Ok(meta) => {
                if !meta.is_dir() && trailing_slash {
                    return None;
                }
                resolved = candidate;
            }
        }
    }
    if resolved.is_empty() {
        resolved = b"/".to_vec();
    }
    Some(resolved)
}

/// `stat -c %a`(8 進の mode の字面。リンクを辿らない)。
pub fn mode_text(p: &[u8]) -> Result<Vec<u8>, i32> {
    fs::symlink_metadata(path(p))
        .map(|m| format!("{:o}", m.mode() & 0o7777).into_bytes())
        .map_err(|e| e.raw_os_error().unwrap_or(os::errno::EIO))
}

/// `stat -c %s`(リンクを辿らない)。
pub fn size_of(p: &[u8]) -> Result<u64, i32> {
    fs::symlink_metadata(path(p))
        .map(|m| m.len())
        .map_err(|e| e.raw_os_error().unwrap_or(os::errno::EIO))
}

/// `stat -c '%d:%i'`(リンクを辿らない)。
pub fn identity(p: &[u8]) -> Option<(u64, u64)> {
    fs::symlink_metadata(path(p))
        .ok()
        .map(|m| (m.dev(), m.ino()))
}

/// bash の `file_status` の結果。
#[derive(Clone, Copy, PartialEq, Eq)]
struct Status {
    exists: bool,
    directory: bool,
    executable: bool,
}

fn file_status(p: &[u8]) -> Status {
    match fs::metadata(path(p)) {
        Err(_) => Status {
            exists: false,
            directory: false,
            executable: false,
        },
        Ok(meta) if meta.is_dir() => Status {
            exists: true,
            directory: true,
            executable: false,
        },
        Ok(_) => Status {
            exists: true,
            directory: false,
            executable: os::effective_executable(p),
        },
    }
}

/// bash の既定の `PATH`(PATH が無い起動で bash が置く値。子へは渡らない)。
pub const DEFAULT_PATH: &[u8] = b"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

/// bash 5.2.21 の `command -v <name>`(`describe_command` の `CDESC_REUSABLE`。ハッシュは空)。
/// `path_list` は bash の `PATH` の値、`cwd` は bash の現在のフォルダ(`MP_DOCWD`)。
pub fn command_v(name: &[u8], path_list: &[u8], cwd: Option<&[u8]>) -> Option<Vec<u8>> {
    let full = if path_list.is_empty() {
        name.to_vec()
    } else {
        find_in_path(name, path_list)?
    };
    if full == name {
        let status = file_status(&full);
        if !status.executable {
            return None;
        }
        if full.first() == Some(&b'/') {
            return Some(full);
        }
        let mut made = match cwd {
            Some(dir) => dir.to_vec(),
            None => b".".to_vec(),
        };
        if made.last() != Some(&b'/') {
            made.push(b'/');
        }
        made.extend_from_slice(&full);
        return Some(made);
    }
    Some(full)
}

/// `find_user_command_in_path`(`FS_EXEC_PREFERRED|FS_NODIRS`)。
fn find_in_path(name: &[u8], path_list: &[u8]) -> Option<Vec<u8>> {
    let mut lose_on: Option<Vec<u8>> = None;
    let mut index = 0usize;
    let length = path_list.len();
    while index < length {
        // extract_colon_unit
        if index > 0 && path_list.get(index) == Some(&b':') {
            index += 1;
        }
        let start = index;
        while index < length && path_list.get(index) != Some(&b':') {
            index += 1;
        }
        let mut element = path_list.get(start..index).unwrap_or_default().to_vec();
        if start == index && index < length {
            index += 1;
        }
        if element.is_empty() {
            element = b".".to_vec();
        }
        // sh_makepath(element, name, 0)
        let mut full = element.clone();
        if full.last() != Some(&b'/') {
            full.push(b'/');
        }
        full.extend_from_slice(name);
        let status = file_status(&full);
        if !status.exists {
            continue;
        }
        if status.executable {
            return Some(full);
        }
        if lose_on.is_none() {
            lose_on = Some(full.clone());
        }
    }
    let candidate = lose_on?;
    if is_dir(&candidate) {
        return None;
    }
    Some(candidate)
}

/// `find_user_command`(PATH から、実行できるものを先に探す)。
pub fn find_command(name: &[u8], path_list: &[u8]) -> Option<Vec<u8>> {
    find_in_path(name, path_list)
}

/// 現在のフォルダの名前が、実行できるファイルか(`file_status` の `FS_EXECABLE`)。
pub fn executable_file(name: &[u8]) -> bool {
    file_status(name).executable
}

/// リンクを辿った (device, inode)(`same_file`)。
pub fn identity_follow(p: &[u8]) -> Option<(u64, u64)> {
    fs::metadata(path(p)).ok().map(|m| (m.dev(), m.ino()))
}
