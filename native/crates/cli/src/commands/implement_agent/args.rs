//! 引数(319〜332 行)・使い方(`usage` 308 行・`need_val` 310〜317 行)・値の検査(336〜365 行)と、
//! 352 行の空白だけかの確かめ(`grep` の起動を含む)。
use super::childenv::ScriptValues;
use super::shell::{Exit, Flow, Point, Shell, write_piece};
use super::sites::{self, Site};
use super::stdio::Writer;
use super::{Context, launch};
use dev_workflow_platform::implement_agent as os;
use std::ffi::OsString;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;

/// 使い方の文(旧版の 2 行目から `# --- end usage ---` の前の行まで。バイト列そのもの)。
pub const USAGE: &[u8] = include_bytes!("usage.txt");

/// 検査した引数(旧版の変数の値)。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Args {
    pub runner: Vec<u8>,
    pub model: Vec<u8>,
    pub prompt_file: Vec<u8>,
    pub cwd: Vec<u8>,
    pub log_file: Vec<u8>,
    pub probe_timeout: Vec<u8>,
    pub run_timeout: Vec<u8>,
    pub dry_run: bool,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            runner: Vec::new(),
            model: Vec::new(),
            prompt_file: Vec::new(),
            cwd: Vec::new(),
            log_file: Vec::new(),
            probe_timeout: b"60".to_vec(),
            run_timeout: b"1800".to_vec(),
            dry_run: false,
        }
    }
}

impl Args {
    /// 子を起動する時点のスクリプトの変数(352 行の時点)。
    pub fn script_values(&self) -> ScriptValues {
        let mut values = ScriptValues::default();
        values.set("RUNNER", &self.runner);
        values.set("MODEL", &self.model);
        values.set("PROMPT_FILE", &self.prompt_file);
        values.set("CWD", &self.cwd);
        values.set("LOG_FILE", &self.log_file);
        values.set("PROBE_TIMEOUT", &self.probe_timeout);
        values.set("RUN_TIMEOUT", &self.run_timeout);
        values.set("HELP_TIMEOUT", b"20");
        values.set("KILL_GRACE", b"5");
        values.set("MAX_PROMPT_BYTES", b"100000");
        values.set("DRY_RUN", if self.dry_run { b"1" } else { b"0" });
        for name in [
            "STATE_DIR",
            "CHILD_PID",
            "RAW_OUT",
            "PROBE_OUT",
            "PROBE_ERR",
            "DISP",
            "PR_VAL",
            "PR_SRC",
        ] {
            values.set(name, b"");
        }
        for name in [
            "MODEL_INFO_LOGGED",
            "LOG_FD_OPEN",
            "LOG_SWAP_NOTED",
            "LOG_EF_OK",
        ] {
            values.set(name, b"0");
        }
        values
    }
}

/// 引数の解析(319〜332 行)。argv はプログラム名を除いたもの。
pub fn parse(shell: &mut Shell, argv: &[OsString]) -> Flow<Args> {
    let words: Vec<&[u8]> = argv.iter().map(|a| a.as_bytes()).collect();
    let mut args = Args::default();
    let mut index = 0usize;
    while let Some(&word) = words.get(index) {
        let remaining = words.len() - index;
        let slot: Option<&mut Vec<u8>> = match word {
            b"--runner" => Some(&mut args.runner),
            b"--prompt-file" => Some(&mut args.prompt_file),
            b"--cwd" => Some(&mut args.cwd),
            b"--model" => Some(&mut args.model),
            b"--log-file" => Some(&mut args.log_file),
            b"--probe-timeout" => Some(&mut args.probe_timeout),
            b"--run-timeout" => Some(&mut args.run_timeout),
            b"--dry-run" => {
                args.dry_run = true;
                index += 1;
                continue;
            }
            _ => None,
        };
        let Some(slot) = slot else {
            let mut text = "ERROR [usage] 不明な引数: ".as_bytes().to_vec();
            text.extend_from_slice(word);
            return Err(usage_error(shell, sites::UNKNOWN_ECHO, &text));
        };
        if remaining < 2 {
            let mut text = b"ERROR [usage] ".to_vec();
            text.extend_from_slice(word);
            text.extend_from_slice(" に値が必要です".as_bytes());
            return Err(usage_error(shell, sites::NEED_VAL_ECHO, &text));
        }
        *slot = words.get(index + 1).copied().unwrap_or_default().to_vec();
        index += 2;
    }
    Ok(args)
}

/// `echo "<文>" >&2; usage; trap - ERR; exit 2`(`need_val` と不明な引数)。
fn usage_error(shell: &mut Shell, echo: Site, line: &[u8]) -> Exit {
    if let Err(exit) = shell.echo_err(echo, line) {
        return exit;
    }
    if let Err(exit) = usage(shell) {
        return exit;
    }
    shell.err_trap = false;
    shell.exit(2)
}

/// `usage` 308 行: `sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2`。2 本目の `sed` の書き方
/// (GNU sed 4.9 は stdout を溜めて書く。溜め場所は `st_blksize` と 8192 の小さい方)を再現する。
/// `sed` は bash の罠の信号を受けないので、`EINTR` は書き直す。失敗の終了値は、`sed` の
/// `couldn't write` の 4(引き継いだ `SIGPIPE`・`SIGXFSZ` が既定なら、書き込みで信号を受けて終わる)。
/// pipefail なので、パイプラインの終了値は 2 本目の `sed` の値。
pub fn usage(shell: &mut Shell) -> Flow<()> {
    // パイプラインの 2 つの子を作った後に、ジョブの cwd を決める。
    shell.job_working_directory();
    let status = write_usage(shell);
    shell.after(sites::USAGE, status == 0, status)?;
    Ok(())
}

fn write_usage(shell: &mut Shell) -> i32 {
    let capacity = match shell.io.blksize(2) {
        Some(size) if size > 0 && size < 8192 => size as usize,
        _ => 8192,
    };
    let mut errno = 0;
    let mut writer = Writer {
        io: shell.io.as_mut(),
        errno: &mut errno,
    };
    // sed は行と改行を 1 回ずつ fwrite する。満杯になった所で溜め場所の全部を書く。
    let mut buffered = 0usize;
    let mut written = 0usize;
    let mut failed_items = None;
    for line in USAGE.split_inclusive(|b| *b == b'\n') {
        let body = line.strip_suffix(b"\n").unwrap_or(line);
        for item in [body, line.get(body.len()..).unwrap_or_default()] {
            if item.is_empty() {
                continue;
            }
            let mut left = item.len();
            while buffered + left > capacity {
                left -= capacity - buffered;
                buffered = capacity;
                let chunk = USAGE.get(written..written + buffered).unwrap_or_default();
                if !write_retrying(&mut writer, chunk) {
                    failed_items = Some(item.len());
                    break;
                }
                written += buffered;
                buffered = 0;
            }
            if failed_items.is_some() {
                break;
            }
            buffered += left;
        }
        if failed_items.is_some() {
            break;
        }
    }
    if failed_items.is_none() && buffered > 0 {
        let chunk = USAGE.get(written..written + buffered).unwrap_or_default();
        if !write_retrying(&mut writer, chunk) {
            failed_items = Some(0);
        }
    }
    let Some(items) = failed_items else {
        return 0;
    };
    let errno = *writer.errno;
    if let Some(signal) = shell.child_died() {
        return 128 + signal;
    }
    let mut message = if items == 0 {
        b"sed: couldn't flush stdout: ".to_vec()
    } else {
        format!(
            "sed: couldn't write {items} item{} to stdout: ",
            if items == 1 { "" } else { "s" }
        )
        .into_bytes()
    };
    message.extend_from_slice(super::strerror(errno).as_bytes());
    message.push(b'\n');
    let mut ignored = 0;
    let mut quiet = Writer {
        io: shell.io.as_mut(),
        errno: &mut ignored,
    };
    let _ = write_piece(&mut quiet, 2, &message);
    let _ = shell.child_died();
    4
}

/// `sed` の書き込み(`EINTR` は書き直す)。
fn write_retrying(writer: &mut Writer<'_>, data: &[u8]) -> bool {
    let mut done = 0usize;
    while done < data.len() {
        match writer.io.write(2, data.get(done..).unwrap_or_default()) {
            Ok(n) => done += n,
            Err(error) if error.raw_os_error() == Some(os::errno::EINTR) => continue,
            Err(error) => {
                *writer.errno = error.raw_os_error().unwrap_or(os::errno::EIO);
                return false;
            }
        }
    }
    true
}

/// bash の `case` の `*[!<集合>]*`(1 バイトでも集合の外があるか)。
fn outside(value: &[u8], allowed: &dyn Fn(u8) -> bool) -> bool {
    value.iter().any(|b| !allowed(*b))
}

fn quoted(prefix: &str, value: &[u8]) -> Vec<u8> {
    let mut text = prefix.as_bytes().to_vec();
    text.push(b'\'');
    text.extend_from_slice(value);
    text.push(b'\'');
    text
}

fn with_value(prefix: &str, value: &[u8]) -> Vec<u8> {
    let mut text = prefix.as_bytes().to_vec();
    text.extend_from_slice(value);
    text
}

/// 値の検査(336〜365 行)。
pub fn check(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    let args = context.args.clone();
    // 336〜339 行。
    if args.runner.is_empty() {
        return Err(shell.fail_usage("--runner が必要です".as_bytes()));
    }
    if outside(&args.runner, &|b| {
        b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-')
    }) {
        return Err(shell.fail_usage(&quoted(
            "--runner は英数字・ドット・ハイフン・アンダースコアのみ: ",
            &args.runner,
        )));
    }
    // 341〜346 行。
    if !args.model.is_empty() {
        if args.model.first() == Some(&b'-') {
            return Err(shell.fail_usage(&quoted(
                "--model に '-' で始まる値は使えない(フラグへの化けを防ぐ): ",
                &args.model,
            )));
        }
        if outside(&args.model, &|b| {
            b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'@' | b'/' | b'+' | b'-')
        }) {
            return Err(shell.fail_usage(&quoted(
                "--model に使える文字は英数字と . _ : @ / + - のみ: ",
                &args.model,
            )));
        }
    }
    // 347〜351 行。
    if args.prompt_file.is_empty() {
        return Err(shell.fail_usage("--prompt-file が必要です".as_bytes()));
    }
    if !super::paths::is_file(&args.prompt_file) {
        return Err(shell.fail_usage(&with_value("プロンプトファイルが無い: ", &args.prompt_file)));
    }
    if !super::paths::non_empty(&args.prompt_file) {
        return Err(shell.fail_usage(&with_value("プロンプトファイルが空: ", &args.prompt_file)));
    }
    // 352〜354 行。
    if !has_non_space(shell, context)? {
        return Err(shell.fail_usage(&with_value(
            "プロンプトファイルが空白文字だけでできている: ",
            &args.prompt_file,
        )));
    }
    // 356〜360 行。
    if args.cwd.is_empty() {
        return Err(shell.fail_usage(
            "--cwd が必要です(実装経路では省略できない。書き込み範囲を呼び出し側が明示する)"
                .as_bytes(),
        ));
    }
    if !super::paths::is_dir(&args.cwd) {
        return Err(shell.fail_usage(&with_value("--cwd が存在しない: ", &args.cwd)));
    }
    if !super::paths::executable(&args.cwd) {
        return Err(shell.fail_usage(&with_value(
            "--cwd に検索(実行)権限が無くて移動できない: ",
            &args.cwd,
        )));
    }
    // 362〜365 行。
    for value in [&args.probe_timeout, &args.run_timeout] {
        if value.is_empty() || outside(value, &|b| b.is_ascii_digit()) {
            let name = if std::ptr::eq(value, &args.probe_timeout) {
                "--probe-timeout"
            } else {
                "--run-timeout"
            };
            return Err(shell.fail_usage(format!("{name} は正の秒数").as_bytes()));
        }
    }
    for (value, name, site) in [
        (
            &args.probe_timeout,
            "--probe-timeout",
            sites::PROBE_TIMEOUT_TEST,
        ),
        (&args.run_timeout, "--run-timeout", sites::RUN_TIMEOUT_TEST),
    ] {
        let positive = match super::childenv::legal_number(value) {
            Some(number) => number > 0,
            None => {
                // `[: <値>: integer expression expected` と終了値 2。
                let mut text = value.to_vec();
                text.extend_from_slice(b": integer expression expected");
                shell.builtin_error(site.line, b"[", &text);
                false
            }
        };
        shell.checkpoint()?;
        if !positive {
            return Err(shell.fail_usage(
                format!("{name} に 0 は指定できない(無制限/即 kill で意味が反転する)").as_bytes(),
            ));
        }
    }
    Ok(())
}

/// 352 行: `LC_ALL=C grep -q '[^[:space:]]' "$PROMPT_FILE" 2>/dev/null` が一致したか。
/// パスが `-` で始まれば GNU `grep` を起動し(決定の表の (1))、そうでなければ Rust がファイルを読む。
/// bash は子を作った直後に一時的な `LC_ALL=C` を戻し(354 行の setlocale の警告)、次にジョブの
/// cwd を決め(`job-working-directory` の文)、それから子を待つ(基準の容器で測った順)。
fn has_non_space(shell: &mut Shell, context: &mut Context) -> Flow<bool> {
    let path = context.args.prompt_file.clone();
    let launched = path.first() == Some(&b'-');
    let child = if launched {
        let argv = vec![
            b"grep".to_vec(),
            b"-q".to_vec(),
            b"[^[:space:]]".to_vec(),
            path.clone(),
        ];
        Some(launch::start(
            shell,
            context,
            &launch::Command {
                name: b"grep",
                argv: &argv,
                line: sites::GREP.line,
                temporary: &[("LC_ALL", b"C")],
                stdout: os::ChildOut::Inherit,
                stderr: os::ChildErr::DevNull,
            },
        ))
    } else {
        None
    };
    after_fork(shell, context)?;
    let matched = match child {
        Some(started) => {
            let status = launch::wait(shell, started, launch::Kind::Foreground)?;
            launch::notify(shell, sites::GREP_END.line, &status, GREP_TEXT);
            shell.after_foreground(status.signal() == Some(os::SIGINT))?;
            status.code() == Some(0)
        }
        None => {
            let matched = file_has_non_space(&path);
            shell.stop_point(Point::Grep, false)?;
            matched
        }
    };
    Ok(matched)
}

/// `p->command` の字面(ジョブの知らせの行の最後)。
pub const GREP_TEXT: &[u8] = b"LC_ALL=C grep -q '[^[:space:]]' \"$PROMPT_FILE\" 2> /dev/null";

/// 子を作った後の bash の動き(一時的な代入を戻す・ジョブの cwd を決める)。
fn after_fork(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    if let Some(value) = context.startup_lc_all.clone()
        && let Some((text, errno)) = context.bash_locale.set_lc_all(&value)
    {
        let mut text = text;
        if errno != 0 {
            text.extend_from_slice(b": ");
            text.extend_from_slice(super::strerror(errno).as_bytes());
        }
        shell.internal_warning(sites::GREP_END.line, &text);
    }
    shell.job_working_directory();
    Ok(())
}

/// ファイルに、NUL と C の地域設定の空白 6 文字のほかのバイトがあるか(GNU grep は NUL を行の区切りとして
/// 扱う。読めなければ一致しない)。
pub fn file_has_non_space(path: &[u8]) -> bool {
    let Ok(mut file) = std::fs::File::open(std::path::Path::new(std::ffi::OsStr::from_bytes(path)))
    else {
        return false;
    };
    let mut buffer = vec![0u8; 65536];
    loop {
        match file.read(&mut buffer) {
            Ok(0) => return false,
            Ok(n) => {
                if buffer
                    .get(..n)
                    .unwrap_or_default()
                    .iter()
                    .any(|b| !matches!(b, 0 | b' ' | b'\t' | b'\n' | 11 | 12 | b'\r'))
                {
                    return true;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return false,
        }
    }
}
