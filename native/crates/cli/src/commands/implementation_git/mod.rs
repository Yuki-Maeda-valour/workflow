//! `implementation-git.py`(旧版)のRust版。承認済みのfilterを無効化して、5つの読取り操作だけでGitを読む。
//!
//! 分岐の正本は旧版のコード。引数の解析(Python 3.10.11のargparse)・検査の順・子の環境の辞書・
//! 事前検査(旧`diff-snapshot.sh --precheck`を子で起動する)・filterの検証・context・実効値の確認・
//! 5操作のargv・revisionの解決・診断・最上位の例外の扱いを旧版と同じにする。
//! 読取りでindexや状態を更新しない(`GIT_OPTIONAL_LOCKS=0`)。
//!
//! 製品の登録表にはまだ載せない。検査専用の入口(`examples/r4g_contract.rs`)だけが使う。

pub mod args;
pub mod help;
pub mod lines;
pub mod path;

use super::r4g_support as support;
use args::{Arg, Namespace, Number, ParserId, Stop};
use dev_workflow_platform::r4g::process::{
    self, Completed, PythonCommand, PythonEnviron, RunError, SpawnError, TimeoutStage, ValueError,
};
use dev_workflow_platform::r4g::{fs, startup};
use path::PathError;
use std::time::Duration;
use support::json::{self, Value};
use support::pyint::PyInt;
use support::python_io::{PyError, PythonStdio};
use support::text::{self, PyText, from_str};
use support::traceback::{self, Frame};

/// 旧版の`VERSION`(contextの版)。
pub const VERSION: u32 = 1;

/// 旧版の`SAFE_GIT`の14項目(後続の診断の移植R4Dでも使う)。各項目は`git`へ渡す引数の並び。
pub const SAFE_GIT: [&[&str]; 14] = [
    &["--no-pager"],
    &["--no-replace-objects"],
    &["-c", "core.quotePath=false"],
    &["-c", "core.fsmonitor="],
    &["-c", "core.hooksPath=/dev/null"],
    &["-c", "core.ignoreCase=false"],
    &["-c", "core.splitIndex=false"],
    &["-c", "core.ignoreStat=false"],
    &["-c", "commit.gpgSign=false"],
    &["-c", "push.gpgSign=false"],
    &["-c", "filter.lfs.smudge="],
    &["-c", "filter.lfs.clean="],
    &["-c", "filter.lfs.process="],
    &["-c", "filter.lfs.required=false"],
];

/// `SAFE_GIT`を旧版のタプルと同じ平らな並び(26個)にしたもの。
pub fn safe_git_argv() -> Vec<&'static str> {
    SAFE_GIT
        .iter()
        .flat_map(|item| item.iter().copied())
        .collect()
}

const ITEMS: [&str; 4] = ["clean", "smudge", "process", "required"];
const PATHSPEC_ENV: [&str; 4] = [
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
];
const FORBIDDEN_ENV: [&str; 6] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
];

/// 子の期限。`text`は旧版の時間切れの文に出す元の期限の字面(`120`・`30`、検査の注入では`0.05`など)。
#[derive(Clone, Debug, PartialEq)]
pub struct Deadline {
    pub seconds: f64,
    pub text: String,
}

impl Deadline {
    pub fn precheck() -> Self {
        Self {
            seconds: 120.0,
            text: "120".to_string(),
        }
    }

    pub fn git() -> Self {
        Self {
            seconds: 30.0,
            text: "30".to_string(),
        }
    }
}

/// 入口が渡す設定。事前検査のスクリプトの場所は検査専用の入口だけが受け、子の環境へ混ぜない。
pub struct Settings {
    /// 旧版の`Path(__file__).with_name("diff-snapshot.sh")`(`str()`の後のパス)。
    pub precheck_script: Vec<u8>,
    pub precheck: Deadline,
    pub git: Deadline,
    /// 検査専用の変異(旧版の既存テストのコピーの書き換え`run_git(cwd, clean_env(), *command)`)。
    pub operation_clean_env: bool,
}

/// 旧Pythonの起動の後の状態。
pub struct Runtime {
    /// `os.environ`(地域設定の強制の後。同じ名前は最初の値・挿入の順)。
    pub environ: PythonEnviron,
    /// `sys.get_int_max_str_digits()`。
    pub int_max_str_digits: u64,
}

/// 旧版の終わり方。
pub enum Ending {
    /// 終了値(終了時のflushの前)。
    Exit(u8),
    /// 捕まえなかった`KeyboardInterrupt`(呼出履歴は書いた。`SIGINT`で終える)。
    Interrupt,
}

/// 旧版の例外の流れ。
enum Exc {
    /// 旧版の`Failure`(文と終了値)。
    Failure(PyText, u8),
    /// `OSError`・`ValueError`・`UnicodeError`・`RuntimeError`(`ERROR [runtime]`と終了20)。
    Runtime(PyText),
    /// ほかの例外(呼出履歴と終了1)。
    Other {
        frames: Vec<Frame>,
        type_name: &'static str,
        message: PyText,
    },
    /// `KeyboardInterrupt`。
    Interrupt,
    /// `SystemExit`(helpの後)。
    Exit(u8),
}

type Result<T> = std::result::Result<T, Exc>;

fn fail(message: &str, code: u8) -> Exc {
    Exc::Failure(from_str(message), code)
}

fn interrupted() -> bool {
    startup::take_python_interrupt()
}

/// 旧Pythonが次の評価の合間に信号を確かめる地点。
fn check() -> Result<()> {
    if interrupted() {
        Err(Exc::Interrupt)
    } else {
        Ok(())
    }
}

fn concat(parts: &[&[u32]]) -> PyText {
    parts.iter().flat_map(|part| part.iter().copied()).collect()
}

/// 書込みの例外を、旧版の最上位の扱いへ。
fn write_error(error: PyError) -> Exc {
    match error {
        PyError::Os(errno) => Exc::Runtime(support::strerror::os_error(errno.raw_os_error(), None)),
        PyError::InvalidLength {
            returned,
            requested,
        } => Exc::Runtime(from_str(&format!(
            "raw write() returned invalid length {returned} (should have been between 0 and {requested})"
        ))),
        PyError::Encode(error) => Exc::Runtime(from_str(&error.message())),
        PyError::Interrupt => Exc::Interrupt,
    }
}

fn list_attribute(frames: Vec<Frame>) -> Exc {
    Exc::Other {
        frames,
        type_name: "AttributeError",
        message: from_str("'list' object has no attribute 'encode'"),
    }
}

fn type_error(frames: Vec<Frame>, message: &str) -> Exc {
    Exc::Other {
        frames,
        type_name: "TypeError",
        message: from_str(message),
    }
}

/// `sys.stdout.buffer.write(data)`か`sys.stderr.buffer.write(data)`。
fn buffer_write(stdio: &mut PythonStdio, stdout: bool, data: &[u8]) -> Result<()> {
    let stream = if stdout {
        &mut stdio.stdout
    } else {
        &mut stdio.stderr
    };
    stream.write_buffer(data).map(|_| ()).map_err(write_error)
}

/// `print(text)`(標準出力)。
fn print_stdout(stdio: &mut PythonStdio, text: &[u32]) -> Result<()> {
    let stream = &mut stdio.stdout;
    stream.write(text).map_err(write_error)?;
    stream.write(&[10]).map(|_| ()).map_err(write_error)
}

/// `print(text, file=sys.stderr)`。
fn print_stderr(stdio: &mut PythonStdio, text: &[u32]) -> Result<()> {
    let stream = &mut stdio.stderr;
    stream.write(text).map_err(write_error)?;
    stream.write(&[10]).map(|_| ()).map_err(write_error)
}

// ---------------------------------------------------------------- 入力

fn strict(value: &[u8]) -> Option<String> {
    std::str::from_utf8(value).ok().map(str::to_string)
}

fn input_error(message: &str) -> Exc {
    fail(&format!("ERROR [input] {message}"), 2)
}

/// 旧版の`physical_cwd`。`value`はargparseの値。
fn physical_cwd(value: &Arg) -> Result<String> {
    let value = match value {
        Arg::EmptyList => {
            return Err(list_attribute(lines::frames(&[
                "module",
                "main_parse",
                "parse_input_cwd",
                "physical_cwd_strict",
                "strict_text_encode",
            ])));
        }
        Arg::Str(text) => text,
    };
    if value.is_empty() {
        return Err(input_error("cwd が空"));
    }
    let resolve_error = |error: PathError| {
        let mut message = from_str("ERROR [input] cwd を解決できない: ");
        message.extend(path_error_text(&error));
        Exc::Failure(message, 2)
    };
    match path::is_dir(value.as_bytes()) {
        Ok(true) => {}
        Ok(false) => return Err(input_error("cwd は存在するディレクトリでなければならない")),
        Err(error) => return Err(resolve_error(error)),
    }
    let resolved = path::resolve(value.as_bytes()).map_err(resolve_error)?;
    strict(&resolved).ok_or_else(|| input_error("物理 cwd は厳密な UTF-8 ではない"))
}

fn path_error_text(error: &PathError) -> PyText {
    match error {
        PathError::Os { errno, filename } => support::strerror::os_error(
            *errno,
            filename.as_ref().map(|name| text::decode(name)).as_deref(),
        ),
        PathError::SymlinkLoop(path) => {
            let mut message = from_str("Symlink loop from ");
            message.extend(text::repr(&text::decode(path)));
            message
        }
    }
}

fn validate_frames(last: &[&'static str]) -> Vec<Frame> {
    let mut names = vec!["module", "main_parse", "parse_input_validate"];
    names.extend_from_slice(last);
    lines::frames(&names)
}

fn number_in(value: &PyInt, low: i64, high: i64) -> bool {
    value
        .to_i64()
        .is_some_and(|number| (low..=high).contains(&number))
}

/// 旧版の`validate_revision`。
fn validate_revision(value: &Arg) -> Result<()> {
    let Arg::Str(value) = value else {
        return Err(list_attribute(validate_frames(&[
            "validate_revision_call",
            "validate_revision_strict",
            "strict_text_encode",
        ])));
    };
    let length = value.chars().count();
    if !(1..=1024).contains(&length)
        || value.starts_with('-')
        || value.contains('\n')
        || value.contains('\r')
    {
        return Err(input_error("revision が不正"));
    }
    Ok(())
}

/// 旧版の`validate_path`。`call`は呼び出した行の地点。
fn validate_path(value: &Arg, call: &'static str) -> Result<()> {
    let Arg::Str(value) = value else {
        return Err(list_attribute(validate_frames(&[
            call,
            "validate_path_strict",
            "strict_text_encode",
        ])));
    };
    if value.is_empty() || value.starts_with('/') || value.split('/').any(|part| part == "..") {
        return Err(input_error("path はルートから外れない相対パスが必要"));
    }
    Ok(())
}

/// 旧版の`validate_run_args`。
fn validate_run_args(ns: &Namespace) -> Result<()> {
    match ns.expect.as_ref().expect("required") {
        Arg::EmptyList => {
            let mut frames = validate_frames(&["validate_hash"]);
            frames.push(lines::RE_FULLMATCH);
            return Err(type_error(frames, "expected string or bytes-like object"));
        }
        Arg::Str(value) => {
            if value.len() != 64
                || !value
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(input_error("expect-context-sha256 は小文字16進64桁が必要"));
            }
        }
    }
    let operation = ns.operation.as_deref().expect("checked before");
    let compare_error = |point: &'static str| {
        type_error(
            validate_frames(&[point]),
            "'<=' not supported between instances of 'int' and 'list'",
        )
    };
    if operation == "diff" {
        let context = match ns.context.as_ref().expect("diff has a context") {
            Number::EmptyList => return Err(compare_error("validate_diff")),
            Number::Int(value) => value,
        };
        if !number_in(context, 0, 100)
            || (ns.cached && ns.target.is_some())
            || (ns.target.is_some() && ns.base.is_none())
        {
            return Err(input_error("diff の範囲または組合せが不正"));
        }
    }
    if operation == "log" {
        match ns.limit.as_ref().expect("log has a limit") {
            Number::EmptyList => return Err(compare_error("validate_limit")),
            Number::Int(value) if !number_in(value, 1, 1000) => {
                return Err(input_error("limit は1〜1000が必要"));
            }
            Number::Int(_) => {}
        }
    }
    if operation == "show" && ns.file.is_some() && ns.format.is_some() {
        return Err(input_error("show --file と --format は併用できない"));
    }
    for value in [&ns.base, &ns.target, &ns.from_rev, &ns.to_rev, &ns.rev]
        .into_iter()
        .flatten()
    {
        validate_revision(value)?;
    }
    for value in &ns.path {
        validate_path(value, "validate_path_call")?;
    }
    if let Some(file) = &ns.file {
        validate_path(file, "validate_file_call")?;
    }
    Ok(())
}

// ---------------------------------------------------------------- 子

fn env_text(value: &str) -> Vec<u8> {
    value.as_bytes().to_vec()
}

/// 旧版の`clean_env`(`dict(os.environ)`の順を保つ)。
fn clean_env(environ: &PythonEnviron) -> Result<PythonEnviron> {
    let mut env = environ.clone();
    let names: Vec<Vec<u8>> = env.iter().map(|(key, _)| key.to_vec()).collect();
    for key in names {
        if key == b"GIT_CONFIG_COUNT"
            || key == b"GIT_CONFIG_PARAMETERS"
            || key.starts_with(b"GIT_CONFIG_KEY_")
            || key.starts_with(b"GIT_CONFIG_VALUE_")
        {
            env.remove(&key);
        }
    }
    for key in PATHSPEC_ENV {
        env.remove(key.as_bytes());
    }
    for key in FORBIDDEN_ENV {
        if env
            .get(key.as_bytes())
            .is_some_and(|value| !value.is_empty())
        {
            return Err(input_error(&format!(
                "対象を変更する環境変数が設定されている: {key}"
            )));
        }
        env.remove(key.as_bytes());
    }
    for (key, value) in [
        ("GIT_NO_LAZY_FETCH", "1"),
        ("GIT_OPTIONAL_LOCKS", "0"),
        ("GIT_TERMINAL_PROMPT", "0"),
    ] {
        env.insert(key.as_bytes(), value.as_bytes());
    }
    Ok(env)
}

/// 旧Pythonの`repr(float)`(最短の表記。指数は`e-05`の形)。
pub fn float_repr(value: f64) -> String {
    if value.is_sign_negative() {
        // 負の数と -0.0 は、絶対値の表記に符号を付ける(Pythonの repr と同じ)。
        return format!("-{}", float_repr(-value));
    }
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').expect("`{:e}` has an exponent");
    let exponent: i32 = exponent.parse().expect("integer exponent");
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    let decpt = exponent + 1;
    if !(-4 < decpt && decpt <= 16) {
        let rest = &digits[1..];
        let sign = if exponent < 0 { '-' } else { '+' };
        let mantissa = if rest.is_empty() {
            digits[..1].to_string()
        } else {
            format!("{}.{rest}", &digits[..1])
        };
        return format!("{mantissa}e{sign}{:02}", exponent.abs());
    }
    let n = digits.len() as i32;
    if decpt <= 0 {
        format!("0.{}{digits}", "0".repeat((-decpt) as usize))
    } else if decpt >= n {
        format!("{digits}{}.0", "0".repeat((decpt - n) as usize))
    } else {
        format!(
            "{}.{}",
            &digits[..decpt as usize],
            &digits[decpt as usize..]
        )
    }
}

fn command_repr(command: &[Vec<u8>]) -> PyText {
    let mut out = from_str("[");
    for (index, item) in command.iter().enumerate() {
        if index > 0 {
            out.extend(from_str(", "));
        }
        out.extend(text::repr(&text::decode(item)));
    }
    out.push(u32::from(b']'));
    out
}

/// 旧`subprocess.run`の例外の`str()`。`Err(Exc)`は旧版が捕まえない例外(`KeyboardInterrupt`)。
fn run_error_text(command: &[Vec<u8>], deadline: &Deadline, error: RunError) -> Result<PyText> {
    let os = |errno: i32, filename: Option<&[u8]>| {
        support::strerror::os_error(errno, filename.map(text::decode).as_deref())
    };
    Ok(match error {
        RunError::Spawn(SpawnError::DevNull(errno)) => os(errno.raw_os_error(), Some(b"/dev/null")),
        RunError::Spawn(SpawnError::Os(errno)) | RunError::Os(errno) => {
            os(errno.raw_os_error(), None)
        }
        RunError::Spawn(SpawnError::Value(ValueError::IllegalEnvironmentName)) => {
            from_str("illegal environment variable name")
        }
        RunError::Spawn(SpawnError::Value(ValueError::EmbeddedNull)) => {
            from_str("embedded null byte")
        }
        RunError::Spawn(SpawnError::Child(failure)) => match failure.stage {
            process::ExecStage::Exec => os(failure.errno.raw_os_error(), Some(&command[0])),
            process::ExecStage::BeforeExec => os(failure.errno.raw_os_error(), None),
        },
        RunError::Timeout { stage, .. } => {
            let seconds = match stage {
                TimeoutStage::Reading => deadline.text.clone(),
                TimeoutStage::Waiting { remaining_seconds } => float_repr(remaining_seconds),
            };
            concat(&[
                &from_str("Command '"),
                &command_repr(command),
                &from_str(&format!("' timed out after {seconds} seconds")),
            ])
        }
        RunError::Interrupted => return Err(Exc::Interrupt),
    })
}

fn run_child(
    command: &[Vec<u8>],
    env: &PythonEnviron,
    deadline: &Deadline,
) -> std::result::Result<Completed, RunError> {
    let timeout = Duration::from_secs_f64(deadline.seconds.max(0.0));
    process::python_run(
        &PythonCommand { args: command, env },
        timeout,
        &mut interrupted,
    )
}

struct Context<'a> {
    settings: &'a Settings,
    runtime: &'a Runtime,
    cwd: String,
}

impl Context<'_> {
    /// 旧版の`run_git`。失敗は`ERROR [git] Git を実行できない`。
    fn run_git(&self, env: &PythonEnviron, args: &[&[u8]]) -> Result<Completed> {
        let mut command: Vec<Vec<u8>> = vec![b"git".to_vec(), b"-C".to_vec(), env_text(&self.cwd)];
        command.extend(safe_git_argv().into_iter().map(env_text));
        command.extend(args.iter().map(|arg| arg.to_vec()));
        match run_child(&command, env, &self.settings.git) {
            Ok(result) => {
                check()?;
                Ok(result)
            }
            Err(error) => {
                let text = run_error_text(&command, &self.settings.git, error)?;
                Err(Exc::Failure(
                    concat(&[&from_str("ERROR [git] Git を実行できない: "), &text]),
                    20,
                ))
            }
        }
    }

    /// 旧版の`checked_git`。
    fn checked_git(
        &self,
        stdio: &mut PythonStdio,
        env: &PythonEnviron,
        args: &[&[u8]],
    ) -> Result<Vec<u8>> {
        let result = self.run_git(env, args)?;
        buffer_write(stdio, false, &result.stderr)?;
        check()?;
        if result.returncode != 0 {
            return Err(fail(
                &format!(
                    "ERROR [git] 内部確認に失敗（終了コード {}）",
                    result.returncode
                ),
                20,
            ));
        }
        Ok(result.stdout)
    }

    /// 旧版の`precheck`。
    fn precheck(
        &self,
        stdio: &mut PythonStdio,
        env: &PythonEnviron,
        accept: Option<&Arg>,
    ) -> Result<Vec<u8>> {
        let script = path::pathlib_str(&self.settings.precheck_script);
        let is_file =
            path::is_file(&script).map_err(|error| Exc::Runtime(path_error_text(&error)))?;
        if !is_file || !path::readable(&script) {
            return Err(fail(
                "ERROR [requirements] diff-snapshot.sh を読み込めない",
                20,
            ));
        }
        let mut command: Vec<Vec<u8>> = vec![
            b"bash".to_vec(),
            script,
            b"--cwd".to_vec(),
            env_text(&self.cwd),
            b"--precheck".to_vec(),
        ];
        match accept {
            None => {}
            Some(Arg::Str(value)) => {
                command.push(b"--accept".to_vec());
                command.push(env_text(value));
            }
            Some(Arg::EmptyList) => {
                let mut frames = lines::frames(&["module", "main_precheck", "precheck_run"]);
                frames.extend(lines::SUBPROCESS_FORK_EXEC);
                return Err(type_error(
                    frames,
                    "expected str, bytes or os.PathLike object, not list",
                ));
            }
        }
        let result = match run_child(&command, env, &self.settings.precheck) {
            Ok(result) => result,
            Err(error) => {
                let text = run_error_text(&command, &self.settings.precheck, error)?;
                return Err(Exc::Failure(
                    concat(&[
                        &from_str("ERROR [precheck] 事前検査を実行できない: "),
                        &text,
                    ]),
                    20,
                ));
            }
        };
        check()?;
        buffer_write(stdio, false, &result.stderr)?;
        if result.returncode != 0 {
            let code = match result.returncode {
                2 => 2,
                20 => 20,
                22 => 22,
                _ => 20,
            };
            return Err(fail(
                &format!(
                    "ERROR [precheck] 事前検査の終了コード {}",
                    result.returncode
                ),
                code,
            ));
        }
        Ok(result.stdout)
    }
}

/// 旧版の`parse_filters`の結果。`semantic`は意味上の辞書(挿入の順)、`pairs`はGitへ渡す原バイト。
struct Filters {
    semantic: Vec<(String, String)>,
    pairs: Vec<(Vec<u8>, Vec<u8>)>,
}

fn is_index(text: &[u8]) -> bool {
    text == b"0" || (!text.is_empty() && text[0] != b'0' && text.iter().all(u8::is_ascii_digit))
}

/// `GIT_CONFIG_(COUNT|KEY_(0|[1-9][0-9]*)|VALUE_(0|[1-9][0-9]*))`。
fn config_name(key: &[u8]) -> bool {
    key == b"GIT_CONFIG_COUNT"
        || key.strip_prefix(b"GIT_CONFIG_KEY_").is_some_and(is_index)
        || key.strip_prefix(b"GIT_CONFIG_VALUE_").is_some_and(is_index)
}

/// 旧版の`parse_filters`。
fn parse_filters(raw: &[u8]) -> Result<Filters> {
    let stop = |message: &str| fail(&format!("ERROR [precheck] {message}"), 20);
    if raw.is_empty() {
        return Ok(Filters {
            semantic: Vec::new(),
            pairs: Vec::new(),
        });
    }
    if !raw.ends_with(b"\0") {
        return Err(stop("出力が NUL 終端でない"));
    }
    // 挿入の順は Vec で保ち、重複の検査と添字の検索は表で引く(大きな出力でも2乗にしない)。
    let mut parsed: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    let mut seen: std::collections::HashSet<Vec<u8>> = std::collections::HashSet::new();
    for record in raw[..raw.len() - 1].split(|byte| *byte == 0) {
        if std::str::from_utf8(record).is_err() {
            return Err(stop("出力が厳密な UTF-8 でない"));
        }
        let Some(index) = record.iter().position(|byte| *byte == b'=') else {
            return Err(stop("設定変数名が不正"));
        };
        let (key, value) = (&record[..index], &record[index + 1..]);
        if !config_name(key) {
            return Err(stop("設定変数名が不正"));
        }
        if !seen.insert(key.to_vec()) {
            return Err(stop("設定変数が重複"));
        }
        parsed.push((key.to_vec(), value.to_vec()));
    }
    let count_text = parsed
        .iter()
        .position(|(name, _)| name == b"GIT_CONFIG_COUNT")
        .map(|index| parsed.remove(index).1);
    let Some(count_text) =
        count_text.filter(|text| !text.is_empty() && text.iter().all(u8::is_ascii_digit))
    else {
        return Err(stop("COUNT が不正"));
    };
    let count = parsed.len() / 2;
    let stripped = |text: &[u8]| -> Vec<u8> {
        text.iter()
            .skip_while(|byte| **byte == b'0')
            .copied()
            .collect()
    };
    if !parsed.len().is_multiple_of(2)
        || stripped(&count_text) != stripped(count.to_string().as_bytes())
    {
        return Err(stop("COUNT と KEY/VALUE の数が一致しない"));
    }
    let mut filters = Filters {
        semantic: Vec::new(),
        pairs: Vec::new(),
    };
    let mut names: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut semantic_keys: std::collections::HashSet<String> = std::collections::HashSet::new();
    let index: std::collections::HashMap<&[u8], &[u8]> = parsed
        .iter()
        .map(|(key, value)| (key.as_slice(), value.as_slice()))
        .collect();
    let get = |name: String| index.get(name.as_bytes()).map(|value| value.to_vec());
    for index in 0..count {
        let (Some(key), Some(value)) = (
            get(format!("GIT_CONFIG_KEY_{index}")),
            get(format!("GIT_CONFIG_VALUE_{index}")),
        ) else {
            return Err(stop("KEY/VALUE の添字が連続していない"));
        };
        let key_text = String::from_utf8(key.clone()).expect("checked as UTF-8");
        let value_text = String::from_utf8(value.clone()).expect("checked as UTF-8");
        let Some((name, item)) = filter_key(&key_text) else {
            return Err(stop("許可する filter 設定ではない"));
        };
        if !semantic_keys.insert(key_text.clone()) {
            return Err(stop("意味上の設定キーが重複"));
        }
        if value_text != if item == "required" { "false" } else { "" } {
            return Err(stop("filter の無効化値が不正"));
        }
        names.insert(name);
        filters.semantic.push((key_text, value_text));
        filters.pairs.push((key, value));
    }
    for name in &names {
        if ITEMS
            .iter()
            .any(|item| !semantic_keys.contains(&format!("filter.{name}.{item}")))
        {
            return Err(stop("filter の4項目がそろっていない"));
        }
    }
    Ok(filters)
}

/// `filter\.(.+)\.(clean|smudge|process|required)`(DOTALLの`fullmatch`)。
fn filter_key(key: &str) -> Option<(String, &'static str)> {
    let rest = key.strip_prefix("filter.")?;
    ITEMS.iter().find_map(|item| {
        let name = rest.strip_suffix(item)?.strip_suffix('.')?;
        (!name.is_empty()).then(|| (name.to_string(), *item))
    })
}

/// 旧版の`context`。
fn context_digest(cwd: &str, accept: Option<&str>, filters: &Filters) -> String {
    let filters = Value::Object(
        filters
            .semantic
            .iter()
            .map(|(key, value)| (from_str(key), Value::str(value)))
            .collect(),
    );
    json::sha256(&Value::Object(vec![
        (from_str("version"), Value::int(VERSION)),
        (from_str("cwd"), Value::str(cwd)),
        (from_str("accept"), accept.map_or(Value::Null, Value::str)),
        (from_str("filters"), filters),
    ]))
}

fn apply_filters(env: &mut PythonEnviron, pairs: &[(Vec<u8>, Vec<u8>)]) {
    env.insert(b"GIT_CONFIG_COUNT", pairs.len().to_string().as_bytes());
    for (index, (key, value)) in pairs.iter().enumerate() {
        env.insert(format!("GIT_CONFIG_KEY_{index}").as_bytes(), key);
        env.insert(format!("GIT_CONFIG_VALUE_{index}").as_bytes(), value);
    }
}

// ---------------------------------------------------------------- 本体

/// `shutil.get_terminal_size().columns`。
fn terminal_columns(runtime: &Runtime) -> i64 {
    let read = |name: &[u8]| -> i64 {
        let Some(value) = runtime.environ.get(name) else {
            return 0;
        };
        match support::pyint::parse(&text::decode(value), runtime.int_max_str_digits) {
            Ok(number) => match number.to_i64() {
                Some(value) => value,
                None if number.negative => i64::MIN,
                None => i64::MAX,
            },
            Err(_) => 0,
        }
    };
    let mut columns = read(b"COLUMNS");
    let lines = read(b"LINES");
    if columns <= 0 || lines <= 0 {
        let size = fs::terminal_columns(fs::standard(1))
            .map(i64::from)
            .unwrap_or(80);
        if columns <= 0 {
            columns = size;
        }
    }
    columns
}

/// `parser.print_help()`(標準出力へ)と`parser.exit()`。
fn print_help(id: ParserId, runtime: &Runtime, stdio: &mut PythonStdio) -> Result<()> {
    let help = from_str(&help::format_help(id, terminal_columns(runtime)));
    stdio.stdout.write(&help).map_err(write_error)?;
    Err(Exc::Exit(0))
}

/// 旧版の`parse_input`。
fn parse_input(
    argv: &[Vec<u8>],
    runtime: &Runtime,
    stdio: &mut PythonStdio,
) -> Result<(Namespace, String)> {
    let mut texts = Vec::new();
    for value in argv {
        match strict(value) {
            Some(text) => texts.push(text),
            None => return Err(input_error("引数 は厳密な UTF-8 ではない")),
        }
    }
    let ns = match args::parse_args(&texts, runtime.int_max_str_digits) {
        Ok(ns) => ns,
        Err(Stop::Error(message)) => {
            return Err(Exc::Failure(
                concat(&[&from_str("ERROR [input] 引数が不正: "), &message]),
                2,
            ));
        }
        Err(Stop::Duplicate(option)) => {
            return Err(input_error(&format!("オプションが重複: {option}")));
        }
        Err(Stop::Help(id)) => {
            print_help(id, runtime, stdio)?;
            unreachable!("print_help always exits");
        }
    };
    check()?;
    if ns.mode.is_none() || (ns.mode.as_deref() == Some("run") && ns.operation.is_none()) {
        return Err(input_error("prepare または run と読取り操作を指定する"));
    }
    let cwd = physical_cwd(ns.cwd.as_ref().expect("required"))?;
    if ns.mode.as_deref() == Some("run") {
        validate_run_args(&ns)?;
    }
    Ok((ns, cwd))
}

fn revision(
    context: &Context<'_>,
    stdio: &mut PythonStdio,
    env: &PythonEnviron,
    value: &Arg,
) -> Result<String> {
    let Arg::Str(value) = value else {
        unreachable!("revisions are validated as text");
    };
    let raw = context.checked_git(
        stdio,
        env,
        &[
            b"rev-parse",
            b"--verify",
            b"--end-of-options",
            format!("{value}^{{commit}}").as_bytes(),
        ],
    )?;
    let body = raw.strip_suffix(b"\n").filter(|body| {
        (body.len() == 40 || body.len() == 64)
            && body
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    });
    match body {
        Some(body) => Ok(String::from_utf8(body.to_vec()).expect("ascii")),
        None => Err(fail(
            "ERROR [git] revision の解決結果が commit ID ではない",
            20,
        )),
    }
}

fn verify(
    context: &Context<'_>,
    stdio: &mut PythonStdio,
    env: &PythonEnviron,
    filters: &Filters,
) -> Result<()> {
    for (key, wanted) in &filters.semantic {
        let raw = context.checked_git(stdio, env, &[b"config", b"--get", key.as_bytes()])?;
        if raw != [wanted.as_bytes(), b"\n"].concat() {
            return Err(fail(
                "ERROR [git] filter の実効値が無効化値と一致しない",
                20,
            ));
        }
    }
    let raw = context.checked_git(stdio, env, &[b"rev-parse", b"--show-toplevel"])?;
    let Some(root) = raw.strip_suffix(b"\n") else {
        return Err(fail(
            "ERROR [git] リポジトリのルート出力に終端改行がない",
            20,
        ));
    };
    let malformed = || fail("ERROR [git] リポジトリのルート出力が不正", 20);
    let mismatch = || fail("ERROR [git] cwd がリポジトリの物理ルートと一致しない", 20);
    if std::str::from_utf8(root).is_err() {
        return Err(malformed());
    }
    if !root.starts_with(b"/") {
        return Err(mismatch());
    }
    if root.contains(&0) {
        // `Path.is_dir`は`ValueError`を偽にする。
        return Err(mismatch());
    }
    match path::is_dir(root) {
        Ok(true) => {}
        Ok(false) => return Err(mismatch()),
        Err(_) => return Err(malformed()),
    }
    match path::resolve(root) {
        Ok(resolved) if resolved == context.cwd.as_bytes() => Ok(()),
        Ok(_) => Err(mismatch()),
        Err(_) => Err(malformed()),
    }
}

fn operation(
    context: &Context<'_>,
    stdio: &mut PythonStdio,
    ns: &Namespace,
    env: &PythonEnviron,
) -> Result<()> {
    let op = ns.operation.as_deref().expect("run has an operation");
    let diff_options = [
        "--no-ext-diff",
        "--no-textconv",
        "--submodule=short",
        "--no-color",
    ];
    let log_options = ["--no-show-signature", "--no-decorate", "--no-color"];
    let concat_error = |point: &'static str| {
        type_error(
            lines::frames(&["module", "main_operation", point]),
            "can only concatenate str (not \"list\") to str",
        )
    };
    let text_of = |value: &Option<Arg>| -> Option<String> {
        match value {
            Some(Arg::Str(text)) => Some(text.clone()),
            _ => None,
        }
    };
    let mut command: Vec<Vec<u8>> = Vec::new();
    let push = |command: &mut Vec<Vec<u8>>, value: &str| command.push(value.as_bytes().to_vec());
    match op {
        "status" => {
            push(&mut command, "status");
            let short = matches!(&ns.format, Some(Arg::Str(text)) if text == "short");
            push(
                &mut command,
                if short { "--short" } else { "--porcelain=v1" },
            );
            let Some(untracked) = text_of(&ns.untracked) else {
                return Err(concat_error("operation_status"));
            };
            push(&mut command, &format!("--untracked-files={untracked}"));
            push(&mut command, "--ignore-submodules=dirty");
        }
        "diff" => {
            push(&mut command, "diff");
            for option in diff_options {
                push(&mut command, option);
            }
            push(&mut command, "--ignore-submodules=dirty");
            let Some(Number::Int(lines_around)) = &ns.context else {
                unreachable!("validated");
            };
            push(
                &mut command,
                &format!("--unified={}", lines_around.to_text()),
            );
            let Some(format) = text_of(&ns.format) else {
                return Err(concat_error("operation_diff"));
            };
            push(&mut command, &format!("--{format}"));
            if ns.cached {
                push(&mut command, "--cached");
            }
            if let Some(base) = &ns.base {
                let oid = revision(context, stdio, env, base)?;
                push(&mut command, &oid);
            }
            if let Some(target) = &ns.target {
                let oid = revision(context, stdio, env, target)?;
                push(&mut command, &oid);
            }
        }
        "log" => {
            push(&mut command, "log");
            for option in log_options {
                push(&mut command, option);
            }
            push(&mut command, "--no-patch");
            let Some(Number::Int(limit)) = &ns.limit else {
                unreachable!("validated");
            };
            push(&mut command, &format!("--max-count={}", limit.to_text()));
            let Some(format) = text_of(&ns.format) else {
                return Err(concat_error("operation_log"));
            };
            push(&mut command, &format!("--format={format}"));
            let target = revision(
                context,
                stdio,
                env,
                ns.to_rev.as_ref().expect("log has --to"),
            )?;
            match &ns.from_rev {
                Some(from) => {
                    let from = revision(context, stdio, env, from)?;
                    push(&mut command, &format!("{from}..{target}"));
                }
                None => push(&mut command, &target),
            }
        }
        "show" => {
            let rev = revision(
                context,
                stdio,
                env,
                ns.rev.as_ref().expect("show has --rev"),
            )?;
            push(&mut command, "show");
            for option in diff_options.iter().chain(log_options.iter()) {
                push(&mut command, option);
            }
            match &ns.file {
                Some(file) => {
                    let Arg::Str(file) = file else {
                        unreachable!("validated");
                    };
                    let object = format!("{rev}:{file}");
                    let kind = context.checked_git(
                        stdio,
                        env,
                        &[b"cat-file", b"-t", object.as_bytes()],
                    )?;
                    if kind != b"blob\n" {
                        return Err(fail("ERROR [git] 指定した file は blob ではない", 20));
                    }
                    push(&mut command, &object);
                }
                None => {
                    let format = match &ns.format {
                        Some(Arg::Str(text)) if !text.is_empty() => text.clone(),
                        _ => "patch".to_string(),
                    };
                    push(&mut command, &format!("--{format}"));
                    push(&mut command, &rev);
                }
            }
        }
        _ => {
            let tracked = matches!(&ns.file_mode, Some(Arg::Str(text)) if text == "tracked");
            if tracked {
                push(&mut command, "ls-files");
                push(&mut command, "--cached");
            } else {
                push(&mut command, "ls-files");
                push(&mut command, "--others");
                push(&mut command, "--exclude-standard");
            }
        }
    }
    if op != "show" {
        push(&mut command, "--");
        for value in &ns.path {
            let Arg::Str(value) = value else {
                unreachable!("validated");
            };
            push(&mut command, &format!(":(literal){value}"));
        }
    }
    let mutated;
    let env = if context.settings.operation_clean_env {
        mutated = clean_env(&context.runtime.environ)?;
        &mutated
    } else {
        env
    };
    let refs: Vec<&[u8]> = command.iter().map(Vec::as_slice).collect();
    let result = context.run_git(env, &refs)?;
    buffer_write(stdio, true, &result.stdout)?;
    check()?;
    buffer_write(stdio, false, &result.stderr)?;
    check()?;
    if result.returncode != 0 {
        return Err(fail(
            &format!(
                "ERROR [git] 読取りに失敗（終了コード {}）。部分的な stdout を成功した観察に使わない",
                result.returncode
            ),
            20,
        ));
    }
    Ok(())
}

fn main_body(
    argv: &[Vec<u8>],
    settings: &Settings,
    runtime: &Runtime,
    stdio: &mut PythonStdio,
) -> Result<()> {
    let (ns, cwd) = parse_input(argv, runtime, stdio)?;
    let mut env = clean_env(&runtime.environ)?;
    let context = Context {
        settings,
        runtime,
        cwd,
    };
    let raw = context.precheck(stdio, &env, ns.accept.as_ref())?;
    let filters = parse_filters(&raw)?;
    let accept = match &ns.accept {
        Some(Arg::Str(text)) => Some(text.as_str()),
        _ => None,
    };
    let digest = context_digest(&context.cwd, accept, &filters);
    check()?;
    if ns.mode.as_deref() == Some("run")
        && !matches!(&ns.expect, Some(Arg::Str(expected)) if *expected == digest)
    {
        return Err(fail(
            "ERROR [context] 承認情報または設定が一致しない。委託元へ戻す",
            22,
        ));
    }
    apply_filters(&mut env, &filters.pairs);
    verify(&context, stdio, &env, &filters)?;
    if ns.mode.as_deref() == Some("prepare") {
        let line = json::dumps(&Value::Object(vec![
            (from_str("version"), Value::int(VERSION)),
            (from_str("cwd"), Value::str(&context.cwd)),
            (from_str("accept"), accept.map_or(Value::Null, Value::str)),
            (from_str("context_sha256"), Value::str(&digest)),
        ]));
        print_stdout(stdio, &from_str(&line))?;
        check()?;
    } else {
        operation(&context, stdio, &ns, &env)?;
    }
    Ok(())
}

/// 捕まえなかった`KeyboardInterrupt`の呼出履歴(途中の行は比べない決定。`<module>`の段だけ書く)。
fn print_keyboard_interrupt(stdio: &mut PythonStdio) {
    traceback::print_exception(
        &mut stdio.stderr,
        &lines::frames(&["module"]),
        "KeyboardInterrupt",
        &[],
    );
}

/// 最上位の`except`の中の`print`が投げた例外(旧版は捕まえず、呼出履歴と終了1)。
fn handler_failed(
    stdio: &mut PythonStdio,
    original: (&str, &[u32]),
    point: &str,
    error: Exc,
) -> Ending {
    let (type_name, message): (String, PyText) = match error {
        Exc::Interrupt => ("KeyboardInterrupt".to_string(), Vec::new()),
        Exc::Runtime(text) => ("OSError".to_string(), text),
        _ => ("RuntimeError".to_string(), Vec::new()),
    };
    let mut first = traceback::render(
        &lines::frames(&["module"]),
        &traceback::exception(original.0, original.1),
    );
    first.extend(from_str(
        "\nDuring handling of the above exception, another exception occurred:\n\n",
    ));
    let _ = stdio.stderr.write(&first);
    traceback::print_exception(
        &mut stdio.stderr,
        &lines::frames(&[point]),
        &type_name,
        &message,
    );
    if type_name == "KeyboardInterrupt" {
        Ending::Interrupt
    } else {
        Ending::Exit(1)
    }
}

/// 旧版の`if __name__ == "__main__":`の全体。`argv`は`sys.argv[1:]`の元のバイト列。
pub fn main(
    argv: &[Vec<u8>],
    settings: &Settings,
    runtime: &Runtime,
    stdio: &mut PythonStdio,
) -> Ending {
    match main_body(argv, settings, runtime, stdio) {
        Ok(()) => Ending::Exit(0),
        Err(Exc::Exit(code)) => Ending::Exit(code),
        Err(Exc::Failure(message, code)) => match print_stderr(stdio, &message) {
            Ok(()) => Ending::Exit(code),
            Err(error) => handler_failed(stdio, ("Failure", &message), "module_failure", error),
        },
        Err(Exc::Runtime(text)) => {
            let message = concat(&[&from_str("ERROR [runtime] 実行に失敗: "), &text]);
            match print_stderr(stdio, &message) {
                Ok(()) => Ending::Exit(20),
                Err(error) => handler_failed(stdio, ("OSError", &text), "module_runtime", error),
            }
        }
        Err(Exc::Other {
            frames,
            type_name,
            message,
        }) => {
            traceback::print_exception(&mut stdio.stderr, &frames, type_name, &message);
            Ending::Exit(1)
        }
        Err(Exc::Interrupt) => {
            print_keyboard_interrupt(stdio);
            Ending::Interrupt
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 本物のPythonの`repr(float)`と、負の数・-0.0・指数の境目で照らす。
    #[test]
    fn float_repr_matches_python_repr() {
        let values: [f64; 16] = [
            0.0,
            -0.0,
            -0.0012345,
            0.0012345,
            -1e-5,
            1e-5,
            1e-4,
            -1e16,
            1e16,
            9999999999999998.0,
            123.456,
            -30.0,
            0.30000000000000004,
            2.5e-308,
            -1.7976931348623157e308,
            5e-324,
        ];
        let script = "import sys\nfor v in sys.argv[1:]: print(repr(float.fromhex(v)))";
        let args: Vec<String> = values.iter().map(|value| hex_float(*value)).collect();
        let output = std::process::Command::new("python3")
            .args(["-I", "-B", "-c", script])
            .args(&args)
            .output()
            .unwrap();
        assert!(output.status.success());
        let expected = String::from_utf8(output.stdout).unwrap();
        let ours: String = values
            .iter()
            .map(|value| float_repr(*value) + "\n")
            .collect();
        assert_eq!(ours, expected);
    }

    /// `float.hex()`の形(Pythonへ値を正確に渡す)。
    fn hex_float(value: f64) -> String {
        let bits = value.to_bits();
        let sign = if bits >> 63 == 1 { "-" } else { "" };
        let exponent = ((bits >> 52) & 0x7ff) as i64;
        let mantissa = bits & ((1u64 << 52) - 1);
        if exponent == 0 && mantissa == 0 {
            return format!("{sign}0x0.0p+0");
        }
        if exponent == 0 {
            return format!("{sign}0x0.{mantissa:013x}p-1022");
        }
        format!("{sign}0x1.{mantissa:013x}p{:+}", exponent - 1023)
    }

    #[test]
    fn float_repr_matches_python() {
        for (value, text) in [
            (0.300_000_000_000_000_04, "0.30000000000000004"),
            (0.1, "0.1"),
            (1e-5, "1e-05"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (30.0, "30.0"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (0.05, "0.05"),
        ] {
            assert_eq!(float_repr(value), text);
        }
    }

    #[test]
    fn safe_git_has_fourteen_items_in_the_old_order() {
        assert_eq!(SAFE_GIT.len(), 14);
        let source = std::fs::read_to_string(lines::old_script_path()).unwrap();
        let start = source.find("SAFE_GIT = (").unwrap();
        let end = start + source[start..].find("\n)\n").unwrap();
        let quoted: Vec<&str> = source[start..end].split('"').skip(1).step_by(2).collect();
        assert_eq!(safe_git_argv(), quoted);
    }

    #[test]
    fn filters_follow_the_old_checks() {
        let good = b"GIT_CONFIG_COUNT=4\0GIT_CONFIG_KEY_0=filter.a.clean\0GIT_CONFIG_VALUE_0=\0GIT_CONFIG_KEY_1=filter.a.smudge\0GIT_CONFIG_VALUE_1=\0GIT_CONFIG_KEY_2=filter.a.process\0GIT_CONFIG_VALUE_2=\0GIT_CONFIG_KEY_3=filter.a.required\0GIT_CONFIG_VALUE_3=false\0";
        let filters = parse_filters(good).ok().unwrap();
        assert_eq!(filters.semantic.len(), 4);
        let message = |raw: &[u8]| match parse_filters(raw) {
            Err(Exc::Failure(text, 20)) => text
                .iter()
                .map(|c| char::from_u32(*c).unwrap())
                .collect::<String>(),
            _ => "ok".to_string(),
        };
        assert_eq!(message(b"x"), "ERROR [precheck] 出力が NUL 終端でない");
        assert_eq!(message(b"GIT_CONFIG_COUNT=00\0"), "ok");
        assert_eq!(
            message(b"GIT_CONFIG_COUNT=-1\0"),
            "ERROR [precheck] COUNT が不正"
        );
        assert_eq!(
            filter_key("filter.a.b.clean"),
            Some(("a.b".to_string(), "clean"))
        );
        assert_eq!(filter_key("filter..clean"), None);
    }
}
