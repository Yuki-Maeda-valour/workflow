//! task-digest 専用のリンクを辿る読取り。C0b の保持実体とは契約が異なる。
use rustix::fd::OwnedFd;
use rustix::fs::{self, FileType, Mode, OFlags};
use std::io;
use std::path::{Path, PathBuf};

/// Python 3.10 shutil.get_terminal_size と同じ stdout の端末を調べる。
/// 端末でない場合と取得失敗だけ80へ戻す。幅0の取得値は変更しない。
pub fn stdout_columns() -> usize {
    rustix::termios::tcgetwinsize(std::io::stdout())
        .map(|size| usize::from(size.ws_col))
        .unwrap_or(80)
}

#[derive(Debug)]
pub enum ReadError {
    NotRegular,
    Io { error: io::Error, filename: bool },
}

fn os_error(error: rustix::io::Errno, filename: bool) -> ReadError {
    ReadError::Io {
        error: error.into(),
        filename,
    }
}

/// 事前の種類確認だけを保持する。属性や inode の一致は要求しない。
#[derive(Debug)]
pub struct RegularPath(PathBuf);

impl RegularPath {
    pub fn inspect(path: &Path) -> Result<Self, ReadError> {
        let before = fs::stat(path).map_err(|e| os_error(e, true))?;
        if FileType::from_raw_mode(before.st_mode) != FileType::RegularFile {
            return Err(ReadError::NotRegular);
        }
        Ok(Self(path.to_owned()))
    }

    /// 待たずに開き、その fd の種類を確認する。リンク拒否は指定しない。
    pub fn open(self) -> Result<RegularFile, ReadError> {
        let fd = fs::open(
            &self.0,
            OFlags::RDONLY | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|e| os_error(e, true))?;
        let opened = fs::fstat(&fd).map_err(|e| os_error(e, false))?;
        if FileType::from_raw_mode(opened.st_mode) != FileType::RegularFile {
            return Err(ReadError::NotRegular);
        }
        Ok(RegularFile(fd))
    }
}

/// 開いた実体だけを読む。パスの再検査・再度の open・量の上限は加えない。
#[derive(Debug)]
pub struct RegularFile(OwnedFd);

impl RegularFile {
    pub fn read_chunk(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        rustix::io::read(&self.0, buffer).map_err(Into::into)
    }
}

/// Linux fullデバイスを、実際に書く同じfdの種類・デバイス番号で確認する。
/// 確認失敗、別OS、通常ファイル、別の文字デバイスは許可しない。
pub fn is_linux_full(fd: &impl std::os::fd::AsFd) -> bool {
    #[cfg(target_os = "linux")]
    {
        fs::fstat(fd).is_ok_and(|info| {
            FileType::from_raw_mode(info.st_mode) == FileType::CharacterDevice
                && fs::major(info.st_rdev) == 1
                && fs::minor(info.st_rdev) == 7
        })
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = fd;
        false
    }
}
