//! R4G専用のOS部品。旧Python 3.10と旧bashの起動・ファイル操作・子の実行を再現する。
//!
//! 製品の登録表からは使わない。検査専用の入口(`r4g_contract`)だけが使う。
//! unsafe(Rustの安全検査を外す書き方)は、このモジュールの3つのファイルの中だけに置く。
//!
//! - `startup`: 標準ライブラリが`main`の前に変える前の、`SIGPIPE`と信号の扱いの起動前の記録と戻し
//!   (fd 0〜2は記録しない。2026-11-12 08:55の決定)。
//! - `fs`: fd相対の読取り(`openat`・`fstatat`・readdir・`..`・上限付きの読取り)と、長いパスの現在のディレクトリ。
//! - `process`: 旧`subprocess`と旧bashと同じ子の実行、環境の辞書、PATHの探索、信号の設定。

pub mod fs;
pub mod process;
pub mod startup;

/// OSのエラー番号。旧Pythonの`OSError.errno`と同じ値を持つ。
pub use rustix::io::Errno;

/// `open`の旗(旧Pythonの`os.O_*`と同じ値)。
pub use rustix::fs::OFlags;

/// 直前の失敗のエラー番号を読む。確保をしないので、forkの後の子でも使える。
fn last_errno() -> Errno {
    Errno::from_raw_os_error(
        std::io::Error::last_os_error()
            .raw_os_error()
            .unwrap_or(libc::EIO),
    )
}
