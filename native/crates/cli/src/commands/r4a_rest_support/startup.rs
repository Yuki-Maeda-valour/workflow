//! R4A five helper commands の CPython 起動前拒否を作る純粋部品。

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StreamKind {
    Other,
    Directory,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StartupFds {
    pub stdin: StreamKind,
    pub stdout: StreamKind,
    pub stderr: StreamKind,
}

fn thread_tail(thread: u64) -> String {
    format!("Current thread 0x{thread:016x} (most recent call first):\n  <no Python frame>\n")
}

fn fatal(message: &str, thread: u64) -> Vec<u8> {
    format!(
        "Fatal Python error: {message}\nPython runtime state: core initialized\n\n{}",
        thread_tail(thread)
    )
    .into_bytes()
}

/// 整数設定拒否は stdio 初期化より先に選ぶ。`int_error` は既存の正確な本文。
pub fn decide(int_error: Option<Vec<u8>>, fds: StartupFds, thread: u64) -> Option<Vec<u8>> {
    if let Some(error) = int_error {
        return Some(error);
    }
    if fds.stdin == StreamKind::Directory {
        return Some(fatal(
            "init_sys_streams: <stdin> is a directory, cannot continue",
            thread,
        ));
    }
    let number = if fds.stdout == StreamKind::Directory {
        Some(1)
    } else if fds.stderr == StreamKind::Directory {
        Some(2)
    } else {
        None
    }?;
    Some(format!("Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: {number}\n\n{}", thread_tail(thread)).into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    const T: u64 = 0x12ab;
    const OK: StreamKind = StreamKind::Other;
    #[test]
    fn priority_and_fd_diagnostics_are_fixed() {
        let error = b"Fatal Python error: integer\n".to_vec();
        assert_eq!(
            decide(
                Some(error.clone()),
                StartupFds {
                    stdin: StreamKind::Directory,
                    stdout: OK,
                    stderr: OK
                },
                T
            ),
            Some(error)
        );
        assert!(
            String::from_utf8(
                decide(
                    None,
                    StartupFds {
                        stdin: StreamKind::Directory,
                        stdout: OK,
                        stderr: OK
                    },
                    T
                )
                .unwrap()
            )
            .unwrap()
            .contains("<stdin> is a directory")
        );
        assert!(
            String::from_utf8(
                decide(
                    None,
                    StartupFds {
                        stdin: OK,
                        stdout: StreamKind::Directory,
                        stderr: OK
                    },
                    T
                )
                .unwrap()
            )
            .unwrap()
            .contains("IsADirectoryError: [Errno 21] Is a directory: 1")
        );
        assert!(
            String::from_utf8(
                decide(
                    None,
                    StartupFds {
                        stdin: OK,
                        stdout: OK,
                        stderr: StreamKind::Directory
                    },
                    T
                )
                .unwrap()
            )
            .unwrap()
            .contains("IsADirectoryError: [Errno 21] Is a directory: 2")
        );
    }
    #[test]
    fn thread_is_exactly_sixteen_hex_digits() {
        let text = String::from_utf8(
            decide(
                None,
                StartupFds {
                    stdin: StreamKind::Directory,
                    stdout: OK,
                    stderr: OK,
                },
                T,
            )
            .unwrap(),
        )
        .unwrap();
        assert!(text.contains("Current thread 0x00000000000012ab"));
    }
}
