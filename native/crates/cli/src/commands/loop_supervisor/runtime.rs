//! 旧 Python の起動と終了の再現(設計の「維持する動作」の 6)。R4U の `check_links` の起動の読み方
//! (`startup`)・書き手(`pyio`)・診断(`display`)を使う。
//!
//! 起動の順(本家 3.10.11 の `Py_BytesMain`):
//! 1. 親から受け継いだ SIGPIPE の扱いへ戻す(main の前の控えがあるとき)。
//! 2. C の地域設定の書き換え(PEP 538。`PYTHONCOERCECLOCALE=0` では書き換えない。`warn` なら警告を書く)。
//! 3. `PYTHONHASHSEED`・`PYTHONINTMAXSTRDIGITS` の検査(不正なら `Fatal Python error` と終了 1)。
//! 4. SIGPIPE と SIGXFSZ を無視にし、SIGINT が既定の扱いなら KeyboardInterrupt に当たる処理を置く。
//! 5. 標準入出力(stdin がディレクトリ・stdout か stderr がディレクトリなら `Fatal Python error`)。
//! 6. `PYTHONCOERCECLOCALE=warn` で C の地域設定のままなら、`sys.stderr` へ警告を書く。
//!
//! 文字コードは UTF-8 と surrogateescape に固定する(stderr は backslashreplace)。2026-11-11 22:24 の決定。

use super::check_links::display::{self, Stage};
use super::check_links::pyio::{IoError, Py, Stream, Which};
use super::check_links::startup;
use super::check_links::traceback as tb;
use super::r4q_python::legacy::Site;
use super::r4q_python::number::DEFAULT_MAX_STR_DIGITS;
use super::r4q_python::{exception, oserror, traceback as r4q_tb, value};
use dev_workflow_platform::check_links as std_os;
use dev_workflow_platform::r4q_process as os;
use dev_workflow_platform::restore_taskmd;

/// 起動を終えた旧 Python の状態。
pub struct Python {
    pub py: Py,
    /// `sys.get_int_max_str_digits()`。
    pub max_str_digits: usize,
    /// `__doc__` があるか(`PYTHONOPTIMIZE` が 2 未満)。
    pub has_doc: bool,
}

/// 起動の結果。
pub enum Start {
    Ready(Python),
    /// 起動の失敗(診断を書いた後の終了値)。
    Exit(u8),
}

fn env(name: &str) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    std::env::var_os(name)
        .map(OsStringExt::into_vec)
        .filter(|value| !value.is_empty())
}

/// `_Py_write_noraise(2, …)`(EINTR だけ繰り返し、失敗は捨てる)。
fn puts(text: &[u8]) {
    while std_os::write(2, text) == Err(os::errno::EINTR) {}
}

/// 地域設定の値が C(旧 Python の `_Py_LegacyLocaleDetected`)か。環境変数だけで決める
/// (入っている地域設定で決まる差は、2026-11-12 08:55 の決定で認める)。
fn legacy_locale() -> bool {
    let value = env("LC_ALL")
        .or_else(|| env("LC_CTYPE"))
        .or_else(|| env("LANG"));
    matches!(value.as_deref(), None | Some(b"C") | Some(b"POSIX"))
}

/// `create_stdio` の前の確かめ(ディレクトリなら起動の失敗の例外)。
fn open_stream(fd: i32, unbuffered: bool) -> Result<Option<Stream>, tb::Exception> {
    if std_os::std_valid_at_start(fd) == Some(false) {
        return Ok(None);
    }
    match std_os::fstat_std(fd) {
        Ok(meta) if meta.kind == std_os::Kind::Directory => Err(tb::Exception {
            type_name: oserror::exception_type(os::errno::EISDIR),
            value: format!(
                "[Errno {}] {}: {fd}",
                os::errno::EISDIR,
                exception::strerror(os::errno::EISDIR)
            ),
        }),
        Ok(meta) => Ok(Some(Stream::new(
            fd,
            unbuffered,
            std_os::isatty_std(fd),
            meta.blksize,
        ))),
        Err(_) => Ok(Some(Stream::new(fd, unbuffered, std_os::isatty_std(fd), 0))),
    }
}

/// 旧 Python の起動の処理を行う。
pub fn start() -> Start {
    std_os::restore_start_sigpipe();
    let coerce = env("PYTHONCOERCECLOCALE");
    let warn = coerce.as_deref() == Some(b"warn");
    if coerce.as_deref() != Some(b"0") && restore_taskmd::coerce_c_locale() && warn {
        let target = env("LC_CTYPE").unwrap_or_default();
        let target: Vec<u8> = target.into_iter().take(20).collect();
        let mut line = b"Python detected LC_CTYPE=C: LC_CTYPE coerced to ".to_vec();
        line.extend_from_slice(&target);
        line.extend_from_slice(
            b" (set another locale or PYTHONCOERCECLOCALE=0 to disable this locale coercion behavior).\n",
        );
        puts(&line);
    }
    let config = startup::Config::from_env();
    if !startup::hash_seed_valid(config.hash_seed.as_deref()) {
        display::fatal(
            Stage::Preinitialized,
            "config_init_hash_seed",
            "PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]",
            None,
        );
        return Start::Exit(1);
    }
    if !startup::int_max_str_digits_valid(config.int_max_str_digits.as_deref()) {
        display::fatal(
            Stage::Preinitialized,
            "config_init_int_max_str_digits",
            "PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.",
            None,
        );
        return Start::Exit(1);
    }
    let max_str_digits = config
        .int_max_str_digits
        .as_deref()
        .and_then(startup::str_to_int)
        .map_or(DEFAULT_MAX_STR_DIGITS, |digits| digits as usize);
    std_os::ignore_sigpipe_and_sigxfsz();
    std_os::install_sigint();
    if std_os::std_valid_at_start(0) != Some(false)
        && std_os::fstat_std(0).is_ok_and(|meta| meta.kind == std_os::Kind::Directory)
    {
        display::fatal(
            Stage::CoreInitialized,
            "init_sys_streams",
            "<stdin> is a directory, cannot continue",
            None,
        );
        return Start::Exit(1);
    }
    let unbuffered = config.unbuffered();
    let streams = open_stream(1, unbuffered)
        .and_then(|stdout| open_stream(2, unbuffered).map(|stderr| (stdout, stderr)));
    let (stdout, stderr) = match streams {
        Ok(streams) => streams,
        Err(exception) => {
            display::fatal(
                Stage::CoreInitialized,
                "init_sys_streams",
                "can't initialize sys standard streams",
                Some(exception),
            );
            return Start::Exit(1);
        }
    };
    let mut py = Py {
        stdout,
        stderr,
        err: None,
    };
    if warn && legacy_locale() {
        // emit_stderr_warning_for_legacy_locale(PySys_FormatStderr。失敗は消す)。
        let _ = py.write_string(
            Which::Stderr,
            b"Python runtime initialized with LC_CTYPE=C (a locale with default ASCII encoding), \
which may cause Unicode compatibility problems. Using C.UTF-8, C.utf8, or UTF-8 (if available) \
as alternative Unicode-compatible locales is recommended.\n",
        );
        py.err = None;
    }
    let has_doc = startup::env_flag(config.optimize.as_deref()) < 2;
    Start::Ready(Python {
        py,
        max_str_digits,
        has_doc,
    })
}

/// 旧版の main の終わり方。
pub enum Ended {
    /// `sys.exit(code)`。
    Code(u8),
    /// 捕まえない例外(前の例外から順の呼出履歴)。
    Raised(Vec<tb::Traceback>),
}

/// R4Q の呼出履歴の場所と例外から、R4U の書き手の呼出履歴を作る。
pub fn traceback(
    site: Option<Site>,
    type_name: &'static str,
    value: impl Into<String>,
) -> tb::Traceback {
    let frames = site
        .map(|site| {
            site.frames()
                .into_iter()
                .map(|frame: r4q_tb::Frame| tb::Frame {
                    file: frame.file,
                    line: frame.line,
                    name: frame.name,
                    source: frame.source,
                })
                .collect()
        })
        .unwrap_or_default();
    tb::Traceback {
        frames,
        exception: tb::Exception {
            type_name,
            value: value.into(),
        },
    }
}

/// R4Q の呼出履歴の並び(前の例外から順)を、R4U の書き手の並びにする。
pub fn chain(chain: Vec<r4q_tb::Traceback>) -> Vec<tb::Traceback> {
    chain
        .into_iter()
        .map(|traceback| tb::Traceback {
            frames: traceback
                .frames
                .into_iter()
                .map(|frame| tb::Frame {
                    file: frame.file,
                    line: frame.line,
                    name: frame.name,
                    source: frame.source,
                })
                .collect(),
            exception: tb::Exception {
                type_name: traceback.exception.type_name,
                value: traceback.exception.value,
            },
        })
        .collect()
}

/// `None.write(message)` の例外(argparse の `_print_message` が、None の流れへ書くとき)。
pub const NONE_WRITE: &str = "'NoneType' object has no attribute 'write'";

/// KeyboardInterrupt の呼出履歴(途中の行は比べない。2026-11-11 22:31 の決定)。
pub fn keyboard_interrupt(module: tb::Frame) -> Ended {
    Ended::Raised(vec![tb::Traceback {
        frames: vec![module],
        exception: tb::Exception::keyboard_interrupt(),
    }])
}

impl Python {
    /// Python の処理の区切りで、立った SIGINT の印を見る。
    pub fn interrupted(&mut self) -> bool {
        std_os::take_interrupt()
    }

    /// 並びの中で最初に開いている流れ(argparse の `if file is None: file = …` と、print の `file=None` の
    /// 置き換え。起動の時に閉じていた fd の流れは None)。
    pub fn first_open(&self, order: &[Which]) -> Option<Which> {
        order.iter().copied().find(|&which| self.py.has(which))
    }

    /// 流れの文字コードで符号化する(stdout は surrogateescape、stderr は backslashreplace)。
    fn encode(which: Which, text: &[u32]) -> Vec<u8> {
        match which {
            Which::Stdout => value::encode_surrogateescape(text)
                .unwrap_or_else(|| value::encode_backslashreplace(text)),
            Which::Stderr => value::encode_backslashreplace(text),
        }
    }

    /// argparse の `file.write(message)`(`target` が None なら `AttributeError`)。失敗は例外の行。
    pub fn argparse_write(
        &mut self,
        target: Option<Which>,
        text: &[u32],
    ) -> Result<(), tb::Exception> {
        let Some(which) = target else {
            return Err(tb::Exception {
                type_name: "AttributeError",
                value: NONE_WRITE.to_owned(),
            });
        };
        let body = Self::encode(which, text);
        if self.py.write_object(which, &body) {
            Ok(())
        } else {
            Err(Self::io_exception(
                self.py.err.take().unwrap_or(IoError::Os(0)),
            ))
        }
    }

    /// `print(text, file=sys.stderr)`(本文と改行を別々に書く)。sys.stderr が None なら `print` は
    /// sys.stdout へ書き、それも None なら何もしない。
    pub fn print_stderr(&mut self, text: &[u32]) -> Result<(), IoError> {
        let Some(which) = self.first_open(&[Which::Stderr, Which::Stdout]) else {
            return Ok(());
        };
        let body = Self::encode(which, text);
        if !self.py.write_object(which, &body) || !self.py.write_string(which, b"\n") {
            return Err(self.py.err.take().unwrap_or(IoError::Os(0)));
        }
        Ok(())
    }

    /// 書込みの例外の呼出履歴の例外の行。
    pub fn io_exception(error: IoError) -> tb::Exception {
        match error {
            IoError::Os(errno) => tb::Exception {
                type_name: oserror::exception_type(errno),
                value: format!("[Errno {errno}] {}", exception::strerror(errno)),
            },
            other => display::io_exception(other),
        }
    }

    /// 終わりの処理(`flush_io` → 呼出履歴 → `Py_FinalizeEx` → `exit_sigint`)。終了値を返す。
    pub fn finish(mut self, ended: Ended) -> u8 {
        self.py.flush_io();
        let (code, interrupted) = match ended {
            Ended::Code(code) => (code, false),
            Ended::Raised(chain) => {
                let interrupted = chain
                    .last()
                    .is_some_and(|last| last.exception.type_name == "KeyboardInterrupt");
                display::display(&mut self.py, &chain);
                (1, interrupted)
            }
        };
        let flushed = display::flush_std_files(&mut self.py);
        std_os::finish_sigint();
        self.py.close_all();
        let code = if flushed { code } else { 120 };
        if interrupted {
            return std_os::exit_by_sigint();
        }
        code
    }
}
