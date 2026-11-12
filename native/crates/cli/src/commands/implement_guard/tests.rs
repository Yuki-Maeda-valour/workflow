//! S3I-A の単体テスト。`cargo test -p dev-workflow-cli --example implement_guard_contract` で流れる。
//! - 失敗の場所の表の各行が、固定した旧版のその行番号の行にあること。
//! - 基準の容器で測った固定期待(`native/ci/implement_guard_fixtures/`)との一致。
//! - 差し込み点(`Shell::hook`)で旧版と同じ地点の失敗と信号を起こし、同じ出力・終了値になること。
//! - 罠の信号の記録はプロセス全体の状態なので、使う件は `serial` で 1 件ずつ流す。プロセスの信号の
//!   扱いは変えない(`Shell::real_signals = false`)。
use super::childenv::{self, ScriptValues, Startup};
use super::meta::fields;
use super::paths::{norm_rest, split_path};
use super::shell::{Exit, Part, Shell, Sub};
use super::sites;
use super::stdio::Io;
use super::taskmd_diff::normalize_line;
use super::text::{self, printf_escape, quote, quoteaf};
use dev_workflow_platform::implement_guard as os;
use sha2::{Digest, Sha256};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::io;
use std::rc::Rc;
use std::sync::Mutex;

static SERIAL: Mutex<()> = Mutex::new(());

fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

const OLD: &[u8] = include_bytes!(
    "../../../../../../plugins/dev-workflow/skills/do-task/scripts/implement-guard.sh"
);
const CUNQUOTE: &str =
    include_str!("../../../../../ci/implement_guard_fixtures/cunquote_printf.tsv");
const QUOTING: &str = include_str!("../../../../../ci/implement_guard_fixtures/quoting.tsv");

fn hex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

#[test]
fn sites_match_the_old_script() {
    let digest: String = Sha256::digest(OLD)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(
        digest,
        "b958cc12b60c26364d3eff11d9f31a98b532363d9556221f7e58b8d2a50a6981"
    );
    let lines: Vec<&[u8]> = OLD.split(|b| *b == b'\n').collect();
    for site in sites::ALL {
        let line = lines[site.line as usize - 1];
        let found = line
            .windows(site.text.len())
            .any(|w| w == site.text.as_bytes());
        assert!(
            found,
            "{} (行 {}) に字面が無い: {}",
            site.name, site.line, site.text
        );
        assert!(site.err_line >= site.line);
    }
    // 名前は重ならない。
    let mut names: Vec<&str> = sites::ALL.iter().map(|s| s.name).collect();
    names.sort();
    names.dedup();
    assert_eq!(names.len(), sites::ALL.len());
}

#[test]
fn usage_is_the_old_header() {
    let marker = b"\n# --- end usage ---\n";
    let end = OLD.windows(marker.len()).position(|w| w == marker).unwrap();
    let start = OLD.iter().position(|b| *b == b'\n').unwrap() + 1;
    assert_eq!(super::args::USAGE, &OLD[start..=end]);
}

#[test]
fn cunquote_printf_matches_measured_bash() {
    let mut rows = 0;
    for line in CUNQUOTE.lines().filter(|l| !l.starts_with('#')) {
        let parts: Vec<&str> = line.split('\t').collect();
        let (oct, rc, value, err) = (hex(parts[0]), parts[1], hex(parts[2]), hex(parts[3]));
        let (got, note) = printf_escape(&oct);
        assert_eq!(got, value, "値: {:?}", String::from_utf8_lossy(&oct));
        match note {
            Some(note) => {
                // `%(` の誤りの文の 1 文字は、旧版が書式の後ろのメモリを読むので実行ごとに変わる(伏せて比べる。
                // 22:31 の決定の「実行ごとに変わる出力」の範囲として採った)。
                let masked = |t: &[u8]| -> Vec<u8> {
                    if t.ends_with(b"': invalid time format specification\n") && t.len() > 10 {
                        let mut t = t.to_vec();
                        t[10] = b'?';
                        t
                    } else {
                        t.to_vec()
                    }
                };
                assert_eq!(
                    masked(&note.text),
                    masked(&err),
                    "文: {:?}",
                    String::from_utf8_lossy(&oct)
                );
                assert_eq!(
                    note.failed,
                    rc == "1",
                    "終了値: {:?}",
                    String::from_utf8_lossy(&oct)
                );
            }
            None => {
                assert!(
                    err.is_empty(),
                    "文が無い: {:?}",
                    String::from_utf8_lossy(&oct)
                );
                assert_eq!(rc, "0");
            }
        }
        rows += 1;
    }
    assert_eq!(rows, 13692);
}

#[test]
fn quoting_matches_measured_coreutils() {
    let mut rows = 0;
    for line in QUOTING.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        let name = hex(parts[0]);
        assert_eq!(quote(&name), hex(parts[1]), "quote: {name:?}");
        assert_eq!(quoteaf(&name), hex(parts[2]), "quoteaf: {name:?}");
        rows += 1;
    }
    assert_eq!(rows, 285);
}

#[test]
fn cunquote_follows_the_old_function() {
    // 前後の `"` を 1 つずつだけ外す。`\` の後ろが知らない文字なら `\` を落とす。
    assert_eq!(
        text::cunquote_all(b"\"/a\\tb\\\\c\\\"\"").value,
        b"/a\tb\\c\""
    );
    assert_eq!(text::cunquote_all(b"\"\"x\"\"").value, b"\"x\"");
    assert_eq!(text::cunquote_all(b"\"/a\\qb\"").value, b"/aqb");
    assert_eq!(text::cunquote_all(b"\"/a\\\"").value, b"/a");
    // \NNN は後ろ 3 文字を printf の書式として解く(NUL で切れる)。
    assert_eq!(text::cunquote_all(b"\"/a\\012b\"").value, b"/a\nb");
    assert_eq!(text::cunquote_all(b"\"/a\\000b\"").value, b"/ab");
    let stopped = text::cunquote_errexit(b"\"/a\\7%zb\\001\"");
    assert_eq!(stopped.value, b"/a\x07");
    assert_eq!(stopped.notes.len(), 1);
    assert!(stopped.notes[0].failed);
    let continued = text::cunquote_all(b"\"/a\\7%zb\\001\"");
    assert_eq!(continued.value, b"/a\x07b\x01");
}

#[test]
fn path_field_quotes_like_the_old_shell() {
    assert_eq!(text::path_field(b"/plain/\xff"), b"/plain/\xff");
    assert_eq!(
        text::path_field(b"/a\n\"\\\x01\x7f\x07\x08\t\x0b\x0c\r\xfe"),
        b"\"/a\\n\\\"\\\\\\001\\177\\a\\b\\t\\v\\f\\r\xfe\""
    );
}

#[test]
fn read_fields_follow_bash_tab_ifs() {
    assert_eq!(fields(b"k\tv", 2), [b"k".to_vec(), b"v".to_vec()]);
    assert_eq!(
        fields(b"\t\tk\t\tv1\t\tv2\t", 2),
        [b"k".to_vec(), b"v1\t\tv2".to_vec()]
    );
    assert_eq!(fields(b"k", 2), [b"k".to_vec(), Vec::new()]);
    assert_eq!(
        fields(b"T\t644\tsha\t/p\textra", 5),
        [
            b"T".to_vec(),
            b"644".to_vec(),
            b"sha".to_vec(),
            b"/p".to_vec(),
            b"extra".to_vec()
        ]
    );
    assert_eq!(fields(b"T\t644\tsha\t/p\tx\ty", 4)[3], b"/p\tx\ty".to_vec());
}

#[test]
fn shell_level_follows_bash() {
    // 基準の容器の実測: 受け継いだ値 → 起動の値 → exec で子へ渡る値。
    for (inherited, start, child) in [
        (Some(&b"  7 "[..]), 8, 7),
        (Some(b"-5"), 0, 0),
        (Some(b"4294967296"), 1, 0),
        (Some(b"2147483647"), 0, 0),
        (Some(b"999"), 1, 0),
        (Some(b"abc"), 1, 0),
        (Some(b""), 1, 0),
        (None, 1, 0),
    ] {
        let (level, _) = childenv::adjust_shell_level(inherited, 1);
        assert_eq!(level, start, "{inherited:?}");
        let (after, _) = childenv::adjust_shell_level(Some(level.to_string().as_bytes()), -1);
        assert_eq!(after, child, "{inherited:?}");
    }
    assert_eq!(childenv::adjust_shell_level(Some(b"999"), 1).1, Some(1000));
}

#[test]
fn split_and_normalize_like_the_old_functions() {
    assert_eq!(split_path(b"/"), (b"/".to_vec(), Vec::new()));
    assert_eq!(split_path(b"/x"), (b"/".to_vec(), b"x".to_vec()));
    assert_eq!(split_path(b"a/b/c"), (b"a/b".to_vec(), b"c".to_vec()));
    assert_eq!(split_path(b"a/b/"), (b"a/b".to_vec(), Vec::new()));
    assert_eq!(split_path(b"a/"), (b".".to_vec(), b"a/".to_vec()));
    assert_eq!(split_path(b"a"), (b".".to_vec(), b"a".to_vec()));
    assert_eq!(norm_rest(b"a/./b/../c"), Some(b"a/c".to_vec()));
    assert_eq!(norm_rest(b"a/../.."), None);
    assert_eq!(norm_rest(b""), Some(Vec::new()));
}

#[test]
fn checks_normalization_matches_norm_sed() {
    for (before, after) in [
        (&b"- [x] a\n"[..], &b"- [ ] a\n"[..]),
        (b"  *\t[X] a", b"  *\t[ ] a"),
        (b"-[x] a", b"-[x] a"),
        (b"a - [x]", b"a - [x]"),
        (b"- [x] [x]", b"- [ ] [x]"),
        (b"- [xx]", b"- [xx]"),
        (b"+  [x]", b"+  [ ]"),
    ] {
        let mut line = before.to_vec();
        normalize_line(&mut line);
        assert_eq!(line, after, "{:?}", String::from_utf8_lossy(before));
    }
}

#[test]
fn child_environment_follows_bash_import_rules() {
    let startup = Startup {
        raw: [
            &b"a-b=1"[..],
            b"a-b=2",
            b"noequal",
            b"=empty",
            b"DUP=1",
            b"DUP=2",
            b"_=x",
            b"RANDOM=1",
            b"GIT_DIR=/x",
            b"IFS=x",
            b"SUB=x",
            b"META=x",
            b"KEEP=y",
            b"SHLVL=5",
            b"LC_ALL=fr",
            b"OLDPWD=/nonexistent",
        ]
        .iter()
        .map(|e| e.to_vec())
        .collect(),
        shell_level: 6,
        pwd: Some(b"/somewhere".to_vec()),
        cwd: None,
        bash: b"/bin/bash".to_vec(),
        root: false,
    };
    let script = ScriptValues {
        values: vec![("STATE", b"/s".to_vec())],
    };
    let entries = childenv::child_environment(&startup, &script, (1, 2));
    let want: Vec<Vec<u8>> = [
        &b"DUP=2"[..],
        b"GIT_NO_LAZY_FETCH=1",
        b"GIT_OPTIONAL_LOCKS=0",
        b"IFS= \t\n",
        b"KEEP=y",
        b"LC_ALL=C",
        b"PWD=/somewhere",
        b"SHLVL=5",
        b"SUB=restore-taskmd",
        b"a-b=2",
    ]
    .iter()
    .map(|e| e.to_vec())
    .collect();
    assert_eq!(entries, want);
}

// ---- 出力の再現(偽の書き込み先) ----

/// 偽の書き込み先。fd ごとに書けたバイト列と、`write` ごとの結果の予定を持つ。
#[derive(Default)]
struct Fake {
    written: [Vec<u8>; 3],
    calls: Vec<(i32, Vec<u8>)>,
    plan: VecDeque<(i32, Option<i32>)>,
    blksize: i64,
}

#[derive(Clone)]
struct FakeIo(Rc<RefCell<Fake>>);

impl Io for FakeIo {
    fn write(&mut self, fd: i32, data: &[u8]) -> io::Result<usize> {
        let mut fake = self.0.borrow_mut();
        fake.calls.push((fd, data.to_vec()));
        let failure = match fake.plan.front() {
            Some((planned, failure)) if *planned == fd => {
                let failure = *failure;
                fake.plan.pop_front();
                failure
            }
            _ => None,
        };
        if let Some(errno) = failure {
            return Err(io::Error::from_raw_os_error(errno));
        }
        if let Some(slot) = fake.written.get_mut(fd as usize) {
            slot.extend_from_slice(data);
        }
        Ok(data.len())
    }

    fn blksize(&mut self, _fd: i32) -> Option<i64> {
        Some(self.0.borrow().blksize)
    }
}

fn fake_shell(sub: Sub) -> (Shell, Rc<RefCell<Fake>>) {
    let fake = Rc::new(RefCell::new(Fake {
        blksize: 4096,
        ..Fake::default()
    }));
    let mut shell = Shell::new(Box::new(FakeIo(fake.clone())));
    shell.sub = Some(sub);
    shell.real_signals = false;
    shell.trapped = [true; 3];
    os::clear_traps();
    (shell, fake)
}

#[test]
fn printf_writes_one_line_per_write() {
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::TaskmdDiff);
    assert_eq!(
        shell.printf(
            sites::SAME_PRINTF,
            &[
                Part::Text(b"TASKMD=same\nDOD="),
                Part::Arg(b"/p"),
                Part::Text(b"\n")
            ]
        ),
        Ok(true)
    );
    let calls: Vec<Vec<u8>> = fake.borrow().calls.iter().map(|c| c.1.clone()).collect();
    assert_eq!(calls, [b"TASKMD=same\n".to_vec(), b"DOD=/p\n".to_vec()]);
}

#[test]
fn eintr_drops_the_line_and_the_arg_clears_the_error() {
    // 設計の実測: taskmd-diff の printf は TASKMD=same の行が消え、DOD の行だけが出る(write error の行は無い)。
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::TaskmdDiff);
    fake.borrow_mut()
        .plan
        .push_back((1, Some(os::errno::EINTR)));
    os::simulate_trap(os::SIGTERM);
    let result = shell.printf(
        sites::SAME_PRINTF,
        &[
            Part::Text(b"TASKMD=same\nDOD="),
            Part::Arg(b"/p"),
            Part::Text(b"\n"),
        ],
    );
    assert_eq!(result, Err(Exit::Code(20)));
    assert_eq!(fake.borrow().written[1], b"DOD=/p\n");
    assert_eq!(fake.borrow().written[2], b"");
    os::clear_traps();
}

#[test]
fn eintr_in_a_plain_printf_reports_then_on_signal_prints() {
    // 設計の実測: restore の digest の失敗の printf は RESTORED=no の行が消え、
    // `line 2259: printf: write error: Interrupted system call` の後に on_signal の 4 行。
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::RestoreTaskmd);
    fake.borrow_mut()
        .plan
        .push_back((1, Some(os::errno::EINTR)));
    os::simulate_trap(os::SIGTERM);
    let result = shell.printf(
        sites::STATE_FAIL_RESTORE,
        &[Part::Text(
            b"RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=digest\n",
        )],
    );
    assert_eq!(result, Err(Exit::Code(20)));
    let fake = fake.borrow();
    assert_eq!(
        fake.written[1],
        b"TOUCHED=none\nTEMP=none\nREASON=digest\nRESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=interrupted\n"
    );
    assert_eq!(
        fake.written[2],
        b"implement-guard.sh: line 2259: printf: write error: Interrupted system call\n"
    );
    os::clear_traps();
}

#[test]
fn stdout_failure_reports_and_runs_the_err_trap() {
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::TaskmdDiff);
    fake.borrow_mut()
        .plan
        .push_back((1, Some(os::errno::ENOSPC)));
    let result = shell.printf(sites::STATE_FAIL_TASKMD, &[Part::Text(b"REASON=digest\n")]);
    assert_eq!(result, Err(Exit::Code(20)));
    assert_eq!(
        String::from_utf8_lossy(&fake.borrow().written[2]),
        "implement-guard.sh: line 2258: printf: write error: No space left on device\n\
         ERROR [internal] 予期しない失敗(終了コード 1・行 2258)\n"
    );
}

#[test]
fn stderr_failure_ends_with_one() {
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::Cleanup);
    for _ in 0..4 {
        fake.borrow_mut()
            .plan
            .push_back((2, Some(os::errno::ENOSPC)));
    }
    let result = shell.echo_err(sites::CLEANUP_NOTE, b"NOTE: x");
    assert_eq!(result, Err(Exit::Code(1)));
}

#[test]
fn long_lines_follow_the_glibc_buffer() {
    let _guard = serial();
    let (mut shell, fake) = fake_shell(Sub::TaskmdDiff);
    let long = vec![b'a'; 9000];
    let _ = shell.echo_err(sites::ARG_ERROR_ECHO, &long);
    let sizes: Vec<usize> = fake.borrow().calls.iter().map(|c| c.1.len()).collect();
    // 基準の容器の実測(引数の誤りの 9044 バイトの行が 4096・4096・852)と同じ割り方。
    assert_eq!(sizes, [4096, 4096, 809]);
}

#[test]
fn errno_text_is_the_generated_table() {
    assert_eq!(super::strerror(os::errno::EINTR), "Interrupted system call");
    assert_eq!(super::strerror(os::errno::EACCES), "Permission denied");
}

fn fixture_names(text: &str) -> Vec<String> {
    let value: serde_json::Value = serde_json::from_str(text).unwrap();
    value["names"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n.as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn not_reproduced_names_follow_decision_4() {
    // 比較器が固定する 6 個の一覧と同じで、再現する名前の一覧と重ならない。
    let not_reproduced = fixture_names(include_str!(
        "../../../../../ci/implement_guard_fixtures/script_env_not_reproduced.json"
    ));
    let reproduced = fixture_names(include_str!(
        "../../../../../ci/implement_guard_fixtures/script_env_reproduced.json"
    ));
    assert_eq!(not_reproduced, childenv::NOT_REPRODUCED);
    assert!(
        reproduced
            .iter()
            .all(|n| !childenv::NOT_REPRODUCED.contains(&n.as_str()))
    );
    assert!(reproduced.iter().any(|n| n == "SP_BASE"));
    // 受け継いだ値はそのまま、受け継いでいなければ無い。SP_BASE は旧スクリプトのファイル名。
    let startup = Startup {
        raw: vec![
            b"CAP=given".to_vec(),
            b"helper=a=b".to_vec(),
            b"SP_BASE=x".to_vec(),
        ],
        shell_level: 1,
        ..Startup::default()
    };
    let script = ScriptValues::default();
    let entries = childenv::child_environment(&startup, &script, (1, 2));
    assert!(entries.contains(&b"CAP=given".to_vec()));
    assert!(entries.contains(&b"helper=a=b".to_vec()));
    assert!(entries.contains(&b"SP_BASE=implement-guard.sh".to_vec()));
    for name in ["RP", "SP_DIR", "helper_dir", "python_path"] {
        let prefix = format!("{name}=");
        assert!(
            !entries.iter().any(|e| e.starts_with(prefix.as_bytes())),
            "{name}"
        );
    }
}

#[test]
fn boundary_code_is_not_an_old_exit_code() {
    // 境界の印の終了値 99 は、固定した旧版(sha256 b958cc12…)のどの終わり方にも無い。
    const OLD: &[u8] = include_bytes!(
        "../../../../../../plugins/dev-workflow/skills/do-task/scripts/implement-guard.sh"
    );
    assert_eq!(
        sha_hex(OLD),
        "b958cc12b60c26364d3eff11d9f31a98b532363d9556221f7e58b8d2a50a6981"
    );
    for needle in [&b"exit 99"[..], b"return 99", b"code=99", b"rc=99"] {
        assert!(
            !OLD.windows(needle.len()).any(|w| w == needle),
            "旧版に {:?} がある",
            String::from_utf8_lossy(needle)
        );
    }
    let _guard = serial();
    let (mut shell, _) = fake_shell(Sub::Take);
    assert_eq!(shell.finish(99), Exit::Code(99));
}

// ---- 旧版を測った固定期待(道具の地点の信号・LD_PRELOAD の故障)と同じ地点を差し込み点で起こす ----

const EXPECTED_OLD: &str =
    include_str!("../../../../../ci/implement_guard_fixtures/expected_old.json");

/// 単体テストの配置(旧版の take の記録と同じ形の最小の記録を、ファイルで作る)。
struct World {
    base: std::path::PathBuf,
    repo: Vec<u8>,
    xdg: Vec<u8>,
    state: Vec<u8>,
    real: Vec<u8>,
    man: String,
    snap: String,
}

impl Drop for World {
    fn drop(&mut self) {
        let _ = std::process::Command::new("chmod")
            .args(["-R", "u+rwX"])
            .arg(&self.base)
            .status();
        let _ = std::fs::remove_dir_all(&self.base);
    }
}

fn bytes_of(path: &std::path::Path) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt;
    path.as_os_str().as_bytes().to_vec()
}

fn sha_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

static WORLDS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn make_world() -> World {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let home = std::env::var_os("HOME").expect("HOME");
    let n = WORLDS.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let base =
        std::path::PathBuf::from(home).join(format!(".s3ia-unit-{}-{n}", std::process::id()));
    let base = {
        std::fs::create_dir_all(&base).unwrap();
        std::fs::canonicalize(&base).unwrap()
    };
    let repo = base.join("repo");
    std::fs::create_dir_all(repo.join("task")).unwrap();
    let task = repo.join("task/t.md");
    let text = b"# t\n- [ ] one\n- [ ] two\nbody\n";
    std::fs::write(&task, text).unwrap();
    std::fs::set_permissions(&task, std::fs::Permissions::from_mode(0o644)).unwrap();
    let xdg = base.join("xdg");
    let place = xdg.join("dev-workflow");
    let state = place.join("guard-fixed");
    std::fs::create_dir_all(state.join("snapshot")).unwrap();
    for dir in [&place, &state, &state.join("snapshot")] {
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let real = bytes_of(&task);
    let sha = sha_hex(text);
    let body = state.join("taskmd-body");
    std::fs::write(&body, text).unwrap();
    std::fs::set_permissions(&body, std::fs::Permissions::from_mode(0o644)).unwrap();
    let mut manifest = format!("T\t644\t{sha}\t").into_bytes();
    manifest.extend_from_slice(&real);
    manifest.extend_from_slice(format!("\nf\t644\t{sha}\ttask/t.md\n").as_bytes());
    std::fs::write(state.join("manifest.tsv"), &manifest).unwrap();
    let repo_b = bytes_of(&repo);
    let mut meta = Vec::new();
    for (key, value) in [
        ("toplevel", repo_b.clone()),
        ("cwd", repo_b.clone()),
        ("exclude", b".claude/reviews".to_vec()),
        ("head", b"0".to_vec()),
        ("index-tree", b"0".to_vec()),
        ("branch", b"refs/heads/master".to_vec()),
        ("hooks-path", [repo_b.as_slice(), b"/.git/hooks"].concat()),
        ("config-path", [repo_b.as_slice(), b"/.git/config"].concat()),
        (
            "config-worktree-path",
            [repo_b.as_slice(), b"/.git/config.worktree"].concat(),
        ),
    ] {
        meta.extend_from_slice(key.as_bytes());
        meta.push(b'\t');
        meta.extend_from_slice(&value);
        meta.push(b'\n');
    }
    let snapshot = state.join("snapshot");
    std::fs::write(snapshot.join("4-meta.txt"), &meta).unwrap();
    let mut taskmd = b"selected\t".to_vec();
    taskmd.extend_from_slice(&real);
    taskmd.extend_from_slice(b"\nreal\t");
    taskmd.extend_from_slice(&real);
    taskmd.extend_from_slice(b"\nkind\tfile\n");
    std::fs::write(snapshot.join("taskmd.txt"), &taskmd).unwrap();
    for name in [
        "1-diff.bin",
        "2-status.z",
        "5-stash.txt",
        "6-contexts.txt",
        "config-contexts.txt",
        "config-origins.txt",
    ] {
        std::fs::write(snapshot.join(name), b"").unwrap();
    }
    let info = std::fs::metadata(&state).unwrap();
    std::fs::write(
        place.join("owner-fixed"),
        format!(
            "version\t1\nguard_dev\t{}\nguard_ino\t{}\n",
            info.dev(),
            info.ino()
        ),
    )
    .unwrap();
    let man = sha_hex(&manifest);
    let mut names: Vec<_> = std::fs::read_dir(&snapshot)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    let mut listing = Vec::new();
    for name in names {
        listing
            .extend_from_slice(sha_hex(&std::fs::read(snapshot.join(&name)).unwrap()).as_bytes());
        listing.extend_from_slice(b"  ");
        listing.extend_from_slice(name.as_bytes());
        listing.push(b'\n');
    }
    let snap = sha_hex(&listing);
    World {
        base,
        repo: repo_b,
        xdg: bytes_of(&xdg),
        state: bytes_of(&state),
        real,
        man,
        snap,
    }
}

fn context_for(world: &World) -> super::Context {
    let mut context = super::Context::default();
    context.args.cwd = world.repo.clone();
    context.args.state = world.state.clone();
    context.args.manifest_sha256 = world.man.as_bytes().to_vec();
    context.args.snapshot_sha256 = world.snap.as_bytes().to_vec();
    context.places = super::state::Places {
        xdg_state_home: Some(world.xdg.clone()),
        home: None,
        tmpdir: None,
    };
    context.path = std::env::var_os("PATH").map(|p| bytes_of(std::path::Path::new(&p)));
    context.startup.cwd = Some(world.repo.clone());
    context
}

/// 1 つの流れを差し込み点つきで流す。終了値・stdout・stderr を返す。
fn run_scenario(
    scenario: &str,
    world: &World,
    hook: super::shell::Hook,
) -> (i32, Vec<u8>, Vec<u8>) {
    let (code, out, err, _) = run_scenario_traced(scenario, world, hook);
    (code, out, err)
}

/// `run_scenario` と同じ。罠の信号を確かめた区切りの種類も返す。
fn run_scenario_traced(
    scenario: &str,
    world: &World,
    hook: super::shell::Hook,
) -> (i32, Vec<u8>, Vec<u8>, Option<&'static str>) {
    let sub = match scenario {
        "restore-taskmd" => Sub::RestoreTaskmd,
        "cleanup" => Sub::Cleanup,
        _ => Sub::TaskmdDiff,
    };
    match scenario {
        "taskmd-diff checks-only" => {
            std::fs::write(path_of(&world.real), b"# t\n- [x] one\n- [ ] two\nbody\n").unwrap();
        }
        "restore-taskmd" => {
            std::fs::write(path_of(&world.real), b"changed\n").unwrap();
        }
        "taskmd-diff body-changed" => {
            std::fs::write(path_of(&world.real), b"# t\n- [ ] one\nother body\n").unwrap();
        }
        _ => {}
    }
    let (mut shell, fake) = fake_shell(sub);
    shell.hook = Some(hook);
    let mut context = context_for(world);
    let flow = match sub {
        Sub::RestoreTaskmd => super::restore::run(&mut shell, &mut context).map(|_| ()),
        Sub::Cleanup => super::cleanup::run(&mut shell, &mut context),
        _ => super::taskmd_diff::run(&mut shell, &mut context),
    };
    let code = match flow {
        Ok(()) => match shell.finish(0) {
            Exit::Code(code) => i32::from(code),
            Exit::Signal(signal) => -signal,
        },
        Err(Exit::Code(code)) => i32::from(code),
        Err(Exit::Signal(signal)) => -signal,
    };
    shell.flush_all();
    os::clear_traps();
    let boundary = shell.signal_boundary;
    let fake = fake.borrow();
    (
        code,
        fake.written[1].clone(),
        fake.written[2].clone(),
        boundary,
    )
}

fn path_of(bytes: &[u8]) -> &std::path::Path {
    use std::os::unix::ffi::OsStrExt;
    std::path::Path::new(std::ffi::OsStr::from_bytes(bytes))
}

fn placed(text: &str, world: &World) -> Vec<u8> {
    let state = String::from_utf8_lossy(&world.state).into_owned();
    let parent = state
        .rsplit_once('/')
        .map(|(p, _)| p.to_string())
        .unwrap_or_default();
    let base = world.base.to_string_lossy().into_owned();
    text.replace("<state>", &state)
        .replace("<parent>", &parent)
        .replace("<base>", &base)
        .into_bytes()
}

fn work_left(world: &World) -> usize {
    std::fs::read_dir(path_of(&world.state))
        .map(|entries| {
            entries
                .filter(|e| {
                    e.as_ref()
                        .is_ok_and(|e| e.file_name().to_string_lossy().starts_with(".work-"))
                })
                .count()
        })
        .unwrap_or(0)
}

/// Rust の結果を、固定期待の書き方(パスは `<state>`・`<parent>`・`<base>`)にする。
fn outcome_value(world: &World, code: i32, out: &[u8], err: &[u8]) -> serde_json::Value {
    let state = String::from_utf8_lossy(&world.state).into_owned();
    let parent = state
        .rsplit_once('/')
        .map(|(p, _)| p.to_string())
        .unwrap_or_default();
    let base = world.base.to_string_lossy().into_owned();
    let place = |data: &[u8]| {
        String::from_utf8_lossy(data)
            .replace(&state, "<state>")
            .replace(&parent, "<parent>")
            .replace(&base, "<base>")
    };
    let mark = std::path::Path::new(&parent).join("owner-fixed");
    serde_json::json!({
        "rc": code,
        "stdout": place(out),
        "stderr": place(err),
        "work_left": work_left(world),
        "state_exists": path_of(&world.state).is_dir(),
        "mark_exists": std::fs::symlink_metadata(mark).is_ok(),
        "timed_out": false,
    })
}

/// 1 つの地点で、罠の信号を道具の直後(`defer` が偽)か次の区切り(真)で確かめた Rust の結果。
fn rust_signal_outcome(
    record: &serde_json::Value,
    defer: bool,
) -> (serde_json::Value, Option<&'static str>) {
    let point = record["rust_point"].as_str().unwrap().to_string();
    let occurrence = record["occurrence"].as_u64().unwrap() as usize;
    let signal = match record["signal"].as_str().unwrap() {
        "TERM" => os::SIGTERM,
        "INT" => os::SIGINT,
        _ => os::SIGHUP,
    };
    let value = if defer {
        -(super::shell::DEFER + signal)
    } else {
        -signal
    };
    let world = make_world();
    let mut seen = 0usize;
    let hook: super::shell::Hook = Box::new(move |name: &str| {
        if name == point {
            seen += 1;
            if seen == occurrence {
                return Some(value);
            }
        }
        None
    });
    let scenario = record["scenario"].as_str().unwrap();
    let (code, out, err, boundary) = run_scenario_traced(scenario, &world, hook);
    (outcome_value(&world, code, &out, &err), boundary)
}

fn record_key(record: &serde_json::Value) -> String {
    format!(
        "{}\t{}\t{}",
        record["scenario"].as_str().unwrap(),
        record["call"],
        record["signal"].as_str().unwrap()
    )
}

/// 決定の表の「旧 bash の信号」の規則の 2 通り: 罠が道具の直後に動いた結果(Rust の結果)と、
/// 次の組み込みの命令の後に動いた結果(次の区切りが組み込みの命令の後で、結果が違うときだけ)。
fn rule_outcomes(record: &serde_json::Value, repeat: usize) -> serde_json::Value {
    let label = record_key(record);
    let (tool, _) = rust_signal_outcome(record, false);
    for _ in 1..repeat {
        assert_eq!(
            rust_signal_outcome(record, false).0,
            tool,
            "{label}: Rust の結果が流すごとに変わる"
        );
    }
    let (later, boundary) = rust_signal_outcome(record, true);
    let next_builtin = (boundary == Some("builtin") && later != tool).then_some(later);
    serde_json::json!({"tool": tool, "next_builtin": next_builtin})
}

#[test]
fn exit_trap_rm_checks_trap_signals_on_every_exit_with_work() {
    // EXIT の罠の `rm -rf -- "$WORK"` の後に罠の信号を確かめる所は Shell::exit で、WORK を持つどの終わり方
    // (0・33・34・35・2・20)も同じ所を通る。body-changed(34)で、rm の最中の TERM が終了値 20 になる。
    let _guard = serial();
    let world = make_world();
    let hook: super::shell::Hook =
        Box::new(|name: &str| (name == "rm-work").then_some(-os::SIGTERM));
    let (code, out, err) = run_scenario("taskmd-diff body-changed", &world, hook);
    assert_eq!(code, 20);
    assert!(String::from_utf8_lossy(&out).starts_with("TASKMD=body-changed\n"));
    assert!(String::from_utf8_lossy(&err).contains("NOTE: タスク MD の要件本文が変わっている"));
    assert_eq!(work_left(&world), 0);
    // 信号が無ければ、そのまま 34。
    let world = make_world();
    let (code, _, _) = run_scenario("taskmd-diff body-changed", &world, Box::new(|_: &str| None));
    assert_eq!(code, 34);
}

#[test]
fn signals_at_tool_points_match_the_measured_old_version() {
    // 旧版の測定の固定期待(implement_guard_measure.py)の地点ごとに、規則の 2 通りを Rust で作り、
    // (1) 固定期待に書いた規則の 2 通りと同じ(固定期待が古くない)、(2) Rust の結果(道具の直後)が
    // 旧版で実際に出た結果のどれかと全部同じ、を確かめる。`S3IA_EXPORT_RULE_OUTCOMES` に書き先を
    // 渡すと、確かめずに規則の 2 通りを書き出す(固定期待を測り直す前の手順)。
    let _guard = serial();
    let data: serde_json::Value = serde_json::from_str(EXPECTED_OLD).unwrap();
    let repeat = data["rust_repeat"].as_u64().unwrap_or(3) as usize;
    let export = std::env::var_os("S3IA_EXPORT_RULE_OUTCOMES");
    let mut rules = serde_json::Map::new();
    let mut checked = 0;
    for record in data["signals"].as_array().unwrap() {
        if record["rust_point"].as_str().is_none() {
            // Rust に地点が無いのは、決定 D5 の道具(python3 の確かめと、python3 と helper の場所の
            // realpath)だけ。ほかの道具の地点を黙って飛ばさない。
            let tool = record["tool"].as_str().unwrap();
            let args = record["args"].as_str().unwrap();
            let d5 = (tool == "python3" && args.contains("run_name=\"restore_taskmd_check\""))
                || (tool == "realpath"
                    && (args.ends_with("/python3") || args.ends_with("/do-task/scripts")));
            assert!(
                d5,
                "{}: Rust の地点が無い記録が決定 D5 の道具でない: {tool} {args}",
                record_key(record)
            );
            continue;
        }
        let label = record_key(record);
        let rule = rule_outcomes(record, repeat);
        if export.is_some() {
            rules.insert(label, rule);
            continue;
        }
        assert_eq!(
            record["rule"], rule,
            "{label}: 固定期待の規則の 2 通りが今の Rust と違う(測り直す)"
        );
        let observed: Vec<serde_json::Value> = record["outcomes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| {
                let mut o = o.clone();
                let map = o.as_object_mut().unwrap();
                map.remove("count");
                map.remove("rule");
                o
            })
            .collect();
        assert!(
            observed.contains(&rule["tool"]),
            "{label}: Rust の結果 {} が、旧版で出た結果 {observed:?} のどれとも違う",
            rule["tool"]
        );
        checked += 1;
    }
    if let Some(path) = export {
        let text = serde_json::to_string_pretty(&serde_json::json!({
            "rust_repeat": repeat,
            "points": rules,
        }))
        .unwrap();
        std::fs::write(path, text + "\n").unwrap();
        return;
    }
    assert!(checked > 0);
}

#[test]
fn faults_match_the_measured_old_version() {
    // LD_PRELOAD で旧版に起こした失敗(mkdir・unlinkat・read・getdelim)と同じ地点を差し込み点で起こし、
    // 結果(終了値・stdout・stderr・`.work-*` の残り・記録と印の有無)が全部同じこと。
    let _guard = serial();
    let data: serde_json::Value = serde_json::from_str(EXPECTED_OLD).unwrap();
    let mut checked = 0;
    for record in data["faults"].as_array().unwrap() {
        let point = record["point"].as_str().unwrap();
        let errno = record["errno"].as_i64().unwrap() as i32;
        let spec = record["spec"].as_str().unwrap();
        let tail = spec.split_once(':').map(|(_, t)| t).unwrap_or("");
        let target = match point {
            "rm-unlink" | "rm-rmdir" => format!("fail:{point}:{tail}"),
            "read" | "read-sed" | "read-cmp" => {
                format!("fail:{point}:{}", tail.rsplit('/').next().unwrap_or(tail))
            }
            other => format!("fail:{other}"),
        };
        let world = make_world();
        let hook: super::shell::Hook =
            Box::new(move |name: &str| (name == target).then_some(errno));
        let scenario = record["scenario"].as_str().unwrap();
        let (code, out, err) = run_scenario(scenario, &world, hook);
        let label = record["fault"].as_str().unwrap().to_string();
        let got = outcome_value(&world, code, &out, &err);
        let mut want = serde_json::Map::new();
        for key in [
            "rc",
            "stdout",
            "stderr",
            "work_left",
            "state_exists",
            "mark_exists",
            "timed_out",
        ] {
            want.insert(key.to_string(), record[key].clone());
        }
        assert_eq!(got, serde_json::Value::Object(want), "{label}");
        checked += 1;
    }
    assert!(checked > 0);
}
