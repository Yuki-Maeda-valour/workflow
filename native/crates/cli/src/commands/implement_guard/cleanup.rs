//! `cleanup` :3041-3053、所属の印の削除(`remove_owner_mark` :2521-2543・`name_absent` :2362-2372)と、
//! `rm -rf` の再現(GNU rm 9.4 の fts の物理の走査: リンクを辿らない・フォルダごとに中身を先に
//! 全部読み、readdir の順に消す・子を消してから親・消せなかった物の先祖は黙って飛ばす)。
use super::paths;
use super::shell::{Flow, Shell};
use super::sites;
use super::state::{check_state_dir, fail_internal, fail_usage, init_excl};
use super::text::{path_field, quoteaf};
use super::{Context, prelude_tools};
use dev_workflow_platform::implement_guard as os;
use std::ffi::OsStr;
use std::fs;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

fn path(bytes: &[u8]) -> &Path {
    Path::new(OsStr::from_bytes(bytes))
}

fn errno_of(error: &std::io::Error) -> i32 {
    error.raw_os_error().unwrap_or(os::errno::EIO)
}

/// `rm: cannot remove '<path>': <strerror>`。rm が文の書き込みで信号を受けて終わったら true
/// (残りは消さない)。
fn cannot_remove(shell: &mut Shell, target: &[u8], errno: i32) -> bool {
    let mut text = b"cannot remove ".to_vec();
    text.extend_from_slice(&quoteaf(target));
    shell.tool_error(b"rm", &text, Some(errno)).is_some()
}

/// rm が信号で終わったことを、走査の外へ伝える印。
struct Died;

/// `rm -rf -- <path>`。全部消せたら true。GNU rm 9.4 の fts と同じく、フォルダは親の fd から
/// リンクを辿らずに開き(`O_NOFOLLOW|O_DIRECTORY`)、開いたものが lstat の device・inode と同じかを
/// 確かめてから辿る。消すのも親の fd からの `unlinkat`。文に出すパスは、引数からつないだ字面。
pub fn remove_tree(shell: &mut Shell, target: &[u8]) -> bool {
    remove_root(shell, target).unwrap_or(false)
}

fn not_found(error: &std::io::Error) -> bool {
    error.kind() == std::io::ErrorKind::NotFound
}

fn remove_root(shell: &mut Shell, target: &[u8]) -> Result<bool, Died> {
    Ok(match os::tree_lstat(None, target) {
        Err(error) if not_found(&error) => true,
        Err(error) => {
            // FTS_NS: unlinkat を試す。
            match os::tree_unlink(None, target, false) {
                Ok(()) => true,
                Err(e) if not_found(&e) => true,
                Err(_) => {
                    if cannot_remove(shell, target, errno_of(&error)) {
                        return Err(Died);
                    }
                    false
                }
            }
        }
        Ok(info) if info.directory => remove_directory(shell, None, target, target, info)?,
        Ok(_) => unlink(shell, None, target, target)?,
    })
}

/// 差し込み点の名前(`<地点>:<末尾の要素>`)。
fn point_name(point: &str, target: &[u8]) -> String {
    let base = target.rsplit(|b| *b == b'/').next().unwrap_or(target);
    format!("{point}:{}", String::from_utf8_lossy(base))
}

/// `parent`(無ければ現在のフォルダ)の `name` を消す。`shown` は文に出すパス。
fn unlink(
    shell: &mut Shell,
    parent: Option<BorrowedFd<'_>>,
    name: &[u8],
    shown: &[u8],
) -> Result<bool, Died> {
    let injected = shell.point(&point_name("fail:rm-unlink", shown));
    let result = match injected.filter(|e| *e > 0) {
        Some(errno) => Err(std::io::Error::from_raw_os_error(errno)),
        None => os::tree_unlink(parent, name, false),
    };
    Ok(match result {
        Ok(()) => true,
        Err(error) if not_found(&error) => true,
        Err(error) => {
            if cannot_remove(shell, shown, errno_of(&error)) {
                return Err(Died);
            }
            false
        }
    })
}

/// 開いたフォルダと、その中身(名前と種類)。
type Listing = (OwnedFd, Vec<(Vec<u8>, os::TreeKind)>);

/// フォルダを開いて中身を全部読む。開いたものが lstat の番号と違えば(走査の間に差し替えられた)、
/// 辿らずに無いものとして扱う(fts は番号が違えば辿らない)。
fn open_listing(
    parent: Option<BorrowedFd<'_>>,
    name: &[u8],
    expected: os::TreeStat,
) -> std::io::Result<Listing> {
    let fd = os::tree_open_dir(parent, name)?;
    let opened = os::tree_fstat(fd.as_fd())?;
    if !opened.directory || (opened.dev, opened.ino) != (expected.dev, expected.ino) {
        return Err(std::io::Error::from_raw_os_error(os::errno::ENOENT));
    }
    let names = os::tree_list(fd.as_fd())?;
    Ok((fd, names))
}

/// フォルダを中から消す。読めないフォルダ(FTS_DNR)は、空なら消せ、空でなければ開けなかった番号で出す。
fn remove_directory(
    shell: &mut Shell,
    parent: Option<BorrowedFd<'_>>,
    name: &[u8],
    shown: &[u8],
    expected: os::TreeStat,
) -> Result<bool, Died> {
    let (fd, names) = match open_listing(parent, name, expected) {
        Ok(listing) => listing,
        Err(error) => {
            let open_errno = errno_of(&error);
            return Ok(match os::tree_unlink(parent, name, true) {
                Ok(()) => true,
                Err(e) if not_found(&e) => true,
                Err(e) => {
                    let errno = errno_of(&e);
                    let replaced = if [
                        os::errno::ENOTEMPTY,
                        os::errno::EISDIR,
                        os::errno::ENOTDIR,
                        os::errno::EEXIST,
                    ]
                    .contains(&errno)
                    {
                        open_errno
                    } else {
                        errno
                    };
                    if cannot_remove(shell, shown, replaced) {
                        return Err(Died);
                    }
                    false
                }
            });
        }
    };
    let mut failed = false;
    for (child, kind) in names {
        let mut child_shown = shown.to_vec();
        child_shown.push(b'/');
        child_shown.extend_from_slice(&child);
        // fts は種類が DT_DIR か分からない子を lstat する。
        let info = match kind {
            os::TreeKind::Other => None,
            os::TreeKind::Directory | os::TreeKind::Unknown => {
                match os::tree_lstat(Some(fd.as_fd()), &child) {
                    Ok(info) => Some(info),
                    Err(error) if not_found(&error) && kind == os::TreeKind::Directory => continue,
                    Err(_) => None,
                }
            }
        };
        let ok = match info {
            Some(info) if info.directory => {
                remove_directory(shell, Some(fd.as_fd()), &child, &child_shown, info)?
            }
            _ => unlink(shell, Some(fd.as_fd()), &child, &child_shown)?,
        };
        if !ok {
            failed = true;
        }
    }
    drop(fd);
    if failed {
        return Ok(false);
    }
    let injected = shell.point(&point_name("fail:rm-rmdir", shown));
    let result = match injected.filter(|e| *e > 0) {
        Some(errno) => Err(std::io::Error::from_raw_os_error(errno)),
        None => os::tree_unlink(parent, name, true),
    };
    Ok(match result {
        Ok(()) => true,
        Err(error) if not_found(&error) => true,
        Err(error) => {
            if cannot_remove(shell, shown, errno_of(&error)) {
                return Err(Died);
            }
            false
        }
    })
}

/// `owner_mark_of`(コマンド置換なので、末尾の改行は落ちる)。
fn owner_mark_of(state: &[u8]) -> Vec<u8> {
    let slash = state.iter().rposition(|b| *b == b'/');
    let parent = match slash {
        Some(index) => state.get(..index).unwrap_or_default(),
        None => state,
    };
    // `${1##*/guard-}`: 最後の `/guard-` より後ろ。
    let suffix = find_last(state, b"/guard-")
        .map(|index| state.get(index + b"/guard-".len()..).unwrap_or_default())
        .unwrap_or(state);
    let mut mark = parent.to_vec();
    mark.extend_from_slice(b"/owner-");
    mark.extend_from_slice(suffix);
    while mark.last() == Some(&b'\n') {
        mark.pop();
    }
    mark
}

fn find_last(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).rposition(|w| w == needle)
}

/// `name_absent`: 親の一覧を読めて、その名前が無いと確かめられた。
fn name_absent(shell: &mut Shell, mark: &[u8]) -> Flow<bool> {
    if paths::exists(mark) || paths::is_link(mark) {
        return Ok(false);
    }
    let slash = mark.iter().rposition(|b| *b == b'/');
    let (parent, base) = match slash {
        Some(index) => (
            mark.get(..index).unwrap_or_default(),
            mark.get(index + 1..).unwrap_or_default(),
        ),
        None => (mark, mark),
    };
    let well_formed = base.starts_with(b"owner-")
        && base.len() > b"owner-".len()
        && base
            .get(b"owner-".len()..)
            .unwrap_or_default()
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
    if !well_formed {
        return Ok(false);
    }
    let injected = shell.point("fail:find");
    let listing = match injected.filter(|e| *e > 0) {
        Some(errno) => Err(std::io::Error::from_raw_os_error(errno)),
        None => fs::read_dir(path(parent)).and_then(|entries| {
            entries
                .map(|e| e.map(|e| e.file_name().as_bytes().to_vec()))
                .collect::<Result<Vec<_>, _>>()
        }),
    };
    shell.tool_point("find")?;
    match listing {
        Ok(names) => Ok(!names.iter().any(|name| name == base)),
        Err(_) => Ok(false),
    }
}

/// `grep -qxF -e <line> -- <file>`(読めなければ偽。NUL を含むファイルは NUL も行の区切りにする)。
fn has_line(shell: &mut Shell, file: &[u8], line: &[u8]) -> Flow<bool> {
    let injected = shell.point("fail:grep");
    let data = match injected.filter(|e| *e > 0) {
        Some(_) => None,
        None => fs::read(path(file)).ok(),
    };
    shell.tool_point("grep")?;
    let Some(data) = data else {
        return Ok(false);
    };
    let binary = data.contains(&0);
    Ok(data
        .split(|b| *b == b'\n' || (binary && *b == 0))
        .any(|candidate| candidate == line))
}

/// `remove_owner_mark`。
fn remove_owner_mark(shell: &mut Shell, state: &[u8], identity: Option<(u64, u64)>) -> Flow<()> {
    let mark = owner_mark_of(state);
    if !paths::exists(&mark) && !paths::is_link(&mark) {
        if name_absent(shell, &mark)? {
            return Ok(());
        }
        let mut text = "ERROR [internal] 保護領域は消したが、所属の印が残ったか確かめられない(機密を含まないので手で消してよい): ".as_bytes().to_vec();
        text.extend_from_slice(&path_field(&mark));
        shell.echo_err(sites::MARK_UNCERTAIN_ECHO, &text)?;
        return Err(shell.finish(20));
    }
    let mut ours = !paths::is_link(&mark) && paths::is_file(&mark);
    if ours {
        match identity {
            None => ours = false,
            Some((dev, ino)) => {
                let dev_line = format!("guard_dev\t{dev}");
                ours = has_line(shell, &mark, dev_line.as_bytes())?;
                if ours {
                    let ino_line = format!("guard_ino\t{ino}");
                    ours = has_line(shell, &mark, ino_line.as_bytes())?;
                }
            }
        }
    }
    if !ours {
        let mut text = "NOTE: 所属の印の名前に、この記録の印と確かめられないものが在る(消さない): "
            .as_bytes()
            .to_vec();
        text.extend_from_slice(&path_field(&mark));
        shell.echo_err(sites::MARK_NOT_OURS_ECHO, &text)?;
        return Ok(());
    }
    let injected = shell.point("fail:rm-mark");
    let removed = match injected.filter(|e| *e > 0) {
        Some(_) => false,
        None => match fs::remove_file(path(&mark)) {
            Ok(()) => true,
            Err(error) => error.kind() == std::io::ErrorKind::NotFound,
        },
    };
    shell.tool_point("rm-mark")?;
    if !removed || !name_absent(shell, &mark)? {
        let mut text = "ERROR [internal] 保護領域は消したが、所属の印を消せない(機密を含まないので手で消してよい): ".as_bytes().to_vec();
        text.extend_from_slice(&path_field(&mark));
        shell.echo_err(sites::MARK_REMOVE_FAIL_ECHO, &text)?;
        return Err(shell.finish(20));
    }
    let mut text = "NOTE: 所属の印を消した: ".as_bytes().to_vec();
    text.extend_from_slice(&path_field(&mark));
    shell.echo_err(sites::MARK_REMOVED_ECHO, &text)?;
    Ok(())
}

/// `cmd_cleanup`。
pub fn run(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    context.excluded = init_excl(&context.places);
    let checked = match check_state_dir(&context.args.state, &context.places, &context.excluded) {
        Ok(checked) => checked,
        Err(_) => {
            let mut text = "--state が保護領域の guard-* でない・無い・symlink(何も消していない): "
                .as_bytes()
                .to_vec();
            text.extend_from_slice(&path_field(&context.args.state));
            return Err(fail_usage(shell, &text));
        }
    };
    let state = checked.state.clone();
    context.checked = Some(checked);
    prelude_tools(shell)?;
    let identity = paths::identity(&state);
    shell.tool_point("stat-identity")?;
    let removed = remove_tree(shell, &state);
    shell.tool_point("rm-state")?;
    if !removed {
        let mut text = "保護領域を消せない: ".as_bytes().to_vec();
        text.extend_from_slice(&path_field(&state));
        return Err(fail_internal(shell, &text));
    }
    let mut text = "NOTE: 保護領域を消した: ".as_bytes().to_vec();
    text.extend_from_slice(&path_field(&state));
    shell.echo_err(sites::CLEANUP_NOTE, &text)?;
    remove_owner_mark(shell, &state, identity)
}
