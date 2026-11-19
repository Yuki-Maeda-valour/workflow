//! Stopと未捕捉例外の経路を分ける。
use dev_workflow_platform::fs::FsError;
use std::io;
#[derive(Debug)]
pub enum LegacyException {
    Context {
        error: Box<LegacyException>,
        cause: Box<LegacyException>,
        direct: bool,
    },
    Traced {
        error: Box<LegacyException>,
        frames: Vec<Frame>,
    },
    Stop(String),
    StopText {
        reason: super::json::Text,
        stderr: String,
    },
    Os {
        error: io::Error,
        path: Option<Vec<u8>>,
        location: &'static str,
    },
    Python {
        kind: &'static str,
        reason: String,
        location: &'static str,
    },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub function: &'static str,
    pub line: u32,
    pub source: &'static str,
    pub module: Option<&'static str>,
}
pub type Result<T> = std::result::Result<T, LegacyException>;
impl LegacyException {
    pub fn contextual(self, cause: Self, direct: bool) -> Self {
        Self::Context {
            error: Box::new(self),
            cause: Box::new(cause),
            direct,
        }
    }
    pub fn stop_reason(&self) -> Option<&str> {
        match self {
            Self::Stop(s) => Some(s),
            Self::StopText { stderr, .. } => Some(stderr),
            Self::Traced { error, .. } | Self::Context { error, .. } => error.stop_reason(),
            _ => None,
        }
    }
    pub fn trace(self, function: &'static str, line: u32, source: &'static str) -> Self {
        match self {
            Self::Traced { error, mut frames } => {
                frames.push(Frame {
                    function,
                    line,
                    source,
                    module: None,
                });
                Self::Traced { error, frames }
            }
            e => Self::Traced {
                error: Box::new(e),
                frames: vec![Frame {
                    function,
                    line,
                    source,
                    module: None,
                }],
            },
        }
    }
    pub fn external(
        self,
        module: &'static str,
        function: &'static str,
        line: u32,
        source: &'static str,
    ) -> Self {
        let mut error = self.trace(function, line, source);
        if let Self::Traced { frames, .. } = &mut error {
            frames.last_mut().unwrap().module = Some(module);
        }
        error
    }
    pub fn at(self, function: &'static str, line: u32, source: &'static str) -> Self {
        match self {
            Self::Stop(_) | Self::StopText { .. } => self,
            Self::Context {
                error,
                cause,
                direct,
            } => Self::Context {
                error: Box::new(error.trace(function, line, source)),
                cause,
                direct,
            },
            e => e.trace(function, line, source),
        }
    }
    pub fn location(&self) -> &str {
        match self {
            Self::Traced { error, .. } | Self::Context { error, .. } => error.location(),
            Self::Python { location, .. } | Self::Os { location, .. } => location,
            Self::Stop(_) | Self::StopText { .. } => "Stop",
        }
    }
    pub fn is_os(&self) -> bool {
        match self {
            Self::Os { .. } => true,
            Self::Traced { error, .. } | Self::Context { error, .. } => error.is_os(),
            _ => false,
        }
    }
    pub fn stop(reason: impl Into<String>) -> Self {
        Self::Stop(reason.into())
    }
    pub fn stop_text(reason: super::json::Text) -> Self {
        let stderr = String::from_utf8(
            super::runtime::encode(&reason, "utf-8", "backslashreplace")
                .expect("backslashreplace encodes every codepoint"),
        )
        .unwrap();
        Self::StopText { reason, stderr }
    }
    pub fn os(error: io::Error, location: &'static str) -> Self {
        Self::Os {
            error,
            path: None,
            location,
        }
    }
}
/// 旧スクリプト自身のフレームの File 欄。ビルド時のパスを埋め込まない(17:58・21:35 の決定)。
pub const LEGACY_SCRIPT: &str = "loop-state.py";
#[cfg(test)]
pub fn legacy_source_path() -> String {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .unwrap()
        .join("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py")
        .display()
        .to_string()
}
pub fn render(error: &LegacyException) -> String {
    match error {
        LegacyException::Context {
            error,
            cause,
            direct,
        } => {
            let mut out = render(cause);
            out.push_str(if *direct {
                "\nThe above exception was the direct cause of the following exception:\n\n"
            } else {
                "\nDuring handling of the above exception, another exception occurred:\n\n"
            });
            out.push_str(&render(error));
            out
        }
        LegacyException::Traced { error, frames } => {
            let mut out = String::from("Traceback (most recent call last):\n");
            let frames = frames.iter().rev().collect::<Vec<_>>();
            let mut at = 0;
            while at < frames.len() {
                let frame = frames[at];
                let count = frames[at..].iter().take_while(|f| ***f == *frame).count();
                for _ in 0..count.min(3) {
                    let file = frame
                        .module
                        .map(|m| format!("<CPython3.10>/{m}"))
                        .unwrap_or_else(|| LEGACY_SCRIPT.to_owned());
                    out.push_str(&format!(
                        "  File \"{}\", line {}, in {}\n    {}\n",
                        file, frame.line, frame.function, frame.source
                    ));
                }
                if count > 3 {
                    out.push_str(&format!(
                        "  [Previous line repeated {} more time{}]\n",
                        count - 3,
                        if count == 4 { "" } else { "s" }
                    ));
                }
                at += count;
            }
            out.push_str(&render(error));
            out
        }
        LegacyException::Stop(reason) => format!("__main__.Stop: {reason}\n"),
        LegacyException::StopText { reason, .. } => format!(
            "__main__.Stop: {}\n",
            String::from_utf8(super::runtime::encode(reason, "utf-8", "backslashreplace").unwrap())
                .unwrap()
        ),
        LegacyException::Python {
            kind,
            reason,
            location,
        } => {
            let _ = location;
            if *kind == "KeyboardInterrupt" && reason.is_empty() {
                format!("{kind}\n")
            } else {
                format!("{kind}: {reason}\n")
            }
        }
        LegacyException::Os {
            error,
            path,
            location,
        } => {
            let _ = location;
            {
                let kind = match error.kind() {
                    io::ErrorKind::PermissionDenied => "PermissionError",
                    io::ErrorKind::NotFound => "FileNotFoundError",
                    io::ErrorKind::AlreadyExists => "FileExistsError",
                    io::ErrorKind::IsADirectory => "IsADirectoryError",
                    io::ErrorKind::NotADirectory => "NotADirectoryError",
                    io::ErrorKind::BrokenPipe => "BrokenPipeError",
                    _ => "OSError",
                };
                let suffix = path
                    .as_ref()
                    .map(|p| format!(": {}", super::args::repr(p)))
                    .unwrap_or_default();
                format!(
                    "{kind}: [Errno {}] {}{suffix}\n",
                    error.raw_os_error().unwrap_or(0),
                    reason(error)
                )
            }
        }
    }
}
impl From<io::Error> for LegacyException {
    fn from(error: io::Error) -> Self {
        Self::os(error, "read_at")
    }
}
impl From<FsError> for LegacyException {
    fn from(error: FsError) -> Self {
        match error {
            FsError::Io(e) => e.into(),
            FsError::Changed => Self::stop("通常ファイルが lstat と open の間に変わった"),
            FsError::NotRegular => Self::stop("通常ファイルが lstat と open の間に変わった"),
            _ => Self::stop("親ディレクトリが観察中に差し替わった"),
        }
    }
}
pub fn reason(error: &io::Error) -> String {
    error
        .to_string()
        .split(" (os error ")
        .next()
        .unwrap()
        .into()
}
pub fn fs_reason(error: &FsError) -> String {
    match error {
        FsError::Io(e) => reason(e),
        _ => "Unknown error".into(),
    }
}
