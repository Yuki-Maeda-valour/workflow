//! 上限付きの読み取り。旧版 `diagnostic-runtime.py` の `Reader`(:89-209)と、手順・予算を確かめる位置・
//! 記録の順まで 1 対 1 で合わせる。元データへは書かず、開く・調べる・読む・一覧を読む・閉じるだけを行う。
//! リンクは辿らない(各段を `O_NOFOLLOW` で開き、最後の名前は `AT_SYMLINK_NOFOLLOW` で調べる)。
//!
//! OS の呼出しは、このモジュールの継ぎ目 [`Seam`] を通す。製品の実装は `platform::diagnostic` を呼び、
//! 旧版と同じく開く・`fstat`・読むだけを EINTR で呼び直す(`fstatat`・閉じる・一覧・作業場所は呼び直さない)。
//! 検査のときだけ、継ぎ目に故障を足せる(`#[cfg(test)]`)。製品の経路から故障を起こす手段は無い。
use super::budget::{Budget, Failure, Kind};
use super::json::{Fingerprint, digest, fingerprint};
use dev_workflow_platform::diagnostic::{self as os, DiagError, NamesError, Stat};
use std::collections::HashMap;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};

/// `read` の既定の上限(16MiB)。
pub const DEFAULT_READ_LIMIT: u64 = 16 * 1024 * 1024;
/// `read` の既定の量(`management_copy`)。
pub const DEFAULT_READ_KIND: Kind = Kind::ManagementCopy;
/// `names` の既定の量(`management_entries`)。
pub const DEFAULT_NAMES_KIND: Kind = Kind::ManagementEntries;
/// `owner` の既定の終了値(22)。
pub const DEFAULT_OWNER_CODE: u8 = 22;
/// 1 回に読む最大のバイト数。
const CHUNK: u64 = 65536;

/// Reader の失敗。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReaderError {
    /// 予算と Reader の失敗(理由と終了値。旧版の `Failure`)。
    Failure(Failure),
    /// OS の失敗(errno の番号。旧版の `OSError`)。
    Os(i32),
    /// パスの段に NUL がある(旧版の `ValueError`)。
    Nul,
}

impl ReaderError {
    /// 旧版の `FileNotFoundError`(errno が ENOENT の `OSError`)か。
    pub fn is_not_found(&self) -> bool {
        *self == ReaderError::Os(os::ENOENT)
    }
}

impl From<Failure> for ReaderError {
    fn from(failure: Failure) -> Self {
        ReaderError::Failure(failure)
    }
}

impl From<DiagError> for ReaderError {
    fn from(error: DiagError) -> Self {
        match error {
            DiagError::Os(number) => ReaderError::Os(number),
            DiagError::Nul => ReaderError::Nul,
        }
    }
}

fn source_changed() -> ReaderError {
    Failure::new("source-changed", 20).into()
}

/// `directory` が返す、開いたフォルダ。呼出元が閉じる(閉じなければ `Drop` で閉じる)。
#[derive(Debug)]
pub struct Dir(OwnedFd);

impl Dir {
    pub fn as_fd(&self) -> BorrowedFd<'_> {
        self.0.as_fd()
    }

    /// 閉じて、閉じる失敗を OS の失敗として返す(旧版の呼出元の `os.close(fd)`)。
    pub fn close(self) -> Result<(), ReaderError> {
        Ok(os::close(self.0)?)
    }
}

/// フォルダの記録(dev・ino・`S_IFMT(mode)`)。
pub type Identity = (i64, u64, u32);
/// ファイルの記録(6 欄と内容の sha256)。
pub type FileRecord = (Fingerprint, String);

/// 入れた順を保つ記録。同じ鍵に入れ直すと、位置を保って値を置き換える。
#[derive(Clone, Debug)]
pub struct Records<V> {
    entries: Vec<(Vec<u8>, V)>,
    index: HashMap<Vec<u8>, usize>,
}

impl<V> Default for Records<V> {
    fn default() -> Self {
        Self {
            entries: Vec::new(),
            index: HashMap::new(),
        }
    }
}

impl<V: Clone> Records<V> {
    pub fn get(&self, key: &[u8]) -> Option<&V> {
        self.index
            .get(key)
            .map(|&position| &self.entries[position].1)
    }

    pub fn contains(&self, key: &[u8]) -> bool {
        self.index.contains_key(key)
    }

    fn insert(&mut self, key: &[u8], value: V) {
        if let Some(&position) = self.index.get(key) {
            self.entries[position].1 = value;
            return;
        }
        self.index.insert(key.to_vec(), self.entries.len());
        self.entries.push((key.to_vec(), value));
    }

    /// 入れた順の鍵と値。
    pub fn iter(&self) -> impl Iterator<Item = (&[u8], &V)> {
        self.entries
            .iter()
            .map(|(key, value)| (key.as_slice(), value))
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn snapshot(&self) -> Vec<(Vec<u8>, V)> {
        self.entries.clone()
    }
}

// ---------------------------------------------------------------- パスの組み立て(本家 3.10.11 の posixpath の bytes)

/// `os.path.join(a, b)`(2 つの引数)。
fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    if b.starts_with(b"/") {
        return b.to_vec();
    }
    let mut path = a.to_vec();
    if !path.is_empty() && !path.ends_with(b"/") {
        path.push(b'/');
    }
    path.extend_from_slice(b);
    path
}

/// `os.path.normpath(path)`(先頭がちょうど 2 つの `/` なら 2 つ残す)。
pub fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.starts_with(b"/"));
    if initial == 1 && path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|b| *b == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part != b".." || (initial == 0 && parts.is_empty()) || parts.last() == Some(&&b".."[..])
        {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let mut out = b"/".repeat(initial);
    out.extend_from_slice(&parts.join(&b'/'));
    if out.is_empty() { b".".to_vec() } else { out }
}

/// `os.path.dirname(p)`。
pub fn dirname(path: &[u8]) -> &[u8] {
    let split = path.iter().rposition(|b| *b == b'/').map_or(0, |i| i + 1);
    let head = &path[..split];
    if !head.is_empty() && head.iter().any(|b| *b != b'/') {
        let end = head.iter().rposition(|b| *b != b'/').map_or(0, |i| i + 1);
        &head[..end]
    } else {
        head
    }
}

/// `os.path.basename(p)`。
pub fn basename(path: &[u8]) -> &[u8] {
    let split = path.iter().rposition(|b| *b == b'/').map_or(0, |i| i + 1);
    &path[split..]
}

// ---------------------------------------------------------------- OS の継ぎ目

/// Reader が OS を呼ぶ唯一の継ぎ目。
#[derive(Default)]
struct Seam {
    #[cfg(test)]
    faults: Faults,
}

/// 旧版の `os.open`・`os.fstat`・`os.read`(本家 3.10.11 の `os_open_impl`・`os_fstat_impl`・`_Py_read`)は
/// EINTR で呼び直す。
fn retry<T>(mut call: impl FnMut() -> Result<T, ReaderError>) -> Result<T, ReaderError> {
    loop {
        match call() {
            Err(ReaderError::Os(number)) if number == os::EINTR => continue,
            other => return other,
        }
    }
}

impl Seam {
    fn open_root(&mut self) -> Result<OwnedFd, ReaderError> {
        retry(|| {
            #[cfg(test)]
            self.faults.on_open(None)?;
            Ok(os::open_root()?)
        })
    }

    fn open_dir_at(&mut self, dir: BorrowedFd<'_>, name: &[u8]) -> Result<OwnedFd, ReaderError> {
        retry(|| {
            #[cfg(test)]
            self.faults.on_open(None)?;
            Ok(os::open_dir_at(dir, name)?)
        })
    }

    fn open_file_at(&mut self, dir: BorrowedFd<'_>, name: &[u8]) -> Result<OwnedFd, ReaderError> {
        retry(|| {
            #[cfg(test)]
            self.faults.on_open(Some(name))?;
            Ok(os::open_file_at(dir, name)?)
        })
    }

    /// `fstatat` は呼び直さない(本家 3.10.11 の `posix_do_stat`)。
    fn stat_at(&mut self, dir: BorrowedFd<'_>, name: &[u8]) -> Result<Stat, ReaderError> {
        #[cfg(test)]
        self.faults.on_stat_at()?;
        Ok(os::stat_at(dir, name)?)
    }

    fn fstat(&mut self, fd: BorrowedFd<'_>) -> Result<Stat, ReaderError> {
        retry(|| {
            #[cfg(test)]
            self.faults.on_fstat()?;
            Ok(os::fstat(fd)?)
        })
    }

    fn read(&mut self, fd: BorrowedFd<'_>, max: usize) -> Result<Vec<u8>, ReaderError> {
        retry(|| {
            #[cfg(test)]
            if let Some(early) = self.faults.on_read()? {
                return Ok(early);
            }
            let block = os::read(fd, max)?;
            #[cfg(test)]
            self.faults.after_read();
            Ok(block)
        })
    }

    /// 閉じる(旧版の `os.close`。呼び直さない)。差し込みの close の回数に数える。
    fn close(&mut self, fd: OwnedFd) -> Result<(), ReaderError> {
        #[cfg(test)]
        if let Some(number) = self.faults.on_close() {
            let _ = os::close(fd);
            return Err(ReaderError::Os(number));
        }
        Ok(os::close(fd)?)
    }

    /// `directory` で前の段の close が失敗した後の `except` の 2 回目の close。番号はもう無効なので
    /// OS に送らず、旧版と同じ EBADF を返す(差し込みの close の回数には数える)。
    fn close_again(&mut self) -> ReaderError {
        #[cfg(test)]
        if let Some(number) = self.faults.on_close() {
            return ReaderError::Os(number);
        }
        ReaderError::Os(os::EBADF)
    }

    fn names(
        &mut self,
        fd: BorrowedFd<'_>,
        mut each: impl FnMut(&[u8]) -> Result<(), ReaderError>,
    ) -> Result<(), ReaderError> {
        #[cfg(test)]
        let mut fault = self.faults.on_names();
        let result = os::names(fd, |name| {
            #[cfg(test)]
            if let Some((remaining, number)) = fault.as_mut() {
                if *remaining == 0 {
                    return Err(ReaderError::Os(*number));
                }
                *remaining -= 1;
            }
            each(name)
        });
        match result {
            Ok(()) => Ok(()),
            Err(NamesError::Os(number)) => Err(ReaderError::Os(number)),
            Err(NamesError::Stopped(error)) => Err(error),
        }
    }

    fn getcwd(&mut self) -> Result<Vec<u8>, ReaderError> {
        #[cfg(test)]
        if let Some(value) = self.faults.on_getcwd()? {
            return Ok(value);
        }
        Ok(os::getcwd()?)
    }

    fn geteuid(&mut self) -> u32 {
        os::geteuid()
    }
}

// ---------------------------------------------------------------- 検査のときだけ使う差し込み

/// 差し込みの対象の操作(EINTR を返す所)。
#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum FaultOp {
    Open,
    Fstat,
    StatAt,
    Read,
    Close,
}

/// 検査のときだけ使う、差し込みの中で呼ぶ関数(ファイルを変えるなど)。
#[cfg(test)]
pub(crate) type Hook = Box<dyn FnMut()>;
/// 検査のときだけ使う、名前を受ける差し込みの関数。
#[cfg(test)]
pub(crate) type NameHook = Box<dyn FnMut(&[u8])>;

/// 検査のときだけ使う故障の差し込み。回数は 1 から数える。
#[cfg(test)]
#[derive(Default)]
pub(crate) struct Faults {
    /// 作業場所の値(プロセスの作業場所を変えずに、相対パスの筋書きを流すため)。
    pub cwd: Option<Vec<u8>>,
    /// 作業場所の取得の失敗(errno)。
    pub cwd_error: Option<i32>,
    /// N 回目の読み取りが 0 バイトで終わる。
    pub read_empty_at: Option<u64>,
    /// 最初の読み取りの後に呼ぶ(ファイルへ 1 バイト足すなど)。
    pub after_first_read: Option<Hook>,
    /// ファイルを開く直前(`fstatat` と開く間)に呼ぶ(置き換えなど)。名前を受ける。
    pub before_file_open: Option<NameHook>,
    /// N 回目の読み取りが errno で失敗する。
    pub read_error: Option<(u64, i32)>,
    /// N 回目の開く(`/`・フォルダ・ファイル)が errno で失敗する。
    pub open_error: Option<(u64, i32)>,
    /// N 回目の一覧が、M 件を渡した後に errno で失敗する。
    pub names_error: Option<(u64, u64, i32)>,
    /// N 回目の close が errno で失敗する(fd は閉じた扱い)。
    pub close_error: Option<(u64, i32)>,
    /// 操作の N 回目に EINTR を 1 回返す。
    pub eintr: Option<(FaultOp, u64)>,
    opens: u64,
    fstats: u64,
    stat_ats: u64,
    reads: u64,
    closes: u64,
    listings: u64,
}

#[cfg(test)]
impl Faults {
    fn eintr_now(&self, op: FaultOp, count: u64) -> bool {
        self.eintr == Some((op, count))
    }

    fn on_open(&mut self, file: Option<&[u8]>) -> Result<(), ReaderError> {
        self.opens += 1;
        if self.eintr_now(FaultOp::Open, self.opens) {
            return Err(ReaderError::Os(os::EINTR));
        }
        if let Some(name) = file
            && let Some(hook) = self.before_file_open.as_mut()
        {
            hook(name);
        }
        match self.open_error {
            Some((at, number)) if at == self.opens => Err(ReaderError::Os(number)),
            _ => Ok(()),
        }
    }

    fn on_stat_at(&mut self) -> Result<(), ReaderError> {
        self.stat_ats += 1;
        if self.eintr_now(FaultOp::StatAt, self.stat_ats) {
            return Err(ReaderError::Os(os::EINTR));
        }
        Ok(())
    }

    fn on_fstat(&mut self) -> Result<(), ReaderError> {
        self.fstats += 1;
        if self.eintr_now(FaultOp::Fstat, self.fstats) {
            return Err(ReaderError::Os(os::EINTR));
        }
        Ok(())
    }

    /// 読む前。故障なら失敗か、早い終わり(0 バイト)を返す。
    fn on_read(&mut self) -> Result<Option<Vec<u8>>, ReaderError> {
        self.reads += 1;
        if self.eintr_now(FaultOp::Read, self.reads) {
            return Err(ReaderError::Os(os::EINTR));
        }
        if let Some((at, number)) = self.read_error
            && at == self.reads
        {
            return Err(ReaderError::Os(number));
        }
        if self.read_empty_at == Some(self.reads) {
            return Ok(Some(Vec::new()));
        }
        Ok(None)
    }

    fn after_read(&mut self) {
        if let Some(mut hook) = self.after_first_read.take() {
            hook();
        }
    }

    fn on_close(&mut self) -> Option<i32> {
        self.closes += 1;
        if self.eintr_now(FaultOp::Close, self.closes) {
            return Some(os::EINTR);
        }
        match self.close_error {
            Some((at, number)) if at == self.closes => Some(number),
            _ => None,
        }
    }

    fn on_names(&mut self) -> Option<(u64, i32)> {
        self.listings += 1;
        match self.names_error {
            Some((at, after, number)) if at == self.listings => Some((after, number)),
            _ => None,
        }
    }

    fn on_getcwd(&mut self) -> Result<Option<Vec<u8>>, ReaderError> {
        if let Some(number) = self.cwd_error {
            return Err(ReaderError::Os(number));
        }
        Ok(self.cwd.clone())
    }
}

// ---------------------------------------------------------------- Reader

/// 上限付きの読み取り。予算は持たず、各関数が呼出元の 1 つの予算を受ける。
#[derive(Default)]
pub struct Reader {
    files: Records<FileRecord>,
    dirs: Records<Identity>,
    missing: Records<()>,
    sets: Records<Vec<Vec<u8>>>,
    owners: Records<(u32, u8)>,
    seam: Seam,
}

fn identity(stat: &Stat) -> Identity {
    (stat.dev, stat.ino, stat.format())
}

impl Reader {
    pub fn new() -> Self {
        Self::default()
    }

    /// 検査のときだけ使う。継ぎ目に故障を足す(前の差し込みは捨てる)。
    #[cfg(test)]
    pub(crate) fn set_faults(&mut self, faults: Faults) {
        self.seam.faults = faults;
    }

    // ---- 記録(呼出元が読む口と、直接扱う 3 つの口)

    /// 読んだファイル(パス → 6 欄と内容の sha256)。
    pub fn files(&self) -> &Records<FileRecord> {
        &self.files
    }
    /// 開いたフォルダ(パス → dev・ino・種類)。
    pub fn dirs(&self) -> &Records<Identity> {
        &self.dirs
    }
    /// 無かったパス(入れた順。重複を持たない)。
    pub fn missing(&self) -> impl Iterator<Item = &[u8]> {
        self.missing.iter().map(|(path, _)| path)
    }
    /// 名前の一覧(パス → 名前の並び)。
    pub fn sets(&self) -> &Records<Vec<Vec<u8>>> {
        &self.sets
    }
    /// 所有者(パス → 所有者の番号と終了値)。
    pub fn owners(&self) -> &Records<(u32, u8)> {
        &self.owners
    }
    /// `reader.files.get(path)`(`diagnostic-plan.py:150`)。
    pub fn file_record(&self, path: &[u8]) -> Option<&FileRecord> {
        self.files.get(path)
    }
    /// `reader.files[path] = (6 欄, sha256)`(`diagnostic-plan.py:152`)。
    pub fn put_file_record(&mut self, path: &[u8], record: FileRecord) {
        self.files.insert(path, record);
    }
    /// `reader.missing.add(path)`(`diagnostic-state.py:175`)。
    pub fn add_missing(&mut self, path: &[u8]) {
        self.missing.insert(path, ());
    }

    /// `os.path.abspath(path)`。相対パスのときだけ作業場所を求める。
    pub fn abspath(&mut self, path: &[u8]) -> Result<Vec<u8>, ReaderError> {
        if path.starts_with(b"/") {
            return Ok(normpath(path));
        }
        let cwd = self.seam.getcwd()?;
        Ok(normpath(&join(&cwd, path)))
    }

    /// `owner(path, info=None, code=22)`。所有者が実行者でなければ `ownership`(終了値は `code`)。
    pub fn owner(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        info: Option<&Stat>,
        code: u8,
    ) -> Result<(), ReaderError> {
        let path = self.abspath(path)?;
        let info = match info {
            Some(info) => *info,
            None => self
                .lstat(budget, &path, false)?
                .ok_or(ReaderError::Os(os::ENOENT))?,
        };
        if info.uid != self.seam.geteuid() {
            return Err(Failure::new("ownership", code).into());
        }
        self.owners.insert(&path, (info.uid, code));
        Ok(())
    }

    /// `check_owner(path, info)`: 記録のあるパスで所有者が変われば `ownership`(記録の終了値)。
    pub fn check_owner(&self, path: &[u8], info: &Stat) -> Result<(), ReaderError> {
        match self.owners.get(path) {
            Some(&(uid, code)) if info.uid != uid => Err(Failure::new("ownership", code).into()),
            _ => Ok(()),
        }
    }

    /// 旧版の `except BaseException: os.close(fd); raise`。閉じる失敗が元の失敗に代わる。
    fn close_then(&mut self, fd: OwnedFd, error: ReaderError) -> ReaderError {
        match self.seam.close(fd) {
            Ok(()) => error,
            Err(close) => close,
        }
    }

    fn directory_fd(&mut self, budget: &mut Budget, path: &[u8]) -> Result<OwnedFd, ReaderError> {
        let path = self.abspath(path)?;
        let mut fd = self.seam.open_root()?;
        let mut current = b"/".to_vec();
        for part in path.split(|b| *b == b'/') {
            if part.is_empty() {
                continue;
            }
            if let Err(failure) = budget.check() {
                return Err(self.close_then(fd, failure.into()));
            }
            let child = match self.seam.open_dir_at(fd.as_fd(), part) {
                Ok(child) => child,
                Err(error) => return Err(self.close_then(fd, error)),
            };
            if self.seam.close(fd).is_err() {
                // 旧版は `fd` がまだ前の段の番号なので、`except` が同じ番号をもう一度閉じ、EBADF が
                // 元の失敗に代わる。子は旧版では閉じないが、ここでは数えない `Drop` で閉じる。
                let error = self.seam.close_again();
                drop(child);
                return Err(error);
            }
            fd = child;
            current = join(&current, part);
            let info = match self.seam.fstat(fd.as_fd()) {
                Ok(info) => info,
                Err(error) => return Err(self.close_then(fd, error)),
            };
            if let Err(error) = self.check_owner(&current, &info) {
                return Err(self.close_then(fd, error));
            }
            let found = identity(&info);
            if self.dirs.get(&current).is_some_and(|held| *held != found) {
                return Err(self.close_then(fd, source_changed()));
            }
            self.dirs.insert(&current, found);
        }
        Ok(fd)
    }

    /// `directory(path)`: `/` から 1 段ずつ、予算を確かめてから開き、各段を確かめて記録する。
    pub fn directory(&mut self, budget: &mut Budget, path: &[u8]) -> Result<Dir, ReaderError> {
        self.directory_fd(budget, path).map(Dir)
    }

    /// 親を開く。無ければ(ENOENT)、`missing` が真なら記録して `None`。
    fn parent(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        missing: bool,
    ) -> Result<Option<OwnedFd>, ReaderError> {
        match self.directory_fd(budget, dirname(path)) {
            Ok(fd) => Ok(Some(fd)),
            Err(error) if error.is_not_found() && missing => {
                self.missing.insert(path, ());
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    /// `lstat(path, missing=True)`: 親を開き、最後の名前をリンクを辿らずに調べる。
    pub fn lstat(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        missing: bool,
    ) -> Result<Option<Stat>, ReaderError> {
        let Some(fd) = self.parent(budget, path, missing)? else {
            return Ok(None);
        };
        let result = match self.seam.stat_at(fd.as_fd(), basename(path)) {
            Ok(info) => Ok(Some(info)),
            Err(error) if error.is_not_found() && missing => {
                self.missing.insert(path, ());
                Ok(None)
            }
            Err(error) => Err(error),
        };
        // finally: os.close(fd)
        self.seam.close(fd)?;
        result
    }

    /// `read(path, limit=16MiB, kind='management_copy', missing=False)`。`kind` が `None` なら量に足さない。
    pub fn read(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        limit: u64,
        kind: Option<Kind>,
        missing: bool,
    ) -> Result<Option<Vec<u8>>, ReaderError> {
        let Some(parent) = self.parent(budget, path, missing)? else {
            return Ok(None);
        };
        let mut file: Option<OwnedFd> = None;
        let result = self.read_body(
            budget,
            path,
            limit,
            kind,
            missing,
            parent.as_fd(),
            &mut file,
        );
        // finally: ファイルを閉じてから親を閉じる。ファイルの close が失敗すると、親は閉じない
        // (旧版は残すが結果に表れない。ここでは数えない `Drop` で閉じる)。
        if let Some(file) = file
            && let Err(error) = self.seam.close(file)
        {
            drop(parent);
            return Err(error);
        }
        self.seam.close(parent)?;
        result
    }

    #[allow(clippy::too_many_arguments)]
    fn read_body(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        limit: u64,
        kind: Option<Kind>,
        missing: bool,
        parent: BorrowedFd<'_>,
        file: &mut Option<OwnedFd>,
    ) -> Result<Option<Vec<u8>>, ReaderError> {
        let name = basename(path);
        let before = match self.seam.stat_at(parent, name) {
            Ok(info) => info,
            Err(error) if error.is_not_found() && missing => {
                self.missing.insert(path, ());
                return Ok(None);
            }
            Err(error) => return Err(error),
        };
        self.check_owner(path, &before)?;
        if !before.is_regular() {
            return Err(Failure::new("not-regular", 23).into());
        }
        if i128::from(before.size) > i128::from(limit) {
            return Err(Failure::new("file-limit", 20).into());
        }
        let opened: &OwnedFd = file.insert(self.seam.open_file_at(parent, name)?);
        let fd = opened.as_fd();
        if fingerprint(&before) != fingerprint(&self.seam.fstat(fd)?) {
            return Err(source_changed());
        }
        let mut raw = Vec::new();
        let mut size: u64 = 0;
        loop {
            budget.check()?;
            let want = (limit - size).saturating_add(1).min(CHUNK) as usize;
            let block = self.seam.read(fd, want)?;
            if block.is_empty() {
                break;
            }
            size += block.len() as u64;
            if size > limit {
                return Err(Failure::new("file-limit", 20).into());
            }
            if let Some(kind) = kind {
                budget.charge(kind, block.len() as u64)?;
            }
            raw.extend_from_slice(&block);
        }
        let after = self.seam.fstat(fd)?;
        self.check_owner(path, &after)?;
        if fingerprint(&before) != fingerprint(&after)
            || i128::from(size) != i128::from(before.size)
        {
            return Err(source_changed());
        }
        if fingerprint(&self.seam.stat_at(parent, name)?) != fingerprint(&before) {
            return Err(source_changed());
        }
        let now = (fingerprint(&before), digest(&raw));
        if self.files.get(path).is_some_and(|old| *old != now) {
            return Err(source_changed());
        }
        self.files.insert(path, now);
        Ok(Some(raw))
    }

    /// `names(path, kind='management_entries', record=True)`: 名前を 1 件ごとに量へ足し、bytes の順に並べる。
    pub fn names(
        &mut self,
        budget: &mut Budget,
        path: &[u8],
        kind: Kind,
        record: bool,
    ) -> Result<Vec<Vec<u8>>, ReaderError> {
        let fd = self.directory_fd(budget, path)?;
        let mut values: Vec<Vec<u8>> = Vec::new();
        let listed = self.seam.names(fd.as_fd(), |name| {
            budget.charge(kind, 1)?;
            values.push(name.to_vec());
            Ok(())
        });
        let result = listed.and_then(|()| {
            values.sort();
            if record {
                if self.sets.get(path).is_some_and(|old| *old != values) {
                    return Err(source_changed());
                }
                self.sets.insert(path, values.clone());
            }
            Ok(values)
        });
        // finally: os.close(fd)
        self.seam.close(fd)?;
        result
    }

    /// `verify()`: 記録を、所有者 → フォルダ → ファイル → 無かったパス → 名前の一覧の順に確かめ直す。
    /// 無かったパスは入れた順に確かめる(旧版は hash の乱数で実行ごとに順が変わる)。
    pub fn verify(&mut self, budget: &mut Budget) -> Result<(), ReaderError> {
        for (path, _) in self.owners.snapshot() {
            budget.check()?;
            let info = self
                .lstat(budget, &path, false)?
                .ok_or(ReaderError::Os(os::ENOENT))?;
            self.check_owner(&path, &info)?;
        }
        for (path, expected) in self.dirs.snapshot() {
            budget.check()?;
            let fd = self.directory_fd(budget, &path)?;
            let result = match self.seam.fstat(fd.as_fd()) {
                Ok(info) if identity(&info) != expected => Err(source_changed()),
                Ok(_) => Ok(()),
                Err(error) => Err(error),
            };
            self.seam.close(fd)?;
            result?;
        }
        for (path, (expected, _)) in self.files.snapshot() {
            budget.check()?;
            let info = self
                .lstat(budget, &path, false)?
                .ok_or(ReaderError::Os(os::ENOENT))?;
            if fingerprint(&info) != expected {
                return Err(source_changed());
            }
        }
        for (path, ()) in self.missing.snapshot() {
            budget.check()?;
            if self.lstat(budget, &path, true)?.is_some() {
                return Err(source_changed());
            }
        }
        for (path, names) in self.sets.snapshot() {
            let fd = self.directory_fd(budget, &path)?;
            let mut found: Vec<Vec<u8>> = Vec::new();
            let listed = self.seam.names(fd.as_fd(), |name| {
                budget.check()?;
                found.push(name.to_vec());
                if found.len() > names.len() {
                    return Err(source_changed());
                }
                Ok(())
            });
            let result = listed.and_then(|()| {
                found.sort();
                if found != names {
                    Err(source_changed())
                } else {
                    Ok(())
                }
            });
            self.seam.close(fd)?;
            result?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::super::budget::BudgetOp;
    use super::*;
    use std::cell::RefCell;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::rc::Rc;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Tree(PathBuf);
    impl Tree {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "r4d2-reader-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn path(&self, name: &str) -> Vec<u8> {
            let mut path = self.0.as_os_str().as_bytes().to_vec();
            if !name.is_empty() {
                path.push(b'/');
                path.extend_from_slice(name.as_bytes());
            }
            path
        }
        fn write(&self, name: &str, data: &[u8]) {
            std::fs::write(self.0.join(name), data).unwrap();
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn fresh() -> Budget {
        Budget::for_test(300.0, Box::new(|| 0.0), Kind::ALL.map(Kind::default_limit))
    }

    fn failure(reason: &'static str, code: u8) -> ReaderError {
        ReaderError::Failure(Failure::new(reason, code))
    }

    #[test]
    fn paths_follow_posixpath() {
        for (path, expected) in [
            (&b""[..], &b"."[..]),
            (b"/", b"/"),
            (b"//", b"//"),
            (b"///", b"/"),
            (b"//a//b/", b"//a/b"),
            (b"/a/./b/../c", b"/a/c"),
            (b"/..", b"/"),
            (b"a/../..", b".."),
            (b"../a/..", b".."),
        ] {
            assert_eq!(normpath(path), expected, "{path:?}");
        }
        for (path, dir, base) in [
            (&b"/a/b"[..], &b"/a"[..], &b"b"[..]),
            (b"/a/b/", b"/a/b", b""),
            (b"b", b"", b"b"),
            (b"/b", b"/", b"b"),
            (b"//b", b"//", b"b"),
            (b"a//b", b"a", b"b"),
            (b"/", b"/", b""),
        ] {
            assert_eq!(dirname(path), dir, "{path:?}");
            assert_eq!(basename(path), base, "{path:?}");
        }
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            cwd: Some(b"/x/y".to_vec()),
            ..Faults::default()
        });
        assert_eq!(reader.abspath(b"../z/./w").unwrap(), b"/x/z/w");
        assert_eq!(reader.abspath(b"").unwrap(), b"/x/y");
        assert_eq!(reader.abspath(b"//q").unwrap(), b"//q");
    }

    /// 旧版の検査 `test_reader_limit_and_exact_contents`(:96)と同じ性質: 上限ちょうどは中身のまま読め、
    /// 確かめ直しも通り、1 バイト超えると `file-limit`。
    #[test]
    fn read_limit_and_exact_contents() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        let mut budget = fresh();
        let mut reader = Reader::new();
        let path = tree.path("f");
        assert_eq!(
            reader.read(&mut budget, &path, 3, Some(Kind::ManagementCopy), false),
            Ok(Some(b"abc".to_vec()))
        );
        assert_eq!(reader.verify(&mut budget), Ok(()));
        assert_eq!(budget.used(Kind::ManagementCopy), 3);
        assert_eq!(
            reader.read(&mut budget, &path, 2, Some(Kind::ManagementCopy), false),
            Err(failure("file-limit", 20))
        );
        assert_eq!(
            reader.read(&mut budget, &path, 3, None, false),
            Ok(Some(b"abc".to_vec()))
        );
        assert_eq!(budget.used(Kind::ManagementCopy), 3);
        assert_eq!(reader.files().len(), 1);
    }

    /// 旧版の検査 `test_reader_refuses_final_and_parent_symlinks`(:121)と同じ性質: 最後の名前と途中の段の
    /// リンクを辿らない。ほかに、フォルダ・無いもの・NUL・末尾の `/` の扱い。
    #[test]
    fn read_refuses_final_and_parent_symlinks() {
        let tree = Tree::new();
        tree.write("f", b"x");
        std::fs::create_dir(tree.0.join("d")).unwrap();
        std::os::unix::fs::symlink("f", tree.0.join("link")).unwrap();
        std::os::unix::fs::symlink("d", tree.0.join("dlink")).unwrap();
        let mut budget = fresh();
        let mut reader = Reader::new();
        assert_eq!(
            reader.read(&mut budget, &tree.path("link"), 10, None, false),
            Err(failure("not-regular", 23))
        );
        assert_eq!(
            reader.read(&mut budget, &tree.path("d"), 10, None, false),
            Err(failure("not-regular", 23))
        );
        let through = reader.read(&mut budget, &tree.path("dlink/x"), 10, None, false);
        assert!(matches!(through, Err(ReaderError::Os(n)) if n == os::ENOTDIR || n == os::ELOOP));
        assert_eq!(
            reader.read(&mut budget, &tree.path("none"), 10, None, true),
            Ok(None)
        );
        assert_eq!(
            reader.lstat(&mut budget, &tree.path("none/deeper"), true),
            Ok(None)
        );
        assert_eq!(
            reader.read(&mut budget, &tree.path("none"), 10, None, false),
            Err(ReaderError::Os(os::ENOENT))
        );
        assert_eq!(
            reader.missing().collect::<Vec<_>>(),
            vec![
                tree.path("none").as_slice(),
                tree.path("none/deeper").as_slice()
            ]
        );
        assert_eq!(
            reader.read(&mut budget, b"/a\0b/c", 10, None, false),
            Err(ReaderError::Nul)
        );
        // 末尾が `/` なら最後の名前は空で、`fstatat` が ENOENT を返す。
        assert_eq!(
            reader.lstat(&mut budget, &tree.path("d/"), false),
            Err(ReaderError::Os(os::ENOENT))
        );
        // ファイルを途中の段にすると ENOTDIR(FileNotFoundError ではないので missing でも記録しない)。
        assert_eq!(
            reader.lstat(&mut budget, &tree.path("f/x"), true),
            Err(ReaderError::Os(os::ENOTDIR))
        );
    }

    /// 名前の一覧の並び・量・変化の検出。
    #[test]
    fn names_are_sorted_charged_and_rechecked() {
        let tree = Tree::new();
        tree.write("b", b"");
        tree.write("a", b"");
        let mut budget = fresh();
        let mut reader = Reader::new();
        let path = tree.path("");
        assert_eq!(
            reader.names(&mut budget, &path, Kind::ManagementEntries, true),
            Ok(vec![b"a".to_vec(), b"b".to_vec()])
        );
        assert_eq!(budget.used(Kind::ManagementEntries), 2);
        tree.write("c", b"");
        assert_eq!(
            reader.names(&mut budget, &path, Kind::ManagementEntries, true),
            Err(failure("source-changed", 20))
        );
        assert_eq!(
            reader
                .names(&mut budget, &path, Kind::WorktreeEntries, false)
                .unwrap()
                .len(),
            3
        );
        assert_eq!(
            reader.verify(&mut budget),
            Err(failure("source-changed", 20))
        );
    }

    /// 旧版の検査 `test_reader_rejects_replacement_mode_change_missing_creation_and_set_changes`(:103)と同じ性質:
    /// 置き換え・mode の変更・無かったパスの作成・名前の追加と削除を、確かめ直しが見つける。
    #[test]
    fn verify_rejects_replacement_mode_change_missing_creation_and_set_changes() {
        for change in ["replace", "mode", "missing", "add", "delete"] {
            let tree = Tree::new();
            std::fs::create_dir(tree.0.join("d")).unwrap();
            std::fs::write(tree.0.join("d/file"), b"old").unwrap();
            let mut budget = fresh();
            let mut reader = Reader::new();
            reader
                .read(
                    &mut budget,
                    &tree.path("d/file"),
                    100,
                    Some(Kind::ManagementCopy),
                    false,
                )
                .unwrap();
            reader
                .names(&mut budget, &tree.path("d"), Kind::ManagementEntries, true)
                .unwrap();
            match change {
                "replace" => {
                    std::fs::write(tree.0.join("d/replacement"), b"old").unwrap();
                    std::fs::rename(tree.0.join("d/replacement"), tree.0.join("d/file")).unwrap();
                }
                "mode" => std::fs::set_permissions(
                    tree.0.join("d/file"),
                    std::fs::Permissions::from_mode(0o700),
                )
                .unwrap(),
                "missing" => {
                    assert_eq!(
                        reader.read(&mut budget, &tree.path("d/new"), 100, None, true),
                        Ok(None)
                    );
                    std::fs::write(tree.0.join("d/new"), b"").unwrap();
                }
                "add" => std::fs::write(tree.0.join("d/new"), b"").unwrap(),
                _ => std::fs::remove_file(tree.0.join("d/file")).unwrap(),
            }
            assert!(reader.verify(&mut budget).is_err(), "{change}");
        }
    }

    /// 確かめ直しが、変化が無くなれば通り、中身の変化を見つける。
    #[test]
    fn verify_finds_changes_after_reading() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        std::fs::create_dir(tree.0.join("d")).unwrap();
        let mut budget = fresh();
        let mut reader = Reader::new();
        reader
            .read(&mut budget, &tree.path("f"), 10, None, false)
            .unwrap();
        reader.lstat(&mut budget, &tree.path("gone"), true).unwrap();
        reader
            .names(&mut budget, &tree.path("d"), Kind::ManagementEntries, true)
            .unwrap();
        reader
            .owner(&mut budget, &tree.path("f"), None, 22)
            .unwrap();
        assert_eq!(reader.verify(&mut budget), Ok(()));
        tree.write("gone", b"");
        assert_eq!(
            reader.verify(&mut budget),
            Err(failure("source-changed", 20))
        );
        std::fs::remove_file(tree.0.join("gone")).unwrap();
        assert_eq!(reader.verify(&mut budget), Ok(()));
        std::fs::write(tree.0.join("f"), b"abcd").unwrap();
        assert_eq!(
            reader.verify(&mut budget),
            Err(failure("source-changed", 20))
        );
    }

    /// 実行者でない所有者は `ownership`(旧版の終了値の引数のとおり)。
    #[test]
    fn owner_refuses_another_user() {
        let mut budget = fresh();
        let mut reader = Reader::new();
        if os::geteuid() == 0 {
            return;
        }
        // `/` の最後の名前は空なので、調べると ENOENT(旧版と同じ)。
        assert_eq!(
            reader.owner(&mut budget, b"/", None, 22),
            Err(ReaderError::Os(os::ENOENT))
        );
        assert_eq!(
            reader.owner(&mut budget, b"/usr", None, 2),
            Err(failure("ownership", 2))
        );
        let info = Stat {
            dev: 0,
            ino: 0,
            mode: 0,
            size: 0,
            uid: os::geteuid(),
            mtime_ns: 0,
            ctime_ns: 0,
        };
        assert_eq!(reader.owner(&mut budget, b"/usr", Some(&info), 23), Ok(()));
        assert_eq!(reader.owners().get(b"/usr"), Some(&(os::geteuid(), 23)));
        // 同じパスへ別の終了値: 位置は最初のまま、値が変わる。
        reader
            .owner(&mut budget, b"//usr/", Some(&info), 2)
            .unwrap();
        assert_eq!(
            reader
                .owners()
                .iter()
                .map(|(p, v)| (p.to_vec(), *v))
                .collect::<Vec<_>>(),
            vec![
                (b"/usr".to_vec(), (os::geteuid(), 23)),
                (b"//usr".to_vec(), (os::geteuid(), 2))
            ]
        );
    }

    #[test]
    fn direct_records_and_budget_order() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        let mut budget = fresh();
        budget.set_test_log(true);
        let mut reader = Reader::new();
        reader
            .read(&mut budget, &tree.path("f"), 10, Some(Kind::Body), false)
            .unwrap();
        let parts = tree
            .path("")
            .split(|b| *b == b'/')
            .filter(|p| !p.is_empty())
            .count();
        let mut expected = vec![BudgetOp::Check; parts];
        expected.extend([
            BudgetOp::Check,
            BudgetOp::Charge(Kind::Body, 3),
            BudgetOp::Check,
            BudgetOp::Check,
        ]);
        assert_eq!(budget.take_test_log(), expected);
        let record = reader.file_record(&tree.path("f")).unwrap().clone();
        assert_eq!(record.1, digest(b"abc"));
        reader.put_file_record(b"/other", record.clone());
        reader.add_missing(b"/none");
        reader.add_missing(b"/none");
        assert_eq!(reader.missing().count(), 1);
        assert_eq!(reader.file_record(b"/other"), Some(&record));
    }

    fn parts_of(tree: &Tree) -> u64 {
        tree.path("")
            .split(|b| *b == b'/')
            .filter(|p| !p.is_empty())
            .count() as u64
    }

    #[test]
    fn eintr_is_retried_only_where_the_legacy_retries() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        let parts = parts_of(&tree);
        for (op, at, expected) in [
            (FaultOp::Open, 1, Ok(Some(b"abc".to_vec()))),
            (FaultOp::Open, parts + 1, Ok(Some(b"abc".to_vec()))),
            (FaultOp::Fstat, 1, Ok(Some(b"abc".to_vec()))),
            (FaultOp::Fstat, parts + 2, Ok(Some(b"abc".to_vec()))),
            (FaultOp::Read, 1, Ok(Some(b"abc".to_vec()))),
            (FaultOp::Read, 2, Ok(Some(b"abc".to_vec()))),
            (FaultOp::StatAt, 1, Err(ReaderError::Os(os::EINTR))),
            (FaultOp::StatAt, 2, Err(ReaderError::Os(os::EINTR))),
            // 親の close(段の close の後のファイルの close の次)。
            (FaultOp::Close, parts + 2, Err(ReaderError::Os(os::EINTR))),
        ] {
            let mut budget = fresh();
            let mut reader = Reader::new();
            reader.set_faults(Faults {
                eintr: Some((op, at)),
                ..Faults::default()
            });
            assert_eq!(
                reader.read(&mut budget, &tree.path("f"), 10, None, false),
                expected,
                "{op:?}"
            );
        }
    }

    #[test]
    fn close_failures_follow_the_table() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        let parts = parts_of(&tree);
        let eio = ReaderError::Os(os::EIO);
        // directory の前の段: 1 回目の close が失敗すると、2 回目の close(数える)の EBADF が返る。
        let mut budget = fresh();
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            close_error: Some((1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &tree.path("f"), 10, None, false),
            Err(ReaderError::Os(os::EBADF))
        );
        // 2 回目の close も差し込みの回数に数える(ここでは EINTR を返させる)。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            close_error: Some((1, os::EIO)),
            eintr: Some((FaultOp::Close, 2)),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &tree.path("f"), 10, None, false),
            Err(ReaderError::Os(os::EINTR))
        );
        // 前の段の close が失敗しなければ、2 回目の close は無い(EIO は 3 回目以降の別の close に当たる)。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            close_error: Some((parts + 3, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &tree.path("f"), 10, None, false),
            Ok(Some(b"abc".to_vec()))
        );
        // read のファイル(parts 回の段の close の後)と親。
        for (at, expected) in [(parts + 1, eio), (parts + 2, eio)] {
            let mut reader = Reader::new();
            reader.set_faults(Faults {
                close_error: Some((at, os::EIO)),
                ..Faults::default()
            });
            assert_eq!(
                reader.read(&mut budget, &tree.path("f"), 10, None, false),
                Err(expected),
                "{at}"
            );
        }
        // lstat の finally。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            close_error: Some((parts + 1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(reader.lstat(&mut budget, &tree.path("f"), false), Err(eio));
        // directory の子を受け取った後の失敗の except: 記録と違うフォルダは source-changed だが、閉じる失敗が代わる。
        let mut reader = Reader::new();
        reader.directory(&mut budget, &tree.path("")).unwrap();
        let tree_path = tree.path("");
        let held = *reader.dirs().get(&tree_path).unwrap();
        reader.dirs.insert(&tree_path, (held.0, held.1 + 1, held.2));
        reader.set_faults(Faults {
            close_error: Some((parts + 1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.directory(&mut budget, &tree.path("")).err(),
            Some(eio)
        );
        let mut reader = Reader::new();
        reader.directory(&mut budget, &tree.path("")).unwrap();
        reader.dirs.insert(&tree_path, (held.0, held.1 + 1, held.2));
        assert_eq!(
            reader.directory(&mut budget, &tree.path("")).err(),
            Some(failure("source-changed", 20))
        );
        // 予算の失敗の except も同じ。
        let mut stopped = budget_stopped();
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            close_error: Some((1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(reader.directory(&mut stopped, b"/usr").err(), Some(eio));
    }

    fn budget_stopped() -> Budget {
        let budget = fresh();
        budget.request_stop();
        budget
    }

    /// 旧版の検査 `test_reader_catches_growth_during_read`(:130)と同じ性質: 読んでいる途中に伸びたファイル。
    #[test]
    fn read_catches_growth_during_read() {
        let tree = Tree::new();
        tree.write("file", &[b'x'; 65536]);
        let target = tree.0.join("file");
        let mut budget = fresh();
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            after_first_read: Some(Box::new(move || {
                use std::io::Write;
                std::fs::OpenOptions::new()
                    .append(true)
                    .open(&target)
                    .unwrap()
                    .write_all(b"!")
                    .unwrap();
            })),
            ..Faults::default()
        });
        assert!(matches!(
            reader.read(
                &mut budget,
                &tree.path("file"),
                DEFAULT_READ_LIMIT,
                Some(DEFAULT_READ_KIND),
                false
            ),
            Err(ReaderError::Failure(_))
        ));
    }

    /// 旧版の検査 `test_reader_rejects_short_read_and_growth_at_exact_limit`(:157)と同じ性質: 読み取りが
    /// 上限ちょうどの大きさより前に 0 バイトで終わる。
    #[test]
    fn read_rejects_short_read_at_exact_limit() {
        let tree = Tree::new();
        tree.write("short", b"abcd");
        let mut budget = fresh();
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            read_empty_at: Some(1),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(
                &mut budget,
                &tree.path("short"),
                4,
                Some(DEFAULT_READ_KIND),
                false
            ),
            Err(failure("source-changed", 20))
        );
    }

    #[test]
    fn injected_read_and_open_faults() {
        let tree = Tree::new();
        tree.write("f", b"abc");
        tree.write("g", b"xyz");
        let path = tree.path("f");
        let mut budget = fresh();
        // 0 バイトで終わる読み取り → 大きさが合わず source-changed。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            read_empty_at: Some(1),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &path, 10, None, false),
            Err(failure("source-changed", 20))
        );
        // 最初の読み取りの後に 1 バイト足す → source-changed。
        let target = tree.0.join("f");
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            after_first_read: Some(Box::new(move || {
                use std::io::Write;
                std::fs::OpenOptions::new()
                    .append(true)
                    .open(&target)
                    .unwrap()
                    .write_all(b"!")
                    .unwrap();
            })),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &path, 10, None, false),
            Err(failure("source-changed", 20))
        );
        // fstatat と開く間の置き換え → source-changed。
        let (from, to) = (tree.0.join("g"), tree.0.join("f"));
        let names = Rc::new(RefCell::new(Vec::new()));
        let seen = Rc::clone(&names);
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            before_file_open: Some(Box::new(move |name: &[u8]| {
                seen.borrow_mut().push(name.to_vec());
                std::fs::rename(&from, &to).unwrap();
            })),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &path, 10, None, false),
            Err(failure("source-changed", 20))
        );
        assert_eq!(*names.borrow(), vec![b"f".to_vec()]);
        // 読み取りと開くの I/O の異常。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            read_error: Some((1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &path, 10, None, false),
            Err(ReaderError::Os(os::EIO))
        );
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            open_error: Some((1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.read(&mut budget, &path, 10, None, false),
            Err(ReaderError::Os(os::EIO))
        );
        // 一覧の途中の I/O の異常(1 件を渡した後)。
        tree.write("h", b"");
        let mut entries = fresh();
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            names_error: Some((1, 1, os::EIO)),
            ..Faults::default()
        });
        assert_eq!(
            reader.names(&mut entries, &tree.path(""), Kind::ManagementEntries, true),
            Err(ReaderError::Os(os::EIO))
        );
        assert_eq!(entries.used(Kind::ManagementEntries), 1);
        // 作業場所の取得の失敗。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            cwd_error: Some(os::EACCES),
            ..Faults::default()
        });
        assert_eq!(
            reader.lstat(&mut budget, b"rel", true),
            Err(ReaderError::Os(os::EACCES))
        );
        // ENOENT は旧版の FileNotFoundError なので、missing が真なら無かったパスとして記録する。
        let mut reader = Reader::new();
        reader.set_faults(Faults {
            cwd_error: Some(os::ENOENT),
            ..Faults::default()
        });
        assert_eq!(reader.lstat(&mut budget, b"rel", true), Ok(None));
        assert_eq!(reader.missing().collect::<Vec<_>>(), vec![&b"rel"[..]]);
        assert_eq!(
            reader.lstat(&mut budget, b"rel", false),
            Err(ReaderError::Os(os::ENOENT))
        );
    }

    #[test]
    fn permission_denied_is_an_os_failure() {
        if os::geteuid() == 0 {
            return;
        }
        let tree = Tree::new();
        tree.write("f", b"abc");
        let file = tree.0.join("f");
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o000)).unwrap();
        let mut budget = fresh();
        let mut reader = Reader::new();
        assert_eq!(
            reader.read(&mut budget, &tree.path("f"), 10, None, false),
            Err(ReaderError::Os(os::EACCES))
        );
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
}
