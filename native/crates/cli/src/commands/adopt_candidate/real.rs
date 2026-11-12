//! 実際の OS 操作(platform の R4C 専用部品)と実環境の読み取り。
use super::diagnostics as text;
use super::flow::{Child, LinkFailure, Ops, Read};
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
        os::run_in_dir(
            dir,
            argv,
            env,
            Duration::from_secs(u64::from(timeout_seconds)),
        )
        .map(|captured| Child {
            code: captured.code,
            stdout: captured.stdout,
            stderr: captured.stderr,
        })
        .map_err(|error| match error {
            os::ChildError::Spawn(failure) => text::os_failure(&failure),
            os::ChildError::Timeout => text::timeout_message(argv, timeout_seconds),
            os::ChildError::Io(errno) => text::os_error(errno, &text::strerror(errno), None),
        })
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

/// 実環境の環境変数。
pub(crate) struct ProcessEnv;

impl Env for ProcessEnv {
    fn get(&self, name: &str) -> Option<Vec<u8>> {
        std::env::var_os(name).map(OsString::into_vec)
    }
}
