//! Git 設定の include の解析(旧版の `git_config_output`・`includes`)。
//!
//! - Git 自身に引用・継続行を解析させる。`include` の展開は止め(`--no-includes`)、各宛先は呼び出し側が
//!   リンクを辿らずに先に検査する。設定の解析は外部のコマンドを実行しない。
//! - 起動は `git <安全用のオプション> config --file - --no-includes --null --list` と、include の鍵ごとに 1 回の
//!   `… --type=path --get-all <鍵>`。標準入力へ設定の本文を書きながら標準出力を読む(複数の fd を同時に待つ)。
//! - 待ちは 0.1 秒ごとに `tick` し、全体で 3 秒・出力 8 MiB・項目 10,000 まで。
//! - 子の環境は、`GIT_CONFIG_KEY_*`・`GIT_CONFIG_VALUE_*`・`GIT_CONFIG_COUNT`・`GIT_CONFIG_PARAMETERS` を除き、
//!   `GIT_CONFIG_NOSYSTEM=1`・`GIT_CONFIG_GLOBAL`・`GIT_CONFIG_SYSTEM`(`/dev/null`)・`GIT_TERMINAL_PROMPT=0`・
//!   `GIT_NO_LAZY_FETCH=1` を足す。Mac の旧 Python が子の環境へ自分で足す `__CF_USER_TEXT_ENCODING` は再現しない。
use super::budget::{EnvBudget, MAX_ENTRIES, MAX_FILE};
use super::error::{PyException, from_io};
use super::fsio::{absolute, site};
use super::pyops::Cps;
use super::pypath::PyPath;
use super::pystr::decode_fs;
use super::specs::Host;
use dev_workflow_platform::environment_guard as os;
use std::collections::HashSet;
use std::io;
use std::os::fd::AsFd;
use std::time::{Duration, Instant};

/// 旧版の `SAFE_GIT_PREFIX`。
pub const SAFE_GIT_PREFIX: [&str; 26] = [
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.quotePath=false",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.ignoreCase=false",
    "-c",
    "core.splitIndex=false",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "push.gpgSign=false",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.clean=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
];

/// 解析 1 回の時間の上限。
pub const GIT_TIMEOUT: Duration = Duration::from_secs(3);
/// 1 回の待ちの長さ(`poll.select(min(left, .1))`)。
const POLL_SLICE: Duration = Duration::from_millis(100);
const CHUNK: usize = 65536;

/// 子の環境変数(名前と値の生バイト)。
pub type EnvList = Vec<(Vec<u8>, Vec<u8>)>;

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn os_failure(error: io::Error) -> PyException {
    // `EINTR` は、`SIGINT` が来ていれば `KeyboardInterrupt` にする。
    if error.raw_os_error() == Some(os::EINTR) && os::take_sigint() {
        return PyException::new("KeyboardInterrupt", "");
    }
    from_io(&error, None)
}

/// `dict(os.environ)`: 旧Python 3.10の`os.environ`と同じ作り方(`environ`の各項目を最初の`=`で分け、
/// 名前が空でもよく、`=`の無い項目は捨て、同じ名前は最初の値と位置を残す)。
pub fn process_environment() -> EnvList {
    os::process_environ()
}

/// `dict[key] = value`(あれば同じ位置で置き換え、無ければ末尾へ)。
fn set_env(list: &mut EnvList, key: &[u8], value: &[u8]) {
    match list.iter_mut().find(|(name, _)| name == key) {
        Some(slot) => slot.1 = value.to_vec(),
        None => list.push((key.to_vec(), value.to_vec())),
    }
}

/// `includes` が作る子の環境。
pub fn git_env(process: &[(Vec<u8>, Vec<u8>)]) -> EnvList {
    let mut env: EnvList = Vec::new();
    for (key, value) in process {
        set_env(&mut env, key, value);
    }
    for (key, value) in [
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("GIT_CONFIG_SYSTEM", "/dev/null"),
        ("GIT_TERMINAL_PROMPT", "0"),
        ("GIT_NO_LAZY_FETCH", "1"),
    ] {
        set_env(&mut env, key.as_bytes(), value.as_bytes());
    }
    env.retain(|(key, _)| {
        !(key.starts_with(b"GIT_CONFIG_KEY_")
            || key.starts_with(b"GIT_CONFIG_VALUE_")
            || key == b"GIT_CONFIG_COUNT"
            || key == b"GIT_CONFIG_PARAMETERS")
    });
    env
}

fn spawn_failure(error: &os::SpawnError, argv0: &[u8], cwd: &[u8]) -> PyException {
    match error {
        os::SpawnError::Os { errno, .. } => {
            let filename = error.filename(argv0, Some(cwd)).map(decode_fs);
            from_io(&io::Error::from_raw_os_error(*errno), filename.as_deref())
        }
        os::SpawnError::EmbeddedNul => PyException::value_error("embedded null byte"),
        os::SpawnError::EmptyArgv => PyException::new("IndexError", "list index out of range"),
        os::SpawnError::IllegalEnvName => {
            PyException::value_error("illegal environment variable name")
        }
    }
}

/// `git_config_output(argv, raw, env, budget)`: 設定の本文を標準入力へ送り、標準出力を返す。
pub fn git_config_output(
    argv: &[Vec<u8>],
    raw: &[u8],
    env: &[(Vec<u8>, Vec<u8>)],
    budget: &mut EnvBudget,
) -> Result<Vec<u8>, PyException> {
    git_config_output_within(argv, raw, env, budget, GIT_TIMEOUT)
}

/// 時間の上限を決められる形(単体テストが短い上限で時間切れを確かめる。製品は `GIT_TIMEOUT`)。
fn git_config_output_within(
    argv: &[Vec<u8>],
    raw: &[u8],
    env: &[(Vec<u8>, Vec<u8>)],
    budget: &mut EnvBudget,
    timeout: Duration,
) -> Result<Vec<u8>, PyException> {
    // stdin と stdout を並行して送受信し、展開後の出力も取得中に制限する。
    // stderr は値や巨大な診断を保持せず捨てる。失敗は固定の診断に変換する。
    let cwd: &[u8] = b"/";
    let mut child = os::spawn(&os::SpawnSpec {
        argv,
        env,
        cwd: Some(cwd),
        stdin: os::Stdio::Pipe,
        stdout: os::Stdio::Pipe,
        stderr: os::Stdio::DevNull,
    })
    .map_err(|error| spawn_failure(&error, argv.first().map_or(&[], Vec::as_slice), cwd))?;
    let deadline = Instant::now() + timeout;
    let result = exchange(&mut child, raw, budget, deadline);
    // 旧版の `finally`: パイプを閉じ、生きていれば kill し、回収する。ここで失敗すれば元の例外に代わる。
    match child.finish() {
        Ok(_) => result,
        Err(error) => Err(os_failure(error)),
    }
}

fn exchange(
    child: &mut os::Child,
    raw: &[u8],
    budget: &mut EnvBudget,
    deadline: Instant,
) -> Result<Vec<u8>, PyException> {
    let mut output: Vec<u8> = Vec::new();
    let mut pending = raw;
    for fd in [child.stdin.as_ref(), child.stdout.as_ref()]
        .into_iter()
        .flatten()
    {
        os::set_nonblocking(fd.as_fd()).map_err(os_failure)?;
    }
    if pending.is_empty() {
        child.close_stdin();
    }
    site("git-after-start");
    while child.stdin.is_some() || child.stdout.is_some() {
        budget.tick(0)?;
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(stop("Git 設定の解析が時間上限を超える"));
        }
        // `epoll.poll` は待ち時間をミリ秒へ切り上げる。
        let wait = left.min(POLL_SLICE);
        let wait_ms = i64::try_from(wait.as_micros().div_ceil(1000)).unwrap_or(i64::MAX);
        // 旧版は標準出力を先に登録する。
        let mut watched: Vec<(bool, os::Interest)> = Vec::new();
        let mut fds = Vec::new();
        if let Some(out) = &child.stdout {
            fds.push((
                out.as_fd(),
                os::Interest {
                    read: true,
                    write: false,
                },
            ));
            watched.push((false, fds[0].1));
        }
        if let Some(input) = &child.stdin {
            fds.push((
                input.as_fd(),
                os::Interest {
                    read: false,
                    write: true,
                },
            ));
            watched.push((true, fds[fds.len() - 1].1));
        }
        let ready = os::retry(|| os::poll_fds(&fds, wait_ms)).map_err(os_failure)?;
        drop(fds);
        for (index, (is_stdin, _)) in watched.iter().enumerate() {
            if !ready[index] {
                continue;
            }
            if *is_stdin {
                let Some(input) = &child.stdin else { continue };
                let chunk = &pending[..pending.len().min(CHUNK)];
                match os::retry(|| os::write(input.as_fd(), chunk)) {
                    Ok(size) => pending = &pending[size..],
                    // `BrokenPipeError`: 子が読むのをやめた。残りは送らない。
                    Err(error) if error.raw_os_error() == Some(os::EPIPE) => {
                        pending = &pending[pending.len()..];
                    }
                    Err(error) => return Err(os_failure(error)),
                }
                if pending.is_empty() {
                    child.close_stdin();
                }
            } else {
                let Some(out) = &child.stdout else { continue };
                let room = (MAX_FILE as usize + 1).saturating_sub(output.len());
                let mut buffer = vec![0u8; CHUNK.min(room)];
                let got = os::retry(|| os::read(out.as_fd(), &mut buffer)).map_err(os_failure)?;
                if got == 0 {
                    child.close_stdout();
                } else {
                    output.extend_from_slice(&buffer[..got]);
                    if output.len() as u64 > MAX_FILE {
                        return Err(stop("Git 設定の解析出力がバイト上限を超える"));
                    }
                }
            }
        }
    }
    let wait = deadline
        .saturating_duration_since(Instant::now())
        .max(Duration::from_millis(10));
    match child.wait_timeout(wait).map_err(os_failure)? {
        None => Err(stop("Git 設定の解析が時間上限を超える").without_context()),
        Some(0) => Ok(output),
        Some(_) => Err(stop("Git 設定を解析できない")),
    }
}

/// 旧版の `includes(path, raw, budget)`: 設定の本文の `include` の宛先(絶対パスの文字列)を、解析した順に返す。
/// `path` は設定ファイルのパス(相対の宛先の基準)。`process_env` は `os.environ`。
pub fn includes(
    host: &dyn Host,
    path: &PyPath,
    raw: &[u8],
    budget: Option<&mut EnvBudget>,
    process_env: &[(Vec<u8>, Vec<u8>)],
) -> Result<Vec<Cps>, PyException> {
    let mut fresh;
    let budget = match budget {
        Some(budget) => budget,
        None => {
            fresh = EnvBudget::new();
            &mut fresh
        }
    };
    // Git 自身に引用・継続行を解析させる。include 展開は止め、各宛先を nofollow で先に検査する。
    // config の解析は外部 command を実行しない。
    let env = git_env(process_env);
    let mut prefix: Vec<Vec<u8>> = vec![b"git".to_vec()];
    prefix.extend(SAFE_GIT_PREFIX.iter().map(|word| word.as_bytes().to_vec()));
    for word in ["config", "--file", "-", "--no-includes", "--null"] {
        prefix.push(word.as_bytes().to_vec());
    }
    let parse = |budget: &mut EnvBudget, args: &[&[u8]]| -> Result<Vec<u8>, PyException> {
        budget.tick(0)?;
        let mut argv = prefix.clone();
        argv.extend(args.iter().map(|word| word.to_vec()));
        git_config_output(&argv, raw, &env, budget)
    };
    let listing = parse(budget, &[b"--list"])?;
    let records: Vec<&[u8]> = listing.split(|&byte| byte == 0).collect();
    if records.len() as u64 > MAX_ENTRIES {
        return Err(stop("Git 設定の項目数が上限を超える"));
    }
    let mut result: Vec<Cps> = Vec::new();
    let mut queried: HashSet<Vec<u8>> = HashSet::new();
    for item in records {
        budget.tick(0)?;
        if item.is_empty() {
            continue;
        }
        let key = item.split(|&byte| byte == b'\n').next().unwrap_or(item);
        let is_include =
            key == b"include.path" || (key.starts_with(b"includeif.") && key.ends_with(b".path"));
        if !is_include {
            continue;
        }
        if !queried.insert(key.to_vec()) {
            continue;
        }
        // Git の path 型で ~ と %(prefix) も解決する。.. は OS が開く順を保ち、
        // symlink/../x を字面で潰して別のファイルを検査しない。
        let found = parse(budget, &[b"--type=path", b"--get-all", key])?;
        let values: Vec<&[u8]> = found.split(|&byte| byte == 0).collect();
        for value in &values[..values.len() - 1] {
            if value.is_empty() {
                return Err(stop("include.path が空"));
            }
            let child = PyPath::new(&decode_fs(value));
            let joined = if child.is_absolute() {
                child
            } else {
                path.parent().join(&child.to_cps())
            };
            result.push(absolute(host, &joined.to_cps())?.to_cps());
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::super::fsio::hooks;
    use super::super::observe_tests::Dir;
    use super::super::pystr::{cps, to_string};
    use super::super::pyvalue::PyDict;
    use super::super::record::{RecordOptions, record};
    use super::super::testing::{FakeHost, fixture};
    use super::*;
    use serde_json::{Value, json};
    use std::collections::HashMap;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    fn find_in_path(name: &str) -> String {
        let path = std::env::var("PATH").unwrap_or_default();
        path.split(':')
            .map(|dir| Path::new(dir).join(name))
            .find(|candidate| candidate.is_file())
            .unwrap_or_else(|| panic!("{name} が PATH に無い"))
            .to_str()
            .unwrap()
            .to_owned()
    }

    fn write_script(dir: &Path, name: &str, body: &str) -> String {
        let path = dir.join(name);
        std::fs::write(&path, body).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path.to_str().unwrap().to_owned()
    }

    fn pair(key: &str, value: &str) -> (Vec<u8>, Vec<u8>) {
        (key.as_bytes().to_vec(), value.as_bytes().to_vec())
    }

    fn message(error: &PyException) -> String {
        to_string(error.message.as_codepoints())
    }

    fn argv_of(script: &str) -> Vec<Vec<u8>> {
        vec![script.as_bytes().to_vec()]
    }

    fn sh_env() -> EnvList {
        vec![pair("PATH", "/usr/bin:/bin")]
    }

    fn host_with_home() -> FakeHost {
        FakeHost {
            env: HashMap::from([("HOME".to_owned(), cps("/fixture-home"))]),
            cwd: cps("/"),
        }
    }

    /// 記録用の偽の git(固定期待の `recorder`)を作る。呼び出しは `<out>/calls.jsonl` に 1 行ずつ残る。
    fn install_recorder(dir: &Path, template: &str) -> String {
        let bin = dir.join("bin");
        std::fs::create_dir(&bin).unwrap();
        let text = template
            .replace("@PYTHON@", &find_in_path("python3"))
            .replace("@OUT@", dir.to_str().unwrap())
            .replace("@GIT@", &find_in_path("git"));
        write_script(&bin, "git", &text);
        bin.to_str().unwrap().to_owned()
    }

    fn base_env(bin: &str) -> EnvList {
        vec![
            pair("PATH", &format!("{bin}:{}", std::env::var("PATH").unwrap())),
            pair("HOME", "/fixture-home"),
            pair("LANG", "C.UTF-8"),
        ]
    }

    fn read_calls(dir: &Path) -> Vec<Value> {
        let path = dir.join("calls.jsonl");
        if !path.exists() {
            return Vec::new();
        }
        std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    /// 記録用の偽の git(Python)が見た環境から、PATH を除いた環境。`sent` は Rust が子へ渡した環境(`git_env` の結果)。
    /// Mac では、記録用の Python が自分で足す変数(`__CF_USER_TEXT_ENCODING` など)があるので、Rust が渡していない名前は除いて比べる。
    /// 渡した名前の値と、固定期待にだけ有る名前の欠けは、Linux と同じく比べる。
    fn comparable_env(call: &Value, sent: &[(Vec<u8>, Vec<u8>)]) -> Value {
        let mut env = call["env"].as_object().unwrap().clone();
        env.remove("PATH");
        env.remove("__CF_USER_TEXT_ENCODING");
        if cfg!(target_os = "macos") {
            env.retain(|name, _| sent.iter().any(|(key, _)| key == name.as_bytes()));
        }
        Value::Object(env)
    }

    fn raw_of(case: &Value) -> Vec<u8> {
        if let Some(count) = case.get("many").and_then(Value::as_u64) {
            let mut text = String::from("[s]\n");
            for index in 0..count {
                text.push_str(&format!("\tk{index} = v\n"));
            }
            text.push_str(case["tail"].as_str().unwrap());
            return text.into_bytes();
        }
        if let Some(hex) = case.get("raw_hex").and_then(Value::as_str) {
            return (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
        }
        case["raw"].as_str().unwrap().as_bytes().to_vec()
    }

    #[test]
    fn includes_match_the_old_python_with_the_real_git() {
        let cases = fixture!("git_cases.json");
        let list = cases["cases"].as_array().unwrap();
        assert!(list.len() >= 15);
        let dir = Dir::new("git-cases");
        let root = dir.0.join("r");
        std::fs::create_dir_all(root.join("cfg")).unwrap();
        let bin = install_recorder(&dir.0, cases["recorder"].as_str().unwrap());
        let env = base_env(&bin);
        let root_text = root.to_str().unwrap().to_owned();
        let host = host_with_home();
        for case in list {
            let name = case["name"].as_str().unwrap();
            let _ = std::fs::remove_file(dir.0.join("calls.jsonl"));
            let raw = raw_of(case);
            let path = PyPath::new(&cps(&format!("{root_text}/cfg/config")));
            let result = includes(&host, &path, &raw, None, &env);
            let shown = match result {
                Ok(items) => json!({"ok": items.iter().map(|item| {
                    replace_root(item, &cps(&root_text)).into_iter().map(|c| json!(c)).collect::<Vec<_>>()
                }).collect::<Vec<_>>()}),
                Err(error) => json!({"error": {
                    "type": error.type_name,
                    "msg": message(&error).replace(&root_text, "{r}"),
                }}),
            };
            assert_eq!(&shown, &case["result"], "{name}: 結果が旧版と違う");
            let calls: Vec<Value> = read_calls(&dir.0)
                .iter()
                .map(|call| {
                    json!({"argv": call["argv"], "stdin": call["stdin"], "env": comparable_env(call, &git_env(&env))})
                })
                .collect();
            assert_eq!(
                &Value::Array(calls),
                &case["calls"],
                "{name}: git の呼び出しが旧版と違う"
            );
        }
    }

    /// 文字番号の列の中の一時フォルダ(`root`)を `{r}` にする(サロゲートの番号をそのまま残すため、文字列にしない)。
    fn replace_root(text: &[u32], root: &[u32]) -> Vec<u32> {
        let mut out = Vec::new();
        let mut index = 0;
        while index < text.len() {
            if text[index..].starts_with(root) {
                out.extend(cps("{r}"));
                index += root.len();
            } else {
                out.push(text[index]);
                index += 1;
            }
        }
        out
    }

    #[test]
    fn non_utf8_include_paths_keep_the_surrogate_escape_numbers() {
        let cases = fixture!("git_cases.json");
        let case = cases["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "non-utf8")
            .unwrap();
        // 旧版は、読めないバイトを `\udce9` のような番号にする。固定期待もその番号。
        let want = case["result"]["ok"][0].as_array().unwrap();
        assert!(
            want.contains(&json!(0xdce9)),
            "固定期待にサロゲートの番号が無い"
        );
    }

    #[test]
    fn the_safe_prefix_and_the_environment_follow_the_old_version() {
        let cases = fixture!("git_cases.json");
        let want: Vec<&str> = cases["safe_prefix"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(SAFE_GIT_PREFIX.to_vec(), want);
        // 子の環境: 除くもの・足すもの・上書きするものと、それ以外はそのまま。
        let dir = Dir::new("git-env");
        let bin = install_recorder(&dir.0, cases["recorder"].as_str().unwrap());
        let mut env = base_env(&bin);
        for (key, value) in cases["env_extra"].as_object().unwrap() {
            env.push(pair(key, value.as_str().unwrap()));
        }
        let case = &cases["env_case"];
        let root = dir.0.join("r");
        std::fs::create_dir_all(root.join("cfg")).unwrap();
        let path = PyPath::new(&cps(root.join("cfg/config").to_str().unwrap()));
        includes(
            &host_with_home(),
            &path,
            case["raw"].as_str().unwrap().as_bytes(),
            None,
            &env,
        )
        .unwrap();
        let calls: Vec<Value> = read_calls(&dir.0)
            .iter()
            .map(|call| json!({"argv": call["argv"], "env": comparable_env(call, &git_env(&env))}))
            .collect();
        assert_eq!(Value::Array(calls), case["calls"]);
    }

    #[test]
    fn git_env_keeps_the_position_of_an_overridden_name_and_appends_the_new_ones() {
        let process = vec![
            pair("A", "1"),
            pair("GIT_TERMINAL_PROMPT", "1"),
            pair("GIT_CONFIG_COUNT", "1"),
            pair("GIT_CONFIG_KEY_0", "x"),
            pair("GIT_CONFIG_VALUE_0", "y"),
            pair("GIT_CONFIG_PARAMETERS", "p"),
            pair("GIT_CONFIG_KEY_", "name-only-prefix"),
            pair("B", "2"),
            pair("A", "dup-later-wins"),
        ];
        let env = git_env(&process);
        let names: Vec<String> = env
            .iter()
            .map(|(k, v)| {
                format!(
                    "{}={}",
                    String::from_utf8_lossy(k),
                    String::from_utf8_lossy(v)
                )
            })
            .collect();
        assert_eq!(
            names,
            [
                "A=dup-later-wins",
                "GIT_TERMINAL_PROMPT=0",
                "B=2",
                "GIT_CONFIG_NOSYSTEM=1",
                "GIT_CONFIG_GLOBAL=/dev/null",
                "GIT_CONFIG_SYSTEM=/dev/null",
                "GIT_NO_LAZY_FETCH=1",
            ]
        );
        // `process_environment` も同じ形(重複を 1 つにし、`=` の無い項目は読まない)。
        let mut seen = std::collections::HashSet::new();
        for (key, _) in process_environment() {
            assert!(seen.insert(key), "名前が重複している");
        }
    }

    #[test]
    fn a_failing_or_missing_git_becomes_the_fixed_diagnostics() {
        let dir = Dir::new("git-fail");
        let fail = write_script(&dir.0, "fail", "#!/bin/sh\nexit 3\n");
        let mut budget = EnvBudget::new();
        let error = git_config_output(&argv_of(&fail), b"x", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(
            (error.type_name.as_str(), message(&error).as_str()),
            ("Stop", "Git 設定を解析できない")
        );
        // 信号で終わった子(returncode が負)も同じ。
        let killed = write_script(&dir.0, "killed", "#!/bin/sh\nkill -KILL $$\n");
        let error = git_config_output(&argv_of(&killed), b"x", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(message(&error), "Git 設定を解析できない");
        // 実行ファイルが無い: `FileNotFoundError` で、ファイル名は `git`。
        let empty = dir.0.join("empty");
        std::fs::create_dir(&empty).unwrap();
        let argv = vec![b"git".to_vec()];
        let env = vec![pair("PATH", empty.to_str().unwrap())];
        let error = git_config_output(&argv, b"x", &env, &mut budget).unwrap_err();
        assert_eq!(error.type_name, "FileNotFoundError");
        assert_eq!(
            message(&error),
            "[Errno 2] No such file or directory: 'git'"
        );
        // 実行できないファイル(権限なし)は `PermissionError`。
        let plain = dir.0.join("plain");
        std::fs::write(&plain, "x").unwrap();
        let argv = vec![plain.to_str().unwrap().as_bytes().to_vec()];
        let error = git_config_output(&argv, b"x", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(error.type_name, "PermissionError");
    }

    #[test]
    fn the_child_runs_in_the_root_directory_with_stderr_discarded() {
        let dir = Dir::new("git-cwd");
        let script = write_script(
            &dir.0,
            "pwd",
            "#!/bin/sh\npwd\necho noise >&2\nhead -c 0 >/dev/null\n",
        );
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget).unwrap();
        // 標準エラーは捨てられ、標準出力に混ざらない。
        assert_eq!(out, b"/\n");
    }

    #[test]
    fn the_childs_standard_error_is_the_null_device() {
        let dir = Dir::new("git-stderr");
        // `/dev/fd/2` が `/dev/null` と同じ実体(同じ dev と inode)か。`/proc` の無い Mac でも使える。
        let script = write_script(
            &dir.0,
            "fd2",
            "#!/bin/sh\nif [ /dev/fd/2 -ef /dev/null ]; then echo /dev/null; else echo other; fi\n",
        );
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget).unwrap();
        assert_eq!(out, b"/dev/null\n");
    }

    #[test]
    fn stdin_and_stdout_are_exchanged_at_the_same_time() {
        let dir = Dir::new("git-both");
        // 子は、標準入力を読む前に 200000 バイトを書く。順に送って受ければ、パイプが詰まって止まる。
        let script = write_script(
            &dir.0,
            "both",
            "#!/bin/sh\nhead -c 200000 /dev/zero\ncat >/dev/null\n",
        );
        let raw = vec![b'a'; 1_000_000];
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&script), &raw, &sh_env(), &mut budget).unwrap();
        assert_eq!(out.len(), 200_000);
    }

    #[test]
    fn a_child_that_stops_reading_does_not_fail_the_parse() {
        let dir = Dir::new("git-broken-pipe");
        let script = write_script(&dir.0, "quit", "#!/bin/sh\necho done\n");
        let raw = vec![b'a'; 4_000_000];
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&script), &raw, &sh_env(), &mut budget).unwrap();
        assert_eq!(out, b"done\n");
    }

    #[test]
    fn output_is_limited_to_eight_mib_while_it_is_received() {
        let dir = Dir::new("git-big");
        let exact = write_script(
            &dir.0,
            "exact",
            &format!("#!/bin/sh\nhead -c {MAX_FILE} /dev/zero\n"),
        );
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&exact), b"", &sh_env(), &mut budget).unwrap();
        assert_eq!(out.len() as u64, MAX_FILE);
        let over = write_script(
            &dir.0,
            "over",
            &format!("#!/bin/sh\nhead -c {} /dev/zero\n", MAX_FILE + 1),
        );
        let error = git_config_output(&argv_of(&over), b"", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(message(&error), "Git 設定の解析出力がバイト上限を超える");
        // 無限に出す子も、上限で止める(待ち続けない)。
        let endless = write_script(&dir.0, "endless", "#!/bin/sh\nyes\n");
        let started = Instant::now();
        let error = git_config_output(&argv_of(&endless), b"", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(message(&error), "Git 設定の解析出力がバイト上限を超える");
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    fn pid_is_gone(pid: i32) -> bool {
        std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| !status.success())
    }

    #[test]
    fn a_slow_child_is_stopped_at_the_deadline_and_killed() {
        let dir = Dir::new("git-slow");
        let pidfile = dir.0.join("pid");
        let script = write_script(
            &dir.0,
            "slow",
            &format!(
                "#!/bin/sh\necho $$ > {}\nexec sleep 30\n",
                pidfile.display()
            ),
        );
        let mut budget = EnvBudget::new();
        let started = Instant::now();
        let error = git_config_output_within(
            &argv_of(&script),
            b"",
            &sh_env(),
            &mut budget,
            Duration::from_millis(400),
        )
        .unwrap_err();
        assert_eq!(message(&error), "Git 設定の解析が時間上限を超える");
        let elapsed = started.elapsed();
        assert!(elapsed >= Duration::from_millis(400), "{elapsed:?}");
        assert!(elapsed < Duration::from_secs(10), "{elapsed:?}");
        let pid: i32 = std::fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // 後始末で kill と回収が済んでいる。
        assert!(pid_is_gone(pid), "子が残っている: {pid}");
        // 製品の上限は 3 秒。
        assert_eq!(GIT_TIMEOUT, Duration::from_secs(3));
    }

    #[test]
    fn the_wait_ticks_the_budget_every_tenth_of_a_second() {
        let dir = Dir::new("git-tick");
        let pidfile = dir.0.join("pid");
        let script = write_script(
            &dir.0,
            "slow",
            &format!(
                "#!/bin/sh\necho $$ > {}\nexec sleep 30\n",
                pidfile.display()
            ),
        );
        // 予算の期限(0.2 秒)が、`tick` で先に見つかる(3 秒の解析の期限より前)。
        let mut budget = EnvBudget::with_deadline(Instant::now() + Duration::from_millis(200));
        let started = Instant::now();
        let error = git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(message(&error), super::super::budget::TICK_STOP);
        let elapsed = started.elapsed();
        // 0.1 秒ごとの `tick` で見つけるので、期限から 0.1 秒ほどの内に止まる(1 秒ごとなら、これより遅い)。
        assert!(elapsed >= Duration::from_millis(200), "{elapsed:?}");
        assert!(elapsed < Duration::from_millis(900), "{elapsed:?}");
        // `tick(0)` なので、項目の数は増えない。
        assert_eq!(budget.entries(), 0);
        let pid: i32 = std::fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(pid_is_gone(pid), "子が残っている: {pid}");
    }

    #[test]
    fn a_child_that_closes_its_output_but_keeps_running_hits_the_deadline_in_wait() {
        let dir = Dir::new("git-close-out");
        // 標準出力を閉じて(読む側は終わる)、そのまま眠る。出力の読取りが終わった後の `wait` の時間切れ。
        let script = write_script(&dir.0, "closed", "#!/bin/sh\nexec >&-\nexec sleep 30\n");
        let mut budget = EnvBudget::new();
        let started = Instant::now();
        let error = git_config_output_within(
            &argv_of(&script),
            b"",
            &sh_env(),
            &mut budget,
            Duration::from_millis(300),
        )
        .unwrap_err();
        assert_eq!(message(&error), "Git 設定の解析が時間上限を超える");
        assert!(started.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn an_empty_body_closes_stdin_at_once() {
        let dir = Dir::new("git-empty-body");
        // 標準入力が閉じられなければ、`cat` は終わらない。
        let script = write_script(&dir.0, "cat", "#!/bin/sh\ncat\necho end\n");
        let mut budget = EnvBudget::new();
        let out = git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget).unwrap();
        assert_eq!(out, b"end\n");
    }

    #[test]
    fn the_item_limit_counts_the_trailing_empty_record() {
        let dir = Dir::new("git-items");
        let make = |count: usize| {
            write_script(
                &dir.0,
                &format!("git{count}"),
                &format!(
                    "#!/bin/sh\ni=0\nwhile [ $i -lt {count} ]; do printf 'a.b\\nv\\0'; i=$((i+1)); done\n"
                ),
            )
        };
        // `git` として呼ばれるので、PATH の先頭に置く。
        let host = host_with_home();
        for (count, ok) in [(9999usize, true), (10000, false)] {
            let bin = dir.0.join(format!("bin{count}"));
            std::fs::create_dir(&bin).unwrap();
            std::fs::copy(make(count), bin.join("git")).unwrap();
            let env = vec![pair("PATH", &format!("{}:/usr/bin:/bin", bin.display()))];
            let path = PyPath::new(&cps("/x/config"));
            let result = includes(&host, &path, b"", None, &env);
            if ok {
                assert!(result.unwrap().is_empty());
            } else {
                assert_eq!(
                    message(&result.unwrap_err()),
                    "Git 設定の項目数が上限を超える"
                );
            }
        }
    }

    #[test]
    fn each_include_key_is_asked_once_and_empty_values_are_refused() {
        let dir = Dir::new("git-once");
        let template = fixture!("git_cases.json")["recorder"]
            .as_str()
            .unwrap()
            .to_owned();
        let bin = install_recorder(&dir.0, &template);
        let env = base_env(&bin);
        let path = PyPath::new(&cps("/cfg/config"));
        let raw = b"[include]\n\tpath = a\n\tpath = b\n[includeIf \"gitdir:/x/\"]\n\tpath = c\n";
        let found = includes(&host_with_home(), &path, raw, None, &env).unwrap();
        let shown: Vec<String> = found.iter().map(|item| to_string(item)).collect();
        assert_eq!(shown, ["/cfg/a", "/cfg/b", "/cfg/c"]);
        // `--list` 1 回と、鍵ごとの `--get-all` 2 回。
        let calls = read_calls(&dir.0);
        assert_eq!(calls.len(), 3);
        let last_words: Vec<&str> = calls
            .iter()
            .map(|call| {
                call["argv"]
                    .as_array()
                    .unwrap()
                    .last()
                    .unwrap()
                    .as_str()
                    .unwrap()
            })
            .collect();
        assert_eq!(
            last_words,
            ["--list", "include.path", "includeif.gitdir:/x/.path"]
        );
    }

    #[test]
    fn a_config_changed_while_git_parses_is_found_by_the_next_record() {
        let dir = Dir::new("git-race");
        let root = dir.0.join("r");
        std::fs::create_dir_all(root.join("cfg")).unwrap();
        let config = root.join("cfg/config");
        std::fs::write(&config, "[include]\n\tpath = a\n").unwrap();
        // 偽の git: 呼ばれるたびに設定ファイルへ追記してから、本物の git を同じ標準入力で呼ぶ。
        let bin = dir.0.join("bin");
        std::fs::create_dir(&bin).unwrap();
        write_script(
            &bin,
            "git",
            &format!(
                "#!/bin/sh\nsleep 0.05\necho '[include]\n\tpath = injected' >> {}\nexec {} \"$@\"\n",
                config.display(),
                find_in_path("git")
            ),
        );
        let env = vec![pair(
            "PATH",
            &format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )];
        let host = host_with_home();
        let key = cps("external:cfg");
        let path_cps = cps(config.to_str().unwrap());
        let mut budget = EnvBudget::new();
        let mut before = PyDict::new();
        let raw = record(
            &host,
            &path_cps,
            &key,
            &mut budget,
            &mut before,
            &RecordOptions::default(),
        )
        .unwrap()
        .unwrap();
        let found = includes(
            &host,
            &PyPath::new(&path_cps),
            &raw,
            Some(&mut budget),
            &env,
        )
        .unwrap();
        // 解析は、渡した本文(追記の前)だけを見る。
        assert_eq!(found.len(), 1);
        // 次の記録は、ファイルが変わったことを、保持した値との違いで見つける。
        let mut after = PyDict::new();
        record(
            &host,
            &path_cps,
            &key,
            &mut budget,
            &mut after,
            &RecordOptions {
                expected: Some(&before),
                ..RecordOptions::default()
            },
        )
        .unwrap();
        assert!(
            super::super::entry::get(&before, &key)
                .unwrap()
                .py_ne(super::super::entry::get(&after, &key).unwrap())
        );
    }

    #[test]
    fn the_hook_lets_a_test_run_code_while_the_child_is_running() {
        // `git-after-start` は、子を起動して非 blocking にした後の位置。
        let dir = Dir::new("git-hook");
        let script = write_script(&dir.0, "ok", "#!/bin/sh\necho ok\n");
        let seen = std::rc::Rc::new(std::cell::Cell::new(0));
        let counter = seen.clone();
        let mut budget = EnvBudget::new();
        let out = hooks::with(
            move |point| {
                if matches!(point, hooks::Point::Site("git-after-start")) {
                    counter.set(counter.get() + 1);
                }
                None
            },
            || git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget),
        )
        .unwrap();
        assert_eq!(out, b"ok\n");
        assert_eq!(seen.get(), 1);
    }

    /// `SIGINT` の印を立てるテストは、プロセス全体の信号の扱いを変えるので、別プロセスで行う。
    fn isolated(name: &str) -> bool {
        if std::env::var_os("S2E_GITCONFIG_ISOLATED").is_some_and(|value| value == name) {
            return true;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("environment_guard::gitconfig::tests::{name}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S2E_GITCONFIG_ISOLATED", name)
            .output()
            .unwrap();
        let text = format!(
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            output.status.success(),
            "別プロセスの検査が失敗({name}): {text}"
        );
        assert!(
            text.contains("1 passed"),
            "別プロセスが検査を流していない({name}): {text}"
        );
        false
    }

    #[test]
    fn sigint_while_waiting_for_git_is_a_keyboard_interrupt_and_the_child_is_killed() {
        if !isolated("sigint_while_waiting_for_git_is_a_keyboard_interrupt_and_the_child_is_killed")
        {
            return;
        }
        let dir = Dir::new("git-sigint");
        let pidfile = dir.0.join("pid");
        let script = write_script(
            &dir.0,
            "slow",
            &format!(
                "#!/bin/sh\necho $$ > {}\nexec sleep 30\n",
                pidfile.display()
            ),
        );
        os::signal_set_default(os::SIGINT).unwrap();
        assert!(os::install_sigint_handler().unwrap());
        // 子が眠り始めた後(0.3 秒後)に、自分へ `SIGINT` を送る別の process。
        let mut sender = std::process::Command::new("sh")
            .args([
                "-c",
                &format!("sleep 0.3; kill -INT {}", std::process::id()),
            ])
            .spawn()
            .unwrap();
        let mut budget = EnvBudget::new();
        let started = Instant::now();
        let error = git_config_output(&argv_of(&script), b"", &sh_env(), &mut budget).unwrap_err();
        assert_eq!(error.type_name, "KeyboardInterrupt");
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(sender.wait().unwrap().success());
        assert!(!os::sigint_pending());
        let pid: i32 = std::fs::read_to_string(&pidfile)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // 後始末で kill と回収が済んでいる。
        assert!(pid_is_gone(pid), "子が残っている: {pid}");
    }

    #[test]
    fn an_interrupted_system_call_is_a_keyboard_interrupt_only_after_sigint() {
        if !isolated("an_interrupted_system_call_is_a_keyboard_interrupt_only_after_sigint") {
            return;
        }
        let eintr = || io::Error::from_raw_os_error(os::EINTR);
        // `SIGINT` が来ていない `EINTR` は、`InterruptedError`。
        let error = os_failure(eintr());
        assert_eq!(error.type_name, "InterruptedError");
        os::signal_set_default(os::SIGINT).unwrap();
        assert!(os::install_sigint_handler().unwrap());
        let sent = std::process::Command::new("kill")
            .args(["-INT", &std::process::id().to_string()])
            .status()
            .unwrap();
        assert!(sent.success());
        let start = Instant::now();
        while !os::sigint_pending() {
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "SIGINT が届かない"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        // `SIGINT` を受けた後の `EINTR` は `KeyboardInterrupt`。印は消える。
        let error = os_failure(eintr());
        assert_eq!(error.type_name, "KeyboardInterrupt");
        assert!(!os::sigint_pending());
        // `EINTR` 以外の失敗は、`SIGINT` が来ていても `OSError` のまま(印は残る)。
        let sent = std::process::Command::new("kill")
            .args(["-INT", &std::process::id().to_string()])
            .status()
            .unwrap();
        assert!(sent.success());
        while !os::sigint_pending() {
            assert!(
                start.elapsed() < Duration::from_secs(20),
                "SIGINT が届かない"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let error = os_failure(io::Error::from_raw_os_error(os::EPIPE));
        assert_eq!(error.type_name, "BrokenPipeError");
        assert!(os::sigint_pending());
    }
}
