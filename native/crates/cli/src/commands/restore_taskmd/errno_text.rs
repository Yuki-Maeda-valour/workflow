//! errno の文と、CPython 3.10 の errno と例外の型の対応(S3I-A #474 で広げた)。
//!
//! - Linux: 基準の機械(CI の Ubuntu 24.04・glibc 2.39)の `strerror` の全番号の表。表は生成物の
//!   `errno_glibc.rs`(`native/ci/implement_guard_fixtures/gen_strerror.py` が基準の容器の glibc から作る)。
//!   表の外の番号は glibc と同じ `Unknown error <番号>`。
//! - Mac: 表を持たない。旧版と同じ libSystem の `strerror_r` を platform の `strerror_text` でその場で呼ぶ。
//! - 型: CPython 3.10 の `Objects/exceptions.c` の `errnomap`(errno ごとの `OSError` の下位の型)。
//!
//! Rust の `io::Error` の表示と musl の `strerror` は使わない(文が違う)。

/// errno の文(基準の C ライブラリの `strerror` と同じ)。
#[cfg(not(target_os = "macos"))]
pub fn strerror(number: i32) -> String {
    let fixed = usize::try_from(number)
        .ok()
        .and_then(|index| super::errno_glibc::LINUX.get(index).copied());
    match fixed {
        Some(text) => text.to_string(),
        None => format!("Unknown error {number}"),
    }
}

/// errno の文(libSystem の `strerror_r`。表の外の番号もその文をそのまま使う)。
#[cfg(target_os = "macos")]
pub fn strerror(number: i32) -> String {
    dev_workflow_platform::restore_taskmd::strerror_text(number)
}

/// CPython 3.10 で、その errno の `OSError` が作る型の名前。
pub fn exception_type(number: i32) -> &'static str {
    use numbers::*;
    match number {
        n if n == EAGAIN || n == EALREADY || n == EWOULDBLOCK || n == EINPROGRESS => {
            "BlockingIOError"
        }
        n if n == ECHILD => "ChildProcessError",
        n if n == EPIPE || n == ESHUTDOWN => "BrokenPipeError",
        n if n == ECONNABORTED => "ConnectionAbortedError",
        n if n == ECONNREFUSED => "ConnectionRefusedError",
        n if n == ECONNRESET => "ConnectionResetError",
        n if n == EEXIST => "FileExistsError",
        n if n == ENOENT => "FileNotFoundError",
        n if n == EISDIR => "IsADirectoryError",
        n if n == ENOTDIR => "NotADirectoryError",
        n if n == EINTR => "InterruptedError",
        n if n == EACCES || n == EPERM => "PermissionError",
        n if n == ESRCH => "ProcessLookupError",
        n if n == ETIMEDOUT => "TimeoutError",
        _ => "OSError",
    }
}

/// 型の対応に使う番号(cli は libc に依らないので、OS ごとの値を持つ。単体テストで platform の値と照らす)。
#[allow(dead_code)]
mod numbers {
    pub const EPERM: i32 = 1;
    pub const ENOENT: i32 = 2;
    pub const ESRCH: i32 = 3;
    pub const EINTR: i32 = 4;
    pub const ECHILD: i32 = 10;
    pub const EACCES: i32 = 13;
    pub const EEXIST: i32 = 17;
    pub const ENOTDIR: i32 = 20;
    pub const EISDIR: i32 = 21;
    pub const EPIPE: i32 = 32;
    #[cfg(not(target_os = "macos"))]
    mod os {
        pub const EAGAIN: i32 = 11;
        pub const EWOULDBLOCK: i32 = 11;
        pub const EINPROGRESS: i32 = 115;
        pub const EALREADY: i32 = 114;
        pub const ESHUTDOWN: i32 = 108;
        pub const ECONNABORTED: i32 = 103;
        pub const ECONNRESET: i32 = 104;
        pub const ETIMEDOUT: i32 = 110;
        pub const ECONNREFUSED: i32 = 111;
    }
    #[cfg(target_os = "macos")]
    mod os {
        pub const EAGAIN: i32 = 35;
        pub const EWOULDBLOCK: i32 = 35;
        pub const EINPROGRESS: i32 = 36;
        pub const EALREADY: i32 = 37;
        pub const ESHUTDOWN: i32 = 58;
        pub const ECONNABORTED: i32 = 53;
        pub const ECONNRESET: i32 = 54;
        pub const ETIMEDOUT: i32 = 60;
        pub const ECONNREFUSED: i32 = 61;
    }
    pub use os::*;
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_platform::restore_taskmd::errno as os;

    #[test]
    fn r4r_numbers_keep_their_text_and_type() {
        // R4R が比べていた 6 つの番号の文と型(R4R の describe_errno と同じ)。
        assert_eq!(strerror(os::EPIPE), "Broken pipe");
        assert_eq!(exception_type(os::EPIPE), "BrokenPipeError");
        assert_eq!(strerror(os::ENOSPC), "No space left on device");
        assert_eq!(strerror(os::EIO), "Input/output error");
        assert_eq!(strerror(os::EBADF), "Bad file descriptor");
        assert_eq!(strerror(os::EFBIG), "File too large");
        for number in [os::ENOSPC, os::EIO, os::EBADF, os::EFBIG, os::EDQUOT] {
            assert_eq!(exception_type(number), "OSError", "{number}");
        }
        #[cfg(target_os = "linux")]
        assert_eq!(strerror(os::EDQUOT), "Disk quota exceeded");
        #[cfg(target_os = "macos")]
        assert_eq!(strerror(os::EDQUOT), "Disc quota exceeded");
    }

    #[test]
    fn numbers_match_the_platform() {
        assert_eq!(numbers::EINTR, os::EINTR);
        assert_eq!(numbers::ENOENT, os::ENOENT);
        assert_eq!(numbers::ECHILD, os::ECHILD);
        assert_eq!(numbers::ESRCH, os::ESRCH);
        assert_eq!(numbers::EPIPE, os::EPIPE);
        assert_eq!(numbers::EACCES, os::EACCES);
        assert_eq!(numbers::EPERM, os::EPERM);
        // 型の対応に使うほかの番号も、その OS の libc の値と照らす(S3I-A の platform の表)。
        use dev_workflow_platform::implement_guard::errno as libc_errno;
        for (ours, platform) in [
            (numbers::EAGAIN, libc_errno::EAGAIN),
            (numbers::EWOULDBLOCK, libc_errno::EWOULDBLOCK),
            (numbers::EINPROGRESS, libc_errno::EINPROGRESS),
            (numbers::EALREADY, libc_errno::EALREADY),
            (numbers::ESHUTDOWN, libc_errno::ESHUTDOWN),
            (numbers::ECONNABORTED, libc_errno::ECONNABORTED),
            (numbers::ECONNRESET, libc_errno::ECONNRESET),
            (numbers::ETIMEDOUT, libc_errno::ETIMEDOUT),
            (numbers::ECONNREFUSED, libc_errno::ECONNREFUSED),
            (numbers::EEXIST, libc_errno::EEXIST),
            (numbers::ENOTDIR, libc_errno::ENOTDIR),
            (numbers::EISDIR, libc_errno::EISDIR),
        ] {
            assert_eq!(ours, platform);
        }
        assert_eq!(exception_type(os::ENOENT), "FileNotFoundError");
        assert_eq!(exception_type(os::EACCES), "PermissionError");
        assert_eq!(exception_type(os::EINTR), "InterruptedError");
        assert_eq!(exception_type(numbers::EAGAIN), "BlockingIOError");
    }

    /// python3 の `os.strerror`(C の `strerror` をそのまま呼ぶ)の文を、`LC_ALL=C`・`LANGUAGE` なしで受け取る。
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    fn python_strerror(numbers: &[i32]) -> Vec<String> {
        let script =
            "import json,os,sys; print(json.dumps([os.strerror(int(n)) for n in sys.argv[1:]]))";
        let mut command = std::process::Command::new("python3");
        command
            .arg("-I")
            .arg("-c")
            .arg(script)
            .env_clear()
            .env("LC_ALL", "C");
        if let Some(path) = std::env::var_os("PATH") {
            command.env("PATH", path);
        }
        command.args(numbers.iter().map(i32::to_string));
        let done = command
            .output()
            .expect("python3 を起動できない(省かずに失敗にする)");
        assert!(done.status.success(), "python3 が失敗した: {done:?}");
        serde_json::from_slice(&done.stdout).expect("python3 の出力が JSON でない")
    }

    /// 全番号(0〜256)と表の外の 1000 で、`text` の文が python3 の `os.strerror` と違う番号。
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    fn strerror_mismatches(text: impl Fn(i32) -> String) -> Vec<i32> {
        let numbers: Vec<i32> = (0..=256).chain([1000]).collect();
        let expected = python_strerror(&numbers);
        numbers
            .iter()
            .zip(expected)
            .filter(|(number, want)| text(**number) != *want)
            .map(|(number, _)| *number)
            .collect()
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mac_strerror_matches_libsystem_for_all_numbers() {
        // 決定の表の「書き出しの失敗の文」: Mac は、全番号と表の外の 1000 を os.strerror と照らす。
        assert_eq!(strerror_mismatches(strerror), Vec::<i32>::new());
        // 壊した文を渡すと、その番号が見つかる(照らしが空振りしない)。
        let broken = strerror_mismatches(|n| {
            if n == 2 {
                "broken".to_string()
            } else {
                strerror(n)
            }
        });
        assert_eq!(broken, vec![2]);
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn outside_the_table_is_glibc_unknown() {
        assert_eq!(strerror(-1), "Unknown error -1");
        assert_eq!(strerror(1000), "Unknown error 1000");
        assert_eq!(strerror(0), "Success");
    }
}
