//! 旧版の`os`と`time`の呼出し1回ずつの層。
//!
//! 走査は、この層の呼出しだけでファイルと時計に触れる。旧版の呼出しと1対1に対応させ、
//! 呼ぶ順と回数を旧版と同じにする。検査専用の入口は、この型を差し替えて故障を与える
//! (製品の引数・環境変数では差し替えられない)。

use super::ScanError;
use super::support::Clock;
use dev_workflow_platform::r4g::fs::{self, ScanError as DirError, Stat};
use dev_workflow_platform::r4g::{Errno, OFlags, startup};
use std::collections::HashMap;
use std::ffi::CString;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd};

/// 旧版で起きうる例外。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Exc {
    /// `OSError`(エラー番号から派生の型が決まる。`FileNotFoundError`は`ENOENT`)。
    Os(Errno),
    /// 旧版の`ScanError`。
    Scan(ScanError),
    /// `OSError`でも`ScanError`でもない`Exception`(型の名前)。NULを含むパス(`ValueError`)と、
    /// 検査専用の注入だけで起きる。
    Unexpected(String),
    /// `KeyboardInterrupt`。
    Interrupt,
}

impl From<ScanError> for Exc {
    fn from(error: ScanError) -> Self {
        Self::Scan(error)
    }
}

/// 旧版のfd(`int`)。
pub type Fd = i32;

/// 旧版の`os`・`time`の呼出し。`stat`は旧版のとおり、いつも`follow_symlinks=False`。
pub trait System {
    /// `os.stat(path, dir_fd=dir, follow_symlinks=False)`。
    fn stat(&mut self, dir: Option<Fd>, path: &[u8]) -> Result<Stat, Exc>;
    /// `os.fstat(fd)`。
    fn fstat(&mut self, fd: Fd) -> Result<Stat, Exc>;
    /// `os.open(path, flags, dir_fd=dir)`。
    fn open(&mut self, dir: Option<Fd>, path: &[u8], flags: OFlags) -> Result<Fd, Exc>;
    /// `os.close(fd)`。
    fn close(&mut self, fd: Fd) -> Result<(), Exc>;
    /// `os.read(fd, length)`。
    fn read(&mut self, fd: Fd, length: usize) -> Result<Vec<u8>, Exc>;
    /// `os.readlink(path)`。
    fn readlink(&mut self, path: &[u8]) -> Result<Vec<u8>, Exc>;
    /// `os.scandir(fd)`の呼出しと、名前の読取り。
    ///
    /// 開始の失敗は`Err`。読んだ名前(最大`limit`個。`.`と`..`は除く)と、途中の読取りの失敗を返す。
    /// 旧版は名前ごとに処理を挟むが、名前の読取りはOSの呼出しの記録に出ないので、先に読んで
    /// 同じ順に処理する。旧版が`limit`個目で止まる所では、ここでも読むのをやめる。
    fn scandir(&mut self, fd: Fd, limit: usize) -> Result<(Vec<Vec<u8>>, Option<Exc>), Exc>;
    /// `os.getcwdb()`。
    fn getcwd(&mut self) -> Result<Vec<u8>, Exc>;
    /// `time.monotonic()`。
    fn monotonic(&mut self) -> Result<f64, Exc>;
    /// `os.environ.get(name)`(起動の時点の環境。同じ名前は最初の値)。
    fn environ(&self, name: &[u8]) -> Option<Vec<u8>>;
    /// 判定の`item["reason"] in OVERLAP_REASONS`の1回。
    fn is_overlap_reason(&mut self, reason: Option<&'static str>) -> Result<bool, Exc> {
        Ok(matches!(reason, Some("overlap") | Some("overlap-unknown")))
    }
    /// 旧Pythonの評価の合間の信号の確かめ。`SIGINT`の印があれば`KeyboardInterrupt`にする。
    fn check_interrupt(&mut self) -> Result<(), Exc>;
}

/// 本物のファイルと時計。
pub struct RealSystem {
    fds: HashMap<Fd, OwnedFd>,
    clock: Box<dyn Clock>,
    environ: Vec<(Vec<u8>, Vec<u8>)>,
}

impl RealSystem {
    /// `environ`は起動の時点の環境(`environ`の順)。
    pub fn new(clock: Box<dyn Clock>, environ: Vec<(Vec<u8>, Vec<u8>)>) -> Self {
        Self {
            fds: HashMap::new(),
            clock,
            environ,
        }
    }

    fn borrow(&self, fd: Fd) -> Result<BorrowedFd<'_>, Exc> {
        self.fds
            .get(&fd)
            .map(|owned| owned.as_fd())
            .ok_or(Exc::Os(Errno::BADF))
    }
}

/// 旧Pythonのパスの変換。NULを含むと`ValueError: embedded null byte`。
fn c_path(path: &[u8]) -> Result<CString, Exc> {
    CString::new(path).map_err(|_| Exc::Unexpected("ValueError".to_string()))
}

impl System for RealSystem {
    fn stat(&mut self, dir: Option<Fd>, path: &[u8]) -> Result<Stat, Exc> {
        self.check_interrupt()?;
        let path = c_path(path)?;
        let base = dir.map(|fd| self.borrow(fd)).transpose()?;
        fs::stat_at(base, &path, false).map_err(Exc::Os)
    }

    fn fstat(&mut self, fd: Fd) -> Result<Stat, Exc> {
        self.check_interrupt()?;
        fs::fstat(self.borrow(fd)?).map_err(Exc::Os)
    }

    fn open(&mut self, dir: Option<Fd>, path: &[u8], flags: OFlags) -> Result<Fd, Exc> {
        self.check_interrupt()?;
        let path = c_path(path)?;
        let base = dir.map(|fd| self.borrow(fd)).transpose()?;
        let owned = fs::open_at(base, &path, flags).map_err(Exc::Os)?;
        let raw = owned.as_raw_fd();
        self.fds.insert(raw, owned);
        Ok(raw)
    }

    fn close(&mut self, fd: Fd) -> Result<(), Exc> {
        self.check_interrupt()?;
        let owned = self.fds.remove(&fd).ok_or(Exc::Os(Errno::BADF))?;
        fs::close(owned).map_err(Exc::Os)
    }

    fn read(&mut self, fd: Fd, length: usize) -> Result<Vec<u8>, Exc> {
        self.check_interrupt()?;
        fs::read(self.borrow(fd)?, length).map_err(Exc::Os)
    }

    fn readlink(&mut self, path: &[u8]) -> Result<Vec<u8>, Exc> {
        self.check_interrupt()?;
        let path = c_path(path)?;
        fs::read_link(None, &path).map_err(Exc::Os)
    }

    fn scandir(&mut self, fd: Fd, limit: usize) -> Result<(Vec<Vec<u8>>, Option<Exc>), Exc> {
        self.check_interrupt()?;
        let mut names = Vec::new();
        let result = fs::scandir(self.borrow(fd)?, |name| {
            names.push(name.to_vec());
            if names.len() >= limit {
                Err(())
            } else {
                Ok(())
            }
        });
        match result {
            Ok(()) | Err(DirError::Stopped(())) => Ok((names, None)),
            Err(DirError::Os(errno)) if names.is_empty() => {
                // 開始(複製・`fdopendir`)か最初の読取りの失敗。どちらも旧版の`except`は同じ。
                Err(Exc::Os(errno))
            }
            Err(DirError::Os(errno)) => Ok((names, Some(Exc::Os(errno)))),
        }
    }

    fn getcwd(&mut self) -> Result<Vec<u8>, Exc> {
        self.check_interrupt()?;
        fs::getcwd().map_err(Exc::Os)
    }

    fn monotonic(&mut self) -> Result<f64, Exc> {
        self.check_interrupt()?;
        Ok(self.clock.now())
    }

    fn environ(&self, name: &[u8]) -> Option<Vec<u8>> {
        self.environ
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    }

    fn check_interrupt(&mut self) -> Result<(), Exc> {
        if startup::take_python_interrupt() {
            Err(Exc::Interrupt)
        } else {
            Ok(())
        }
    }
}

impl Drop for RealSystem {
    fn drop(&mut self) {
        // 旧版は走査の後にすべて閉じる。途中の失敗で残ったfdも、ここで閉じる。
        self.fds.clear();
    }
}

/// 旧版の`ScanError`を作る。
pub fn scan_error(
    code: &'static str,
    message: &str,
    root_id: Option<&'static str>,
    path: Option<&[u8]>,
) -> Exc {
    Exc::Scan(ScanError {
        code,
        message: message.to_string(),
        root_id,
        path: path.map(<[u8]>::to_vec),
        exit_code: 11,
    })
}
