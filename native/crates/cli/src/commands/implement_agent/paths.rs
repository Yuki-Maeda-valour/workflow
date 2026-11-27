//! パスの判定と論理の cwd: bash の `[ -f ]`・`[ -s ]`・`[ -d ]`・`[ -x ]`・`[ -e ]`・`[ -L ]`(物理で辿る)、
//! 論理の `cd`(`cd_builtin`・`change_to_directory`・`sh_canonpath`)、`pwd -P`、コマンド置換の中の
//! `real_dir`(699 行)、bash のコマンドの探し方(`search_for_command`)。Rust は実際の cwd を移さない。
use dev_workflow_platform::implement_agent as os;
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

fn path(bytes: &[u8]) -> &Path {
    Path::new(OsStr::from_bytes(bytes))
}

/// `[ -f ]`(リンクを辿る)。
pub fn is_file(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok_and(|m| m.is_file())
}

/// `[ -d ]`。
pub fn is_dir(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok_and(|m| m.is_dir())
}

/// `[ -e ]`。
pub fn exists(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok()
}

/// `[ -s ]`(大きさが 0 より大きい)。
pub fn non_empty(p: &[u8]) -> bool {
    !p.is_empty() && fs::metadata(path(p)).is_ok_and(|m| m.size() > 0)
}

/// `[ -L ]`。
pub fn is_link(p: &[u8]) -> bool {
    !p.is_empty() && fs::symlink_metadata(path(p)).is_ok_and(|m| m.file_type().is_symlink())
}

/// `[ -x ]`(実効の uid)。
pub fn executable(p: &[u8]) -> bool {
    !p.is_empty() && os::effective_access(p, os::Access::Execute)
}

/// 現在のフォルダの物理パス(`getcwd`)。失敗の番号も返す。
pub fn getcwd() -> Result<Vec<u8>, i32> {
    std::env::current_dir()
        .map(|p| p.as_os_str().as_bytes().to_vec())
        .map_err(|e| e.raw_os_error().unwrap_or(os::errno::ENOENT))
}

/// リンクを辿った (device, inode)(`same_file`)。
pub fn identity_follow(p: &[u8]) -> Option<(u64, u64)> {
    fs::metadata(path(p)).ok().map(|m| (m.dev(), m.ino()))
}

/// bash の `sh_physpath`(リンクを辿って解いた絶対パス。最後の要素まで在ること)。
pub fn physpath(p: &[u8]) -> Option<Vec<u8>> {
    if p.is_empty() {
        return None;
    }
    fs::canonicalize(path(p))
        .ok()
        .map(|r| r.as_os_str().as_bytes().to_vec())
}

/// bash の `sh_canonpath(path, PATH_CHECKDOTDOT|PATH_CHECKEXISTS)`: 字面で `.` と `..` と重なった `/`
/// を消す。`..` の前はフォルダであること、結果が在ることを確かめる。先頭のちょうど 2 つの `/` は残す。
pub fn canonpath(p: &[u8]) -> Option<Vec<u8>> {
    if p.first() != Some(&b'/') {
        return None;
    }
    let double = p.starts_with(b"//") && !p.starts_with(b"///");
    let mut parts: Vec<&[u8]> = Vec::new();
    for segment in p.split(|b| *b == b'/') {
        match segment {
            b"" | b"." => {}
            b".." => {
                // PATH_CHECKDOTDOT: `..` の前までの字面がフォルダであること。
                let prefix = join(double, &parts);
                if !is_dir(&prefix) {
                    return None;
                }
                parts.pop();
            }
            other => parts.push(other),
        }
    }
    let out = join(double, &parts);
    // PATH_CHECKEXISTS。
    if !exists(&out) {
        return None;
    }
    Some(out)
}

fn join(double: bool, parts: &[&[u8]]) -> Vec<u8> {
    let mut out = if double { b"/".to_vec() } else { Vec::new() };
    for part in parts {
        out.push(b'/');
        out.extend_from_slice(part);
    }
    if out.is_empty() || out == b"/" {
        return if double {
            b"//".to_vec()
        } else {
            b"/".to_vec()
        };
    }
    out
}

/// bash の `make_absolute`(相対なら論理の cwd に `/` でつなぐ)。
fn make_absolute(dir: &[u8], cwd: Option<&[u8]>) -> Vec<u8> {
    if dir.first() == Some(&b'/') {
        return dir.to_vec();
    }
    let mut out = cwd.map(<[u8]>::to_vec).unwrap_or_default();
    if out.last() != Some(&b'/') {
        out.push(b'/');
    }
    out.extend_from_slice(dir);
    out
}

/// bash の論理の cwd(`the_current_working_directory`)と、`cd` が見る変数。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Logical {
    /// `the_current_working_directory`(起動で決められなければ無し)。
    pub cwd: Option<Vec<u8>>,
    /// `HOME` の値(変数が無ければ無し)。
    pub home: Option<Vec<u8>>,
    /// `OLDPWD` の値(起動時にフォルダでなければ、値の無い変数)。
    pub oldpwd: Option<Vec<u8>>,
}

/// `cd` の結果。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Cd {
    /// 成功したか(終了値 0)。
    pub ok: bool,
    /// stdout へ出したもの(`cd -` の行き先・`cd --help` の文)。
    pub stdout: Vec<u8>,
    /// 移った後の論理の cwd。
    pub cwd: Option<Vec<u8>>,
}

/// 論理の `cd <arg>`(`-P` の無い `cd "$1"`。CDPATH は 107 行で消してある)。`cwd` は移る前の
/// 論理の cwd。Rust は実際の cwd を移さず、移れるかを確かめる(相対の字面は起動時の cwd の下)。
pub fn cd(state: &Logical, arg: &[u8]) -> Cd {
    let fail = || Cd {
        ok: false,
        stdout: Vec::new(),
        cwd: state.cwd.clone(),
    };
    // internal_getopt(list, "eLP"): `-` だけと、`-` で始まらない字は選択肢ではない。
    let mut no_symlinks = false;
    let mut operand: Option<&[u8]> = Some(arg);
    if arg == b"--" {
        operand = None;
    } else if arg == b"--help" {
        // CASE_HELPOPT: builtin_help の後に EX_USAGE。
        return Cd {
            ok: false,
            stdout: Vec::new(),
            cwd: state.cwd.clone(),
        };
    } else if arg.len() > 1 && arg.first() == Some(&b'-') {
        for &ch in arg.get(1..).unwrap_or_default() {
            match ch {
                b'P' => no_symlinks = true,
                b'L' => no_symlinks = false,
                b'e' => {}
                _ => return fail(),
            }
        }
        operand = None;
    }
    let mut print = false;
    let dirname: Vec<u8> = match operand {
        None => match &state.home {
            Some(home) => home.clone(),
            None => return fail(),
        },
        Some(b"-") => match &state.oldpwd {
            Some(old) => {
                print = true;
                old.clone()
            }
            None => return fail(),
        },
        Some(other) => other.to_vec(),
    };
    let Some(cwd) = change_to_directory(state, &dirname, no_symlinks) else {
        return fail();
    };
    let mut stdout = Vec::new();
    if print {
        stdout.extend_from_slice(&dirname);
        stdout.push(b'\n');
    }
    Cd {
        ok: true,
        stdout,
        cwd: Some(cwd),
    }
}

/// 移れるか(`chdir` が通るか)。
fn can_chdir(p: &[u8]) -> bool {
    if p.is_empty() {
        // chdir("") は ENOENT。ただし `cd ""` は make_absolute で論理の cwd になる。
        return false;
    }
    is_dir(p) && executable(p)
}

/// `change_to_directory`(移った後の論理の cwd を返す)。
fn change_to_directory(state: &Logical, newdir: &[u8], nolinks: bool) -> Option<Vec<u8>> {
    let t = make_absolute(newdir, state.cwd.as_deref());
    let canon = if nolinks { physpath(&t) } else { canonpath(&t) };
    let (tdir, canon_failed) = match canon {
        Some(c) if !c.is_empty() => (c, false),
        _ => (t.clone(), true),
    };
    let first = if nolinks {
        newdir.to_vec()
    } else {
        tdir.clone()
    };
    let target = if newdir.is_empty() && !nolinks {
        tdir.clone()
    } else {
        first
    };
    if can_chdir(&target) {
        if canon_failed {
            return Some(physpath(&target).unwrap_or(tdir));
        }
        return Some(tdir);
    }
    if nolinks {
        return None;
    }
    // 字面のまま移り直す(POSIX モードでないとき)。
    if can_chdir(newdir) {
        return Some(physpath(newdir).unwrap_or(tdir));
    }
    None
}

/// `pwd -P`(`sh_physpath(the_current_working_directory)`。無いか解けなければ getcwd)。
/// 失敗の番号は getcwd のもの(bash は `pwd: error retrieving current directory: …` を出す)。
pub fn pwd_physical(state: &Logical) -> Result<Vec<u8>, i32> {
    if let Some(cwd) = &state.cwd
        && let Some(resolved) = physpath(cwd)
    {
        return Ok(resolved);
    }
    getcwd()
}

/// 699 行の `real_dir`: `$( ( cd "$1" 2>/dev/null && pwd -P ) || return 1 )` の値(末尾の改行を
/// 除いたもの)。失敗なら `None`。
pub fn real_dir(state: &Logical, arg: &[u8]) -> Option<Vec<u8>> {
    let moved = cd(state, arg);
    if !moved.ok {
        return None;
    }
    let after = Logical {
        cwd: moved.cwd.clone(),
        ..state.clone()
    };
    let resolved = pwd_physical(&after).ok()?;
    let mut out = moved.stdout;
    out.extend_from_slice(&resolved);
    out.push(b'\n');
    Some(strip_newlines(out))
}

/// コマンド置換の末尾の改行を全部消す。
pub fn strip_newlines(mut out: Vec<u8>) -> Vec<u8> {
    while out.last() == Some(&b'\n') {
        out.pop();
    }
    out
}

/// bash の既定の `PATH`(PATH が無い起動で bash が置く値。子へは渡らない)。
pub const DEFAULT_PATH: &[u8] = b"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

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
            executable: os::effective_access(p, os::Access::Execute),
        },
    }
}

/// bash の `search_for_command`(ハッシュは空)。`path_list` は bash の `PATH` の値(受け継がなければ
/// 既定の値)、`home` は `HOME` の値(要素の先頭の `~` を展開する)。PATH が空なら名前のまま。
/// 見つからなければ `None`(bash は `command not found` で 127)。
pub fn search_for_command(name: &[u8], path_list: &[u8], home: Option<&[u8]>) -> Option<Vec<u8>> {
    if name.contains(&b'/') {
        return Some(name.to_vec());
    }
    if path_list.is_empty() {
        return Some(name.to_vec());
    }
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
        if element.first() == Some(&b'~') {
            element = tilde(&element, home);
        }
        let mut full = element.clone();
        if full.last() != Some(&b'/') {
            full.push(b'/');
        }
        full.extend_from_slice(name);
        let status = file_status(&full);
        if !status.exists || status.directory {
            continue;
        }
        if status.executable {
            return Some(full);
        }
        if lose_on.is_none() {
            lose_on = Some(full);
        }
    }
    lose_on
}

/// PATH の要素の先頭の `~`・`~/…` を `HOME` で展開する(`~利用者` はそのまま)。
fn tilde(element: &[u8], home: Option<&[u8]>) -> Vec<u8> {
    let rest = element.get(1..).unwrap_or_default();
    if (rest.is_empty() || rest.first() == Some(&b'/'))
        && let Some(home) = home
    {
        let mut out = home.to_vec();
        out.extend_from_slice(rest);
        return out;
    }
    element.to_vec()
}
