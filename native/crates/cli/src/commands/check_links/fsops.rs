//! 旧版が pathlib・posixpath を通して行う OS 呼出し(本家 3.10.11 と同じ種類・順・完全なパスの文字列)。
//! - 判定(`is_dir`・`is_file`・`exists`): stat(リンクを辿る)。ENOENT・ENOTDIR・EBADF・ELOOP と NUL は偽。
//! - 再帰の走査(`rglob("*.md")`): `pathlib.py:398-500` の `_Selector.select_from` から。
//! - 読取り(`read_text`): FileIO の open・fstat・read。EINTR は印を見てから繰り返す。
//! - 解決(`resolve()`): `pathlib.py:1064-1090` と `posixpath.py:338-445` の非厳密の動作。
use super::path::PurePath;
use super::posix;
use super::traceback::{Exception, Resolve, Site, Traceback};
use dev_workflow_platform::check_links::{self as os, EntryKind, Kind, Meta, ScandirError};
use std::collections::HashMap;

/// errno の名前ごとの番号(Linux と Mac で違うものだけ OS ごとに分ける)。
pub mod errno {
    pub const EPERM: i32 = 1;
    pub const ENOENT: i32 = 2;
    pub const EINTR: i32 = 4;
    pub const EBADF: i32 = 9;
    pub const EACCES: i32 = 13;
    pub const ENOTDIR: i32 = 20;
    pub const EISDIR: i32 = 21;
    #[cfg(target_os = "macos")]
    pub const EAGAIN: i32 = 35;
    #[cfg(not(target_os = "macos"))]
    pub const EAGAIN: i32 = 11;
    #[cfg(target_os = "macos")]
    pub const ELOOP: i32 = 62;
    #[cfg(not(target_os = "macos"))]
    pub const ELOOP: i32 = 40;
}

/// ディレクトリの深さの再帰の上限(本家 3.10.11 の実測。`_iterate_directories` がこの数だけ続くと、
/// 次の scandir の `__fspath__` で RecursionError になる)。
pub const RGLOB_RECURSION_DEPTH: usize = 989;
/// リンクを辿る深さの再帰の上限(本家 3.10.11 の実測。`_joinrealpath` がこの数だけ続くと、
/// 次の `isabs(rest)` の `_get_sep` で RecursionError になる)。
pub const LINK_RECURSION_DEPTH: usize = 993;

/// 本処理の例外(前の例外から順の呼出履歴の列)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raised(pub Vec<Traceback>);

impl Raised {
    pub fn single(site: Site, exception: Exception) -> Self {
        Self(vec![Traceback {
            frames: site.frames(),
            exception,
        }])
    }

    /// OS エラー。ファイル名は repr して本文に入れる。
    pub fn os(site: Site, errno: i32, filename: Option<&[u8]>) -> Self {
        Self::single(
            site,
            Exception::os(errno, &super::strerror(errno), filename),
        )
    }

    pub fn interrupt(site: Site) -> Self {
        Self::single(site, Exception::keyboard_interrupt())
    }

    /// 最後の例外が KeyboardInterrupt か(終了で自分へ SIGINT を送るか)。
    pub fn is_interrupt(&self) -> bool {
        self.0
            .last()
            .is_some_and(|traceback| traceback.exception.type_name == "KeyboardInterrupt")
    }
}

/// Python の処理の区切りで印を見る(eval loop の CHECK_EVAL_BREAKER)。
pub fn eval_breaker(site: Site) -> Result<(), Raised> {
    if os::take_interrupt() {
        Err(Raised::interrupt(site))
    } else {
        Ok(())
    }
}

/// `_ignore_error`(pathlib)。
fn ignored(errno: i32) -> bool {
    matches!(
        errno,
        errno::ENOENT | errno::ENOTDIR | errno::EBADF | errno::ELOOP
    )
}

/// errno が PermissionError に当たるか。
fn permission(errno: i32) -> bool {
    matches!(errno, errno::EACCES | errno::EPERM)
}

/// `Path.stat()` を使う判定の共通部分。無視する誤りと NUL(ValueError)は None。
fn stat_test(path: &[u8], site: Site) -> Result<Option<Meta>, Raised> {
    if path.contains(&0) {
        return Ok(None);
    }
    let result = os::stat(path);
    eval_breaker(site)?;
    match result {
        Ok(meta) => Ok(Some(meta)),
        Err(errno) if ignored(errno) => Ok(None),
        Err(errno) => Err(Raised::os(site, errno, Some(path))),
    }
}

/// `Path.is_dir()`。
pub fn is_dir(path: &PurePath, site: Site) -> Result<bool, Raised> {
    Ok(stat_test(&path.to_bytes(), site)?.is_some_and(|meta| meta.kind == Kind::Directory))
}

/// `Path.is_file()`。
pub fn is_file(path: &PurePath, site: Site) -> Result<bool, Raised> {
    Ok(stat_test(&path.to_bytes(), site)?.is_some_and(|meta| meta.kind == Kind::Regular))
}

/// `Path.exists()`。
pub fn exists(path: &PurePath, site: Site) -> Result<bool, Raised> {
    Ok(stat_test(&path.to_bytes(), site)?.is_some())
}

/// `os.scandir(path)` の `DirEntry.path`(scandir の引数の文字列と名前を `/` で連結する)。
fn entry_path(dir: &[u8], name: &[u8]) -> Vec<u8> {
    let mut path = dir.to_vec();
    if !path.is_empty() && path.last() != Some(&b'/') {
        path.push(b'/');
    }
    path.extend_from_slice(name);
    path
}

/// `DirEntry.is_dir()` と、真のときの `DirEntry.is_symlink()`(CPython v3.10.11 の posixmodule.c の
/// `DirEntry_test_mode`・`DirEntry_get_stat`・`DirEntry_is_symlink` と同じ OS 呼出し)。
/// 戻り値は (is_dir, is_symlink)。is_symlink は is_dir が真のときだけ意味を持つ。
fn entry_test(path: &[u8], kind: EntryKind) -> Result<(bool, bool), i32> {
    let followed = |path: &[u8]| match os::stat(path) {
        Ok(meta) => Ok(meta.kind == Kind::Directory),
        Err(errno::ENOENT) => Ok(false),
        Err(errno) => Err(errno),
    };
    match kind {
        EntryKind::Directory => Ok((true, false)),
        EntryKind::Other => Ok((false, false)),
        EntryKind::Symlink => Ok((followed(path)?, true)),
        EntryKind::Unknown => match os::lstat(path) {
            // is_symlink の判定の lstat が無いと分かったら、stat の代わりの lstat をもう一度呼ぶ。
            Err(errno::ENOENT) => match os::lstat(path) {
                Ok(meta) => Ok((meta.kind == Kind::Directory, meta.kind == Kind::Symlink)),
                Err(errno::ENOENT) => Ok((false, false)),
                Err(errno) => Err(errno),
            },
            Err(errno) => Err(errno),
            Ok(meta) if meta.kind == Kind::Symlink => Ok((followed(path)?, true)),
            Ok(meta) => Ok((meta.kind == Kind::Directory, false)),
        },
    }
}

/// `Path.rglob("*.md")` の全件(並べる前)。
pub fn rglob(root: &PurePath) -> Result<Vec<PurePath>, Raised> {
    let mut found = Vec::new();
    if is_dir(root, Site::RglobSelectIsDir)? {
        iterate(root, 0, &mut found)?;
    }
    Ok(found)
}

/// `_RecursiveWildcardSelector._iterate_directories`。先に自分を渡して `*.md` を集め、その後に子へ進む。
fn iterate(dir: &PurePath, depth: usize, found: &mut Vec<PurePath>) -> Result<(), Raised> {
    select_md(dir, found)?;
    if depth >= RGLOB_RECURSION_DEPTH {
        return Err(Raised::single(
            Site::RglobRecursion { depth },
            Exception::recursion(false),
        ));
    }
    let path = dir.to_bytes();
    let result = os::scandir(&path);
    eval_breaker(Site::RglobIterateScandir { depth })?;
    let entries = match result {
        Ok(entries) => entries,
        Err(ScandirError::Open(errno) | ScandirError::Read(errno)) if permission(errno) => {
            return Ok(());
        }
        Err(ScandirError::Open(errno) | ScandirError::Read(errno)) => {
            return Err(Raised::os(
                Site::RglobIterateScandir { depth },
                errno,
                Some(&path),
            ));
        }
    };
    for entry in entries {
        let full = entry_path(&path, &entry.name);
        let tested = entry_test(&full, entry.kind);
        eval_breaker(Site::RglobEntryIsDir { depth })?;
        let (is_dir, is_symlink) = match tested {
            Ok(tested) => tested,
            Err(errno) if ignored(errno) => (false, false),
            // PermissionError は、このディレクトリの残りの子への走査を打ち切る。
            Err(errno) if permission(errno) => return Ok(()),
            Err(errno) => {
                return Err(Raised::os(
                    Site::RglobEntryIsDir { depth },
                    errno,
                    Some(&full),
                ));
            }
        };
        if is_dir && !is_symlink {
            iterate(&dir.child(&entry.name), depth + 1, found)?;
        }
    }
    Ok(())
}

/// `_WildcardSelector._select_from`(`*.md` は大文字小文字を区別し、`.` で始まる名前も種類も問わない)。
fn select_md(dir: &PurePath, found: &mut Vec<PurePath>) -> Result<(), Raised> {
    let path = dir.to_bytes();
    let result = os::scandir(&path);
    eval_breaker(Site::RglobWildcardScandir)?;
    let entries = match result {
        Ok(entries) => entries,
        Err(ScandirError::Open(errno) | ScandirError::Read(errno)) if permission(errno) => {
            return Ok(());
        }
        Err(ScandirError::Open(errno) | ScandirError::Read(errno)) => {
            return Err(Raised::os(Site::RglobWildcardScandir, errno, Some(&path)));
        }
    };
    for entry in entries {
        if entry.name.ends_with(b".md") {
            found.push(dir.child(&entry.name));
        }
    }
    Ok(())
}

/// FileIO.readall の読取りの大きさ(`SMALLCHUNK`・`LARGE_BUFFER_CUTOFF_SIZE`・`new_buffersize`)。
const SMALLCHUNK: usize = 8192;
const LARGE_BUFFER_CUTOFF_SIZE: usize = 65536;

fn new_buffersize(current: usize) -> usize {
    let addend = if current > LARGE_BUFFER_CUTOFF_SIZE {
        current >> 3
    } else {
        256 + current
    };
    addend.max(SMALLCHUNK) + current
}

/// `Path.read_text(encoding="utf-8", errors="replace")` の生のバイト列。
/// OS 呼出しは旧版と同じ順(open → fstat → isatty → lseek → lseek → fstat → read… → close)。
pub fn read_bytes(path: &[u8]) -> Result<Vec<u8>, Raised> {
    let file = loop {
        match os::open_read(path) {
            Ok(file) => break file,
            Err(errno::EINTR) => {
                if os::take_interrupt() {
                    return Err(Raised::interrupt(Site::ReadOpen));
                }
            }
            Err(errno) => return Err(Raised::os(Site::ReadOpen, errno, Some(path))),
        }
    };
    eval_breaker(Site::ReadOpen)?;
    // FileIO は開いた実体がディレクトリなら EISDIR にする。fstat の失敗は EBADF だけを誤りにする。
    match file.meta() {
        Ok(meta) if meta.kind == Kind::Directory => {
            return Err(Raised::os(Site::ReadOpen, errno::EISDIR, Some(path)));
        }
        Err(errno::EBADF) => return Err(Raised::os(Site::ReadOpen, errno::EBADF, None)),
        _ => {}
    }
    // _io.open の isatty と BufferedReader の tell(どちらも結果は読取りに効かない)。
    let _ = file.isatty();
    let _ = file.tell();
    eval_breaker(Site::ReadOpen)?;
    // FileIO.readall: 位置と大きさから最初の大きさを決め、足りなければ広げて EOF まで読む。
    let position = file
        .tell()
        .ok()
        .and_then(|position| i64::try_from(position).ok());
    let end = file.meta().map_or(-1, |meta| meta.size);
    let mut size = match position {
        Some(position) if end > 0 && end >= position => {
            usize::try_from(end - position + 1).unwrap_or(SMALLCHUNK)
        }
        _ => SMALLCHUNK,
    };
    let mut data = Vec::new();
    let mut buffer = Vec::new();
    loop {
        if data.len() >= size {
            size = new_buffersize(data.len());
        }
        buffer.resize(size - data.len(), 0);
        match file.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => data.extend_from_slice(&buffer[..count]),
            Err(errno::EINTR) => {
                if os::take_interrupt() {
                    return Err(Raised::interrupt(Site::ReadRead));
                }
            }
            // 待たない読取りは使わないので、EAGAIN は読めた分があるときだけ起きる(readall は読めた分で終える)。
            Err(errno::EAGAIN) if !data.is_empty() => break,
            Err(errno) => return Err(Raised::os(Site::ReadRead, errno, None)),
        }
    }
    drop(file);
    eval_breaker(Site::ReadRead)?;
    Ok(data)
}

/// `Path.resolve()`(非厳密)。
pub fn resolve(path: &PurePath, call: Resolve) -> Result<PurePath, Raised> {
    let filename = path.to_bytes();
    let mut seen = HashMap::new();
    let (joined, _) = joinrealpath(Vec::new(), &filename, &mut seen, call, 0)?;
    let absolute = abspath(&joined, call)?;
    let resolved = PurePath::new(&absolute);
    let text = resolved.to_bytes();
    // 非厳密では realpath が輪で止まらないので、stat して ELOOP なら RuntimeError にする。
    if text.contains(&0) {
        return Err(Raised::single(
            Site::ResolveStatValue(call),
            Exception::embedded_null(),
        ));
    }
    let result = os::stat(&text);
    eval_breaker(Site::ResolveStatValue(call))?;
    if result == Err(errno::ELOOP) {
        return Err(Raised(vec![
            Traceback {
                frames: Site::ResolveStat.frames(),
                exception: Exception::os(errno::ELOOP, &super::strerror(errno::ELOOP), Some(&text)),
            },
            Traceback {
                frames: Site::ResolveLoop(call).frames(),
                exception: Exception::symlink_loop(&text),
            },
        ]));
    }
    Ok(resolved)
}

/// `posixpath._joinrealpath(path, rest, False, seen)`。戻り値の真偽は輪に当たらなかったか。
fn joinrealpath(
    mut path: Vec<u8>,
    rest: &[u8],
    seen: &mut HashMap<Vec<u8>, Option<Vec<u8>>>,
    call: Resolve,
    depth: usize,
) -> Result<(Vec<u8>, bool), Raised> {
    if depth >= LINK_RECURSION_DEPTH {
        return Err(Raised::single(
            Site::ResolveRecursion { call, depth },
            Exception::recursion(true),
        ));
    }
    let mut rest = rest;
    if posix::isabs(rest) {
        rest = &rest[1..];
        path = b"/".to_vec();
    }
    while !rest.is_empty() {
        let (name, tail) = match rest.iter().position(|&byte| byte == b'/') {
            Some(at) => (&rest[..at], &rest[at + 1..]),
            None => (rest, &rest[rest.len()..]),
        };
        rest = tail;
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if path.is_empty() {
                path = b"..".to_vec();
            } else {
                let (head, last) = posix::split(&path);
                path = if last == b".." {
                    posix::join(&head, &[b"..", b".."])
                } else {
                    head
                };
            }
            continue;
        }
        let newpath = posix::join(&path, &[name]);
        if newpath.contains(&0) {
            return Err(Raised::single(
                Site::ResolveLstat { call, depth },
                Exception::embedded_null(),
            ));
        }
        let is_link = matches!(os::lstat(&newpath), Ok(meta) if meta.kind == Kind::Symlink);
        eval_breaker(Site::ResolveLstat { call, depth })?;
        if !is_link {
            path = newpath;
            continue;
        }
        if let Some(known) = seen.get(&newpath) {
            match known {
                Some(resolved) => {
                    path = resolved.clone();
                    continue;
                }
                // 未解決のリンクへ戻った: 輪なので、残りを解決せずに返す。
                None => return Ok((posix::join(&newpath, &[rest]), false)),
            }
        }
        seen.insert(newpath.clone(), None);
        let target = match os::readlink(&newpath) {
            Ok(target) => target,
            Err(errno) => {
                return Err(Raised::os(
                    Site::ResolveReadlink { call, depth },
                    errno,
                    Some(&newpath),
                ));
            }
        };
        let (resolved, ok) = joinrealpath(path, &target, seen, call, depth + 1)?;
        path = resolved;
        if !ok {
            return Ok((posix::join(&path, &[rest]), false));
        }
        seen.insert(newpath, Some(path.clone()));
    }
    Ok((path, true))
}

/// `posixpath.abspath`(相対なら作業場所と連結し、最後に normpath)。
fn abspath(path: &[u8], call: Resolve) -> Result<Vec<u8>, Raised> {
    let joined = if posix::isabs(path) {
        path.to_vec()
    } else {
        let cwd =
            os::getcwd().map_err(|errno| Raised::os(Site::ResolveGetcwd(call), errno, None))?;
        posix::join(&cwd, &[path])
    };
    Ok(posix::normpath(&joined))
}
