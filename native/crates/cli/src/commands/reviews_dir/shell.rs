//! 旧`reviews-dir.sh`を動かす旧bash(5.2.21。2026-11-12 00:35の利用者決定の基準)の振る舞いのうち、
//! このスクリプトが使う部分: 環境の取り込みと子へ渡す環境・外部コマンドの探索と起動・リダイレクト・
//! パイプライン・コマンド置換・`set -eEuo pipefail`とERR・EXITの罠・組込みの書込みの失敗・
//! EXITの罠を置いた非対話のbashの信号の扱い・子が信号で終わったときの知らせ。
//!
//! bash自身の行は`LC_ALL=C`の英語の文面(2026-11-11 22:31の利用者決定)。旧bashの起動を変える環境変数
//! (`BASH_ENV`・`BASH_FUNC_<名>%%`・`SHELLOPTS`・`BASHOPTS`・`POSIXLY_CORRECT`)の効果は再現しない。

use super::super::r4g_support::bash;
use super::lines;
use dev_workflow_platform::r4g::fs::{self, WriteError};
use dev_workflow_platform::r4g::process::{
    self, BashCommand, BashExecFailure, BashExecRules, BashLookup, BashSignals, BashSpawnError,
    Child, Status, StdTarget,
};
use dev_workflow_platform::r4g::{Errno, OFlags};
use std::ffi::CString;
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::raw::c_int;

/// 旧bashの終わり方。EXITの罠を動かしてから、終了値か信号で終える。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    Code(u8),
    Signal(c_int),
}

/// 子の標準入力。
pub enum In {
    Inherit,
    /// `<path`
    File(Vec<u8>),
    Pipe(OwnedFd),
}

/// 子の標準出力か標準エラー。
pub enum Out {
    Inherit,
    /// `>path`(作るか空にする)
    File(Vec<u8>),
    /// `>/dev/null`
    Null,
    /// `>&2`
    ToStderr,
    /// `2>&1`
    ToStdout,
    Pipe(OwnedFd),
}

/// 外部コマンド1つ。
pub struct Cmd {
    pub argv: Vec<Vec<u8>>,
    /// 見つからない・起動できない・リダイレクトの失敗の診断に出す行。
    pub line: u32,
    pub stdin: In,
    pub stdout: Out,
    pub stderr: Out,
    /// 子が信号で終わったときの知らせの字面(旧bashの構文木から作り直した文)。
    pub text: &'static str,
}

impl Cmd {
    pub fn new(argv: &[&[u8]], line: u32, text: &'static str) -> Self {
        Self {
            argv: argv.iter().map(|arg| arg.to_vec()).collect(),
            line,
            stdin: In::Inherit,
            stdout: Out::Inherit,
            stderr: Out::Inherit,
            text,
        }
    }
}

/// 起動した(か、起動の前に終わった)子1つ。
enum Running {
    Started(Child, &'static str),
    /// 起動しなかった子の終了値(見つからない・リダイレクトの失敗・起動の失敗)。
    Done(u8),
}

/// 子の終わり方(終了値と、信号で終わったか)。
#[derive(Clone, Copy, Debug)]
pub struct Finished {
    pub status: u8,
    signal: Option<(c_int, bool)>,
    pid: i32,
    text: &'static str,
}

/// 旧bash 5.2.21の`BASH_VERSION`。
const BASH_VERSION: &[u8] = b"5.2.21(1)-release";

/// 環境から取り込まない(非対話のbashが消す・自分で決める)変数。
const DROPPED: &[&[u8]] = &[
    b"PS1",
    b"PS2",
    b"PPID",
    b"BASHPID",
    b"RANDOM",
    b"SRANDOM",
    b"BASH_VERSINFO",
    b"BASH_ARGV0",
    b"COMP_WORDBREAKS",
    b"BASH_SUBSHELL",
    b"BASH_COMMAND",
    b"HISTCMD",
];

/// 旧bashの`legal_number`(前後の空白を許す十進)。
fn legal_number(text: &[u8]) -> Option<i64> {
    let trimmed = text
        .iter()
        .position(|byte| !matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r'))
        .map_or(&text[text.len()..], |start| &text[start..]);
    let end = trimmed
        .iter()
        .rposition(|byte| !matches!(byte, b' ' | b'\t'))
        .map_or(0, |index| index + 1);
    let body = std::str::from_utf8(&trimmed[..end]).ok()?;
    let digits = body.strip_prefix(['+', '-']).unwrap_or(body);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    body.parse::<i64>().ok()
}

fn same_file(a: &[u8], b: &[u8]) -> bool {
    let (Ok(a), Ok(b)) = (CString::new(a), CString::new(b)) else {
        return false;
    };
    match (fs::stat_at(None, &a, true), fs::stat_at(None, &b, true)) {
        (Ok(left), Ok(right)) => (left.dev, left.ino) == (right.dev, right.ino),
        _ => false,
    }
}

fn is_directory(path: &[u8]) -> bool {
    CString::new(path)
        .ok()
        .and_then(|path| fs::stat_at(None, &path, true).ok())
        .is_some_and(|stat| stat.is_dir())
}

/// 旧bashの`sh_canonpath`(`.`と`..`と重なった`/`を畳む。`..`の前はディレクトリであること)。
fn canonical_path(path: &[u8]) -> Option<Vec<u8>> {
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|byte| *byte == b'/') {
        match part {
            b"" | b"." => {}
            b".." => {
                let prefix: Vec<u8> = parts
                    .iter()
                    .flat_map(|p| [b"/".as_slice(), p].concat())
                    .collect();
                if !prefix.is_empty() && !is_directory(&prefix) {
                    return None;
                }
                parts.pop();
            }
            other => parts.push(other),
        }
    }
    let result: Vec<u8> = if parts.is_empty() {
        b"/".to_vec()
    } else {
        parts
            .iter()
            .flat_map(|p| [b"/".as_slice(), p].concat())
            .collect()
    };
    let exists = CString::new(result.clone())
        .ok()
        .is_some_and(|path| fs::stat_at(None, &path, true).is_ok());
    exists.then_some(result)
}

/// 旧bashの子の終わり方を、終了値に直す(`128 + 信号`)。
fn status_of(status: Status) -> u8 {
    match status {
        Status::Exited(code) => code,
        Status::Signaled(signal) => (128 + signal) as u8,
    }
}

/// glibcの`strsignal`の英語の文(旧bashが子の終わり方を知らせる行に出す)。
pub fn strsignal(signal: c_int) -> String {
    #[cfg(target_os = "linux")]
    let names: &[&str] = &[
        "Unknown signal 0",
        "Hangup",
        "Interrupt",
        "Quit",
        "Illegal instruction",
        "Trace/breakpoint trap",
        "Aborted",
        "Bus error",
        "Floating point exception",
        "Killed",
        "User defined signal 1",
        "Segmentation fault",
        "User defined signal 2",
        "Broken pipe",
        "Alarm clock",
        "Terminated",
        "Stack fault",
        "Child exited",
        "Continued",
        "Stopped (signal)",
        "Stopped",
        "Stopped (tty input)",
        "Stopped (tty output)",
        "Urgent I/O condition",
        "CPU time limit exceeded",
        "File size limit exceeded",
        "Virtual timer expired",
        "Profiling timer expired",
        "Window changed",
        "I/O possible",
        "Power failure",
        "Bad system call",
    ];
    #[cfg(not(target_os = "linux"))]
    let names: &[&str] = &[
        "Signal 0",
        "Hangup",
        "Interrupt",
        "Quit",
        "Illegal instruction",
        "Trace/BPT trap",
        "Abort trap",
        "EMT trap",
        "Floating point exception",
        "Killed",
        "Bus error",
        "Segmentation fault",
        "Bad system call",
        "Broken pipe",
        "Alarm clock",
        "Terminated",
        "Urgent I/O condition",
        "Suspended (signal)",
        "Suspended",
        "Continued",
        "Child exited",
        "Stopped (tty input)",
        "Stopped (tty output)",
        "I/O possible",
        "Cputime limit exceeded",
        "Filesize limit exceeded",
        "Virtual timer expired",
        "Profiling timer expired",
        "Window size changes",
        "Information request",
        "User defined signal 1",
        "User defined signal 2",
    ];
    match usize::try_from(signal)
        .ok()
        .and_then(|index| names.get(index))
    {
        Some(name) => (*name).to_string(),
        #[cfg(target_os = "linux")]
        None if (34..=64).contains(&signal) => format!("Real-time signal {}", signal - 34),
        None => format!("Unknown signal {signal}"),
    }
}

/// 名前と値の並び。
type Variables = Vec<(Vec<u8>, Vec<u8>)>;

/// 旧bashの状態。
pub struct Shell {
    /// 旧スクリプトの呼出名(`$0`)。診断の先頭と使い方の`sed`の引数に使う。
    pub script: Vec<u8>,
    /// 子へ渡す変数(取り込んだ順。名前の正しくない項目もそのまま渡す)。
    vars: Vec<(Vec<u8>, Vec<u8>)>,
    /// `PATH`(探索に使う値。無ければ旧bashの既定)。
    path: Vec<u8>,
    signals: BashSignals,
    rules: BashExecRules,
    /// `$TMP`(EXITの罠が消す一時ファイル)。
    pub tmp: Vec<u8>,
    /// ERRの罠が置かれているか。
    err_trap: bool,
    /// 今動いている最上位のコマンドの字面(EXITの罠の中の子の知らせに出る)。
    pub current: &'static str,
    /// 旧bashが最初に作った子の環境の、動く変数(`LINENO`・`EPOCHSECONDS`・`EPOCHREALTIME`)の値。
    /// 旧bashは子の環境を、書き出す変数が変わるまで作り直さないので、最初に親で作った値が残る。
    frozen: std::cell::RefCell<Option<Variables>>,
    /// 今起動する子がパイプラインかコマンド置換の中か(その子で作った環境は、親の控えにならない)。
    in_child_shell: std::cell::Cell<bool>,
    /// 終わらせる信号で待つのを止めた子(EXITの罠の中で回収できたら知らせる)。
    pending: std::cell::RefCell<Vec<(Child, &'static str)>>,
    /// コマンド置換のサブシェル(forkした子)の中か。EXITの罠を持たず、`set -e`を受け継がず、
    /// 子の終わり方を知らせない。終わらせる信号では、`SIGHUP`なら仕事の群へ送ってから終える。
    subshell: bool,
    /// 旧bashの`hash`の表(最上位で探して見つけた道具の場所。2回目からは探し直さない)。
    hash: std::cell::RefCell<Vec<(Vec<u8>, Vec<u8>)>>,
}

/// 旧bashの`strtol(text, &end, 10)`(前の空白・符号・十進。全体を読めたときだけ)を`int`へ切った値。
fn strtol_int(text: &[u8]) -> Option<i32> {
    let mut index = 0;
    while index < text.len() && matches!(text[index], b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r') {
        index += 1;
    }
    let mut negative = false;
    if index < text.len() && (text[index] == b'+' || text[index] == b'-') {
        negative = text[index] == b'-';
        index += 1;
    }
    let start = index;
    let mut value: i128 = 0;
    while index < text.len() && text[index].is_ascii_digit() {
        value = (value * 10 + i128::from(text[index] - b'0')).min(i128::from(i64::MAX) + 1);
        index += 1;
    }
    if index == start || index != text.len() {
        return None;
    }
    let value = if negative { -value } else { value };
    let long = value.clamp(i128::from(i64::MIN), i128::from(i64::MAX)) as i64;
    Some(long as i32)
}

/// 旧bashの`sv_shcompat`が受ける値(`D.D`か`DD`で31〜52)。
fn compat_ok(value: &[u8]) -> bool {
    let digit = |index: usize| value.get(index).is_some_and(u8::is_ascii_digit);
    let level = if value.len() == 3 && digit(0) && value[1] == b'.' && digit(2) {
        (value[0] - b'0') * 10 + (value[2] - b'0')
    } else if value.len() == 2 && digit(0) && digit(1) {
        (value[0] - b'0') * 10 + (value[1] - b'0')
    } else {
        return false;
    };
    (31..=52).contains(&level)
}

/// 旧bashの`PATH`の既定(環境に`PATH`が無いとき。子へは渡さない)。
const DEFAULT_PATH: &[u8] = b"/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:.";

impl Shell {
    /// 起動: 環境の取り込み(警告を含む)・信号の設定。閉じていた標準のfdは、標準ライブラリが開いた
    /// `/dev/null`のまま使う(2026-11-12 08:55の決定)。
    pub fn start(script: Vec<u8>) -> Self {
        let signals = match BashSignals::capture_and_prepare() {
            Ok(signals) => signals,
            Err(_) => std::process::exit(70),
        };
        let mut warnings = Vec::new();
        let mut vars: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
        let entries = process::environ_entries();
        for entry in &entries {
            let Some(index) = entry.iter().position(|byte| *byte == b'=') else {
                continue;
            };
            if index == 0 {
                continue;
            }
            let (name, value) = (entry[..index].to_vec(), entry[index + 1..].to_vec());
            match vars.iter_mut().find(|(key, _)| *key == name) {
                Some(slot) => slot.1 = value,
                None => vars.push((name, value)),
            }
        }
        vars.retain(|(name, _)| !DROPPED.contains(&name.as_slice()) && name != b"CDPATH");
        let current = |vars: &Vec<(Vec<u8>, Vec<u8>)>, name: &[u8]| {
            vars.iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
        };
        for (name, value) in vars.iter_mut() {
            match name.as_slice() {
                b"IFS" => *value = b" \t\n".to_vec(),
                b"OPTIND" | b"OPTERR" => *value = b"1".to_vec(),
                b"BASH_VERSION" => *value = BASH_VERSION.to_vec(),
                _ => {}
            }
        }
        let path = current(&vars, b"PATH").unwrap_or_else(|| DEFAULT_PATH.to_vec());
        if let Some(slot) = vars.iter_mut().find(|(name, _)| name == b"BASH") {
            slot.1 = match process::bash_find_command(b"bash", Some(&path), &mut |_| None) {
                BashLookup::Found(found) | BashLookup::NotExecutable(found) => found,
                BashLookup::NotFound => b"bash".to_vec(),
            };
        }
        // OLDPWD はディレクトリのときだけ残す。PWD は、環境の値が今の場所の絶対パスならそれを畳んだもの。
        if let Some(old) = current(&vars, b"OLDPWD")
            && !is_directory(&old)
        {
            vars.retain(|(name, _)| name != b"OLDPWD");
        }
        let pwd = current(&vars, b"PWD")
            .filter(|value| value.starts_with(b"/") && same_file(value, b"."))
            .and_then(|value| canonical_path(&value))
            .or_else(|| fs::getcwd().ok());
        match (pwd, vars.iter_mut().find(|(name, _)| name == b"PWD")) {
            (Some(pwd), Some(slot)) => slot.1 = pwd,
            (Some(pwd), None) => vars.push((b"PWD".to_vec(), pwd)),
            (None, _) => vars.retain(|(name, _)| name != b"PWD"),
        }
        // SHLVL: 1を足す。読めない値は0とみなす。負は0、1000以上は警告して1。
        let old = current(&vars, b"SHLVL")
            .filter(|value| !value.is_empty())
            .and_then(|value| legal_number(&value))
            .unwrap_or(0);
        let mut level = (old as i32).wrapping_add(1);
        if level < 0 {
            level = 0;
        } else if level >= 1000 {
            warnings.push(
                format!("bash: warning: shell level ({level}) too high, resetting to 1\n")
                    .into_bytes(),
            );
            level = 1;
        }
        let level = level.to_string().into_bytes();
        match vars.iter_mut().find(|(name, _)| name == b"SHLVL") {
            Some(slot) => slot.1 = level,
            None => vars.push((b"SHLVL".to_vec(), level)),
        }
        // 変数を取り込んだ後の起動の確かめ(旧bashの順): BASH_XTRACEFD・BASH_COMPAT・LC_ALL の setlocale。
        if let Some(value) = current(&vars, b"BASH_XTRACEFD").filter(|value| !value.is_empty()) {
            let suffix: Option<&str> = match strtol_int(&value) {
                Some(fd) if fs::valid_fd(fd) => {
                    (!fs::writable_fd(fd)).then_some("cannot open as FILE")
                }
                _ => Some("invalid value for trace file descriptor"),
            };
            if let Some(suffix) = suffix {
                let mut line = b"bash: BASH_XTRACEFD: ".to_vec();
                line.extend_from_slice(&value);
                line.extend_from_slice(format!(": {suffix}\n").as_bytes());
                warnings.push(line);
            }
        }
        if let Some(value) = current(&vars, b"BASH_COMPAT").filter(|value| !value.is_empty())
            && !compat_ok(&value)
        {
            let mut line = b"bash: BASH_COMPAT: ".to_vec();
            line.extend_from_slice(&value);
            line.extend_from_slice(b": compatibility value out of range\n");
            warnings.push(line);
        }
        // libc の getenv は環境の最初の値を読む。旧bashの LC_ALL は取り込んだ最後の値。
        let first = |name: &[u8]| -> Option<Vec<u8>> {
            entries.iter().find_map(|entry| {
                (entry.len() > name.len() && entry.starts_with(name) && entry[name.len()] == b'=')
                    .then(|| entry[name.len() + 1..].to_vec())
            })
        };
        let lc_all = current(&vars, b"LC_ALL").unwrap_or_default();
        if let Some(line) = super::locale::lc_all_warning(&first, &lc_all) {
            warnings.push(line);
        }
        match vars
            .iter_mut()
            .find(|(name, _)| name == b"GIT_NO_LAZY_FETCH")
        {
            Some(slot) => slot.1 = b"1".to_vec(),
            None => vars.push((b"GIT_NO_LAZY_FETCH".to_vec(), b"1".to_vec())),
        }
        for warning in warnings {
            raw_write(2, &warning);
        }
        // 旧bashは自分の SIGQUIT を無視する(子は起動時の扱いへ戻す)。
        let _ = process::bash_ignore_sigquit();
        let _ = process::install_bash_exit_trap_signals(&signals);
        Self {
            script,
            vars,
            path,
            signals,
            rules: BashExecRules::baseline(),
            tmp: Vec::new(),
            err_trap: true,
            current: "",
            frozen: std::cell::RefCell::new(None),
            in_child_shell: std::cell::Cell::new(false),
            pending: std::cell::RefCell::new(Vec::new()),
            subshell: false,
            hash: std::cell::RefCell::new(Vec::new()),
        }
    }

    /// 子の環境(`_`は起動するコマンドのパス。`LINENO`などは起動の時点の値)。
    fn child_env(&self, path: &[u8], line: u32) -> Vec<Vec<u8>> {
        let mut env = Vec::with_capacity(self.vars.len() + 1);
        let frozen = self.frozen.borrow().clone();
        let mut dynamic = Vec::new();
        for (name, value) in &self.vars {
            if name == b"_" {
                continue;
            }
            if let Some(found) = frozen
                .as_ref()
                .and_then(|values| values.iter().find(|(key, _)| key == name))
            {
                let mut entry = name.clone();
                entry.push(b'=');
                entry.extend_from_slice(&found.1);
                env.push(entry);
                continue;
            }
            let value = match name.as_slice() {
                b"LINENO" => line.to_string().into_bytes(),
                b"EPOCHSECONDS" | b"EPOCHREALTIME" => {
                    let now = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default();
                    if name == b"EPOCHSECONDS" {
                        now.as_secs().to_string().into_bytes()
                    } else {
                        format!("{}.{:06}", now.as_secs(), now.subsec_micros()).into_bytes()
                    }
                }
                _ => value.clone(),
            };
            if matches!(
                name.as_slice(),
                b"LINENO" | b"EPOCHSECONDS" | b"EPOCHREALTIME"
            ) {
                dynamic.push((name.clone(), value.clone()));
            }
            let mut entry = name.clone();
            entry.push(b'=');
            entry.extend_from_slice(&value);
            env.push(entry);
        }
        if frozen.is_none() && !self.in_child_shell.get() && !self.subshell {
            *self.frozen.borrow_mut() = Some(dynamic);
        }
        let mut underscore = b"_=".to_vec();
        underscore.extend_from_slice(path);
        env.push(underscore);
        env
    }

    // ------------------------------------------------------------ 旧スクリプトの変数(2026-11-12 14:24 の決定 4)

    /// 旧スクリプトの変数への代入。呼出元が同じ名前を export していれば、旧bashはその後に起動する子へ
    /// 代入した値を渡す(関数の`local`も同じ)。子の環境の値を置き換え、旧bashと同じく子の環境を作り直す
    /// (`array_needs_making`。動く変数の控えを捨てる)。export していない名前では、何も変えない。
    pub fn assign(&mut self, name: &[u8], value: &[u8]) {
        if let Some(slot) = self.vars.iter_mut().find(|(key, _)| key == name) {
            slot.1 = value.to_vec();
            *self.frozen.borrow_mut() = None;
        }
    }

    /// 関数の`local`で隠す前の値(関数から戻るときに[`Shell::restore`]へ渡す)。export していなければ`None`。
    pub fn saved(&self, name: &[u8]) -> Option<Vec<u8>> {
        self.vars
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    }

    /// 関数から戻るときに、`local`の前の値へ戻す。
    pub fn restore(&mut self, name: &[u8], saved: Option<Vec<u8>>) {
        if let Some(value) = saved {
            self.assign(name, &value);
        }
    }

    /// 配列の代入(`NAME=(…)`)。旧bashは配列を書き出さないので、呼出元が export した値は子の環境から消える。
    pub fn assign_array(&mut self, name: &[u8]) {
        let before = self.vars.len();
        self.vars.retain(|(key, _)| key != name);
        if self.vars.len() != before {
            *self.frozen.borrow_mut() = None;
        }
    }

    // ------------------------------------------------------------ 信号

    /// 旧bashの`CHECK_TERMSIG`: 受けた終わらせる信号があれば、罠を動かして終える経路へ。
    pub fn checkpoint(&self) -> Result<(), Exit> {
        match process::bash_terminating_signal() {
            Some(signal) => Err(terminating(signal)),
            None => Ok(()),
        }
    }

    // ------------------------------------------------------------ 組込みの書込み

    /// 組込みの`echo`・`printf`の1回の書込み(`fd`は1か2)。失敗はbashの書込みエラーの行と終了値1。
    /// 閉じたpipeで`SIGPIPE`を受けたら(起動時に既定だったとき)、行を出さずに信号で終える。
    pub fn builtin_write(
        &self,
        fd: i32,
        data: &[u8],
        line: u32,
        builtin: &str,
    ) -> Result<u8, Exit> {
        match write_all(fd, data) {
            Ok(()) => {
                self.checkpoint()?;
                Ok(0)
            }
            Err(errno) => {
                self.checkpoint()?;
                raw_write(
                    2,
                    &bash::write_error(&self.script, line, builtin, errno.raw_os_error()),
                );
                self.checkpoint()?;
                Ok(1)
            }
        }
    }

    /// `echo "<text>" >&2`。失敗は`set -e`で止まる。
    pub fn echo_err(&mut self, line: u32, text: &[u8]) -> Result<(), Exit> {
        let mut data = text.to_vec();
        data.push(b'\n');
        let status = self.builtin_write(2, &data, line, "echo")?;
        self.errexit(status, line)
    }

    /// `set -e`: 失敗ならERRの罠を動かして終える。罠の`echo`が失敗したら、罠の中の`set -e`で
    /// その終了値(1)のまま終える。
    pub fn errexit(&mut self, status: u8, line: u32) -> Result<(), Exit> {
        if status == 0 {
            return Ok(());
        }
        if !self.err_trap {
            return Err(Exit::Code(status));
        }
        // 旧bashは罠を動かす間 SIG_INPROGRESS を立て、罠の中の`set -e`や`exit`で抜けると下ろさない
        // (parse_and_execute が最上位へ飛ぶ)。罠は必ず`exit`か失敗で抜けるので、以後は二度と動かない
        // (EXITの罠の rm の失敗でも動かない)。
        self.err_trap = false;
        // 罠の`ec=$?`(EXITの罠の rm へ渡る)。
        self.assign(b"ec", status.to_string().as_bytes());
        let message = format!("ERROR [internal] 予期しない失敗(終了コード {status}・行 {line})\n");
        // 罠の中の LINENO は失敗したコマンドの行のまま。
        let written = self.builtin_write(2, message.as_bytes(), line, "echo")?;
        if written != 0 {
            return Err(Exit::Code(written));
        }
        Err(Exit::Code(20))
    }

    /// `trap - ERR`。
    pub fn clear_err_trap(&mut self) {
        self.err_trap = false;
    }

    // ------------------------------------------------------------ test

    pub fn test_symlink(&self, path: &[u8]) -> bool {
        CString::new(path)
            .ok()
            .and_then(|path| fs::stat_at(None, &path, false).ok())
            .is_some_and(|stat| stat.mode & 0o170000 == 0o120000)
    }

    pub fn test_exists(&self, path: &[u8]) -> bool {
        CString::new(path)
            .ok()
            .is_some_and(|path| fs::stat_at(None, &path, true).is_ok())
    }

    pub fn test_directory(&self, path: &[u8]) -> bool {
        is_directory(path)
    }

    pub fn test_regular(&self, path: &[u8]) -> bool {
        CString::new(path)
            .ok()
            .and_then(|path| fs::stat_at(None, &path, true).ok())
            .is_some_and(|stat| stat.is_regular())
    }

    /// `command -v <name> >/dev/null 2>&1`(実行できるファイル、無ければ最初に見つかったファイル)。
    pub fn command_v(&self, name: &[u8]) -> bool {
        !matches!(
            process::bash_find_command(name, Some(&self.path), &mut |_| None),
            BashLookup::NotFound
        )
    }

    // ------------------------------------------------------------ 外部コマンド

    /// リダイレクトを開く(旧bashは子の中で左から右へ開く)。失敗は`(path, errno)`。
    fn open_in(target: &In) -> Result<Option<OwnedFd>, (Vec<u8>, Errno)> {
        match target {
            In::File(path) => {
                let c = CString::new(path.clone()).map_err(|_| (path.clone(), Errno::NOENT))?;
                fs::open_at(None, &c, OFlags::RDONLY)
                    .map(Some)
                    .map_err(|errno| (path.clone(), errno))
            }
            _ => Ok(None),
        }
    }

    fn open_out(target: &Out) -> Result<Option<OwnedFd>, (Vec<u8>, Errno)> {
        let path: &[u8] = match target {
            Out::File(path) => path,
            Out::Null => b"/dev/null",
            _ => return Ok(None),
        };
        let c = CString::new(path).map_err(|_| (path.to_vec(), Errno::NOENT))?;
        fs::create_truncate(&c)
            .map(Some)
            .map_err(|errno| (path.to_vec(), errno))
    }

    /// 1つのコマンドを起動する(旧bashの`execute_disk_command`)。診断は子の標準エラーへ書く。
    fn spawn(&self, cmd: Cmd) -> Result<Running, Exit> {
        // 標準入力・標準出力・標準エラーの順に開く。標準エラーは、開いた後の標準出力を指せる。
        let input = match Self::open_in(&cmd.stdin) {
            Ok(fd) => fd,
            Err((path, errno)) => return Ok(self.virtual_failure(&cmd, None, &path, errno)),
        };
        let output = match Self::open_out(&cmd.stdout) {
            Ok(fd) => fd,
            Err((path, errno)) => return Ok(self.virtual_failure(&cmd, None, &path, errno)),
        };
        let stdout_target: Option<BorrowedFd<'_>> = match (&cmd.stdout, &output) {
            (_, Some(fd)) => Some(fd.as_fd()),
            (Out::Pipe(fd), None) => Some(fd.as_fd()),
            (Out::ToStderr, None) => Some(stderr_fd()),
            _ => None,
        };
        let error_fd = match Self::open_out(&cmd.stderr) {
            Ok(fd) => fd,
            Err((path, errno)) => {
                return Ok(self.virtual_failure(&cmd, stdout_target, &path, errno));
            }
        };
        let stderr_target: Option<BorrowedFd<'_>> = match (&cmd.stderr, &error_fd) {
            (_, Some(fd)) => Some(fd.as_fd()),
            (Out::Pipe(fd), None) => Some(fd.as_fd()),
            (Out::ToStdout, None) => stdout_target.or(Some(stdout_fd())),
            _ => None,
        };
        let stdin_target: Option<BorrowedFd<'_>> = match (&cmd.stdin, &input) {
            (_, Some(fd)) => Some(fd.as_fd()),
            (In::Pipe(fd), None) => Some(fd.as_fd()),
            _ => None,
        };
        let name = cmd.argv[0].clone();
        // 旧bashの hash: 最上位で探して見つけた場所を覚え、次からは探し直さない(消えていても使う)。
        let hashed = (!name.contains(&b'/'))
            .then(|| {
                self.hash
                    .borrow()
                    .iter()
                    .find(|(key, _)| *key == name)
                    .map(|(_, path)| path.clone())
            })
            .flatten();
        let path = match hashed {
            Some(path) => path,
            None => match process::bash_find_command(&name, Some(&self.path), &mut |_| None) {
                BashLookup::Found(path) | BashLookup::NotExecutable(path) => {
                    let top = !self.subshell && !self.in_child_shell.get();
                    let remember = !name.contains(&b'/')
                        && (path != name
                            || CString::new(path.clone())
                                .ok()
                                .is_some_and(|c| fs::x_ok(&c)));
                    if top && remember {
                        self.hash.borrow_mut().push((name.clone(), path.clone()));
                    }
                    path
                }
                BashLookup::NotFound => {
                    let message = bash::command_not_found(&self.script, cmd.line, &name);
                    return Ok(self.message_child(stderr_target, &message, 127, cmd.text));
                }
            },
        };
        let env = self.child_env(&path, cmd.line);
        let command = BashCommand {
            path: &path,
            argv: &cmd.argv,
            env: &env,
            stdio: [
                std_target(stdin_target),
                std_target(stdout_target),
                std_target(stderr_target),
            ],
            signals: &self.signals,
        };
        match process::bash_spawn(&command) {
            Ok(child) => Ok(Running::Started(child, cmd.text)),
            Err(BashSpawnError::Child(failure)) => {
                let failure = match process::bash_exec_failure(&path, failure.errno, self.rules) {
                    // 2026-11-11 23:19 の決定: 壊れた道具(shebang の無い文字のファイル)の差は前提の外。
                    // Rust はスクリプトとして実行せず、起動の失敗として扱う。
                    BashExecFailure::ShellScript => BashExecFailure::FileError {
                        errno: Errno::NOEXEC,
                        status: 126,
                    },
                    other => other,
                };
                if let Some(message) = bash::exec_failure(&self.script, cmd.line, &path, &failure) {
                    write_to(stderr_target, &message);
                }
                Ok(Running::Done(failure.status().unwrap_or(126)))
            }
            Err(BashSpawnError::EmbeddedNull) | Err(BashSpawnError::Os(_)) => {
                Ok(Running::Done(126))
            }
        }
    }

    /// 旧bashが子を作ってから診断を書く経路(見つからない・リダイレクトの失敗)。子を作り、子が診断を
    /// 書いて終える(親は普通の子と同じく待つ。待つ間に、ほかの終わった子も回収される)。
    fn message_child(
        &self,
        target: Option<BorrowedFd<'_>>,
        message: &[u8],
        code: u8,
        text: &'static str,
    ) -> Running {
        match process::bash_fork_subshell() {
            Ok(None) => {
                process::bash_subshell_reset(&self.signals);
                write_to(target, message);
                process::bash_exit_subshell(code)
            }
            Ok(Some(child)) => Running::Started(child, text),
            Err(_) => {
                write_to(target, message);
                Running::Done(code)
            }
        }
    }

    /// リダイレクトの失敗(`<script>: line N: <path>: <strerror>`と終了値1)。
    fn virtual_failure(
        &self,
        cmd: &Cmd,
        stdout: Option<BorrowedFd<'_>>,
        path: &[u8],
        errno: Errno,
    ) -> Running {
        let mut message = bash::prefix(&self.script, Some(cmd.line));
        message.extend_from_slice(path);
        message.extend_from_slice(b": ");
        message.extend_from_slice(
            super::super::r4g_support::strerror::strerror(errno.raw_os_error()).as_bytes(),
        );
        message.push(b'\n');
        let target = match cmd.stderr {
            Out::ToStdout => stdout,
            _ => None,
        };
        self.message_child(target, &message, 1, cmd.text)
    }

    /// 前景の子を待つ(旧bashの`wait_for`と`waitchld`)。子は`waitpid(-1)`で終わった順に回収する
    /// (待つのを止めた前の子〈`pending`〉が、この間に終われば、それも回収して覚える。ほかの子は捨てる)。
    /// 終わらせる信号で待つのを止めたら、子を残して終える。
    ///
    /// 待つ間に受けた`SIGINT`(旧bashの`wait_sigint_received`)は、`child_caught_sigint`で決める:
    /// `SIGINT`を受けた後に中断された回と、`SIGINT`以外で終わった子の回収で立ち、`SIGINT`で終わった子の
    /// 回収で下りる(最後に回収した子で決まる)。立っていなければ、自分も`SIGINT`で終える。
    fn wait(&self, running: Vec<Running>) -> Result<Vec<Finished>, Exit> {
        process::bash_set_waiting(true);
        let mut slots: Vec<(Option<Child>, Option<Status>, &'static str)> = running
            .into_iter()
            .map(|item| match item {
                Running::Done(status) => (None, Some(Status::Exited(status)), ""),
                Running::Started(child, text) => (Some(child), None, text),
            })
            .collect();
        let mut caught = false;
        while slots.iter().any(|(_, status, _)| status.is_none()) {
            match process::bash_wait_any() {
                Ok((pid, raw)) => {
                    if let Some(slot) = slots.iter_mut().find(|(child, status, _)| {
                        status.is_none() && child.as_ref().is_some_and(|child| child.pid() == pid)
                    }) {
                        let child = slot.0.as_mut().expect("started child");
                        child.record_reaped(pid, raw);
                        let status = child.status().expect("recorded status");
                        let by_sigint = status == Status::Signaled(libc_sigint());
                        if process::bash_wait_sigint_received() && !by_sigint {
                            caught = true;
                        }
                        if by_sigint {
                            caught = false;
                        }
                        slot.1 = Some(status);
                    } else if let Some((child, _)) = self
                        .pending
                        .borrow_mut()
                        .iter_mut()
                        .find(|(child, _)| child.pid() == pid)
                    {
                        child.record_reaped(pid, raw);
                    }
                }
                Err(Errno::INTR) => {
                    if let Some(signal) = process::bash_terminating_signal() {
                        process::bash_set_waiting(false);
                        // 旧bashは、EXITの罠の中の子を待つ間に、残した子も回収して知らせる。
                        let mut pending = self.pending.borrow_mut();
                        for (child, status, text) in slots {
                            if let (Some(child), None) = (child, status) {
                                pending.push((child, text));
                            }
                        }
                        return Err(terminating(signal));
                    }
                    if process::bash_wait_sigint_received() {
                        caught = true;
                    }
                }
                Err(_) => {
                    // 回収できる子が無い(起きない): 残りは失敗とする。
                    for slot in slots.iter_mut().filter(|slot| slot.1.is_none()) {
                        slot.1 = Some(Status::Exited(127));
                    }
                }
            }
        }
        let got_sigint = process::bash_set_waiting(false);
        let finished: Vec<Finished> = slots
            .into_iter()
            .map(|(child, status, text)| {
                let status = status.expect("all reaped");
                Finished {
                    status: status_of(status),
                    signal: match (status, &child) {
                        (Status::Signaled(signal), Some(child)) => {
                            Some((signal, child.core_dumped()))
                        }
                        _ => None,
                    },
                    pid: child.as_ref().map_or(0, Child::pid),
                    text,
                }
            })
            .collect();
        if got_sigint && !caught {
            return Err(terminating(libc_sigint()));
        }
        Ok(finished)
    }

    /// 前景の子が信号で終わったときの知らせ(非対話の旧bash。`SIGINT`・`SIGPIPE`は出さず、
    /// `SIGTERM`は語だけ、ほかは行番号・番号・文・コマンドの字面)。
    fn notify(&self, finished: &[Finished], line: u32) {
        let Some(first) = finished.first() else {
            return;
        };
        // 仕事の終わり方は、pipefail の右端の失敗(無ければ最後)。
        let job = finished
            .iter()
            .rev()
            .find(|item| item.status != 0)
            .unwrap_or(&finished[finished.len() - 1]);
        let Some((signal, core)) = job.signal else {
            return;
        };
        if signal == libc_sigint() || signal == libc_sigpipe() {
            return;
        }
        if signal == libc_sigterm() {
            let mut text = strsignal(signal);
            if core {
                text.push_str(" (core dumped)");
            }
            text.push('\n');
            raw_write(2, text.as_bytes());
            return;
        }
        let mut out = bash::prefix(&self.script, Some(line));
        for (index, item) in finished.iter().enumerate() {
            if index > 0 {
                out.extend_from_slice(b"     ");
            }
            out.extend_from_slice(format!("{:5} ", item.pid).as_bytes());
            let status = match item.signal {
                Some((signal, _)) => strsignal(signal),
                None if item.status == 0 => "Done".to_string(),
                None => format!("Exit {}", item.status),
            };
            let same = index > 0 && item.signal == first.signal && item.status == first.status;
            let shown = if same { String::new() } else { status };
            let width = if shown.is_empty() { 2 } else { shown.len() };
            out.extend_from_slice(shown.as_bytes());
            out.extend(std::iter::repeat_n(b' ', 24usize.saturating_sub(width)));
            if !shown.is_empty() && item.signal.is_some_and(|(_, core)| core) {
                out.extend_from_slice(b"(core dumped) ");
            }
            if index > 0 {
                out.extend_from_slice(b"| ");
            }
            out.extend_from_slice(item.text.as_bytes());
            out.push(b'\n');
        }
        raw_write(2, &out);
    }

    /// 最上位の1つのコマンドを動かし、終了値を返す(知らせは`job_line`の行で出す)。
    pub fn run(&mut self, cmd: Cmd, job_line: u32) -> Result<u8, Exit> {
        let running = self.spawn(cmd)?;
        let finished = self.wait(vec![running])?;
        self.notify(&finished, job_line);
        self.checkpoint()?;
        Ok(finished[0].status)
    }

    /// 最上位のパイプライン(`pipefail`)。知らせは`job_line`の行で出す。
    pub fn run_pipeline(
        &mut self,
        cmds: Vec<Cmd>,
        job_line: u32,
        notify: bool,
    ) -> Result<u8, Exit> {
        let finished = self.pipeline(cmds)?;
        if notify {
            self.notify(&finished, job_line);
        }
        self.checkpoint()?;
        Ok(finished
            .iter()
            .rev()
            .find(|item| item.status != 0)
            .map_or(0, |item| item.status))
    }

    /// パイプラインを起動して待つ。
    fn pipeline(&mut self, cmds: Vec<Cmd>) -> Result<Vec<Finished>, Exit> {
        let running = self.spawn_pipeline(cmds, None)?;
        self.wait(running)
    }

    /// パイプラインを起動する。最後の標準出力を`last_stdout`へ向けられる(子へ渡した後、親の側は閉じる)。
    fn spawn_pipeline(
        &mut self,
        cmds: Vec<Cmd>,
        last_stdout: Option<OwnedFd>,
    ) -> Result<Vec<Running>, Exit> {
        let count = cmds.len();
        let mut running = Vec::new();
        let mut next_input: Option<OwnedFd> = None;
        let mut last_stdout = last_stdout;
        self.in_child_shell.set(count > 1 || last_stdout.is_some());
        for (index, mut cmd) in cmds.into_iter().enumerate() {
            if let Some(fd) = next_input.take() {
                cmd.stdin = In::Pipe(fd);
            }
            if index + 1 < count {
                let (reader, writer) = process::pipe().map_err(|_| Exit::Code(126))?;
                if matches!(cmd.stdout, Out::Inherit) {
                    cmd.stdout = Out::Pipe(writer);
                }
                next_input = Some(reader);
            } else if let Some(fd) = last_stdout.take()
                && matches!(cmd.stdout, Out::Inherit)
            {
                cmd.stdout = Out::Pipe(fd);
            }
            running.push(self.spawn(cmd)?);
        }
        self.in_child_shell.set(false);
        Ok(running)
    }

    /// コマンド置換(`$(...)`)。旧bashと同じく、サブシェル(forkした子)が`cmds`を動かし、親はpipeを
    /// 読み終えてから子を待つ。読む間は`SIGINT`を止め、読み終えたら戻す(止めていた間の`SIGINT`はここで
    /// 受け、値を変数へ入れる前に終える)。最初のNULで警告し、NULは捨てる。末尾の改行を除く。
    /// 戻り値の終了値はサブシェルの終了値(信号なら128+番号)。
    ///
    /// サブシェルは`set -e`を受け継がない。`err_trap_line`があれば(ERRの罠を受け継ぐ文脈)、失敗で
    /// ERRの罠の行を書いて20で終える。子の終わり方は知らせない。親が先に終わっても続く。
    pub fn command_substitution(
        &mut self,
        cmds: Vec<Cmd>,
        line: u32,
        merge_stderr: bool,
        err_trap_line: Option<u32>,
    ) -> Result<(Vec<u8>, u8), Exit> {
        let (reader, writer) = process::bash_pipe().map_err(|_| Exit::Code(126))?;
        let mut cmds = cmds;
        if merge_stderr && let Some(last) = cmds.last_mut() {
            last.stderr = Out::ToStdout;
        }
        let mut child = match process::bash_fork_subshell() {
            Ok(Some(child)) => child,
            Ok(None) => {
                drop(reader);
                self.run_subshell(cmds, writer, err_trap_line)
            }
            Err(_) => return Err(Exit::Code(126)),
        };
        drop(writer);
        let mask = process::bash_block_sigint().ok();
        let mut data = Vec::new();
        let mut warned = false;
        let outcome = loop {
            match fs::read_checked(reader.as_fd(), 65536, &mut || {
                process::bash_terminating_signal().is_some()
            }) {
                Ok(part) if part.is_empty() => break Ok(()),
                Ok(part) => {
                    if !warned && part.contains(&0) {
                        warned = true;
                        let mut warning = bash::prefix(&self.script, Some(line));
                        warning.extend_from_slice(
                            b"warning: command substitution: ignored null byte in input\n",
                        );
                        raw_write(2, &warning);
                    }
                    data.extend(part.into_iter().filter(|byte| *byte != 0));
                }
                Err(WriteError::Interrupted) => {
                    if let Some(signal) = process::bash_terminating_signal() {
                        break Err(terminating(signal));
                    }
                }
                Err(WriteError::Os(_)) => break Ok(()),
            }
        };
        drop(reader);
        if let Some(mask) = mask {
            mask.restore();
        }
        outcome?;
        // 旧bashの wait_for(pid, JWAIT_NOTERM): 終わらせる信号でも、子が終わるまで待つ。
        let status = loop {
            match child.wait() {
                Ok(status) => break status,
                Err(Errno::INTR) => continue,
                Err(_) => break Status::Exited(127),
            }
        };
        if status == Status::Signaled(libc_sigint()) {
            process::bash_raise_terminating(libc_sigint());
        }
        self.checkpoint()?;
        while data.last() == Some(&b'\n') {
            data.pop();
        }
        Ok((data, status_of(status)))
    }

    /// コマンド置換のサブシェル(forkした子の側)。戻らない。
    fn run_subshell(&mut self, cmds: Vec<Cmd>, writer: OwnedFd, err_trap_line: Option<u32>) -> ! {
        process::bash_subshell_reset(&self.signals);
        if fs::duplicate_to(writer.as_fd(), 1).is_err() {
            process::bash_exit_subshell(1);
        }
        // 標準の番号(0〜2)は、標準ライブラリが開いたままなので、pipe の端は 3 以上(複製の後は閉じる)。
        drop(writer);
        self.subshell = true;
        self.tmp.clear();
        let outcome = self.spawn_pipeline(cmds, None).and_then(|running| {
            let finished = self.wait(running)?;
            // 子を待った後の CHECK_TERMSIG(受けた終わらせる信号は、ERRの罠より先に処理する)。
            self.checkpoint()?;
            Ok(finished
                .iter()
                .rev()
                .find(|item| item.status != 0)
                .map_or(0, |item| item.status))
        });
        let status = match outcome {
            Ok(status) => status,
            Err(Exit::Code(code)) => code,
            Err(Exit::Signal(signal)) => self.subshell_terminate(signal),
        };
        let status = match err_trap_line {
            Some(trap_line) if status != 0 => {
                // ERRの罠(set -E で受け継ぐ)。サブシェルは set -e を持たないので、echo が失敗しても 20。
                let message = format!(
                    "ERROR [internal] 予期しない失敗(終了コード {status}・行 {trap_line})\n"
                );
                if let Err(Exit::Signal(signal)) =
                    self.builtin_write(2, message.as_bytes(), trap_line, "echo")
                {
                    self.subshell_terminate(signal);
                }
                20
            }
            _ => status,
        };
        process::bash_exit_subshell(status)
    }

    /// サブシェルの`termsig_handler`: `SIGHUP`なら仕事の群へ送り(`hangup_all_jobs`)、EXITの罠は無く、
    /// 自分を信号で終える。
    fn subshell_terminate(&self, signal: c_int) -> ! {
        process::bash_begin_termsig();
        if signal == libc_sighup() {
            process::bash_hangup_group();
        }
        process::bash_kill_shell(signal)
    }

    /// EXITの罠(`cleanup`)を動かし、終える。罠の中の失敗は`set -e`でその終了値になる。
    pub fn finish(mut self, outcome: Result<(), Exit>) -> ! {
        let exit = outcome.err().unwrap_or(Exit::Code(0));
        let mut code = match exit {
            Exit::Code(code) => code,
            Exit::Signal(_) => 0,
        };
        if let Exit::Signal(_) = exit {
            // 旧bashの termsig_handler: この後に来る終わらせる信号は、直ちにシェルを終える。
            process::bash_begin_termsig();
        }
        if !self.tmp.is_empty() {
            let tmp = std::mem::take(&mut self.tmp);
            let text = self.current;
            let mut cmd = Cmd::new(&[b"rm", b"-f", b"--", &tmp], lines::line("cleanup"), "");
            cmd.text = text;
            let waited = self.spawn(cmd).and_then(|running| self.wait(vec![running]));
            if let Err(Exit::Signal(signal)) = waited
                && let Exit::Code(_) = exit
            {
                // 普通の終わりのEXITの罠の中で受けた終わらせる信号: 罠は2回動かさず、信号で終える。
                process::bash_kill_shell(signal);
            }
            if let Ok(finished) = waited {
                // 残した子は、EXITの罠の子を待つ間(`waitpid(-1)`)に終わって回収したものだけを知らせる
                // (旧bashの waitchld。罠の子の後に終わった子は回収せず、知らせない)。
                let pending = std::mem::take(&mut *self.pending.borrow_mut());
                for (child, text) in pending {
                    if let Some(status @ Status::Signaled(number)) = child.status() {
                        self.notify(
                            &[Finished {
                                status: status_of(status),
                                signal: Some((number, child.core_dumped())),
                                pid: child.pid(),
                                text,
                            }],
                            lines::line("cleanup"),
                        );
                    }
                }
                self.notify(&finished, lines::line("cleanup"));
                if finished[0].status != 0 {
                    code = finished[0].status;
                    if self.err_trap {
                        // 罠の中の rm の失敗に、残っている ERR の罠が動く(信号の経路では終わり方を変えない)。
                        let status = finished[0].status;
                        let message = format!(
                            "ERROR [internal] 予期しない失敗(終了コード {status}・行 {})\n",
                            lines::line("cleanup")
                        );
                        self.err_trap = false;
                        self.assign(b"ec", status.to_string().as_bytes());
                        let written = self.builtin_write(
                            2,
                            message.as_bytes(),
                            lines::line("cleanup"),
                            "echo",
                        );
                        // 罠の echo が失敗したら、罠の中の`set -e`で抜け、EXITの罠はその終了値(1)を
                        // 返す(旧bashの run_exit_trap が ERREXIT を受ける)。
                        code = match written {
                            Ok(0) | Err(_) => 20,
                            Ok(status) => status,
                        };
                    }
                }
            }
        }
        match exit {
            Exit::Signal(signal) => process::bash_kill_shell(signal),
            Exit::Code(_) => std::process::exit(i32::from(code)),
        }
    }
}

/// 旧bashの`CHECK_TERMSIG`から`termsig_handler`へ入る所: 処理中の印を立てる(この後の終わらせる信号は、
/// 信号の関数の中で直ちに終える)。
fn terminating(signal: c_int) -> Exit {
    process::bash_begin_termsig();
    Exit::Signal(signal)
}

fn libc_sigint() -> c_int {
    2
}

fn libc_sigpipe() -> c_int {
    13
}

fn libc_sigterm() -> c_int {
    15
}

fn libc_sighup() -> c_int {
    1
}

fn stdout_fd() -> BorrowedFd<'static> {
    fs::standard(1)
}

fn stderr_fd() -> BorrowedFd<'static> {
    fs::standard(2)
}

fn std_target(fd: Option<BorrowedFd<'_>>) -> StdTarget<'_> {
    fd.map_or(StdTarget::Inherit, StdTarget::Fd)
}

/// 番号1か2へ全部を書く。部分書込みは続けて書く。`EINTR`は、終わらせる信号を受けていれば止める。
fn write_all(fd: i32, data: &[u8]) -> Result<(), Errno> {
    let target = if fd == 1 { stdout_fd() } else { stderr_fd() };
    let mut rest = data;
    while !rest.is_empty() {
        match fs::write(target, rest, &mut || {
            process::bash_terminating_signal().is_some()
        }) {
            Ok(count) => rest = &rest[count..],
            Err(WriteError::Os(errno)) => return Err(errno),
            Err(WriteError::Interrupted) => return Err(Errno::INTR),
        }
    }
    Ok(())
}

/// bash自身の行を書く(失敗は表に出さない)。
pub fn raw_write(fd: i32, data: &[u8]) {
    let _ = write_all(fd, data);
}

/// 子の標準エラーの向き先へ書く(`None`は標準エラー)。
fn write_to(target: Option<BorrowedFd<'_>>, data: &[u8]) {
    match target {
        None => raw_write(2, data),
        Some(fd) => {
            let mut rest = data;
            while !rest.is_empty() {
                match fs::write(fd, rest, &mut || false) {
                    Ok(0) | Err(_) => return,
                    Ok(count) => rest = &rest[count..],
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 旧bashの BASH_XTRACEFD の strtol(前の空白・符号・全体・long から int への切り詰め)。
    #[test]
    fn xtracefd_values_are_read_like_strtol() {
        assert_eq!(strtol_int(b"5"), Some(5));
        assert_eq!(strtol_int(b" \t5"), Some(5));
        assert_eq!(strtol_int(b"+5"), Some(5));
        assert_eq!(strtol_int(b"-1"), Some(-1));
        assert_eq!(strtol_int(b"4294967298"), Some(2));
        assert_eq!(strtol_int(b"99999999999999999999"), Some(-1));
        for bad in [&b""[..], b" ", b"5x", b"x", b"5 ", b"+"] {
            assert_eq!(strtol_int(bad), None, "{bad:?}");
        }
    }

    /// 旧bashの sv_shcompat が受ける値(`D.D`か`DD`で31〜52)。
    #[test]
    fn compat_values_follow_sv_shcompat() {
        for good in [&b"31"[..], b"3.1", b"50", b"5.2", b"52"] {
            assert!(compat_ok(good), "{good:?}");
        }
        for bad in [
            &b"30"[..],
            b"3.0",
            b"53",
            b"99",
            b"bogus",
            b"5",
            b"5.20",
            b" 50",
            b"5,0",
        ] {
            assert!(!compat_ok(bad), "{bad:?}");
        }
    }
}
