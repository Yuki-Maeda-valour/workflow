//! 実際の OS 操作(platform の R4C 専用部品)と実環境の読み取り。
use super::diagnostics as text;
use super::flow::{Child, LinkFailure, Ops, Precheck, Read};
use super::paths::PyPath;
use super::runtime::Env;
use dev_workflow_platform::adopt_candidate as os;
use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::time::Duration;

pub(crate) struct RealOps;

impl Ops for RealOps {
    type Dir = os::Dir;

    fn open_dir(&self, path: &PyPath) -> Result<os::Dir, Vec<u8>> {
        os::open_chain(path.components()).map_err(|error| match error {
            os::DirError::Os(failure) => text::os_failure(&failure),
            os::DirError::ParentReference => text::PARENT_REFERENCE.as_bytes().to_vec(),
        })
    }

    fn read_candidate(&self, dir: &os::Dir, name: &[u8]) -> Result<Read, Vec<u8>> {
        self.read_candidate_during(dir, name, &mut || {})
    }

    fn read_candidate_during(
        &self,
        dir: &os::Dir,
        name: &[u8],
        hook: &mut dyn FnMut(),
    ) -> Result<Read, Vec<u8>> {
        match os::read_candidate_with(dir, name, super::flow::MAX_CANDIDATE, hook) {
            Ok((data, identity)) => Ok(Read {
                data,
                identity: super::candidate::FileIdentity {
                    dev: i128::from(identity.dev),
                    ino: i128::from(identity.ino),
                },
            }),
            Err(os::ReadError::Os(failure)) => Err(text::os_failure(&failure)),
            Err(os::ReadError::NotRegularOrLarge) => {
                Err(text::NOT_REGULAR_OR_LARGE.as_bytes().to_vec())
            }
            Err(os::ReadError::Changed) => Err(text::CHANGED_WHILE_READING.as_bytes().to_vec()),
        }
    }

    fn stat(&self, dir: &os::Dir, name: &[u8]) -> Result<Option<os::Entry>, Vec<u8>> {
        os::stat_nofollow(dir, name).map_err(|failure| text::os_failure(&failure))
    }

    fn lexists(&self, path: &[u8]) -> bool {
        os::lexists(path)
    }

    fn rename_noreplace(
        &self,
        dir: &os::Dir,
        source: &[u8],
        destination: &[u8],
    ) -> Result<bool, Vec<u8>> {
        os::rename_noreplace(dir, source, destination).map_err(|failure| {
            // 旧版は `OSError(errno, '上書きしない改名に失敗')`。ファイル名は持たない。
            text::os_error(failure.errno, "上書きしない改名に失敗", None)
        })
    }

    fn link(&self, dir: &os::Dir, source: &[u8], destination: &[u8]) -> Result<(), LinkFailure> {
        os::link_noreplace(dir, source, destination).map_err(|error| match error {
            os::LinkError::Exists => LinkFailure::Exists,
            os::LinkError::Os(failure) => LinkFailure::Other(text::os_failure(&failure)),
        })
    }

    fn unlink(&self, dir: &os::Dir, name: &[u8]) -> Result<(), Vec<u8>> {
        os::unlink(dir, name).map_err(|failure| text::os_failure(&failure))
    }

    fn run(
        &self,
        dir: &os::Dir,
        argv: &[Vec<u8>],
        env: &[(Vec<u8>, Vec<u8>)],
        timeout_seconds: u32,
    ) -> Result<Child, Vec<u8>> {
        child_result(
            os::run_in_dir(
                dir,
                argv,
                env,
                Duration::from_secs(u64::from(timeout_seconds)),
            ),
            argv,
            timeout_seconds,
        )
    }

    fn protect(&self, enter: bool) -> bool {
        os::interrupt_protection(enter)
    }

    fn interrupted(&self) -> bool {
        os::interrupted()
    }

    fn interrupt_exit(&self) {
        os::interrupt_exit()
    }

    fn environment(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        // 旧 Python の `os.environ` は、同じ名前が複数あれば最初の値を使う(`PyDict_SetDefault`)。
        let mut vars: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
        for (key, value) in std::env::vars_os() {
            let key = key.into_vec();
            if !vars.iter().any(|(seen, _)| *seen == key) {
                vars.push((key, value.into_vec()));
            }
        }
        super::runtime::python_environment(vars, &ProcessEnv)
    }

    fn loop_iteration(&self) -> Option<Vec<u8>> {
        std::env::var_os("DEV_WORKFLOW_LOOP_ITER").map(|value| value.as_bytes().to_vec())
    }
}

/// 子の結果を旧版の表示へ直す。`display` は時間切れの表示に出す引数(Python の `Command` の `cmd`)。
fn child_result(
    result: Result<os::Captured, os::ChildError>,
    display: &[Vec<u8>],
    timeout_seconds: u32,
) -> Result<Child, Vec<u8>> {
    result
        .map(|captured| Child {
            code: captured.code,
            stdout: captured.stdout,
            stderr: captured.stderr,
        })
        .map_err(|error| match error {
            os::ChildError::Spawn(failure) => text::os_failure(&failure),
            os::ChildError::Timeout => text::timeout_message(display, timeout_seconds),
            os::ChildError::Io(errno) => text::os_error(errno, &text::strerror(errno), None),
        })
}

/// 事前検査の子の `argv[0]` と、時間切れの表示に出す補助スクリプトの名前(2026-11-11 21:35 の決定の固定名)。
pub(crate) const PRECHECK_NAME: &[u8] = b"diff-snapshot.sh";
/// 事前検査の全体の期限(旧 `root_run(root_fd, precheck, env, 60)`)。
pub(crate) const PRECHECK_TIMEOUT: u32 = 60;

/// 事前検査の子の引数(`argv[0]` から)。旧版は `bash <補助スクリプト> --cwd <root> --precheck [--accept <a>]`で、
/// Rust は自分自身を `argv[0]` だけ `diff-snapshot.sh` にして起動する(`bash` と補助スクリプトの分は無い)。
pub(crate) fn precheck_argv(root: &[u8], accept: Option<&[u8]>) -> Vec<Vec<u8>> {
    let mut argv = vec![
        PRECHECK_NAME.to_vec(),
        b"--cwd".to_vec(),
        root.to_vec(),
        b"--precheck".to_vec(),
    ];
    if let Some(accept) = accept {
        argv.push(b"--accept".to_vec());
        argv.push(accept.to_vec());
    }
    argv
}

/// 時間切れの表示に出す引数。旧版の `cmd` は `['bash', '<補助スクリプト>', '--cwd', …]` で、補助スクリプトだけ固定名にする。
pub(crate) fn precheck_display(argv: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let mut display = vec![b"bash".to_vec()];
    display.extend_from_slice(argv);
    display
}

/// #472 の事前検査の実物。自分の実行ファイルを、`argv[0]` だけ `diff-snapshot.sh` にして直接の子として起動する
/// (子は、`argv[0]` が `diff-snapshot.sh` と完全に一致するときだけ S3D の入口へ入る)。
/// 実行ファイルの絶対パスは、起動したとき(作業場所を変える前)に決めて保持する。決められなかった errno は、使うときに停止の文にする。
pub(crate) struct SelfPrecheck {
    executable: Result<Vec<u8>, i32>,
    timeout_seconds: u32,
}

impl SelfPrecheck {
    /// 起動した直後に呼ぶ。
    pub(crate) fn new() -> Self {
        Self::with_executable(os::own_executable_path())
    }

    pub(crate) fn with_executable(executable: Result<Vec<u8>, i32>) -> Self {
        Self {
            executable,
            timeout_seconds: PRECHECK_TIMEOUT,
        }
    }

    /// 検査専用: 期限を短くする(実物の 60 秒を単体テストで待たないため)。製品の経路(`new`)は 60 秒。
    #[cfg(test)]
    pub(crate) fn with_executable_and_timeout(
        executable: Result<Vec<u8>, i32>,
        timeout_seconds: u32,
    ) -> Self {
        Self {
            executable,
            timeout_seconds,
        }
    }
}

impl Precheck<os::Dir> for SelfPrecheck {
    fn run(
        &self,
        dir: &os::Dir,
        root: &[u8],
        accept: Option<&[u8]>,
        env: &[(Vec<u8>, Vec<u8>)],
    ) -> Result<Child, Vec<u8>> {
        let argv = precheck_argv(root, accept);
        let executable = match &self.executable {
            Ok(path) => path,
            Err(errno) => {
                return Err(text::os_failure(&os::Failure {
                    errno: *errno,
                    filename: Some(PRECHECK_NAME.to_vec()),
                    filename2: None,
                }));
            }
        };
        child_result(
            os::run_in_dir_with_executable(
                dir,
                executable,
                &argv,
                env,
                Duration::from_secs(u64::from(self.timeout_seconds)),
            ),
            &precheck_display(&argv),
            self.timeout_seconds,
        )
    }
}

/// 実環境の環境変数。
pub(crate) struct ProcessEnv;

impl Env for ProcessEnv {
    fn get(&self, name: &str) -> Option<Vec<u8>> {
        std::env::var_os(name).map(OsString::into_vec)
    }
}
