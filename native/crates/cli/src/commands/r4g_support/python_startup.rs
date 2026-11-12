//! 旧Python 3.10.11の起動の処理のうち、旧版どおり再現するもの。
//!
//! - `PYTHONUNBUFFERED`・`PYTHONINTMAXSTRDIGITS`(有効な値の効果と、不正な値の起動の失敗)。
//! - 不正な`PYTHONHASHSEED`の起動の失敗と`PYTHONOPTIMIZE`(2026-11-12 00:28の利用者決定。旧スクリプトは
//!   `assert`・`__debug__`・docstringの値を使わないので、`PYTHONOPTIMIZE`は出力を変えない)。
//! - 標準入出力の作り方(使えないfdは`None`、標準入力がディレクトリなら起動の失敗)。
//! - `LC_CTYPE`の強制(PEP 538)のうち、環境変数だけで決まる場合(2026-11-11 22:31の回答)。
//!
//! 起動を変えるほかの環境変数(`PYTHONPATH`など)の効果は再現しない(2026-11-11 22:31の利用者決定)。

use super::python_io::{Raw, Stream, StreamConfig, TextStream};

/// 環境変数の値を読む関数(旧Pythonの`_Py_GetEnv`と同じく、空の値は無いとみなす)。
fn get<'a>(env: &'a [(Vec<u8>, Vec<u8>)], name: &[u8]) -> Option<&'a [u8]> {
    env.iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_slice())
        .filter(|value| !value.is_empty())
}

/// Cの`strtol(text, &end, 10)`と`_Py_str_to_int`。前の空白と符号を受け、最後まで数字で、intに収まる値だけ。
fn str_to_int(text: &[u8]) -> Option<i32> {
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
    i32::try_from(value).ok()
}

/// `_Py_get_env_flag`: 整数として読めないか負なら1、ほかはその値。
fn env_flag(env: &[(Vec<u8>, Vec<u8>)], name: &[u8]) -> i32 {
    match get(env, name) {
        None => 0,
        Some(value) => match str_to_int(value) {
            Some(number) if number >= 0 => number,
            _ => 1,
        },
    }
}

/// Cの`strtoul(text, &end, 10)`が文字列の全体を読み、値が`0..=4294967295`か(旧Pythonの`config_init_hash_seed`)。
/// 前の空白と符号を受ける。`-`は符号なしへの変換で巨大な値になる(`-0`だけが0)。
fn hash_seed_is_valid(text: &[u8]) -> bool {
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
    let mut value: u128 = 0;
    while index < text.len() && text[index].is_ascii_digit() {
        value = (value * 10 + u128::from(text[index] - b'0')).min(u128::from(u64::MAX) + 1);
        index += 1;
    }
    if index == start || index != text.len() {
        return false;
    }
    if value > u128::from(u64::MAX) {
        return false;
    }
    let value = if negative && value != 0 {
        u128::from(u64::MAX) + 1 - value
    } else {
        value
    };
    value <= 4_294_967_295
}

/// 起動の時点で旧Pythonが止まる診断。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StartupFailure {
    /// `PYTHONHASHSEED`が`random`でも`0..=4294967295`の整数でもない。
    HashSeed,
    /// `PYTHONINTMAXSTRDIGITS`が640未満(0を除く)か、整数でない。
    IntMaxStrDigits,
    /// 標準入力がディレクトリ。
    StdinIsDirectory,
    /// 標準出力か標準エラーがディレクトリ(`IsADirectoryError`)。値はfd。
    StreamIsDirectory(i32),
}

impl StartupFailure {
    /// 旧Pythonの`Fatal Python error`の診断(標準エラーへ直接書く)と、終了値1。
    /// `Current thread`の値は実行ごとに変わり、比較では位置と形式だけを見る(2026-11-11の利用者決定)。
    pub fn diagnostic(&self, thread: usize) -> Vec<u8> {
        let current = format!(
            "\nCurrent thread 0x{thread:016x} (most recent call first):\n  <no Python frame>\n"
        );
        match self {
            Self::HashSeed => b"Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\nPython runtime state: preinitialized\n\n".to_vec(),
            Self::IntMaxStrDigits => b"Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\nPython runtime state: preinitialized\n\n".to_vec(),
            Self::StdinIsDirectory => format!(
                "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n{current}"
            )
            .into_bytes(),
            Self::StreamIsDirectory(fd) => format!(
                "Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] {}: {fd}\n{current}",
                super::strerror::strerror(21)
            )
            .into_bytes(),
        }
    }
}

/// 環境変数から決まる旧Pythonの設定。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PythonConfig {
    /// `PYTHONUNBUFFERED`が効いていない。
    pub buffered: bool,
    /// `int()`の桁数の上限(0は上限なし)。
    pub int_max_str_digits: u64,
}

/// `config_read_env_vars`(`PYTHONHASHSEED`を含む)と`config_init_int_max_str_digits`の順に読む。
pub fn config(env: &[(Vec<u8>, Vec<u8>)]) -> Result<PythonConfig, StartupFailure> {
    let buffered = env_flag(env, b"PYTHONUNBUFFERED") == 0;
    if let Some(seed) = get(env, b"PYTHONHASHSEED")
        && seed != b"random"
        && !hash_seed_is_valid(seed)
    {
        return Err(StartupFailure::HashSeed);
    }
    let int_max_str_digits = match get(env, b"PYTHONINTMAXSTRDIGITS") {
        None => super::pyint::DEFAULT_MAX_DIGITS,
        Some(value) => match str_to_int(value) {
            Some(0) => 0,
            Some(number) if number >= 640 => number as u64,
            _ => return Err(StartupFailure::IntMaxStrDigits),
        },
    };
    Ok(PythonConfig {
        buffered,
        int_max_str_digits,
    })
}

/// 標準のfd 1本の起動の時点の状態(`main`の後の`fstat`から作る)。閉じていたfdは、Rustの標準ライブラリが
/// `/dev/null`で開いたまま使う(2026-11-12 08:55の決定)ので、旧Pythonの`sys.stdin`などが`None`になる場合は無い。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StandardState {
    pub directory: bool,
    pub terminal: bool,
    pub blksize: i128,
}

/// 今のfd 0〜2の`fstat`・`isatty`から、旧Pythonが見る標準の状態を作る。
pub fn standard_states() -> [StandardState; 3] {
    use dev_workflow_platform::r4g::fs;
    use std::os::fd::AsFd;
    let (stdin, stdout, stderr) = (std::io::stdin(), std::io::stdout(), std::io::stderr());
    [stdin.as_fd(), stdout.as_fd(), stderr.as_fd()].map(|fd| {
        let stat = fs::fstat(fd).ok();
        StandardState {
            directory: stat.is_some_and(|stat| stat.is_dir()),
            terminal: fs::isatty(fd),
            blksize: stat.map_or(0, |stat| stat.blksize),
        }
    })
}

/// `init_sys_streams`: 標準入力がディレクトリなら止める。標準出力と標準エラーを作る。
pub fn stdio(
    config: PythonConfig,
    states: [StandardState; 3],
    raws: [Box<dyn Raw>; 2],
) -> Result<super::python_io::PythonStdio, StartupFailure> {
    if states[0].directory {
        return Err(StartupFailure::StdinIsDirectory);
    }
    let [out_raw, err_raw] = raws;
    let make = |index: usize, stream: Stream, raw: Box<dyn Raw>| {
        let state = states[index];
        if state.directory {
            return Err(StartupFailure::StreamIsDirectory(index as i32));
        }
        Ok(TextStream::new(
            stream,
            raw,
            StreamConfig {
                buffered: config.buffered,
                terminal: state.terminal,
                blksize: state.blksize,
            },
        ))
    };
    let stdout = make(1, Stream::Stdout, out_raw)?;
    let stderr = make(2, Stream::Stderr, err_raw)?;
    Ok(super::python_io::PythonStdio { stdout, stderr })
}

/// `LC_CTYPE`の強制(PEP 538)の判定。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Coercion {
    /// 強制しない。`warn`なら、Cのままである警告を出す。
    None { warn: bool },
    /// `LC_CTYPE`を書き換える。`warn`なら強制の警告を出す。
    Coerce { warn: bool },
    /// 入っている地域設定で決まる(環境変数だけでは決まらない。ケースごとの固定期待にする)。
    DependsOnInstalledLocale,
}

/// 旧Pythonが強制で試す名前の順(`_TARGET_LOCALES`)。
const COERCION_TARGETS: [&str; 3] = ["C.UTF-8", "C.utf8", "UTF-8"];

/// 強制の後の`LC_CTYPE`の値。旧Pythonは`C.UTF-8`・`C.utf8`・`UTF-8`の順に`setlocale(LC_CTYPE, …)`を
/// 試し、最初に受け入れた名前にする。どれも受け入れなければ強制しない(`None`)。
///
/// - Linux: 基準の機械のglibc 2.39は`C.UTF-8`を内蔵するので、常に`C.UTF-8`。
/// - Mac: libcは`/usr/share/locale/<名前>/LC_CTYPE`を読めるときに受け入れるので、その有無で選ぶ
///   (macOS 15 に`C.UTF-8`が無ければ`UTF-8`になる。CI の macOS で旧版と比べて確かめる)。
pub fn coerced_locale() -> Option<&'static str> {
    if cfg!(target_os = "linux") {
        return Some(COERCION_TARGETS[0]);
    }
    COERCION_TARGETS.into_iter().find(|name| {
        std::path::Path::new("/usr/share/locale")
            .join(name)
            .join("LC_CTYPE")
            .is_file()
    })
}

/// 旧Pythonの`_Py_LegacyLocaleDetected`と`_Py_CoerceLegacyLocale`の、環境変数だけで決まる部分。
///
/// - `PYTHONCOERCECLOCALE=0`なら強制も警告もしない。
/// - `LC_ALL`が空でなければ強制しない。警告(`warn`のとき)は、その名前が`C`・`POSIX`なら出し、
///   ほかの名前では入っている地域設定で決まる(入っていなければCのままになり警告が出る)。
/// - `LC_ALL`が無ければ、`LC_CTYPE`・`LANG`の順に最初の空でない名前を見る。無いか`C`・`POSIX`なら強制する。
///   ほかの名前は、入っている地域設定で決まる(入っていなければCのままになり強制する)。
///
/// `POSIX`の扱いは機械で違う。旧Pythonは`setlocale(LC_CTYPE, NULL)`が`"C"`のときだけ強制し、警告する
/// (CPython 3.10.11 の`_Py_LegacyLocaleDetected`)。glibcは`POSIX`を`C`の別名にして`"C"`を返すが、Macの
/// libcは`"POSIX"`のまま返す(CPython の`test_c_locale_coercion`も、`POSIX`を`C`と同じに扱うのは Linux だけ。
/// bpo-30672)。そのため Mac の`POSIX`は、強制も警告もしない(どの機械にも入っている名前なので、入っている
/// 地域設定では決まらない)。
pub fn coercion(env: &[(Vec<u8>, Vec<u8>)]) -> Coercion {
    let setting = get(env, b"PYTHONCOERCECLOCALE");
    if setting == Some(b"0".as_slice()) {
        return Coercion::None { warn: false };
    }
    let warn = setting == Some(b"warn".as_slice());
    let linux = cfg!(target_os = "linux");
    let is_c = |name: &[u8]| name == b"C" || (linux && name == b"POSIX");
    let posix_kept = |name: &[u8]| !linux && name == b"POSIX";
    if let Some(all) = get(env, b"LC_ALL") {
        return match (warn, is_c(all)) {
            (false, _) => Coercion::None { warn: false },
            (true, true) => Coercion::None { warn: true },
            (true, false) if posix_kept(all) => Coercion::None { warn: false },
            (true, false) => Coercion::DependsOnInstalledLocale,
        };
    }
    match get(env, b"LC_CTYPE").or_else(|| get(env, b"LANG")) {
        Some(name) if posix_kept(name) => Coercion::None { warn: false },
        Some(name) if !is_c(name) => Coercion::DependsOnInstalledLocale,
        // 試す名前をどれも受け入れなければ、旧Pythonは強制せず、`warn`なら Cのままである警告を出す。
        _ if coerced_locale().is_none() => Coercion::None { warn },
        _ => Coercion::Coerce { warn },
    }
}

/// `warn`のときの警告の文(標準エラーへ直接書く)。強制の文には、受け入れた名前(`coerced_locale`)が入る。
pub fn coercion_warning(coercion: &Coercion) -> Option<String> {
    match coercion {
        Coercion::Coerce { warn: true } => Some(format!(
            "Python detected LC_CTYPE=C: LC_CTYPE coerced to {} (set another locale or PYTHONCOERCECLOCALE=0 to disable this locale coercion behavior).\n",
            coerced_locale().unwrap_or(COERCION_TARGETS[0])
        )),
        Coercion::None { warn: true } => Some(
            "Python runtime initialized with LC_CTYPE=C (a locale with default ASCII encoding), which may cause Unicode compatibility problems. Using C.UTF-8, C.utf8, or UTF-8 (if available) as alternative Unicode-compatible locales is recommended.\n".to_string(),
        ),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn env(pairs: &[(&str, &str)]) -> Vec<(Vec<u8>, Vec<u8>)> {
        pairs
            .iter()
            .map(|(key, value)| (key.as_bytes().to_vec(), value.as_bytes().to_vec()))
            .collect()
    }

    /// 旧Pythonを、指定の環境変数だけで起動する。
    fn python(pairs: &[(&str, &str)], code: &str) -> std::process::Output {
        let mut command = Command::new("python3");
        command.args(["-c", code]).env_clear();
        if let Some(path) = std::env::var_os("PATH") {
            command.env("PATH", path);
        }
        for (key, value) in pairs {
            command.env(key, value);
        }
        command.output().unwrap()
    }

    #[test]
    fn unbuffered_and_digit_limit_match_python() {
        for value in [
            "",
            "0",
            " 0",
            "1",
            "x",
            "-1",
            "0 ",
            "+1",
            "00",
            "2147483648",
        ] {
            let output = python(
                &[("PYTHONUNBUFFERED", value)],
                "import sys; print(sys.stdout.write_through)",
            );
            let expected = String::from_utf8(output.stdout).unwrap() == "True\n";
            let ours = !config(&env(&[("PYTHONUNBUFFERED", value)]))
                .unwrap()
                .buffered;
            assert_eq!(ours, expected, "{value:?}");
        }
        for value in [
            "",
            "0",
            "640",
            "639",
            "+700",
            " 700",
            "700 ",
            "-1",
            "abc",
            "99999999999",
            "4300",
        ] {
            let output = python(
                &[("PYTHONINTMAXSTRDIGITS", value)],
                "import sys; print(sys.get_int_max_str_digits())",
            );
            let ours = config(&env(&[("PYTHONINTMAXSTRDIGITS", value)]));
            match ours {
                Ok(config) => {
                    assert!(output.status.success(), "{value:?}");
                    assert_eq!(
                        String::from_utf8(output.stdout).unwrap(),
                        format!("{}\n", config.int_max_str_digits),
                        "{value:?}"
                    );
                }
                Err(failure) => {
                    assert_eq!(output.status.code(), Some(1), "{value:?}");
                    assert_eq!(output.stderr, failure.diagnostic(0), "{value:?}");
                }
            }
        }
    }

    #[test]
    fn hash_seed_failure_and_its_order_match_python() {
        for value in [
            "",
            "random",
            "0",
            "-0",
            "+5",
            " 12",
            "\t7",
            "4294967295",
            "4294967296",
            "-1",
            "12 ",
            "5\n",
            "abc",
            "0x10",
            "-",
            " ",
            "- 5",
            "18446744073709551616",
            "99999999999999999999999",
            "１２",
        ] {
            let output = python(&[("PYTHONHASHSEED", value)], "print('ok')");
            let ours = config(&env(&[("PYTHONHASHSEED", value)]));
            match ours {
                Ok(_) => assert_eq!(output.stdout, b"ok\n", "{value:?}"),
                Err(failure) => {
                    assert_eq!(failure, StartupFailure::HashSeed, "{value:?}");
                    assert_eq!(output.status.code(), Some(1), "{value:?}");
                    assert_eq!(output.stderr, failure.diagnostic(0), "{value:?}");
                }
            }
        }
        // 両方が不正なら、旧版と同じく PYTHONHASHSEED が先に止める。
        let both = [("PYTHONHASHSEED", "x"), ("PYTHONINTMAXSTRDIGITS", "x")];
        let output = python(&both, "print('ok')");
        assert_eq!(
            output.stderr,
            config(&env(&both)).unwrap_err().diagnostic(0)
        );
    }

    #[test]
    fn coercion_matches_python_when_the_environment_decides_it() {
        let cases: [&[(&str, &str)]; 14] = [
            &[],
            &[("PYTHONCOERCECLOCALE", "warn")],
            &[("PYTHONCOERCECLOCALE", "warn"), ("LC_ALL", "C")],
            &[("PYTHONCOERCECLOCALE", "warn"), ("LANG", "POSIX")],
            &[("PYTHONCOERCECLOCALE", "warn"), ("LC_ALL", "POSIX")],
            &[("PYTHONCOERCECLOCALE", "0")],
            &[("LC_ALL", "C")],
            &[("LANG", "C"), ("LC_CTYPE", "POSIX")],
            &[("PYTHONCOERCECLOCALE", "1"), ("LC_CTYPE", "C")],
            &[("PYTHONCOERCECLOCALE", "warn"), ("LC_ALL", "C.UTF-8")],
            // LC_ALL が C・POSIX でない名前なら、入っていてもいなくても書き換えず警告もしない。
            &[("LC_ALL", "C.UTF-8")],
            &[("LC_ALL", "xx_YY.UTF-8")],
            &[("LC_ALL", "xx_YY.UTF-8"), ("LC_CTYPE", "C")],
            &[("PYTHONCOERCECLOCALE", "0"), ("LC_ALL", "C")],
        ];
        // POSIX の組の期待: Linux は C と同じに扱い、Mac は強制も警告もしない(上の`coercion`の説明)。
        let linux = cfg!(target_os = "linux");
        assert_eq!(
            coercion(&env(&[("PYTHONCOERCECLOCALE", "warn"), ("LANG", "POSIX")])),
            if linux {
                Coercion::Coerce { warn: true }
            } else {
                Coercion::None { warn: false }
            }
        );
        assert_eq!(
            coercion(&env(&[
                ("PYTHONCOERCECLOCALE", "warn"),
                ("LC_ALL", "POSIX")
            ])),
            Coercion::None { warn: linux }
        );
        assert_eq!(
            coercion(&env(&[("LANG", "C"), ("LC_CTYPE", "POSIX")])),
            if linux {
                Coercion::Coerce { warn: false }
            } else {
                Coercion::None { warn: false }
            }
        );
        for pairs in cases {
            let output = python(pairs, "import os, sys; print(os.environ.get('LC_CTYPE'))");
            let decided = coercion(&env(pairs));
            let stdout = String::from_utf8(output.stdout).unwrap();
            let stderr = String::from_utf8(output.stderr).unwrap();
            let given = pairs
                .iter()
                .find(|(key, _)| *key == "LC_CTYPE")
                .map(|(_, value)| *value);
            match &decided {
                Coercion::Coerce { .. } => {
                    assert_eq!(
                        stdout,
                        format!("{}\n", coerced_locale().unwrap()),
                        "{pairs:?}"
                    )
                }
                Coercion::None { .. } => assert_eq!(
                    stdout,
                    format!("{}\n", given.unwrap_or("None")),
                    "{pairs:?}"
                ),
                Coercion::DependsOnInstalledLocale => {
                    assert_eq!(pairs.last().unwrap().1, "C.UTF-8", "{pairs:?}");
                    continue;
                }
            }
            assert_eq!(
                stderr,
                coercion_warning(&decided).unwrap_or_default(),
                "{pairs:?}"
            );
        }
    }

    #[test]
    fn stdin_and_stdout_directory_failures_match_python_except_the_thread_value() {
        let script = "import os, subprocess, sys\nfd = os.open('/', os.O_RDONLY)\nfor target in ('stdin', 'stdout'):\n    r = subprocess.run([sys.executable, '-c', 'print(1)'], stderr=subprocess.PIPE, **{target: fd})\n    sys.stdout.buffer.write(b'%d\\0' % r.returncode + r.stderr + b'\\0')";
        let output = Command::new("python3")
            .args(["-I", "-c", script])
            .output()
            .unwrap();
        assert!(output.status.success());
        let parts: Vec<&[u8]> = output.stdout.split(|byte| *byte == 0).collect();
        let normalize = |bytes: &[u8]| {
            let text = String::from_utf8(bytes.to_vec()).unwrap();
            let start = text.find("Current thread 0x").unwrap() + "Current thread 0x".len();
            assert!(
                text[start..start + 16]
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit())
            );
            format!("{}{}", &text[..start], &text[start + 16..])
        };
        assert_eq!(parts[0], b"1");
        assert_eq!(
            normalize(parts[1]),
            normalize(&StartupFailure::StdinIsDirectory.diagnostic(0x7f))
        );
        assert_eq!(parts[2], b"1");
        assert_eq!(
            normalize(parts[3]),
            normalize(&StartupFailure::StreamIsDirectory(1).diagnostic(0x7f))
        );
        let state = |directory| StandardState {
            directory,
            terminal: false,
            blksize: 4096,
        };
        let raws = || -> [Box<dyn Raw>; 2] {
            [
                Box::new(super::super::python_io::StandardRaw { fd: 1 }),
                Box::new(super::super::python_io::StandardRaw { fd: 2 }),
            ]
        };
        let config = config(&[]).unwrap();
        assert!(matches!(
            stdio(config, [state(true), state(false), state(false)], raws()),
            Err(StartupFailure::StdinIsDirectory)
        ));
        assert!(matches!(
            stdio(config, [state(false), state(true), state(false)], raws()),
            Err(StartupFailure::StreamIsDirectory(1))
        ));
        assert!(stdio(config, [state(false), state(false), state(false)], raws()).is_ok());
    }
}
