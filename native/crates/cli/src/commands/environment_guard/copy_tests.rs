//! `read`・`exec`・`hook`・`verify-pair` の固定期待の検査と、単体の検査。
//!
//! 固定期待(`native/ci/s2e_fixtures/copy_cases.json`)は、旧 guard を `python3 -B <guard> <コマンド>` で直接起動した
//! 結果(U5)で、`gen_cases.py` の area `copy` が作る。同じ物理入力(一時フォルダ)で `bootstrap` →(入力の変更)→
//! コマンドの流れを Rust で動かし、終了コード・標準出力・標準エラーと、`exec` の子が書いた観測用のファイルを比べる。
//! `verify-pair` は、標準入力の代わりのパイプへ、`verified` が出た後(`pair-after-verified` の位置)に `go` などを書く。
//!
//! 固定期待の流れは Linux だけで流す(Mac は旧版も Rust も既定の監視先 `/etc/...` のリンクで `bootstrap` に失敗する。
//! command_tests.rs と同じ理由)。`wait_for_go`・`run_child`・引数の検査などの単体の検査は全機種で流す。
use super::command_tests::{
    GUARD_BYTES, TestHost, find_in_path, mask_sha, place_guard, replace_body, run_cli, run_ops,
    set_mode,
};
use super::error::PyException;
use super::observe_tests::{Dir, build_layout};
use super::pyops::sha_hex;
use super::pystr::to_string;
use super::testing::fixture;
use serde_json::{Value, json};
use std::path::Path;
use std::time::{Duration, Instant};

const R4P_LINE: &str = "ERROR [environment-guard] hook は R4P まで旧版の判定器で実行する\n";

fn latin1(raw: &[u8]) -> String {
    raw.iter().map(|&byte| char::from(byte)).collect()
}

fn base_pairs(root: &str) -> Vec<(String, String)> {
    vec![
        ("PATH".to_owned(), std::env::var("PATH").unwrap()),
        ("HOME".to_owned(), format!("{root}/home")),
        ("GIT_CONFIG_NOSYSTEM".to_owned(), "1".to_owned()),
        ("LANG".to_owned(), "C.UTF-8".to_owned()),
    ]
}

/// `{r}`・`{state:名}`・`{sha:名}`・`{guard:名}`(控えた guard の SHA-256)を置き換える(`gen_cases.py` の `cp_sub`)。
fn sub(text: &str, root: &str) -> String {
    let mut out = text.replace("{r}", root);
    while let Some(start) = ["{state:", "{sha:", "{guard:"]
        .iter()
        .filter_map(|token| out.find(token))
        .min()
    {
        let end = start + out[start..].find('}').unwrap();
        let (kind, label) = out[start + 1..end].split_once(':').unwrap();
        let state = Path::new(root)
            .join("out")
            .join(label)
            .join("environment.json");
        let value = match kind {
            "state" => state.to_str().unwrap().to_owned(),
            "sha" => std::fs::read(&state).map_or("0".repeat(64), |raw| sha_hex(&raw)),
            _ => sha_hex(GUARD_BYTES),
        };
        out.replace_range(start..=end, &value);
    }
    out
}

/// 入力の変更。`rewrite`(今の本文をそのまま書き戻す。時刻だけが変わる)をここで行い、残りは `run_ops`。
fn apply(root: &Path, ops: &[Value]) {
    let mut rest: Vec<Value> = Vec::new();
    for op in ops {
        if op["op"] == "rewrite" {
            if !rest.is_empty() {
                run_ops(root, &std::mem::take(&mut rest));
            }
            std::thread::sleep(Duration::from_millis(30));
            let path = root.join(op["path"].as_str().unwrap());
            let raw = std::fs::read(&path).unwrap();
            std::fs::write(&path, raw).unwrap();
        } else {
            rest.push(op.clone());
        }
    }
    if !rest.is_empty() {
        run_ops(root, &rest);
    }
}

/// 一時フォルダに配置・guard・出力先を作る。
fn prepare(label: &str, layout: &[Value]) -> (Dir, std::path::PathBuf, String) {
    let dir = Dir::new(label);
    let root_path = dir.0.join("case");
    std::fs::create_dir(&root_path).unwrap();
    set_mode(&root_path, 0o775);
    let root = root_path.to_str().unwrap().to_owned();
    let layout: Vec<Value> = layout
        .iter()
        .map(|item| {
            let mut item = item.clone();
            if let Some(target) = item.get("target").and_then(Value::as_str) {
                item["target"] = json!(
                    target
                        .replace("{python3}", &find_in_path("python3"))
                        .replace("{bash}", &find_in_path("bash"))
                );
            }
            item
        })
        .collect();
    build_layout(&root_path, &replace_body(&layout, &root));
    place_guard(&root_path);
    std::fs::create_dir(root_path.join("out")).unwrap();
    set_mode(&root_path.join("out"), 0o755);
    (dir, root_path, root)
}

/// 標準入力の代わりに、`verified` の後に書く物。
struct Feed {
    chunks: Vec<String>,
    gap: Duration,
    delay: Duration,
    close: bool,
}

impl Feed {
    fn from(spec: &Value) -> Self {
        Self {
            chunks: spec["chunks"]
                .as_array()
                .unwrap()
                .iter()
                .map(|chunk| chunk.as_str().unwrap().to_owned())
                .collect(),
            gap: Duration::from_secs_f64(spec.get("gap").and_then(Value::as_f64).unwrap_or(0.0)),
            delay: Duration::from_secs_f64(
                spec.get("delay").and_then(Value::as_f64).unwrap_or(0.0),
            ),
            close: spec.get("close") == Some(&json!(true)),
        }
    }
}

/// `verify-pair` を、`verified` の後(`pair-after-verified`)に入力を変えて `feed` を書く形で動かす。
fn run_pair(
    pairs: &[(String, String)],
    root_path: &Path,
    args: &[String],
    feed: Feed,
    ops: Option<Vec<Value>>,
) -> (i32, Vec<u8>, Vec<u8>) {
    use super::fsio::hooks::{Point, with};
    use std::cell::RefCell;
    use std::io::Write;
    use std::os::fd::OwnedFd;
    use std::rc::Rc;
    let (reader, writer) = std::io::pipe().unwrap();
    let host = TestHost::new(pairs, root_path).with_go(OwnedFd::from(reader));
    let (done_tx, done_rx) = std::sync::mpsc::channel::<()>();
    let writer_thread = Rc::new(RefCell::new(None));
    let slot = Rc::clone(&writer_thread);
    let root = root_path.to_path_buf();
    let mut parts = Some((writer, done_rx, feed));
    let result = with(
        move |point| {
            if matches!(point, Point::Site("pair-after-verified")) {
                if let Some(ops) = &ops {
                    apply(&root, ops);
                }
                let (mut writer, done, feed) = parts.take().expect("1 回だけ");
                *slot.borrow_mut() = Some(std::thread::spawn(move || {
                    std::thread::sleep(feed.delay);
                    for chunk in &feed.chunks {
                        // 読む側が先に終わっていれば(時間切れなど)、書込みは失敗する。旧版の側と同じく無視する。
                        let _ = writer.write_all(chunk.as_bytes());
                        std::thread::sleep(feed.gap);
                    }
                    if !feed.close {
                        // 閉じない流れは、`verify-pair` が終わるまで開いたままにする。
                        let _ = done.recv();
                    }
                    drop(writer);
                }));
            }
            None
        },
        || run_cli(&host, args),
    );
    drop(done_tx);
    if let Some(handle) = writer_thread.borrow_mut().take() {
        handle.join().unwrap();
    }
    result
}

fn probes_of(root_path: &Path, root: &str) -> Value {
    // 機種ごとに違う既定の PATH は印にする(`gen_cases.py` の `cp_scenario` と同じ)。
    let path_value = std::env::var("PATH").unwrap();
    let mut found = serde_json::Map::new();
    let probe = root_path.join("probe");
    let mut names: Vec<String> = std::fs::read_dir(&probe)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    for name in names {
        let raw = std::fs::read(probe.join(&name)).unwrap();
        let text = String::from_utf8_lossy(&raw)
            .replace(root, "{r}")
            .replace(&path_value, "{PATH}");
        found.insert(name.clone(), Value::String(text));
        std::fs::remove_file(probe.join(&name)).unwrap();
    }
    Value::Object(found)
}

fn run_step(root_path: &Path, root: &str, step: &Value, want: &Value, label: &str) {
    let mut pairs = base_pairs(root);
    if let Some(extra) = step.get("env").and_then(Value::as_object) {
        for (key, value) in extra {
            // 旧版の `env[key] = value`(あれば同じ位置で置き換え、無ければ末尾へ)。`null` は削除。
            match (
                value.as_str(),
                pairs.iter().position(|(held, _)| held == key),
            ) {
                (Some(value), Some(at)) => pairs[at].1 = sub(value, root),
                (Some(value), None) => pairs.push((key.clone(), sub(value, root))),
                (None, Some(at)) => {
                    pairs.remove(at);
                }
                (None, None) => {}
            }
        }
    }
    if let Some(ops) = step.get("ops_before").and_then(Value::as_array) {
        apply(root_path, ops);
    }
    let args: Vec<String> = step["args"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| sub(arg.as_str().unwrap(), root))
        .collect();
    let (code, out, err) = if args[0] == "verify-pair" {
        run_pair(
            &pairs,
            root_path,
            &args,
            Feed::from(&step["stdin"]),
            step.get("ops_after_verified")
                .and_then(Value::as_array)
                .cloned(),
        )
    } else {
        let host = TestHost::new(&pairs, root_path);
        match step.get("ops_after_verify").and_then(Value::as_array) {
            // `verify` の後、コピーの判定器を読むまでの間に、コピーを書き換える(旧版の同じ窓)。
            Some(ops) => {
                use super::fsio::hooks::{Point, with};
                let root = root_path.to_path_buf();
                let ops = ops.clone();
                with(
                    move |point| {
                        if matches!(point, Point::Site("hook-after-verify")) {
                            apply(&root, &ops);
                        }
                        None
                    },
                    || run_cli(&host, &args),
                )
            }
            None => run_cli(&host, &args),
        }
    };
    let stdout = mask_sha(&latin1(&out).replace(root, "{r}"));
    let stderr = String::from_utf8_lossy(&err).replace(root, "{r}");
    if step.get("u3") == Some(&json!(true)) {
        // U3: 旧版は判定器を実行する(旧版の結果は固定期待にあるが、Rust は確認の後に必ず止まる)。
        assert_eq!(
            (code, stdout.as_str(), stderr.as_str()),
            (20, "", R4P_LINE),
            "{label}"
        );
        assert_eq!(probes_of(root_path, root), json!({}), "{label}");
        return;
    }
    assert_eq!(
        i64::from(code),
        want["code"].as_i64().unwrap(),
        "{label}: 終了コード"
    );
    match want.get("stdout_len") {
        Some(len) => {
            assert_eq!(json!(out.len()), *len, "{label}: 標準出力の長さ");
            assert_eq!(
                json!(sha_hex(&out)),
                want["stdout_sha256"],
                "{label}: 標準出力の SHA-256"
            );
        }
        None => assert_eq!(
            stdout,
            want["stdout"].as_str().unwrap(),
            "{label}: 標準出力"
        ),
    }
    if let Some(last) = want.get("stderr_last") {
        // 捕まえない例外(U4): traceback の途中は比べない。最後の行だけを比べる。
        let found = stderr.trim().lines().next_back().unwrap_or("");
        assert_eq!(json!(found), *last, "{label}: 標準エラーの最後の行");
        assert!(
            stderr.starts_with("Traceback (most recent call last):\n"),
            "{label}"
        );
    } else if let Some(want_err) = want.get("stderr") {
        assert_eq!(stderr, want_err.as_str().unwrap(), "{label}: 標準エラー");
    }
    assert_eq!(
        probes_of(root_path, root),
        want["probes"],
        "{label}: 子が書いた観測用のファイル"
    );
}

fn run_case(case: &Value) {
    let name = case["name"].as_str().unwrap();
    let (_dir, root_path, root) = prepare("copy", case["layout"].as_array().unwrap());
    let steps = case["steps"].as_array().unwrap();
    let results = case["results"].as_array().unwrap();
    assert_eq!(steps.len(), results.len(), "{name}");
    for (index, (step, want)) in steps.iter().zip(results).enumerate() {
        run_step(&root_path, &root, step, want, &format!("{name} #{index}"));
    }
}

fn run_named(names: &[&str]) {
    let cases = fixture!("copy_cases.json");
    let list = cases["cases"].as_array().unwrap();
    let mut ran = 0;
    for case in list {
        if names.contains(&case["name"].as_str().unwrap())
            || (names.contains(&"refusal-*")
                && case["name"].as_str().unwrap().starts_with("refusal-"))
            || (names.contains(&"pair-after-*")
                && case["name"].as_str().unwrap().starts_with("pair-after-"))
        {
            run_case(case);
            ran += 1;
        }
    }
    assert!(ran >= names.len().min(1), "流した流れが無い: {names:?}");
}

#[cfg(target_os = "linux")]
#[test]
fn read_matches_the_old_python_3_10() {
    run_named(&["read"]);
}

#[cfg(target_os = "linux")]
#[test]
fn every_entry_point_checks_the_state_before_anything_else() {
    // 件数: 7 つの入力の変更 × (read・exec・hook・verify-pair)
    run_named(&["refusal-*"]);
}

#[cfg(target_os = "linux")]
#[test]
fn exec_matches_the_old_python_3_10() {
    run_named(&["exec"]);
}

#[cfg(target_os = "linux")]
#[test]
fn hook_checks_match_the_old_python_3_10_and_stop_where_the_old_one_runs_the_judge() {
    run_named(&["hook", "hook-missing", "hook-after-verify"]);
}

#[cfg(target_os = "linux")]
#[test]
fn verify_pair_matches_the_old_python_3_10() {
    run_named(&["pair"]);
}

#[cfg(target_os = "linux")]
#[test]
fn verify_pair_finds_changes_made_after_verified_like_the_old_python_3_10() {
    run_named(&["pair-after-*"]);
}

#[cfg(target_os = "linux")]
#[test]
fn the_fixture_covers_every_command_and_case_kind() {
    let cases = fixture!("copy_cases.json");
    let list = cases["cases"].as_array().unwrap();
    let steps: usize = list
        .iter()
        .map(|case| case["steps"].as_array().unwrap().len())
        .sum();
    assert_eq!((list.len(), steps), (EXPECTED_FLOWS, EXPECTED_STEPS));
    let mut by_command = std::collections::BTreeMap::new();
    for case in list {
        for step in case["steps"].as_array().unwrap() {
            *by_command
                .entry(step["args"][0].as_str().unwrap().to_owned())
                .or_insert(0usize) += 1;
        }
    }
    assert_eq!(
        by_command.into_iter().collect::<Vec<_>>(),
        EXPECTED_COMMANDS
            .iter()
            .map(|(name, count)| ((*name).to_owned(), *count))
            .collect::<Vec<_>>()
    );
}

/// 固定期待の件数(流れの数・回数・コマンドごとの回数)。増減は、固定期待を作り直したときに見直す。
const EXPECTED_FLOWS: usize = 33;
const EXPECTED_STEPS: usize = 234;
const EXPECTED_COMMANDS: &[(&str, usize)] = &[
    ("bootstrap", 33),
    ("exec", 60),
    ("hook", 23),
    ("read", 38),
    ("verify-pair", 80),
];

// ---------------------------------------------------------------- 単体の検査(全機種)

use super::copy::run_child;
use super::pair::wait_for_go;
use std::io::Write;
use std::os::fd::AsFd;

fn pipe_with(bytes: &[u8], close: bool) -> (std::io::PipeReader, Option<std::io::PipeWriter>) {
    let (reader, mut writer) = std::io::pipe().unwrap();
    writer.write_all(bytes).unwrap();
    (reader, (!close).then_some(writer))
}

fn go(bytes: &[u8], close: bool, timeout: f64) -> Result<bool, PyException> {
    let (reader, _held) = pipe_with(bytes, close);
    wait_for_go(reader.as_fd(), timeout)
}

#[test]
fn only_go_and_a_newline_is_accepted_and_everything_else_is_a_silent_stop() {
    assert!(go(b"go\n", false, 5.0).unwrap());
    assert!(go(b"go\nmore", false, 5.0).unwrap());
    for (bytes, close) in [
        (&b"stop\n"[..], false),
        (b"GO\n", false),
        (b"go\r\n", false),
        (b"xyz", false),
        (b"go", true),
        (b"g", true),
        (b"", true),
    ] {
        assert!(!go(bytes, close, 5.0).unwrap(), "{bytes:?}");
    }
}

#[test]
fn go_may_arrive_in_pieces_and_a_missing_rest_is_a_time_out() {
    let (reader, mut writer) = std::io::pipe().unwrap();
    let handle = std::thread::spawn(move || {
        for piece in [&b"g"[..], b"o", b"\n"] {
            std::thread::sleep(Duration::from_millis(60));
            writer.write_all(piece).unwrap();
        }
        writer
    });
    assert!(wait_for_go(reader.as_fd(), 10.0).unwrap());
    drop(handle.join().unwrap());
    // 時間内に続きが来なければ、何も出さずに止まる(`false`)。期限は旧版と同じく単調時計で測る。
    let (reader, mut writer) = std::io::pipe().unwrap();
    writer.write_all(b"g").unwrap();
    let started = Instant::now();
    assert!(!wait_for_go(reader.as_fd(), 0.4).unwrap());
    let waited = started.elapsed();
    assert!(
        waited >= Duration::from_millis(390) && waited < Duration::from_secs(5),
        "{waited:?}"
    );
}

#[test]
fn a_wait_longer_than_the_old_select_accepts_is_an_overflow_error_after_verified() {
    // 旧版の `select` は、待ち時間のナノ秒(上向きに丸めた値)が 64 ビットに収まらないと `OverflowError`。
    // 境目は 2^63 ナノ秒 ≒ 9223372036.85 秒(旧版を Linux で測った: 9223372036 は通り、9223372037 は落ちる)。
    for timeout in [9_223_372_037.0, 1e10, 1e30, 1e308, f64::MAX] {
        let error = go(b"go\n", false, timeout).unwrap_err();
        assert_eq!(error.type_name, "OverflowError", "{timeout}");
        assert_eq!(
            to_string(error.message.as_codepoints()),
            "timestamp too large to convert to C _PyTime_t",
            "{timeout}"
        );
    }
    // 収まる値は、データがあればすぐ返る。Mac の `select` は 1e8 秒を超えると `EINVAL`(旧版を Mac で測っていない。未確認)。
    for timeout in [100_000_001.0, 9e9, 9_223_372_036.0] {
        let found = go(b"go\n", false, timeout);
        if cfg!(target_os = "macos") {
            let error = found.unwrap_err();
            assert_eq!(error.type_name, "OSError", "{timeout}");
            assert_eq!(
                to_string(error.message.as_codepoints()),
                "[Errno 22] Invalid argument"
            );
        } else {
            assert!(found.unwrap(), "{timeout}");
        }
    }
    assert!(go(b"go\n", false, 100_000_000.0).unwrap());
}

fn env_with_path(path: &str) -> Vec<(Vec<u8>, Vec<u8>)> {
    vec![(b"PATH".to_vec(), path.as_bytes().to_vec())]
}

fn argv(words: &[&str]) -> Vec<Vec<u8>> {
    words.iter().map(|word| word.as_bytes().to_vec()).collect()
}

#[test]
fn a_child_returns_its_exit_code_and_a_signal_is_a_negative_number() {
    let env = env_with_path("/usr/bin:/bin");
    for (script, want) in [
        ("exit 0", 0),
        ("exit 1", 1),
        ("exit 3", 3),
        ("exit 255", 255),
        ("kill -9 $$", -9),
        ("kill -15 $$", -15),
        ("kill -1 $$", -1),
    ] {
        let code = run_child(&argv(&["sh", "-c", script]), &env).unwrap();
        assert_eq!(code, want, "{script}");
    }
}

#[test]
fn spawn_failures_have_the_old_exception_types_and_messages() {
    let message = |error: PyException| {
        (
            error.type_name.clone(),
            to_string(error.message.as_codepoints()),
        )
    };
    let missing = run_child(
        &argv(&["s2e-no-such-program"]),
        &env_with_path("/nonexistent"),
    );
    assert_eq!(
        message(missing.unwrap_err()),
        (
            "FileNotFoundError".to_owned(),
            "[Errno 2] No such file or directory: 's2e-no-such-program'".to_owned()
        )
    );
    // PATH の最初の候補が実行できない物でも、後の候補があればそれを使う。後が無ければ最初の誤り(`PermissionError`)。
    let dir = Dir::new("copy-path");
    let noexec = dir.0.join("noexec");
    std::fs::create_dir(&noexec).unwrap();
    std::fs::write(noexec.join("s2e-prog"), "#!/bin/sh\nexit 4\n").unwrap();
    set_mode(&noexec.join("s2e-prog"), 0o644);
    let only = run_child(
        &argv(&["s2e-prog"]),
        &env_with_path(noexec.to_str().unwrap()),
    );
    assert_eq!(
        message(only.unwrap_err()),
        (
            "PermissionError".to_owned(),
            "[Errno 13] Permission denied: 's2e-prog'".to_owned()
        )
    );
    let good = dir.0.join("good");
    std::fs::create_dir(&good).unwrap();
    std::fs::write(good.join("s2e-prog"), "#!/bin/sh\nexit 4\n").unwrap();
    set_mode(&good.join("s2e-prog"), 0o755);
    let both = format!("{}:{}", noexec.display(), good.display());
    assert_eq!(
        run_child(&argv(&["s2e-prog"]), &env_with_path(&both)).unwrap(),
        4
    );
    assert_eq!(
        message(run_child(&[b"a\0b".to_vec()], &env_with_path("/bin")).unwrap_err()),
        ("ValueError".to_owned(), "embedded null byte".to_owned())
    );
    assert_eq!(
        message(run_child(&argv(&["sh"]), &[(b"A=B".to_vec(), b"x".to_vec())]).unwrap_err()),
        (
            "ValueError".to_owned(),
            "illegal environment variable name".to_owned()
        )
    );
}

fn quick(args: &[&str]) -> (i32, String, String) {
    let pairs = [("HOME".to_owned(), "/nonexistent-s2e-home".to_owned())];
    let host = TestHost::new(&pairs, Path::new("/"));
    let args: Vec<String> = args.iter().map(|arg| (*arg).to_owned()).collect();
    let (code, out, err) = run_cli(&host, &args);
    (
        code,
        String::from_utf8(out).unwrap(),
        String::from_utf8(err).unwrap(),
    )
}

#[test]
fn verify_pair_arguments_are_checked_before_anything_is_observed() {
    let pair = |guard: &str, timeout: &str| {
        quick(&[
            "verify-pair",
            "--state",
            "/nonexistent/s",
            "--expect-sha256",
            "x",
            "--current-inventory",
            "/nonexistent/c",
            "--guard-sha256",
            guard,
            "--go-timeout",
            timeout,
        ])
    };
    let ok_hash = "a".repeat(64);
    for guard in ["", "xyz", &"A".repeat(64), &"a".repeat(63), &"a".repeat(65)] {
        assert_eq!(
            pair(guard, "5"),
            (
                20,
                String::new(),
                "ERROR [environment-guard] guard 本体の SHA-256 が必要\n".to_owned()
            ),
            "{guard}"
        );
    }
    // 形の検査が先(待ち時間が 0 でも、guard の形の誤りが出る)。
    assert!(pair("xyz", "0").2.contains("guard 本体の SHA-256 が必要"));
    for timeout in ["0", "-3", "-0"] {
        assert_eq!(
            pair(&ok_hash, timeout),
            (
                20,
                String::new(),
                "ERROR [environment-guard] go の待ち時間は正の整数でなければならない\n".to_owned()
            ),
            "{timeout}"
        );
    }
    // 2 つの検査が通ると、1 回目の照合へ進む(控えの SHA-256 の形が先)。
    assert_eq!(
        pair(&ok_hash, "5"),
        (
            20,
            String::new(),
            "ERROR [environment-guard] 外部に保持した SHA-256 が必要\n".to_owned()
        )
    );
}

#[test]
fn read_exec_and_hook_check_the_state_first() {
    for command in ["read", "exec", "hook"] {
        let (code, out, err) = quick(&[
            command,
            "--state",
            "/nonexistent/s",
            "--expect-sha256",
            "xyz",
            "--path",
            "/etc/passwd",
        ]);
        assert_eq!(
            (code, out.as_str(), err.as_str()),
            (
                20,
                "",
                "ERROR [environment-guard] 外部に保持した SHA-256 が必要\n"
            ),
            "{command}"
        );
    }
}

// ---------------------------------------------------------------- SIGINT(別プロセス)

/// `SIGINT` の印を立てるテストは、プロセス全体の信号の扱いを変えるので、別プロセスで行う。
#[cfg(target_os = "linux")]
fn isolated(name: &str) -> bool {
    if std::env::var_os("S2E_COPY_ISOLATED").is_some_and(|value| value == name) {
        return true;
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            &format!("environment_guard::copy_tests::{name}"),
            "--nocapture",
            "--test-threads=1",
        ])
        .env("S2E_COPY_ISOLATED", name)
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

/// 固定期待の最初の流れの配置(SIGINT の検査が使う)に `extra` を足し、`bootstrap` まで済ませる。
#[cfg(target_os = "linux")]
fn bootstrapped(label: &str, extra: &[Value]) -> (Dir, std::path::PathBuf, String) {
    let cases = fixture!("copy_cases.json");
    let mut layout: Vec<Value> = cases["cases"][0]["layout"].as_array().unwrap().clone();
    layout.extend_from_slice(extra);
    let (dir, root_path, root) = prepare(label, &layout);
    let pairs = base_pairs(&root);
    let host = TestHost::new(&pairs, &root_path);
    let args: Vec<String> = [
        "bootstrap",
        "--root",
        &format!("{root}/plugin"),
        "--output",
        &format!("{root}/out/a"),
    ]
    .iter()
    .map(|arg| (*arg).to_owned())
    .collect();
    let (code, _, err) = run_cli(&host, &args);
    assert_eq!(code, 0, "{}", String::from_utf8_lossy(&err));
    (dir, root_path, root)
}

#[cfg(target_os = "linux")]
fn pid_is_gone(pid: i32) -> bool {
    let started = Instant::now();
    loop {
        // 回収されていない(ゾンビ)でも、`kill` で消えたことは `/proc/<pid>/stat` の状態で分かる。
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Err(_) => return true,
            Ok(text) if text.contains(") Z ") => return true,
            Ok(_) => {}
        }
        if started.elapsed() > Duration::from_secs(5) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(target_os = "linux")]
fn install_sigint() {
    use dev_workflow_platform::environment_guard as os;
    os::signal_set_default(os::SIGINT).unwrap();
    assert!(os::install_sigint_handler().unwrap());
}

/// `verified` の行は、`go` を待つ前に出口へ渡す(旧版は `sys.stdout.flush()`。親は `verified` を読んでから `go` を送る)。
#[cfg(target_os = "linux")]
#[test]
fn verified_is_flushed_to_the_pipe_before_go_is_awaited() {
    use super::dispatch::dispatch;
    use super::fsio::hooks::{Point, with};
    use super::output::run_main;
    use super::pystr::decode_fs;
    use super::runtime::{Env, PyStream, StreamKind};
    use std::cell::RefCell;
    use std::os::fd::OwnedFd;
    use std::rc::Rc;
    struct Shared(Rc<RefCell<Vec<u8>>>);
    impl Write for Shared {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            self.0.borrow_mut().extend_from_slice(data);
            Ok(data.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let (_dir, root_path, root) = bootstrapped("copy-flush", &[]);
    let state = format!("{root}/out/a/environment.json");
    let sha = sha_hex(&std::fs::read(&state).unwrap());
    let args: Vec<Vec<u32>> = [
        "verify-pair",
        "--state",
        &state,
        "--expect-sha256",
        &sha,
        "--current-inventory",
        "/nonexistent",
        "--guard-sha256",
        &sha_hex(GUARD_BYTES),
        "--go-timeout",
        "30",
    ]
    .iter()
    .map(|arg| decode_fs(arg.as_bytes()))
    .collect();
    let (reader, mut writer) = std::io::pipe().unwrap();
    // `/nonexistent` は 2 回目の照合で読む。ここでは `stop` を送って、その前に止める。
    let host = TestHost::new(&base_pairs(&root), &root_path).with_go(OwnedFd::from(reader));
    let seen = Rc::new(RefCell::new(Vec::new()));
    let at_go = Rc::new(RefCell::new(None));
    let (sink, probe) = (Shared(Rc::clone(&seen)), Rc::clone(&at_go));
    let watched = Rc::clone(&seen);
    let env = Env {
        columns: None,
        terminal_columns: 80,
        unbuffered: false,
        max_str_digits: 4300,
    };
    let (code, _) = with(
        move |point| {
            if matches!(point, Point::Site("pair-after-verified")) {
                *probe.borrow_mut() = Some(watched.borrow().clone());
                writer.write_all(b"stop\n").unwrap();
            }
            None
        },
        || {
            let (mut out, mut sink) = (Vec::new(), sink);
            let mut err = Vec::new();
            let mut stdout = PyStream::new(&mut sink, StreamKind::Stdout, false, 8192, false);
            let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 8192, false);
            let code = run_main(
                &args,
                &env,
                &mut stdout,
                &mut stderr,
                &mut |parsed, stdout, _| dispatch(&host, parsed, stdout),
            );
            out.extend_from_slice(&err);
            (code, out)
        },
    );
    assert_eq!(code, 20);
    assert_eq!(at_go.borrow().as_deref(), Some(&b"verified\n"[..]));
    assert_eq!(seen.borrow().as_slice(), b"verified\n");
}

/// 今のスレッドの識別子(`/proc/thread-self` は `<pid>/task/<tid>` を指す)。
#[cfg(target_os = "linux")]
fn thread_id() -> String {
    let link = std::fs::read_link("/proc/thread-self").unwrap();
    link.file_name().unwrap().to_str().unwrap().to_owned()
}

/// `seconds` 後に、検査を動かしているスレッドへ `SIGINT` を送る別の process。試験の枠組みは、検査を主スレッドでない
/// スレッドで動かすので、プロセス宛の `kill` では、待っているシステムコールが中断されないことがある(Linux は、
/// `kill` に渡した識別子のスレッドを優先して信号を届ける)。製品の入口は 1 スレッドで、主スレッドが受ける。
#[cfg(target_os = "linux")]
fn send_sigint_after(seconds: &str) -> std::process::Child {
    std::process::Command::new("sh")
        .args(["-c", &format!("sleep {seconds}; kill -INT {}", thread_id())])
        .spawn()
        .unwrap()
}

#[cfg(target_os = "linux")]
#[test]
fn sigint_while_exec_waits_is_a_keyboard_interrupt_and_the_child_is_killed() {
    if !isolated("sigint_while_exec_waits_is_a_keyboard_interrupt_and_the_child_is_killed") {
        return;
    }
    let script = json!({"path": "plugin/scripts/sleep.py", "kind": "file", "body":
        "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(30)\n"});
    let (_dir, root_path, root) = bootstrapped("copy-sigint-exec", &[script]);
    let pairs = base_pairs(&root);
    let state = format!("{root}/out/a/environment.json");
    let sha = sha_hex(&std::fs::read(&state).unwrap());
    let args: Vec<String> = [
        "exec",
        "--state",
        &state,
        "--expect-sha256",
        &sha,
        "--path",
        "scripts/sleep.py",
        "--",
        "python3",
        &format!("{root}/probe/pid"),
    ]
    .iter()
    .map(|arg| (*arg).to_owned())
    .collect();
    install_sigint();
    let mut sender = send_sigint_after("1.0");
    let started = Instant::now();
    let (code, out, err) = run_cli(&TestHost::new(&pairs, &root_path), &args);
    let elapsed = started.elapsed();
    assert!(sender.wait().unwrap().success());
    // 旧版: 最初の `KeyboardInterrupt` で子を 0.25 秒だけ待ち、`kill` して、捕まえずに終わる(U4: 最後の行は `KeyboardInterrupt`)。
    let text = String::from_utf8_lossy(&err).into_owned();
    assert_eq!((code, out.is_empty()), (1, true), "{text}");
    assert!(text.ends_with("KeyboardInterrupt\n"), "{text}");
    assert!(
        elapsed >= Duration::from_millis(1200) && elapsed < Duration::from_secs(10),
        "{elapsed:?}"
    );
    let pid: i32 = std::fs::read_to_string(format!("{root}/probe/pid"))
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(pid_is_gone(pid), "子が残っている: {pid}");
}

#[cfg(target_os = "linux")]
#[test]
fn sigint_while_verify_pair_waits_for_go_is_a_keyboard_interrupt_after_verified() {
    if !isolated("sigint_while_verify_pair_waits_for_go_is_a_keyboard_interrupt_after_verified") {
        return;
    }
    let (_dir, root_path, root) = bootstrapped("copy-sigint-pair", &[]);
    let pairs = base_pairs(&root);
    let state = format!("{root}/out/a/environment.json");
    let sha = sha_hex(&std::fs::read(&state).unwrap());
    let args: Vec<String> = [
        "verify-pair",
        "--state",
        &state,
        "--expect-sha256",
        &sha,
        "--current-inventory",
        "/nonexistent",
        "--guard-sha256",
        &sha_hex(GUARD_BYTES),
        "--go-timeout",
        "30",
    ]
    .iter()
    .map(|arg| (*arg).to_owned())
    .collect();
    install_sigint();
    let mut sender = send_sigint_after("1.0");
    let started = Instant::now();
    let feed = Feed {
        chunks: vec![],
        gap: Duration::ZERO,
        delay: Duration::ZERO,
        close: false,
    };
    let (code, out, err) = run_pair(&pairs, &root_path, &args, feed, None);
    assert!(sender.wait().unwrap().success());
    let text = String::from_utf8_lossy(&err).into_owned();
    assert_eq!((code, out.as_slice()), (1, &b"verified\n"[..]), "{text}");
    assert!(text.ends_with("KeyboardInterrupt\n"), "{text}");
    assert!(started.elapsed() < Duration::from_secs(10));
}

#[cfg(target_os = "linux")]
#[test]
fn preparing_the_process_twice_keeps_sigint_a_keyboard_interrupt() {
    if !isolated("preparing_the_process_twice_keeps_sigint_a_keyboard_interrupt") {
        return;
    }
    use dev_workflow_platform::environment_guard as os;
    os::signal_set_default(os::SIGINT).unwrap();
    // 比較用の隠しコマンドの入口が先に呼び、`run_process` がもう一度呼ぶ。2 回目も同じ答え(`SIGINT` を `KeyboardInterrupt` にする)。
    assert!(super::prepare_process());
    assert!(super::prepare_process());
}

/// 設定の段階の Fatal の診断は、親から受け継いだ `SIGPIPE` の扱いのまま書く(旧 Python は初期化の前に診断を書く)。
/// 標準エラーを読む側の閉じたパイプにした別プロセスで、`config_fatal` を呼ぶ。親が無視にして起動したときは書込みが失敗して
/// 終了コード 1、既定で起動したときは信号 13 で終わる。
#[cfg(target_os = "linux")]
#[test]
fn config_fatal_keeps_the_inherited_sigpipe_disposition() {
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Command, Stdio};
    if std::env::var_os("S2E_CONFIG_FATAL_CHILD").is_some() {
        // 孫の中: 標準エラーは、親が読む側を閉じてから渡したパイプ。すぐ診断を書く。
        std::process::exit(i32::from(super::config_fatal(
            super::startup::HASH_SEED_FATAL,
        )));
    }
    // 同じプロセスの別の検査のスレッドが `fork` すると、読む側の複製を持った子が生まれて、読む側が残る。
    // その窓を無くすため、この検査だけを流す別プロセス(1 スレッド)の中でパイプを作り、孫を起動する。
    if !isolated("config_fatal_keeps_the_inherited_sigpipe_disposition") {
        return;
    }
    let exe = std::env::current_exe().unwrap();
    let test_args = [
        "--exact",
        "environment_guard::copy_tests::config_fatal_keeps_the_inherited_sigpipe_disposition",
        "--nocapture",
        "--test-threads=1",
    ];
    for ignored in [false, true] {
        // 無視で起動するときは、`sh` が `SIGPIPE` を無視にしてから `exec` する(無視は `exec` で受け継がれる)。
        // 既定で起動するときは、標準ライブラリの `Command` が既定へ戻して起動する。
        let mut command = if ignored {
            let mut command = Command::new("/bin/sh");
            command
                .args(["-c", "trap '' PIPE; exec \"$0\" \"$@\""])
                .arg(&exe);
            command
        } else {
            Command::new(&exe)
        };
        // 読む側は `spawn` の前に閉じる(書く側だけを子の標準エラーにする)。
        let (reader, writer) = std::io::pipe().unwrap();
        drop(reader);
        let mut child = command
            .args(test_args)
            .env("S2E_CONFIG_FATAL_CHILD", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(writer)
            .spawn()
            .unwrap();
        // `command` が書く側を持ち続けないように手放す(子の標準エラーは `spawn` で複製済み)。
        drop(command);
        let status = child.wait().unwrap();
        if ignored {
            assert_eq!(status.code(), Some(1), "無視で起動: {status:?}");
        } else {
            assert_eq!(status.signal(), Some(13), "既定で起動: {status:?}");
        }
    }
}

#[test]
fn the_held_hash_of_the_judge_is_looked_up_like_the_old_python() {
    use super::copy::held_sha;
    use super::pyops::text;
    use super::pyvalue::{PyDict, PyValue};
    let key = "plugin/skills/ship-task/scripts/loop-permission.py";
    let given = super::pystr::cps("skills/ship-task/scripts/loop-permission.py");
    let state = |entry: PyValue| {
        let mut entries = PyDict::new();
        entries.insert(PyValue::from(key), entry).unwrap();
        let mut state = PyDict::new();
        state.insert_str("entries", PyValue::Dict(entries));
        state
    };
    let kind =
        |result: Result<PyValue, PyException>| result.map(|_| ()).map_err(|error| error.type_name);
    let hash = text(super::pystr::cps(&"a".repeat(64)));
    let entry = PyValue::List(vec![
        PyValue::from("file"),
        PyValue::from(420),
        hash.clone(),
    ]);
    assert!(held_sha(&state(entry), &given).unwrap().py_eq(&hash));
    // 旧版の `value['entries']['plugin/…'][2]` の失敗(欠け・短い・辞書・数)は、型名まで同じ。
    assert_eq!(
        kind(held_sha(&PyDict::new(), &given)),
        Err("KeyError".to_owned())
    );
    let other = super::pystr::cps("skills/other.py");
    assert_eq!(
        kind(held_sha(&state(PyValue::List(vec![])), &other)),
        Err("KeyError".to_owned())
    );
    assert_eq!(
        kind(held_sha(
            &state(PyValue::List(vec![PyValue::from(1)])),
            &given
        )),
        Err("IndexError".to_owned())
    );
    assert_eq!(
        kind(held_sha(&state(PyValue::Dict(PyDict::new())), &given)),
        Err("KeyError".to_owned())
    );
    assert_eq!(
        kind(held_sha(&state(PyValue::from(5)), &given)),
        Err("TypeError".to_owned())
    );
}
