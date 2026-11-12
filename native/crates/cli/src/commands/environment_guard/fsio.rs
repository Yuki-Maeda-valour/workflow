//! 観測のファイル操作(旧版の `identity`・`directory_identity`・`absolute`・`parent`・`read_at`・
//! `read_regular`・`sha`・`write_new`)。OS の呼出しは `platform` の `environment_guard` だけが行う。
//!
//! - 例外は旧版と同じ型名と理由(`OSError` の `filename` を含む)で返す。`EINTR` は `SIGINT` が来ていなければ
//!   再試行し、来ていれば `KeyboardInterrupt` にする(旧版の `PyErr_CheckSignals`)。
//! - 細かい競合(`lstat` と `open` の間など)の注入は、`cfg(test)` のフックだけで行う。フックは製品と同じ関数の中の
//!   定めた位置で呼ばれ、製品のビルドでは何も呼ばない(`hook!` は式を評価しない)。
use super::budget::EnvBudget;
use super::error::{PyException, from_io};
use super::pystr::{encode_error, encode_fs};
use super::specs::Host;
use dev_workflow_platform::environment_guard as os;
use std::io;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};

pub use super::specs::absolute;

const S_IMODE: u32 = 0o7777;

/// 旧版の `identity(st)`: dev・ino・mode・size・mtime_ns・ctime_ns。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Identity {
    pub dev: u64,
    pub ino: u64,
    pub mode: u32,
    pub size: i64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

/// 旧版の `directory_identity(st)`: dev・ino・mode。
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct DirIdentity {
    pub dev: u64,
    pub ino: u64,
    pub mode: u32,
}

pub fn identity(stat: &os::Stat) -> Identity {
    Identity {
        dev: stat.dev,
        ino: stat.ino,
        mode: stat.mode,
        size: stat.size,
        mtime_ns: stat.mtime_ns,
        ctime_ns: stat.ctime_ns,
    }
}

pub fn directory_identity(stat: &os::Stat) -> DirIdentity {
    DirIdentity {
        dev: stat.dev,
        ino: stat.ino,
        mode: stat.mode,
    }
}

/// 旧版の `sha(raw)`: SHA-256 の小文字の 16 進。
pub fn sha(raw: &[u8]) -> String {
    super::pyops::sha_hex(raw)
}

// ---------------------------------------------------------------- テスト用のフック

/// フックの呼出し。製品のビルドでは引数を評価せず、常に `None`。
macro_rules! hook {
    ($point:expr) => {{
        #[cfg(test)]
        let fault = hooks::fire(&$point);
        #[cfg(not(test))]
        let fault: Option<Fault> = None;
        fault
    }};
}

#[cfg(not(test))]
pub enum Fault {}

#[cfg(test)]
use hooks::{Fault, Point};

// ---------------------------------------------------------------- 例外への変換

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

/// OS の誤りを旧版の例外にする。`SIGINT` を受けて中断した呼出しは `KeyboardInterrupt`。
fn os_err(error: io::Error, filename: Option<&[u32]>) -> PyException {
    if error.raw_os_error() == Some(os::EINTR) && os::take_sigint() {
        return PyException::new("KeyboardInterrupt", "");
    }
    from_io(&error, filename)
}

/// `os.fsencode(name)` と、NUL の確認(旧版の `path_converter` の順)。
fn enc(name: &[u32]) -> Result<Vec<u8>, PyException> {
    let bytes = encode_fs(name).map_err(|failure| encode_error(&failure))?;
    if bytes.contains(&0) {
        return Err(PyException::value_error("embedded null byte"));
    }
    Ok(bytes)
}

/// `os.stat(name, dir_fd=fd, follow_symlinks=False)`。例外のファイル名は `name`。
pub fn lstat_at(fd: BorrowedFd<'_>, name: &[u32]) -> Result<os::Stat, PyException> {
    let bytes = enc(name)?;
    os::retry(|| os::lstat_at(fd, &bytes)).map_err(|error| os_err(error, Some(name)))
}

/// `os.fstat(fd)`。
pub fn fstat(fd: BorrowedFd<'_>) -> Result<os::Stat, PyException> {
    os::retry(|| os::fstat(fd)).map_err(|error| os_err(error, None))
}

/// `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=fd)`。
pub fn open_dir_at(fd: BorrowedFd<'_>, name: &[u32]) -> Result<OwnedFd, PyException> {
    let bytes = enc(name)?;
    os::retry(|| os::open_dir_at(fd, &bytes)).map_err(|error| os_err(error, Some(name)))
}

/// `os.chmod(name, mode, dir_fd=fd, follow_symlinks=False)`。リンクを辿れない環境(リンク自身を
/// 変えられない)は、旧 Python 3.10 と同じ `ValueError`(測った文面)にする。
pub fn chmod_nofollow_at(fd: BorrowedFd<'_>, name: &[u32], mode: u32) -> Result<(), PyException> {
    let bytes = enc(name)?;
    os::retry(|| os::chmod_nofollow_at(fd, &bytes, mode)).map_err(|error| {
        if matches!(error.raw_os_error(), Some(code) if code == os::EOPNOTSUPP || code == os::ENOTSUP) {
            return PyException::value_error("chmod: cannot use dir_fd and follow_symlinks together");
        }
        os_err(error, Some(name))
    })
}

// ---------------------------------------------------------------- parent

/// 旧版の `parent(path)`。`/` から 1 段ずつリンクを辿らずに開き、`body` の後に全段の
/// dev・ino・mode を照らし直す。`body` が失敗したときは照らし直さない(旧版は `yield` の後の
/// 文が実行されない)。`body` が値を返したときは、`return` の場合も含めて照らし直す。
pub fn with_parent<T>(
    host: &dyn Host,
    path: &[u32],
    body: impl FnOnce(BorrowedFd<'_>, &[u32]) -> Result<T, PyException>,
) -> Result<T, PyException> {
    let path = absolute(host, path)?;
    let slash = [0x2f];
    let root = os::retry(os::open_root).map_err(|error| os_err(error, Some(&slash)))?;
    let parts = path.parts();
    let mut fds: Vec<OwnedFd> = vec![root];
    // (親の fd の番号, 名前, 開いた直後の 3 項目)
    let mut chain: Vec<(usize, Vec<u32>, DirIdentity)> = Vec::new();
    if parts.len() > 2 {
        for part in &parts[1..parts.len() - 1] {
            let old = fds.len() - 1;
            let fd = open_dir_at(fds[old].as_fd(), part)?;
            let held = directory_identity(&fstat(fd.as_fd())?);
            chain.push((old, part.clone(), held));
            fds.push(fd);
            let _ = hook!(Point::ParentOpened(part.clone()));
        }
    }
    let name = path.name();
    let last = fds.len() - 1;
    let result = body(fds[last].as_fd(), &name)?;
    for (old, part, held) in &chain {
        if directory_identity(&lstat_at(fds[*old].as_fd(), part)?) != *held {
            return Err(stop("検査中に親ディレクトリが差し替えられた"));
        }
    }
    Ok(result)
}

// ---------------------------------------------------------------- read_at・read_regular

fn is_regular(mode: u32) -> bool {
    mode & os::S_IFMT == os::S_IFREG
}

/// 旧版の `read_at(fd, name, budget, expected_identity)`。戻り値は本文と `stat.S_IMODE(mode)`。
///
/// 順序: `lstat` → 保持値との比較 → 種類と `0o444` → 全長の予約 → nofollow・非 block の `open` →
/// `fstat` の比較 → (`tick` → 最大 65536 バイトの `read`)の繰返し → 1 バイトの追加 `read` →
/// `fstat` と `lstat` の再照合。
#[cfg_attr(not(test), allow(unused_variables, unused_assignments))]
pub fn read_at(
    fd: BorrowedFd<'_>,
    name: &[u32],
    budget: &mut EnvBudget,
    expected: Option<Identity>,
) -> Result<(Vec<u8>, u32), PyException> {
    let before = lstat_at(fd, name)?;
    let _ = hook!(Point::AfterLstat);
    if let Some(expected) = expected
        && identity(&before) != expected
    {
        return Err(stop("本文読取前に監視対象が変わった"));
    }
    if !is_regular(before.mode) || before.mode & 0o444 == 0 {
        return Err(stop("監視対象が読める通常ファイルでない"));
    }
    // `st_size` は負にならない。
    let size = u64::try_from(before.size).unwrap_or(0);
    budget.reserve(size)?;
    let bytes = enc(name)?;
    let _ = hook!(Point::BeforeOpen);
    let opened =
        os::retry(|| os::open_read_at(fd, &bytes)).map_err(|error| os_err(error, Some(name)))?;
    let _ = hook!(Point::AfterOpen);
    if identity(&before) != identity(&fstat(opened.as_fd())?) {
        return Err(stop("open 時に監視対象が変わった"));
    }
    let mut data: Vec<u8> = Vec::with_capacity(size.min(65536) as usize);
    let mut left = size;
    let mut count = 0;
    while left > 0 {
        budget.tick(0)?;
        let _ = hook!(Point::BeforeRead(count));
        count += 1;
        let want = left.min(65536) as usize;
        let start = data.len();
        data.resize(start + want, 0);
        let got = os::retry(|| os::read(opened.as_fd(), &mut data[start..]))
            .map_err(|error| os_err(error, None))?;
        data.truncate(start + got);
        if got == 0 {
            return Err(stop("読取中に短くなった"));
        }
        left -= got as u64;
    }
    let _ = hook!(Point::BeforeExtraRead);
    let mut extra = [0u8; 1];
    let more =
        os::retry(|| os::read(opened.as_fd(), &mut extra)).map_err(|error| os_err(error, None))?;
    if more > 0 {
        return Err(stop("読取中に大きくなった"));
    }
    let _ = hook!(Point::BeforeFinalCheck);
    let held = identity(&before);
    if held != identity(&fstat(opened.as_fd())?) || held != identity(&lstat_at(fd, name)?) {
        return Err(stop("読取中に監視対象が変わった"));
    }
    Ok((data, before.mode & S_IMODE))
}

/// 旧版の `read_regular(path, budget=None)`。予算が無ければ、親を開いた後に新しく取る。
pub fn read_regular(
    host: &dyn Host,
    path: &[u32],
    budget: Option<&mut EnvBudget>,
) -> Result<Vec<u8>, PyException> {
    with_parent(host, path, |fd, name| {
        let mut fresh;
        let budget = match budget {
            Some(budget) => budget,
            None => {
                fresh = EnvBudget::new();
                &mut fresh
            }
        };
        read_at(fd, name, budget, None).map(|(raw, _)| raw)
    })
}

// ---------------------------------------------------------------- write_new

/// 旧版の `write_new(path, raw, mode=0o600)`: 排他作成 → 全量の書込み → `fchmod` → `fsync`。
/// 失敗しても作ったファイルは消さない(旧版と同じ)。
#[cfg_attr(not(test), allow(unused_variables, unused_assignments))]
pub fn write_new(host: &dyn Host, path: &[u32], raw: &[u8], mode: u32) -> Result<(), PyException> {
    with_parent(host, path, |fd, name| {
        let bytes = enc(name)?;
        let out = os::retry(|| os::create_excl_at(fd, &bytes, mode))
            .map_err(|error| os_err(error, Some(name)))?;
        let _ = hook!(Point::AfterCreate);
        let mut left = raw;
        let mut count = 0;
        while !left.is_empty() {
            let fault = hook!(Point::Write(count));
            count += 1;
            #[cfg(test)]
            let chunk = match fault {
                Some(Fault::Error(error)) => return Err(os_err(error, None)),
                Some(Fault::Short(limit)) => &left[..limit.min(left.len())],
                None => left,
            };
            #[cfg(not(test))]
            let chunk = {
                let _ = fault;
                left
            };
            let written =
                os::retry(|| os::write(out.as_fd(), chunk)).map_err(|error| os_err(error, None))?;
            left = &left[written..];
        }
        let fault = hook!(Point::Fchmod);
        #[cfg(test)]
        if let Some(Fault::Error(error)) = fault {
            return Err(os_err(error, None));
        }
        #[cfg(not(test))]
        let _ = fault;
        os::retry(|| os::fchmod(out.as_fd(), mode)).map_err(|error| os_err(error, None))?;
        let fault = hook!(Point::Fsync);
        #[cfg(test)]
        if let Some(Fault::Error(error)) = fault {
            return Err(os_err(error, None));
        }
        #[cfg(not(test))]
        let _ = fault;
        os::retry(|| os::fsync(out.as_fd())).map_err(|error| os_err(error, None))?;
        Ok(())
    })
}

#[cfg(test)]
pub(crate) mod hooks {
    use std::cell::RefCell;
    use std::io;

    /// 競合を差し込める位置。
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub enum Point {
        /// 親の 1 段(文字番号の名前)を開いて、`fstat` した直後。
        ParentOpened(Vec<u32>),
        /// `read_at` の最初の `lstat` の直後。
        AfterLstat,
        /// 本文の `open` の直前(予約が済んだ後)。
        BeforeOpen,
        /// 本文の `open` の直後(`fstat` の比較の前)。
        AfterOpen,
        /// `n` 回目の本文の `read` の前。
        BeforeRead(usize),
        /// 1 バイトの追加 `read` の前。
        BeforeExtraRead,
        /// 最後の `fstat`・`lstat` の再照合の前。
        BeforeFinalCheck,
        /// `write_new` の排他作成の直後。
        AfterCreate,
        /// `write_new` の `n` 回目の `write` の前。
        Write(usize),
        Fchmod,
        Fsync,
    }

    pub enum Fault {
        Error(io::Error),
        /// 書込みを `n` バイトまでにする(短い書込み)。
        Short(usize),
    }

    type Hook = Box<dyn FnMut(&Point) -> Option<Fault>>;

    thread_local! {
        static HOOK: RefCell<Option<Hook>> = const { RefCell::new(None) };
    }

    pub fn fire(point: &Point) -> Option<Fault> {
        // フックの中でファイル操作をしても、再入しない。
        let taken = HOOK.with(|slot| slot.borrow_mut().take());
        let mut hook = taken?;
        let result = hook(point);
        HOOK.with(|slot| *slot.borrow_mut() = Some(hook));
        result
    }

    /// `hook` を入れて `body` を流し、終わったら外す。
    pub fn with<T>(
        hook: impl FnMut(&Point) -> Option<Fault> + 'static,
        body: impl FnOnce() -> T,
    ) -> T {
        struct Reset;
        impl Drop for Reset {
            fn drop(&mut self) {
                HOOK.with(|slot| *slot.borrow_mut() = None);
            }
        }
        HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
        let _reset = Reset;
        body()
    }
}

#[cfg(test)]
mod tests {
    use super::super::dispatch::ProcessHost;
    use super::super::pystr::{cps, decode_fs, to_string};
    use super::super::testing::fixture;
    use super::hooks::{Fault, Point, with};
    use super::*;
    use std::cell::RefCell;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::rc::Rc;
    use std::time::{Duration, Instant};

    /// テストの一時フォルダ。作った正確なパスだけを、終わりに消す。
    struct Dir(PathBuf);

    impl Dir {
        fn new(label: &str) -> Self {
            let base = std::env::var_os("S2E_TEST_TMP")
                .map(PathBuf::from)
                .unwrap_or_else(std::env::temp_dir);
            // `parent` はリンクを辿らないので、実パスで使う(Mac の `/var` はリンク)。
            let base = std::fs::canonicalize(base).unwrap();
            // 一意の名前を `create_dir`(既にあれば次の名前)で作り、作れたパスだけを後で消す。
            for count in 0u32.. {
                let path = base.join(format!("s2e-fsio-{}-{label}-{count}", std::process::id()));
                match std::fs::create_dir(&path) {
                    Ok(()) => return Self(path),
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                    Err(error) => panic!("{}: {error}", path.display()),
                }
            }
            unreachable!()
        }

        fn path(&self, rel: &str) -> PathBuf {
            self.0.join(rel)
        }

        fn cps(&self, rel: &str) -> Vec<u32> {
            cps_of(&self.path(rel))
        }

        fn write(&self, rel: &str, body: &[u8], mode: u32) -> PathBuf {
            let path = self.path(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, body).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
            path
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn cps_of(path: &Path) -> Vec<u32> {
        decode_fs(path.as_os_str().as_bytes())
    }

    fn message(error: &PyException) -> String {
        to_string(error.message.as_codepoints())
    }

    /// `read_regular` と同じ親の開き方で `read_at` を呼び、予算と戻り値を調べられるようにする。
    fn read_with(
        path: &[u32],
        budget: &mut EnvBudget,
        expected: Option<Identity>,
    ) -> Result<(Vec<u8>, u32), PyException> {
        with_parent(&ProcessHost, path, |fd, name| {
            read_at(fd, name, budget, expected)
        })
    }

    fn log() -> Rc<RefCell<Vec<Point>>> {
        Rc::new(RefCell::new(Vec::new()))
    }

    /// 親の段の記録(`ParentOpened`)を除いた、記録の列。
    fn steps(seen: &Rc<RefCell<Vec<Point>>>) -> Vec<Point> {
        seen.borrow()
            .iter()
            .filter(|point| !matches!(point, Point::ParentOpened(_)))
            .cloned()
            .collect()
    }

    fn pattern(size: usize) -> Vec<u8> {
        (0..size).map(|i| (i % 251) as u8).collect()
    }

    fn lstat_path(path: &Path) -> os::Stat {
        let root = os::open_root().unwrap();
        let parent = path.parent().unwrap();
        let mut fd = root;
        for part in parent.components().skip(1) {
            fd = os::open_dir_at(fd.as_fd(), part.as_os_str().as_bytes()).unwrap();
        }
        os::lstat_at(fd.as_fd(), path.file_name().unwrap().as_bytes()).unwrap()
    }

    #[test]
    fn identity_carries_every_compared_field() {
        let dir = Dir::new("identity");
        let path = dir.write("f", b"abc", 0o640);
        let stat = lstat_path(&path);
        let meta = std::fs::symlink_metadata(&path).unwrap();
        let id = identity(&stat);
        assert_eq!(id.dev, meta.dev());
        assert_eq!(id.ino, meta.ino());
        assert_eq!(id.mode, meta.mode());
        assert_eq!(id.size, 3);
        assert_eq!(
            id.mtime_ns,
            i128::from(meta.mtime()) * 1_000_000_000 + i128::from(meta.mtime_nsec())
        );
        assert_eq!(
            id.ctime_ns,
            i128::from(meta.ctime()) * 1_000_000_000 + i128::from(meta.ctime_nsec())
        );
        let directory = directory_identity(&stat);
        assert_eq!(
            (directory.dev, directory.ino, directory.mode),
            (id.dev, id.ino, id.mode)
        );
        assert_eq!(
            sha(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn read_regular_returns_the_bytes_and_the_budget_counts_them() {
        let dir = Dir::new("read-ok");
        let body = pattern(70_000);
        dir.write("f", &body, 0o640);
        let mut budget = EnvBudget::new();
        let (raw, mode) = read_with(&dir.cps("f"), &mut budget, None).unwrap();
        assert_eq!((raw, mode), (body.clone(), 0o640));
        assert_eq!(budget.bytes(), 70_000);
        assert_eq!(
            read_regular(&ProcessHost, &dir.cps("f"), None).unwrap(),
            body
        );
        let mut shared = EnvBudget::new();
        read_regular(&ProcessHost, &dir.cps("f"), Some(&mut shared)).unwrap();
        assert_eq!(shared.bytes(), 70_000);
        // 空のファイルは、本文の `read` を 1 回もしない。
        dir.write("empty", b"", 0o600);
        assert_eq!(
            read_regular(&ProcessHost, &dir.cps("empty"), None).unwrap(),
            b""
        );
    }

    #[test]
    fn read_follows_the_observation_order() {
        let dir = Dir::new("read-order");
        dir.write("f", &pattern(70_000), 0o644);
        dir.write("e", b"", 0o644);
        let seen = log();
        let sink = seen.clone();
        with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || read_with(&dir.cps("f"), &mut EnvBudget::new(), None).unwrap(),
        );
        assert_eq!(
            steps(&seen),
            [
                Point::AfterLstat,
                Point::BeforeOpen,
                Point::AfterOpen,
                Point::BeforeRead(0),
                Point::BeforeRead(1),
                Point::BeforeExtraRead,
                Point::BeforeFinalCheck,
            ]
        );
        seen.borrow_mut().clear();
        let sink = seen.clone();
        with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || read_with(&dir.cps("e"), &mut EnvBudget::new(), None).unwrap(),
        );
        assert_eq!(
            steps(&seen),
            [
                Point::AfterLstat,
                Point::BeforeOpen,
                Point::AfterOpen,
                Point::BeforeExtraRead,
                Point::BeforeFinalCheck
            ]
        );
    }

    /// 開く前に止まる入力は、`open` の位置(`AfterOpen`)に届かず、予算も使わない。
    fn assert_stops_before_open(path: &[u32], expected: Option<Identity>, reason: &str) {
        let reached = log();
        let sink = reached.clone();
        let mut budget = EnvBudget::new();
        let error = with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || read_with(path, &mut budget, expected).unwrap_err(),
        );
        assert_eq!(error.type_name, "Stop");
        assert_eq!(message(&error), reason);
        assert_eq!(steps(&reached), [Point::AfterLstat], "{reason}");
        assert_eq!((budget.bytes(), budget.entries()), (0, 0), "{reason}");
    }

    #[test]
    fn rejections_happen_before_the_open_and_the_reservation() {
        let dir = Dir::new("reject");
        let not_regular = "監視対象が読める通常ファイルでない";
        std::fs::create_dir(dir.path("d")).unwrap();
        assert_stops_before_open(&dir.cps("d"), None, not_regular);
        symlink("f", dir.path("l")).unwrap();
        dir.write("f", b"x", 0o644);
        assert_stops_before_open(&dir.cps("l"), None, not_regular);
        assert!(
            std::process::Command::new("mkfifo")
                .arg(dir.path("p"))
                .status()
                .unwrap()
                .success()
        );
        assert_stops_before_open(&dir.cps("p"), None, not_regular);
        dir.write("w", b"x", 0o200);
        assert_stops_before_open(&dir.cps("w"), None, not_regular);
        // 保持値との比較は、種類の確認より先。
        let mut stale = identity(&lstat_path(&dir.path("f")));
        stale.size += 1;
        assert_stops_before_open(&dir.cps("f"), Some(stale), "本文読取前に監視対象が変わった");
        assert_stops_before_open(&dir.cps("p"), Some(stale), "本文読取前に監視対象が変わった");
        // 保持値が合えば通る。
        let held = identity(&lstat_path(&dir.path("f")));
        assert_eq!(
            read_with(&dir.cps("f"), &mut EnvBudget::new(), Some(held))
                .unwrap()
                .0,
            b"x"
        );
        // 無い名前は、名前つきの `FileNotFoundError`。
        let error = read_with(&dir.cps("none"), &mut EnvBudget::new(), None).unwrap_err();
        assert_eq!(error.type_name, "FileNotFoundError");
        assert_eq!(
            message(&error),
            "[Errno 2] No such file or directory: 'none'"
        );
    }

    #[test]
    fn reservation_runs_before_the_open_and_uses_the_limits() {
        let dir = Dir::new("reserve");
        let file = std::fs::File::create(dir.path("big")).unwrap();
        file.set_len(super::super::budget::MAX_FILE + 1).unwrap();
        let reached = log();
        let sink = reached.clone();
        let mut budget = EnvBudget::new();
        let error = with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || read_with(&dir.cps("big"), &mut budget, None).unwrap_err(),
        );
        assert_eq!(message(&error), super::super::budget::RESERVE_STOP);
        assert_eq!(steps(&reached), [Point::AfterLstat]);
        assert_eq!(budget.bytes(), 0);
        // ちょうど 1 ファイルの上限は読める。
        std::fs::File::options()
            .write(true)
            .open(dir.path("big"))
            .unwrap()
            .set_len(super::super::budget::MAX_FILE)
            .unwrap();
        let (raw, _) = read_with(&dir.cps("big"), &mut EnvBudget::new(), None).unwrap();
        assert_eq!(raw.len() as u64, super::super::budget::MAX_FILE);
        // 総量の上限の直後は、`open` の前に止まる。
        let mut full = EnvBudget::new();
        for _ in 0..7 {
            full.reserve(super::super::budget::MAX_FILE).unwrap();
        }
        full.reserve(super::super::budget::MAX_FILE - 2).unwrap();
        dir.write("two", b"ab", 0o644);
        dir.write("three", b"abc", 0o644);
        assert!(read_with(&dir.cps("two"), &mut full, None).is_ok());
        let error = read_with(&dir.cps("three"), &mut full, None).unwrap_err();
        assert_eq!(message(&error), super::super::budget::RESERVE_STOP);
    }

    fn race(
        label: &str,
        body: &[u8],
        point: Point,
        action: impl Fn(&Path) + 'static,
    ) -> PyException {
        let dir = Dir::new(&format!("race-{label}"));
        let path = dir.write("f", body, 0o644);
        let watched = path.clone();
        with(
            move |at| {
                if *at == point {
                    action(&watched);
                }
                None
            },
            || read_with(&cps_of(&path), &mut EnvBudget::new(), None).unwrap_err(),
        )
    }

    fn replace_with_new_file(path: &Path) {
        let body = std::fs::read(path).unwrap();
        std::fs::remove_file(path).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        std::fs::write(path, body).unwrap();
    }

    #[test]
    fn races_between_the_steps_are_rejected_with_the_old_reasons() {
        let small = pattern(100);
        let big = pattern(70_000);
        // lstat と open の間で別のファイルに替わった。
        let error = race("swap", &small, Point::AfterLstat, replace_with_new_file);
        assert_eq!(message(&error), "open 時に監視対象が変わった");
        // lstat と open の間でリンクに替わった: nofollow の open が止める(本文は読まない)。
        let error = race("link", &small, Point::AfterLstat, |path| {
            std::fs::remove_file(path).unwrap();
            symlink("/etc/hostname", path).unwrap();
        });
        assert_eq!(error.type_name, "OSError");
        assert!(message(&error).starts_with("[Errno "));
        assert!(message(&error).ends_with(": 'f'"), "{}", message(&error));
        // open の後、fstat の比較の前に変わった。
        let error = race("after-open", &small, Point::AfterOpen, |path| {
            std::fs::OpenOptions::new().append(true).open(path).unwrap();
            std::fs::write(path, b"changed").unwrap();
        });
        assert_eq!(message(&error), "open 時に監視対象が変わった");
        // 1 回目の read の前に、0 バイトへ。
        let error = race("short-0", &small, Point::BeforeRead(0), |path| {
            std::fs::File::options()
                .write(true)
                .open(path)
                .unwrap()
                .set_len(0)
                .unwrap();
        });
        assert_eq!(message(&error), "読取中に短くなった");
        // 2 回目の read の前に、位置より小さく。
        let error = race("short-1", &big, Point::BeforeRead(1), |path| {
            std::fs::File::options()
                .write(true)
                .open(path)
                .unwrap()
                .set_len(100)
                .unwrap();
        });
        assert_eq!(message(&error), "読取中に短くなった");
        // 全部読んだ後、1 バイトの追加 read の前に増えた。
        let error = race("grow", &big, Point::BeforeExtraRead, |path| {
            use std::io::Write;
            std::fs::OpenOptions::new()
                .append(true)
                .open(path)
                .unwrap()
                .write_all(b"x")
                .unwrap();
        });
        assert_eq!(message(&error), "読取中に大きくなった");
        // 最後の照合の前に、属性だけが変わった(ctime と mode)。
        let error = race("attrs", &small, Point::BeforeFinalCheck, |path| {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
        });
        assert_eq!(message(&error), "読取中に監視対象が変わった");
        // 最後の照合の前に、同じ中身の別ファイルに名前だけが差し替わった(fstat は変わらず lstat が変わる)。
        let error = race(
            "rename",
            &small,
            Point::BeforeFinalCheck,
            replace_with_new_file,
        );
        assert_eq!(message(&error), "読取中に監視対象が変わった");
        assert_eq!(error.type_name, "Stop");
    }

    #[test]
    fn a_swapped_parent_is_rejected_after_the_body() {
        // 1 段を別のフォルダに差し替える。
        let dir = Dir::new("parent-swap");
        dir.write("a/b/f", b"data", 0o644);
        let root = dir.0.clone();
        let error = with(
            move |point| {
                if *point == Point::ParentOpened(cps("b")) {
                    std::fs::rename(root.join("a"), root.join("a2")).unwrap();
                    std::fs::create_dir(root.join("a")).unwrap();
                }
                None
            },
            || read_regular(&ProcessHost, &dir.cps("a/b/f"), None).unwrap_err(),
        );
        assert_eq!(error.type_name, "Stop");
        assert_eq!(message(&error), "検査中に親ディレクトリが差し替えられた");
        // 差し替えずに名前だけが消えたときは、名前つきの `FileNotFoundError`(照合の `stat` が失敗する)。
        let dir = Dir::new("parent-gone");
        dir.write("a/b/f", b"data", 0o644);
        let root = dir.0.clone();
        let error = with(
            move |point| {
                if *point == Point::ParentOpened(cps("b")) {
                    std::fs::rename(root.join("a"), root.join("a2")).unwrap();
                }
                None
            },
            || read_regular(&ProcessHost, &dir.cps("a/b/f"), None).unwrap_err(),
        );
        assert_eq!(error.type_name, "FileNotFoundError");
        assert_eq!(message(&error), "[Errno 2] No such file or directory: 'a'");
        // mode だけが変わった親も、3 項目の照合で止まる。
        let dir = Dir::new("parent-mode");
        dir.write("a/f", b"data", 0o644);
        let root = dir.0.clone();
        let error = with(
            move |point| {
                if *point == Point::ParentOpened(cps("a")) {
                    std::fs::set_permissions(
                        root.join("a"),
                        std::fs::Permissions::from_mode(0o750),
                    )
                    .unwrap();
                }
                None
            },
            || read_regular(&ProcessHost, &dir.cps("a/f"), None).unwrap_err(),
        );
        assert_eq!(message(&error), "検査中に親ディレクトリが差し替えられた");
        // 変わらなければ通り、最後の名前は空にならない。
        let dir = Dir::new("parent-ok");
        dir.write("a/f", b"data", 0o644);
        assert_eq!(
            read_regular(&ProcessHost, &dir.cps("a/f"), None).unwrap(),
            b"data"
        );
    }

    #[test]
    fn the_recheck_is_skipped_when_the_body_fails() {
        let dir = Dir::new("parent-body-fails");
        dir.write("a/f", b"data", 0o644);
        let root = dir.0.clone();
        let error = with(
            move |point| {
                if *point == Point::ParentOpened(cps("a")) {
                    std::fs::rename(root.join("a"), root.join("a2")).unwrap();
                }
                None
            },
            || {
                with_parent(
                    &ProcessHost,
                    &dir.cps("a/f"),
                    |_, _| -> Result<(), PyException> { Err(PyException::stop("本文の失敗")) },
                )
                .unwrap_err()
            },
        );
        assert_eq!(message(&error), "本文の失敗");
    }

    #[test]
    fn links_and_special_names_are_not_followed() {
        let dir = Dir::new("links");
        dir.write("real/f", b"data", 0o644);
        symlink("real", dir.path("ln")).unwrap();
        // 途中がリンク: nofollow の open が止め、リンク先の本文は読まない。
        let error = read_regular(&ProcessHost, &dir.cps("ln/f"), None).unwrap_err();
        assert!(
            matches!(error.type_name.as_str(), "OSError" | "NotADirectoryError"),
            "{}",
            error.type_name
        );
        assert!(message(&error).ends_with(": 'ln'"), "{}", message(&error));
        // 最後がリンク。
        symlink("real/f", dir.path("last")).unwrap();
        let error = read_regular(&ProcessHost, &dir.cps("last"), None).unwrap_err();
        assert_eq!(message(&error), "監視対象が読める通常ファイルでない");
        // 根だけのパスは名前が空。旧版の `os.stat('', dir_fd=…)` と同じく名前つきの `FileNotFoundError`。
        let error = read_regular(&ProcessHost, &cps("/"), None).unwrap_err();
        assert_eq!(error.type_name, "FileNotFoundError");
        assert_eq!(message(&error), "[Errno 2] No such file or directory: ''");
        // NUL を含む名前。
        let mut bad = dir.cps("real");
        bad.extend(cps("/a"));
        bad.push(0);
        let error = read_regular(&ProcessHost, &bad, None).unwrap_err();
        assert_eq!(error.type_name, "ValueError");
        assert_eq!(message(&error), "embedded null byte");
        // 符号化できない文字。
        let mut bad = dir.cps("real");
        bad.extend(cps("/"));
        bad.push(0xd800);
        let error = read_regular(&ProcessHost, &bad, None).unwrap_err();
        assert_eq!(error.type_name, "UnicodeEncodeError");
    }

    #[test]
    fn write_new_creates_writes_chmods_and_syncs_in_order() {
        let dir = Dir::new("write-ok");
        let seen = log();
        let sink = seen.clone();
        with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || write_new(&ProcessHost, &dir.cps("out"), b"hello", 0o666).unwrap(),
        );
        assert_eq!(
            steps(&seen),
            [
                Point::AfterCreate,
                Point::Write(0),
                Point::Fchmod,
                Point::Fsync
            ]
        );
        assert_eq!(std::fs::read(dir.path("out")).unwrap(), b"hello");
        // `fchmod` が umask を受けず、指定どおりの mode になる。
        assert_eq!(
            std::fs::metadata(dir.path("out")).unwrap().mode() & 0o7777,
            0o666
        );
        // 空の本文は `write` を呼ばない。
        let seen = log();
        let sink = seen.clone();
        with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                None
            },
            || write_new(&ProcessHost, &dir.cps("empty"), b"", 0o600).unwrap(),
        );
        assert_eq!(
            steps(&seen),
            [Point::AfterCreate, Point::Fchmod, Point::Fsync]
        );
    }

    #[test]
    fn write_new_refuses_existing_names_and_missing_parents() {
        let dir = Dir::new("write-excl");
        dir.write("f", b"old", 0o644);
        let error = write_new(&ProcessHost, &dir.cps("f"), b"new", 0o600).unwrap_err();
        assert_eq!(error.type_name, "FileExistsError");
        assert_eq!(message(&error), "[Errno 17] File exists: 'f'");
        assert_eq!(std::fs::read(dir.path("f")).unwrap(), b"old");
        // 既存のリンク(宛先が無くても)の先は作らない。
        symlink("target", dir.path("l")).unwrap();
        let error = write_new(&ProcessHost, &dir.cps("l"), b"new", 0o600).unwrap_err();
        assert_eq!(error.type_name, "FileExistsError");
        assert!(!dir.path("target").exists());
        // 親が無い。
        let error = write_new(&ProcessHost, &dir.cps("none/f"), b"new", 0o600).unwrap_err();
        assert_eq!(error.type_name, "FileNotFoundError");
        assert_eq!(
            message(&error),
            "[Errno 2] No such file or directory: 'none'"
        );
    }

    #[test]
    fn write_new_loops_over_short_writes_and_keeps_the_file_on_failure() {
        let dir = Dir::new("write-fail");
        let seen = log();
        let sink = seen.clone();
        with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                matches!(point, Point::Write(_)).then_some(Fault::Short(3))
            },
            || write_new(&ProcessHost, &dir.cps("short"), b"abcdefghij", 0o600).unwrap(),
        );
        assert_eq!(std::fs::read(dir.path("short")).unwrap(), b"abcdefghij");
        assert_eq!(
            seen.borrow()
                .iter()
                .filter(|point| matches!(point, Point::Write(_)))
                .count(),
            4
        );
        // 書込みの失敗: 作ったファイルは残り、`fchmod`・`fsync` には進まない。
        let seen = log();
        let sink = seen.clone();
        let error = with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                matches!(point, Point::Write(_))
                    .then(|| Fault::Error(io::Error::from_raw_os_error(28)))
            },
            || write_new(&ProcessHost, &dir.cps("nospace"), b"abc", 0o600).unwrap_err(),
        );
        assert_eq!(error.type_name, "OSError");
        assert_eq!(message(&error), "[Errno 28] No space left on device");
        assert_eq!(steps(&seen), [Point::AfterCreate, Point::Write(0)]);
        assert_eq!(std::fs::read(dir.path("nospace")).unwrap(), b"");
        // `fchmod` の失敗: 本文は書けていて、`fsync` には進まない。
        let seen = log();
        let sink = seen.clone();
        let error = with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                (*point == Point::Fchmod).then(|| Fault::Error(io::Error::from_raw_os_error(1)))
            },
            || write_new(&ProcessHost, &dir.cps("nochmod"), b"abc", 0o666).unwrap_err(),
        );
        assert_eq!(error.type_name, "PermissionError");
        assert_eq!(std::fs::read(dir.path("nochmod")).unwrap(), b"abc");
        assert!(!seen.borrow().contains(&Point::Fsync));
        // `fsync` の失敗: mode は適用済み。
        let error = with(
            |point| (*point == Point::Fsync).then(|| Fault::Error(io::Error::from_raw_os_error(5))),
            || write_new(&ProcessHost, &dir.cps("nosync"), b"abc", 0o666).unwrap_err(),
        );
        assert_eq!(message(&error), "[Errno 5] Input/output error");
        assert_eq!(
            std::fs::metadata(dir.path("nosync")).unwrap().mode() & 0o7777,
            0o666
        );
    }

    #[test]
    fn write_new_rechecks_the_parent_after_the_body() {
        let dir = Dir::new("write-parent");
        std::fs::create_dir(dir.path("d")).unwrap();
        let root = dir.0.clone();
        let error = with(
            move |point| {
                if *point == Point::AfterCreate {
                    std::fs::rename(root.join("d"), root.join("d2")).unwrap();
                    std::fs::create_dir(root.join("d")).unwrap();
                }
                None
            },
            || write_new(&ProcessHost, &dir.cps("d/out"), b"abc", 0o600).unwrap_err(),
        );
        assert_eq!(message(&error), "検査中に親ディレクトリが差し替えられた");
        // 作ったファイルは、差し替え前のフォルダに残る。
        assert!(dir.path("d2/out").exists());
        assert!(!dir.path("d/out").exists());
    }

    #[test]
    fn chmod_nofollow_cases_match_python_3_10() {
        if !cfg!(target_os = "linux") {
            eprintln!("固定期待は Linux(glibc)の旧版。Mac は未確認");
            return;
        }
        let dir = Dir::new("chmod");
        std::fs::create_dir(dir.path("d")).unwrap();
        symlink("d", dir.path("l")).unwrap();
        dir.write("f", b"", 0o644);
        let fixtures = fixture!("child_cases.json");
        let cases = fixtures["chmod_cases"].as_array().unwrap();
        assert_eq!(cases.len(), 4);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let target = dir.cps(name);
            let got = with_parent(&ProcessHost, &target, |fd, name| {
                chmod_nofollow_at(fd, name, 0o750)
            });
            match case.get("error") {
                None => assert!(got.is_ok(), "{name}"),
                Some(kind) => {
                    let error = got.unwrap_err();
                    assert_eq!(error.type_name, kind.as_str().unwrap(), "{name}");
                    assert_eq!(message(&error), case["message"].as_str().unwrap(), "{name}");
                }
            }
        }
        assert_eq!(
            std::fs::metadata(dir.path("d")).unwrap().mode() & 0o777,
            0o750
        );
        assert_eq!(
            std::fs::metadata(dir.path("f")).unwrap().mode() & 0o777,
            0o750
        );
    }

    #[test]
    fn the_deadline_is_checked_before_every_read_and_before_it() {
        let dir = Dir::new("deadline");
        dir.write("f", &pattern(100), 0o644);
        let seen = log();
        let sink = seen.clone();
        // 予約は期限内に済み、`open` の後で期限が過ぎる。読取りのループの先頭の `tick` が止める。
        let mut budget = EnvBudget::with_deadline(Instant::now() + Duration::from_millis(300));
        let error = with(
            move |point| {
                sink.borrow_mut().push(point.clone());
                if *point == Point::AfterOpen {
                    std::thread::sleep(Duration::from_millis(600));
                }
                None
            },
            || read_with(&dir.cps("f"), &mut budget, None).unwrap_err(),
        );
        assert_eq!(message(&error), super::super::budget::TICK_STOP);
        // `read` の前の記録(`BeforeRead`)には届かない。`tick` が `read` の後ろへ動けば届く。
        assert_eq!(
            steps(&seen),
            [Point::AfterLstat, Point::BeforeOpen, Point::AfterOpen]
        );
    }

    /// 予約は `open` より先。`open` の直前(予約の後)に差し替えれば、`open` 後の `fstat` の比較が止める。
    /// 予約より前に `open` していれば、差し替えは `open` の後に起き、多くは最後の照合(読取中に監視対象が変わった)で止まる。
    /// ただし `unlink` で `ctime` が変われば同じ理由になることがあるので、確実に見つけるのは
    /// `an_oversized_unreadable_file_stops_at_the_reservation_not_at_the_open`。
    #[test]
    fn a_swap_right_before_the_open_is_caught_by_the_fstat_comparison() {
        let error = race(
            "before-open",
            &pattern(100),
            Point::BeforeOpen,
            replace_with_new_file,
        );
        assert_eq!(error.type_name, "Stop");
        assert_eq!(message(&error), "open 時に監視対象が変わった");
    }

    /// 読めない(所有者に読み取り権限が無い)大きなファイルは、`open` の権限の誤りではなく予約の上限で止まる。
    /// `open` が予約より先なら、`[Errno 13] Permission denied` になる。
    #[test]
    fn an_oversized_unreadable_file_stops_at_the_reservation_not_at_the_open() {
        use std::os::unix::fs::PermissionsExt;
        if std::process::Command::new("id")
            .arg("-u")
            .output()
            .map(|o| o.stdout)
            .unwrap_or_default()
            == b"0\n"
        {
            eprintln!("root は権限を無視するので省く");
            return;
        }
        let dir = Dir::new("unreadable-big");
        let path = dir.path("big");
        let file = std::fs::File::create(&path).unwrap();
        file.set_len(super::super::budget::MAX_FILE + 1).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o004)).unwrap();
        let error = read_with(&dir.cps("big"), &mut EnvBudget::new(), None).unwrap_err();
        assert_eq!(error.type_name, "Stop");
        assert_eq!(message(&error), super::super::budget::RESERVE_STOP);
    }

    #[test]
    fn a_fifo_swapped_in_after_lstat_is_opened_without_blocking_and_rejected() {
        // 非 block の `open` でなければ、書き手の居ない FIFO の `open` は止まる。
        let error = race("fifo", &pattern(100), Point::AfterLstat, |path| {
            std::fs::remove_file(path).unwrap();
            assert!(
                std::process::Command::new("mkfifo")
                    .arg(path)
                    .status()
                    .unwrap()
                    .success()
            );
        });
        assert_eq!(error.type_name, "Stop");
        assert_eq!(message(&error), "open 時に監視対象が変わった");
    }

    /// `SIGINT` の印を立てるテストは、プロセス全体の信号の扱いを変えるので、別プロセスで行う。
    fn isolated(name: &str) -> bool {
        if std::env::var_os("S2E_FSIO_ISOLATED").is_some_and(|value| value == name) {
            return true;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("environment_guard::fsio::tests::{name}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S2E_FSIO_ISOLATED", name)
            .output()
            .unwrap();
        let text = format!(
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            output.status.success(),
            "別プロセスの検査が失敗({name}): {text}"
        );
        assert!(
            text.contains("1 passed"),
            "別プロセスが検査を流していない({name}): {text}"
        );
        false
    }

    #[test]
    fn an_interrupted_call_is_a_keyboard_interrupt_only_after_sigint() {
        if !isolated("an_interrupted_call_is_a_keyboard_interrupt_only_after_sigint") {
            return;
        }
        let dir = Dir::new("interrupt");
        let eintr = || Fault::Error(io::Error::from_raw_os_error(os::EINTR));
        // `SIGINT` が来ていない `EINTR` は、`InterruptedError`(旧版の `os.write` 以外の素の `OSError`)。
        let error = with(
            move |point| (*point == Point::Write(0)).then(eintr),
            || write_new(&ProcessHost, &dir.cps("a"), b"abc", 0o600).unwrap_err(),
        );
        assert_eq!(error.type_name, "InterruptedError");
        assert_eq!(message(&error), "[Errno 4] Interrupted system call");
        // `SIGINT` を受けた後の `EINTR` は `KeyboardInterrupt`。印は消える。
        os::signal_set_default(os::SIGINT).unwrap();
        assert!(os::install_sigint_handler().unwrap());
        let sent = std::process::Command::new("kill")
            .args(["-INT", &std::process::id().to_string()])
            .status()
            .unwrap();
        assert!(sent.success());
        let start = Instant::now();
        while !os::sigint_pending() {
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "SIGINT が届かない"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let error = with(
            move |point| (*point == Point::Write(0)).then(eintr),
            || write_new(&ProcessHost, &dir.cps("b"), b"abc", 0o600).unwrap_err(),
        );
        assert_eq!(error.type_name, "KeyboardInterrupt");
        assert!(!os::sigint_pending());
    }
}
