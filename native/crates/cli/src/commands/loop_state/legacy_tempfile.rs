// CPython 3.10 Lib/tempfile.py の mkdtemp の順の写し。CPYTHON-LICENSE を参照。
//! `tempfile.mkdtemp(prefix=...)`:置き場の候補(TMPDIR・TEMP・TMP・/tmp・/var/tmp・
//! /usr/tmp・現在のディレクトリ)を順に、乱数の名前の試しのファイルを書いて確かめ、
//! 使える候補の下に `<prefix><8 文字>` のディレクトリを mode 0700 で作る。
use super::{
    error::{LegacyException, Result},
    state,
};
use dev_workflow_platform::loop_state::{OpenFlags, open_file, write_once};
use std::{
    ffi::OsString,
    hash::{BuildHasher, Hasher},
    os::unix::{ffi::OsStrExt, fs::DirBuilderExt},
    path::{Path, PathBuf},
};

const CHARACTERS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789_";
/// tempfile.TMP_MAX(= os.TMP_MAX、旧 CPython を作った libc の TMP_MAX)。
/// Linux の CPython は glibc(238328)、Mac は Apple libc(308915776)。取れないときは 10000。
#[cfg(target_os = "linux")]
const TMP_MAX: usize = 238328;
#[cfg(target_os = "macos")]
const TMP_MAX: usize = 308915776;
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
const TMP_MAX: usize = 10000;

/// _RandomNameSequence と同じ文字の集まりからの 8 文字。乱数の列は旧版と同じにしない。
struct Names {
    state: std::collections::hash_map::RandomState,
    counter: u64,
}
impl Names {
    fn new() -> Self {
        Self {
            state: std::collections::hash_map::RandomState::new(),
            counter: 0,
        }
    }
    fn next(&mut self) -> String {
        let mut hasher = self.state.build_hasher();
        hasher.write_u64(self.counter);
        self.counter += 1;
        let mut value = hasher.finish();
        (0..8)
            .map(|_| {
                let c = CHARACTERS[(value % CHARACTERS.len() as u64) as usize];
                value /= CHARACTERS.len() as u64;
                c as char
            })
            .collect()
    }
}

fn frame(
    error: LegacyException,
    function: &'static str,
    line: u32,
    source: &'static str,
) -> LegacyException {
    error.external("tempfile.py", function, line, source)
}

/// _candidate_tempdir_list(文字はUTF-8とsurrogateescapeで解く)。
fn candidates() -> Vec<OsString> {
    let mut list = Vec::new();
    for name in ["TMPDIR", "TEMP", "TMP"] {
        if let Some(value) = std::env::var_os(name).filter(|v| !v.is_empty()) {
            list.push(value);
        }
    }
    list.extend(["/tmp", "/var/tmp", "/usr/tmp"].map(OsString::from));
    list.push(
        std::env::current_dir()
            .map(PathBuf::into_os_string)
            .unwrap_or_else(|_| OsString::from(".")),
    );
    list
}

/// Python の str の repr を並べた list の表記(例外の文に出る)。
fn list_repr(list: &[OsString]) -> String {
    let items = list
        .iter()
        .map(|v| super::args::repr(v.as_bytes()))
        .collect::<Vec<_>>();
    format!("[{}]", items.join(", "))
}

/// _get_default_tempdir。使える候補の abspath を返す。
fn default_directory() -> Result<PathBuf> {
    let mut names = Names::new();
    let list = candidates();
    for candidate in &list {
        let directory = if candidate == "." {
            PathBuf::from(".")
        } else {
            state::absolute(Path::new(candidate))?
        };
        for _ in 0..100 {
            let filename = directory.join(names.next());
            // _bin_openflags = O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW、mode 0600。
            let opened = open_file(
                &filename,
                OpenFlags {
                    read: true,
                    create_exclusive: true,
                    truncate: false,
                    nofollow: true,
                },
            );
            match opened {
                Ok(file) => {
                    // 1 回の write(途中までの書込みは失敗にしない)。
                    let written = write_once(&file, b"blat");
                    drop(file);
                    let removed = std::fs::remove_file(&filename);
                    match (written, removed) {
                        (_, Err(_)) | (Err(_), _) => break,
                        (Ok(_), Ok(())) => return Ok(directory),
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(_) => break,
            }
        }
    }
    Err(LegacyException::Python {
        kind: "FileNotFoundError",
        reason: format!(
            "[Errno 2] No usable temporary directory found in {}",
            list_repr(&list)
        ),
        location: "tempfile",
    }
    .external(
        "tempfile.py",
        "_get_default_tempdir",
        223,
        "raise FileNotFoundError(_errno.ENOENT,",
    ))
}

/// tempfile.mkdtemp(prefix=prefix)。
pub fn mkdtemp(prefix: &str) -> Result<PathBuf> {
    let directory = default_directory()
        .map_err(|e| frame(e, "_gettempdir", 292, "tempdir = _get_default_tempdir()"))
        .map_err(|e| frame(e, "gettempdir", 299, "return _os.fsdecode(_gettempdir())"))
        .map_err(|e| frame(e, "_sanitize_params", 126, "dir = gettempdir()"))
        .map_err(|e| {
            frame(
                e,
                "mkdtemp",
                357,
                "prefix, suffix, dir, output_type = _sanitize_params(prefix, suffix, dir)",
            )
        })?;
    let mut names = Names::new();
    for _ in 0..TMP_MAX {
        let file = directory.join(format!("{prefix}{}", names.next()));
        match std::fs::DirBuilder::new().mode(0o700).create(&file) {
            Ok(()) => return Ok(file),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(frame(
                    LegacyException::Os {
                        error,
                        path: Some(file.as_os_str().as_bytes().to_vec()),
                        location: "tempfile.mkdtemp",
                    },
                    "mkdtemp",
                    368,
                    "_os.mkdir(file, 0o700)",
                ));
            }
        }
    }
    Err(frame(
        LegacyException::Python {
            kind: "FileExistsError",
            reason: "[Errno 17] No usable temporary directory name found".into(),
            location: "tempfile",
        },
        "mkdtemp",
        381,
        "raise FileExistsError(_errno.EEXIST,",
    ))
}

#[cfg(test)]
pub fn candidate_list() -> Vec<OsString> {
    candidates()
}
#[cfg(test)]
pub fn sample_name() -> String {
    Names::new().next()
}
