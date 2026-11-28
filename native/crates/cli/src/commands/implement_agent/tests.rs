//! R4X-1 の単体テスト。旧版との比較で見えない部分(論理の `cd` の値・EXIT の罠の時点・翻訳ファイルを探す順・
//! 組み込みの書き込みの誤りの文・訳した errno の文)と、入力からは届かない分岐(既定表の形の崩れ G1・G4)を確かめる。
//! 論理の `cd` の値は、基準の容器の bash 5.2.21 で測った固定期待。
use super::locale::{Catalog, Codeset, ToolLocale, catalog_candidates, select_languages};
use super::shell::{Exit, Phase, Shell};
use super::stdio::Io;
use super::*;
use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;

const OLD: &[u8] = include_bytes!(
    "../../../../../../plugins/dev-workflow/skills/do-task/scripts/implement-agent.sh"
);

/// 受けた信号の印はプロセスの全体で1つなので、Shell を使う単体テストは1つずつ流す。
static SIGNALS: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn lock() -> std::sync::MutexGuard<'static, ()> {
    let guard = SIGNALS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    os::clear_all_pending();
    guard
}

/// 書き込みを覚える偽の書き先。`fail` は (fd, 何回目の write から失敗するか, errno)。`raise` は、書くたびに
/// 受けたことにする信号。
#[derive(Default)]
struct Capture {
    out: Rc<RefCell<Vec<(i32, Vec<u8>)>>>,
    fail: Option<(i32, usize, i32)>,
    count: usize,
    raise: Option<i32>,
}

impl Io for Capture {
    fn write(&mut self, fd: i32, data: &[u8]) -> std::io::Result<usize> {
        if let Some(signal) = self.raise {
            os::simulate_signal(signal);
        }
        if let Some((target, from, errno)) = self.fail
            && target == fd
        {
            self.count += 1;
            if self.count >= from && self.count < from + 1 {
                return Err(std::io::Error::from_raw_os_error(errno));
            }
        }
        self.out.borrow_mut().push((fd, data.to_vec()));
        Ok(data.len())
    }

    fn blksize(&mut self, _fd: i32) -> Option<i64> {
        Some(4096)
    }
}

type Written = Rc<RefCell<Vec<(i32, Vec<u8>)>>>;

fn shell_with(
    fail: Option<(i32, usize, i32)>,
) -> (Shell, Written, std::sync::MutexGuard<'static, ()>) {
    shell_raising(fail, None)
}

fn shell_raising(
    fail: Option<(i32, usize, i32)>,
    raise: Option<i32>,
) -> (Shell, Written, std::sync::MutexGuard<'static, ()>) {
    let guard = lock();
    let out = Rc::new(RefCell::new(Vec::new()));
    let capture = Capture {
        out: out.clone(),
        fail,
        count: 0,
        raise,
    };
    let mut shell = Shell::new(Box::new(capture));
    shell.real_signals = false;
    shell.set_exit_trap();
    shell.err_trap = true;
    (shell, out, guard)
}

fn written(out: &Rc<RefCell<Vec<(i32, Vec<u8>)>>>, fd: i32) -> Vec<u8> {
    out.borrow()
        .iter()
        .filter(|(f, _)| *f == fd)
        .flat_map(|(_, d)| d.clone())
        .collect()
}

#[test]
fn usage_copy_matches_old_lines() {
    let lines: Vec<&[u8]> = OLD.split_inclusive(|b| *b == b'\n').collect();
    let expected: Vec<u8> = lines[1..98].concat();
    assert_eq!(args::USAGE, expected.as_slice());
    assert_eq!(lines[98], b"# --- end usage ---\n");
}

#[test]
fn site_table_matches_old_lines() {
    let lines: Vec<&[u8]> = OLD.split(|b| *b == b'\n').collect();
    for site in sites::ALL {
        let line = lines[site.line as usize - 1];
        let text = site.text.as_bytes();
        assert!(
            line.windows(text.len()).any(|w| w == text),
            "{}: 行 {} に {:?} が無い: {:?}",
            site.name,
            site.line,
            site.text,
            String::from_utf8_lossy(line)
        );
    }
}

#[test]
fn g1_start_command_without_write_flag_is_caught_by_argv_check() {
    // 旧 selftest の G1: 起動コマンドから書き込みフラグを落とすと、argv 照合(R4X-3 の判定 3′-argv)が止める。
    let table = state::lookup(b"codex").unwrap();
    let shape = state::read_tokens(table.write_flag);
    let good = state::build_cmd(table.command, b"", b"prompt");
    assert!(state::tokens_in_argv(&good, &shape[0], &shape[1]));
    let broken = state::build_cmd(b"codex exec -m {model}", b"", b"prompt");
    assert!(!state::tokens_in_argv(&broken, &shape[0], &shape[1]));
    // `{model}` が空ならフラグごと落とす。`-` で始まるプロンプトの前に `--`。
    assert_eq!(
        state::build_cmd(table.command, b"", b"-x"),
        vec![
            b"codex".to_vec(),
            b"exec".to_vec(),
            b"--sandbox".to_vec(),
            b"workspace-write".to_vec(),
            b"--".to_vec(),
            b"-x".to_vec()
        ]
    );
    assert!(state::tokens_in_argv(
        &[b"--sandbox=workspace-write".to_vec()],
        b"--sandbox",
        b"workspace-write"
    ));
}

#[test]
fn g4_broken_table_shape_dies_internal_20() {
    for (write, probe, label, value) in [
        (
            &b"--sandbox"[..],
            &b"--sandbox read-only"[..],
            "書き込みフラグ",
            "--sandbox",
        ),
        (b"--sandbox workspace-write", b"", "プローブ用フラグ", ""),
    ] {
        let (mut shell, out, _guard) = shell_with(None);
        let table = state::Table {
            write_flag: write,
            probe_flag: probe,
            ..state::TABLE[0]
        };
        let result = state::check_shape(&mut shell, &table);
        assert_eq!(result.err(), Some(Exit::Code(20)));
        let want = format!(
            "ERROR [internal] 実装用既定表の{label}が「フラグ 値」の形になっていない: '{value}'\n"
        );
        assert_eq!(String::from_utf8(written(&out, 2)).unwrap(), want);
    }
    let (mut shell, _, _guard) = shell_with(None);
    let shape = state::check_shape(&mut shell, &state::TABLE[0]).unwrap();
    assert_eq!(shape.write_name, b"--sandbox");
    assert_eq!(shape.write_value, b"workspace-write");
}

#[test]
fn echo_write_error_goes_to_err_trap_with_old_line() {
    // need_val の echo(312 行)が書けない → bash の誤りの文 → ERR の罠(on_err)の行と終了値 20。
    let (mut shell, out, _guard) = shell_with(Some((2, 1, 28)));
    let argv = vec![std::ffi::OsString::from("--runner")];
    let result = args::parse(&mut shell, &argv);
    assert_eq!(result.err(), Some(Exit::Code(20)));
    assert_eq!(
        String::from_utf8(written(&out, 2)).unwrap(),
        "implement-agent.sh: line 312: echo: write error: No space left on device\nERROR [internal] 予期しない失敗(終了コード 1・行 312)\n"
    );
}

#[test]
fn fail_usage_and_die_lines() {
    let (mut shell, out, _guard) = shell_with(None);
    assert_eq!(shell.fail_usage(b"x"), Exit::Code(2));
    assert_eq!(written(&out, 2), "ERROR [usage] x\n".as_bytes());
    // 受けた信号の印の鍵は入れ子にできないので、次の Shell を作る前に放す。
    drop(_guard);
    let (mut shell, out, _guard) = shell_with(Some((2, 1, 28)));
    assert_eq!(shell.die(20, "internal", b"y"), Exit::Code(20));
    assert_eq!(
        String::from_utf8(written(&out, 2)).unwrap(),
        "implement-agent.sh: line 275: echo: write error: No space left on device\nERROR [internal] 予期しない失敗(終了コード 1・行 275)\n"
    );
}

#[test]
fn exit_trap_runs_once_at_exit_and_signal_death() {
    let (mut shell, _, _guard) = shell_with(None);
    shell.state_dir = b"/nonexistent/state".to_vec();
    assert_eq!(shell.exit(2), Exit::Code(2));
    assert!(shell.state_dir.is_empty(), "EXIT の罠が cleanup を走らせた");
    shell.state_dir = b"again".to_vec();
    assert_eq!(shell.exit(3), Exit::Code(3));
    assert_eq!(shell.state_dir, b"again", "EXIT の罠は 1 回だけ");
    // 受けた信号の印の鍵は入れ子にできないので、次の Shell を作る前に放す。
    drop(_guard);
    let (mut shell, _, _guard) = shell_with(None);
    shell.state_dir = b"x".to_vec();
    assert_eq!(shell.die_by(os::SIGPIPE), Exit::Signal(os::SIGPIPE));
    assert!(
        shell.state_dir.is_empty(),
        "終わりの信号でも EXIT の罠が走る"
    );
}

#[test]
fn on_signal_writes_aborted_line_and_code() {
    for (signal, name, code) in [
        (os::SIGTERM, "TERM", 143u8),
        (os::SIGHUP, "HUP", 129),
        (os::SIGINT, "INT", 130),
    ] {
        let (mut shell, out, _guard) = shell_with(None);
        shell.set_signal_traps();
        assert_eq!(shell.phase, Phase::Traps);
        assert_eq!(shell.on_signal(signal), Exit::Code(code));
        assert_eq!(
            String::from_utf8(written(&out, 2)).unwrap(),
            format!(
                "ERROR [aborted] シグナル {name} を受信したため中止した(外部ランナーのプロセスグループと直接の子プロセスに終了のシグナルを送った)\n"
            )
        );
    }
    // 罠の中の書き込みの失敗は、ERR の罠を外した後の set -e で終了値 1。
    let (mut shell, _, _guard) = shell_with(Some((2, 1, 28)));
    shell.set_signal_traps();
    assert_eq!(shell.on_signal(os::SIGTERM), Exit::Code(1));
}

#[test]
fn job_notice_line_matches_bash_format() {
    let status = |signal, core| launch::Status {
        pid: 20,
        status: os::ChildStatus::Signaled(signal, core),
        stdout: Vec::new(),
    };
    for (signal, core, want) in [
        (
            25,
            true,
            "implement-agent.sh: line 354:    20 File size limit exceeded(core dumped) LC_ALL=C grep -q '[^[:space:]]' \"$PROMPT_FILE\" 2> /dev/null\n",
        ),
        (
            25,
            false,
            "implement-agent.sh: line 354:    20 File size limit exceededLC_ALL=C grep -q '[^[:space:]]' \"$PROMPT_FILE\" 2> /dev/null\n",
        ),
        (
            3,
            true,
            "implement-agent.sh: line 354:    20 Quit                    (core dumped) LC_ALL=C grep -q '[^[:space:]]' \"$PROMPT_FILE\" 2> /dev/null\n",
        ),
        (2, false, ""),
        (13, false, ""),
    ] {
        let (mut shell, out, _guard) = shell_with(None);
        launch::notify(&mut shell, 354, &status(signal, core), args::GREP_TEXT);
        assert_eq!(String::from_utf8(written(&out, 2)).unwrap(), want);
    }
}

#[test]
fn second_signal_after_trap_reset_runs_exit_trap_and_dies() {
    // trap - TERM HUP INT ERR の後に同じ信号をもう一度受けると、bash 5.2.21 は EXIT の罠を走らせてからその信号で終わる。
    for signal in [os::SIGTERM, os::SIGHUP, os::SIGINT] {
        let (mut shell, out, _guard) = shell_raising(None, Some(signal));
        shell.set_signal_traps();
        shell.state_dir = b"x".to_vec();
        assert_eq!(shell.on_signal(signal), Exit::Signal(signal));
        assert!(shell.state_dir.is_empty(), "EXIT の罠が走った");
        // 1 回目の中止の行は、書いた所まで出る。
        assert!(written(&out, 2).starts_with("ERROR [aborted]".as_bytes()));
        os::clear_all_pending();
    }
}

#[test]
fn whitespace_check_follows_gnu_grep_in_c() {
    let dir = tempdir("space");
    for (data, want) in [
        (&b""[..], false),
        (b" \t\n\x0b\x0c\r", false),
        (b"\0", false),
        (b" \0\n\0", false),
        (b"\0x", true),
        (b"\x80", true),
        (b"x", true),
    ] {
        let path = dir.join("p");
        std::fs::write(&path, data).unwrap();
        assert_eq!(
            args::file_has_non_space(path.as_os_str().as_encoded_bytes()),
            want,
            "{data:?}"
        );
    }
    let mut big = vec![b' '; 40001];
    big.push(b'\0');
    std::fs::write(dir.join("big"), &big).unwrap();
    assert!(!args::file_has_non_space(
        dir.join("big").as_os_str().as_encoded_bytes()
    ));
    assert!(!args::file_has_non_space(b"/nonexistent/prompt"));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn lex_norm_and_numbers() {
    for (input, want) in [
        (&b"/a/./b//c/../d"[..], &b"/a/b/d"[..]),
        (b"/..", b"/"),
        (b"/a/../../b", b"/b"),
        (b"/", b"/"),
        (b"", b"/"),
        (b"/x\n/y", b"/x\n/y"),
    ] {
        assert_eq!(state::lex_norm(input), want);
    }
    assert_eq!(childenv::legal_number(b"007"), Some(7));
    assert_eq!(
        childenv::legal_number(b"9223372036854775807"),
        Some(i64::MAX)
    );
    assert_eq!(childenv::legal_number(b"9223372036854775808"), None);
    assert_eq!(childenv::legal_number(b" 5 "), Some(5));
    // 末尾は bash の whitespace()(空白と TAB)だけ。先頭は strtoimax が isspace を読み飛ばす。
    assert_eq!(childenv::legal_number(b"7\t"), Some(7));
    assert_eq!(childenv::legal_number(b"\n7"), Some(7));
    assert_eq!(childenv::legal_number(b"7\n"), None);
    assert_eq!(childenv::legal_number(b"999\n"), None);
    assert_eq!(childenv::legal_number(b"7\r"), None);
    assert_eq!(childenv::adjust_shell_level(Some(b"7\n"), 1), (1, None));
    assert_eq!(childenv::adjust_shell_level(Some(b"999\n"), 1), (1, None));
    assert_eq!(
        childenv::adjust_shell_level(Some(b"999"), 1),
        (1, Some(1000))
    );
    assert_eq!(childenv::adjust_shell_level(Some(b"-5"), 1), (0, None));
}

fn tempdir(label: &str) -> PathBuf {
    let base = std::env::temp_dir().canonicalize().unwrap();
    let dir = base.join(format!("r4x1-test-{label}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// 2 つの項目(見出しと訳)を持つ小さな `.mo` を組む。
fn mo(charset: &str, pairs: &[(&[u8], &[u8])]) -> Vec<u8> {
    let header = format!("Content-Type: text/plain; charset={charset}\n");
    let mut all: Vec<(Vec<u8>, Vec<u8>)> = vec![(Vec::new(), header.into_bytes())];
    for (a, b) in pairs {
        all.push((a.to_vec(), b.to_vec()));
    }
    all.sort();
    let count = all.len() as u32;
    let mut data = Vec::new();
    for value in [0x9504_12de_u32, 0, count, 28, 28 + count * 8, 0, 0] {
        data.extend_from_slice(&value.to_le_bytes());
    }
    let base = 28 + count * 16;
    let mut strings = Vec::new();
    let mut originals = Vec::new();
    let mut translations = Vec::new();
    for (original, _) in &all {
        originals.push((original.len() as u32, base + strings.len() as u32));
        strings.extend_from_slice(original);
        strings.push(0);
    }
    for (_, translated) in &all {
        translations.push((translated.len() as u32, base + strings.len() as u32));
        strings.extend_from_slice(translated);
        strings.push(0);
    }
    for (length, offset) in originals.into_iter().chain(translations) {
        data.extend_from_slice(&length.to_le_bytes());
        data.extend_from_slice(&offset.to_le_bytes());
    }
    data.extend_from_slice(&strings);
    data
}

#[test]
fn gettext_selects_languages_like_glibc() {
    // C・POSIX・C.UTF-8 は LANGUAGE を見ない(基準の容器で確かめた)。
    for name in [&b"C"[..], b"POSIX", b"C.UTF-8", b"C.utf8"] {
        assert!(select_languages(name, Some(b"ja")).is_empty());
    }
    assert_eq!(
        select_languages(b"ja_JP.UTF-8", Some(b"fr::ja")),
        vec![b"fr".to_vec(), b"ja".to_vec()]
    );
    assert_eq!(
        select_languages(b"ja_JP.UTF-8", None),
        vec![b"ja_JP.UTF-8".to_vec()]
    );
    assert_eq!(
        select_languages(b"ja_JP.UTF-8", Some(b"")),
        vec![b"ja_JP.UTF-8".to_vec()]
    );
}

#[test]
fn msgid_is_searched_in_every_found_catalog() {
    // glibc は、最初に見つけた翻訳ファイルに msgid が無ければ、後の候補(ほかの名前の形と locale-langpack)でも探す。
    let dir = tempdir("split");
    let first = dir.join("locale");
    let second = dir.join("langpack");
    for (base, name, msgid, text) in [
        (
            &first,
            "ja_JP",
            &b"missing operand"[..],
            "オペランドがありません",
        ),
        (&first, "ja", b"write error", "書き込みエラー"),
        (&second, "ja", b"extra operand %s", "余分な演算子 %s"),
    ] {
        let folder = base.join(name).join("LC_MESSAGES");
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(
            folder.join("coreutils.mo"),
            mo("UTF-8", &[(msgid, text.as_bytes())]),
        )
        .unwrap();
    }
    let mut locale = ToolLocale::fixed(b"ja_JP.UTF-8", &[b"ja_JP.UTF-8"], Codeset::Utf8);
    locale.dirs = vec![first, second];
    assert_eq!(
        locale.gettext("coreutils", b"missing operand").unwrap(),
        "オペランドがありません".as_bytes()
    );
    assert_eq!(
        locale.gettext("coreutils", b"write error").unwrap(),
        "書き込みエラー".as_bytes()
    );
    assert_eq!(
        locale.gettext("coreutils", b"extra operand %s").unwrap(),
        "余分な演算子 %s".as_bytes()
    );
    assert_eq!(locale.gettext("coreutils", b"none").unwrap(), b"none");
    std::fs::remove_dir_all(dir).unwrap();
}

fn hex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).unwrap())
        .collect()
}

#[test]
fn tools_match_the_measured_gnu_outputs() {
    // 基準の容器で本物の dirname・basename を測った固定期待(native/ci/implement_agent_fixtures/tool_outputs.json)。
    // 翻訳ファイルは固定期待の訳から作り、道具は起動しない(全 OS で流れる)。
    let data: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../ci/implement_agent_fixtures/tool_outputs.json"
    ))
    .unwrap();
    let dir = tempdir("tools");
    for domain in ["coreutils", "libc"] {
        let pairs: Vec<(Vec<u8>, Vec<u8>)> = data["catalogs"][domain]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| {
                (
                    hex(pair[0].as_str().unwrap()),
                    hex(pair[1].as_str().unwrap()),
                )
            })
            .filter(|(msgid, _)| !msgid.is_empty())
            .collect();
        let borrowed: Vec<(&[u8], &[u8])> = pairs
            .iter()
            .map(|(a, b)| (a.as_slice(), b.as_slice()))
            .collect();
        let folder = dir.join("ja/LC_MESSAGES");
        std::fs::create_dir_all(&folder).unwrap();
        std::fs::write(folder.join(format!("{domain}.mo")), mo("UTF-8", &borrowed)).unwrap();
    }
    let cases = data["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 594);
    let mut compared = 0;
    let mut launched = 0;
    for case in cases {
        let tool = match case["tool"].as_str().unwrap() {
            "dirname" => tools::Tool::Dirname,
            _ => tools::Tool::Basename,
        };
        let label = case["setting"].as_str().unwrap();
        let setting = &data["settings"][label];
        let languages: Vec<Vec<u8>> = setting["languages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| l.as_str().unwrap().as_bytes().to_vec())
            .collect();
        let borrowed: Vec<&[u8]> = languages.iter().map(Vec::as_slice).collect();
        let codeset = locale::Codeset::from_name(setting["codeset"].as_str().unwrap().as_bytes());
        let value = hex(case["value"].as_str().unwrap());
        if let Codeset::Other(_) = codeset {
            // 決定の表の (2): Rust の中で作らず、道具を起動する(state::run_tool)。
            launched += 1;
            continue;
        }
        let mut tool_locale = ToolLocale::fixed(
            setting["messages"].as_str().unwrap().as_bytes(),
            &borrowed,
            codeset,
        );
        tool_locale.dirs = vec![dir.clone()];
        let run = tools::run(tool, &mut tool_locale, tool.name(), &[&value]).unwrap();
        let what = format!(
            "{label} {} {:?}",
            case["tool"],
            String::from_utf8_lossy(&value)
        );
        assert_eq!(
            run.stdout,
            hex(case["stdout"].as_str().unwrap()),
            "stdout {what}"
        );
        assert_eq!(
            run.stderr.concat(),
            hex(case["stderr"].as_str().unwrap()),
            "stderr {what}"
        );
        assert_eq!(
            i64::from(run.status),
            case["status"].as_i64().unwrap(),
            "status {what}"
        );
        compared += 1;
    }
    assert_eq!((compared, launched), (540, 54));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn catalogs_are_searched_in_the_measured_order() {
    // 基準の容器で strace で確かめた順: /usr/share/locale の全部の形、次に /usr/share/locale-langpack の全部の形。
    let dirs: Vec<PathBuf> = locale::CATALOG_DIRS.iter().map(PathBuf::from).collect();
    let found: Vec<String> = catalog_candidates(&dirs, b"ja_JP.UTF-8", "coreutils")
        .into_iter()
        .map(|p| p.display().to_string())
        .collect();
    let mut want = Vec::new();
    for dir in ["/usr/share/locale", "/usr/share/locale-langpack"] {
        for name in [
            "ja_JP.UTF-8",
            "ja_JP.utf8",
            "ja_JP",
            "ja.UTF-8",
            "ja.utf8",
            "ja",
        ] {
            want.push(format!("{dir}/{name}/LC_MESSAGES/coreutils.mo"));
        }
    }
    assert_eq!(found, want);
}

#[test]
fn translations_are_read_at_run_time_and_converted() {
    let dir = tempdir("mo");
    let first = dir.join("locale");
    let second = dir.join("langpack");
    std::fs::create_dir_all(second.join("ja/LC_MESSAGES")).unwrap();
    std::fs::write(
        second.join("ja/LC_MESSAGES/coreutils.mo"),
        mo(
            "UTF-8",
            &[(b"missing operand", "オペランドがありません".as_bytes())],
        ),
    )
    .unwrap();
    std::fs::write(
        second.join("ja/LC_MESSAGES/libc.mo"),
        mo(
            "UTF-8",
            &[(
                b"No such file or directory",
                "そのようなファイルやディレクトリはありません".as_bytes(),
            )],
        ),
    )
    .unwrap();
    std::fs::create_dir_all(first.join("fr/LC_MESSAGES")).unwrap();
    std::fs::write(
        first.join("fr/LC_MESSAGES/coreutils.mo"),
        mo("ISO-8859-1", &[(b"other", b"autre")]),
    )
    .unwrap();
    let dirs = vec![first.clone(), second.clone()];
    let mut locale = ToolLocale::fixed(b"ja_JP.UTF-8", &[b"fr", b"ja"], Codeset::Utf8);
    locale.dirs = dirs.clone();
    // fr の翻訳ファイルはあるが msgid が無い → 次の ja。
    assert_eq!(
        locale.gettext("coreutils", b"missing operand").unwrap(),
        "オペランドがありません".as_bytes()
    );
    assert_eq!(locale.gettext("coreutils", b"none").unwrap(), b"none");
    // 訳が UTF-8 でない翻訳ファイルにあれば、道具を起動する。
    assert!(locale.gettext("coreutils", b"other").is_err());
    // 訳した errno の文。
    assert_eq!(
        locale.strerror(2).unwrap(),
        "そのようなファイルやディレクトリはありません".as_bytes()
    );
    // ASCII の地域設定では 1 字ずつ `?`。
    let mut ascii = ToolLocale::fixed(b"ja_JP.UTF-8", &[b"ja"], Codeset::Ascii);
    ascii.dirs = dirs;
    assert_eq!(
        ascii.gettext("coreutils", b"missing operand").unwrap(),
        b"???????????"
    );
    // UTF-8 でも ASCII でもない文字コードは、訳を作らない。
    let mut other = ToolLocale::fixed(b"ja_JP.eucJP", &[b"ja"], Codeset::Other(b"EUC-JP".to_vec()));
    assert!(other.gettext("coreutils", b"missing operand").is_err());
    assert!(Catalog::parse(vec![0; 3]).is_none());
    std::fs::remove_dir_all(dir).unwrap();
}

/// 論理の `cd` の値(`real_dir` の値。基準の容器の bash 5.2.21 で測った固定期待)。`@T` は木の根。
const LOGICAL_CD: &[(&str, Option<&str>, Option<&str>, &str, Option<&str>)] = &[
    // (名前, HOME, OLDPWD, 値, 期待〈None は失敗〉)
    ("sub", Some("@T/home"), None, "sub", Some("@T/w/sub")),
    ("l2dotdot", Some("@T/home"), None, "l2/..", Some("@T/w")),
    (
        "abslinkdotdot",
        Some("@T/home"),
        None,
        "@T/link/..",
        Some("@T"),
    ),
    (
        "dash-old",
        Some("@T/home"),
        Some("@T/old"),
        "-",
        Some("@T/old\n@T/old"),
    ),
    ("dash-noold", Some("@T/home"), None, "-", None),
    (
        "dash-oldfile",
        Some("@T/home"),
        Some("@T/a/b/../../w/sub/../../link"),
        "-",
        Some("@T/a/b/../../w/sub/../../link\n@T/a/b"),
    ),
    ("P-home", Some("@T/home"), None, "-P", Some("@T/home")),
    ("P-nohome", None, None, "-P", None),
    ("L-home", Some("@T/home"), None, "-L", Some("@T/home")),
    ("dd-home", Some("@T/home"), None, "--", Some("@T/home")),
    ("e-home", Some("@T/home"), None, "-e", Some("@T/home")),
    ("Pe-home", Some("@T/home"), None, "-Pe", Some("@T/home")),
    ("LP-home", Some("@T/home"), None, "-LP", Some("@T/home")),
    ("x", Some("@T/home"), None, "-x", None),
    ("at", Some("@T/home"), None, "-@", None),
    ("help", Some("@T/home"), None, "--help", None),
    ("dot", Some("@T/home"), None, ".", Some("@T/w")),
    ("dotdot", Some("@T/home"), None, "..", Some("@T")),
    ("L-emptyhome", Some(""), None, "-L", Some("@T/w")),
    ("P-emptyhome", Some(""), None, "-P", None),
    (
        "up-sub",
        Some("@T/home"),
        None,
        "../w/sub",
        Some("@T/w/sub"),
    ),
    ("missing", Some("@T/home"), None, "nonexist", None),
    ("dd-relhome", Some("sub"), None, "--", Some("@T/w/sub")),
    ("link", Some("@T/home"), None, "@T/link", Some("@T/a/b")),
];

#[test]
fn logical_cd_matches_measured_bash() {
    // 相対の値は起動時の cwd の下と読むので、cwd を @T/w にした子のテスト実行物の中で確かめる。
    const CHILD: &str = "R4X1_LOGICAL_CD_ROOT";
    if let Ok(root) = std::env::var(CHILD) {
        let root = root.as_bytes().to_vec();
        let at = |text: &str| -> Vec<u8> {
            text.as_bytes()
                .split(|b| *b == b'@')
                .enumerate()
                .flat_map(|(i, part)| {
                    if i == 0 {
                        part.to_vec()
                    } else {
                        let mut out = root.clone();
                        out.extend_from_slice(part.strip_prefix(b"T").unwrap_or(part));
                        out
                    }
                })
                .collect()
        };
        let mut logical_cwd = root.clone();
        logical_cwd.extend_from_slice(b"/wl");
        for (name, home, oldpwd, value, want) in LOGICAL_CD {
            let state = paths::Logical {
                cwd: paths::canonpath(&logical_cwd),
                home: home.map(at),
                oldpwd: oldpwd.map(at).filter(|v| paths::is_dir(v)),
            };
            let got = paths::real_dir(&state, &at(value));
            assert_eq!(got, want.map(at), "{name}");
        }
        println!("CHILD-OK");
        return;
    }
    let root = tempdir("cd");
    for dir in ["a/b", "home", "old", "w/sub", "w/-P", "w/-", "w/-x", "w/--"] {
        std::fs::create_dir_all(root.join(dir)).unwrap();
    }
    std::os::unix::fs::symlink("a/b", root.join("link")).unwrap();
    std::os::unix::fs::symlink("w", root.join("wl")).unwrap();
    std::os::unix::fs::symlink("../a/b", root.join("w/l2")).unwrap();
    let binary = std::env::current_exe().unwrap();
    let output = std::process::Command::new(binary)
        .current_dir(root.join("w"))
        .env(CHILD, &root)
        .args([
            "implement_agent::tests::logical_cd_matches_measured_bash",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&output.stdout);
    std::fs::remove_dir_all(&root).unwrap();
    assert!(
        output.status.success() && stdout.contains("CHILD-OK"),
        "{stdout}\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn candidates_skip_the_three_fixed_names() {
    for (xdg, home, want) in [
        (
            Some(&b"/x"[..]),
            Some(&b"/h"[..]),
            Some(&b"/x/dev-workflow"[..]),
        ),
        (None, Some(b"/h"), Some(b"/h/.local/state/dev-workflow")),
        (Some(b""), Some(b""), None),
        (
            Some(b"/.local/state"),
            Some(b"/"),
            Some(b"//.local/state/dev-workflow"),
        ),
        (Some(b"/.cache"), None, None),
        (Some(b"rel"), None, Some(b"rel/dev-workflow")),
    ] {
        let (mut shell, out, _guard) = shell_with(None);
        shell.set_signal_traps();
        let mut context = Context::new(Vec::new());
        context.places.xdg_state_home = xdg.map(<[u8]>::to_vec);
        context.places.home = home.map(<[u8]>::to_vec);
        context.args.cwd = b"/".to_vec();
        context.logical.cwd = Some(b"/".to_vec());
        let result = state::candidates(&mut shell, &mut context);
        match want {
            Some(cand) => assert_eq!(result.unwrap(), cand),
            None => {
                assert_eq!(result.err(), Some(Exit::Code(20)));
                assert!(String::from_utf8(written(&out, 2)).unwrap().contains(
                    "(試した候補: なし。/tmp・$TMPDIR・--cwd の配下は使えない。$XDG_STATE_HOME を書き込み範囲外へ設定する)"
                ));
            }
        }
    }
}

#[test]
fn boundary_text_is_fixed() {
    assert_eq!(
        String::from_utf8(boundary_text(b"/h/.local/state/dev-workflow")).unwrap(),
        "ERROR [r4x-1-boundary] 保護領域の候補 /h/.local/state/dev-workflow の mkdir から後は R4X-2 で移す"
    );
}

#[test]
fn child_environment_follows_bash_rules() {
    let startup = childenv::Startup {
        raw: vec![
            b"A=1".to_vec(),
            b"A=2".to_vec(),
            b"=x".to_vec(),
            b"noeq".to_vec(),
            b"POSIXLY_CORRECT=y".to_vec(),
            b"CDPATH=/c".to_vec(),
            b"CMD=arr".to_vec(),
            b"RUNNER=old".to_vec(),
            b"LOG_SWAP_PY=/py".to_vec(),
            b"LC_ALL=ja_JP.UTF-8".to_vec(),
            b"a-b=3".to_vec(),
            b"IFS=x".to_vec(),
            b"LINENO=7".to_vec(),
        ],
        shell_level: 2,
        pwd: Some(b"/w".to_vec()),
        bash: b"/usr/bin/bash".to_vec(),
        root: false,
    };
    let mut script = childenv::ScriptValues::default();
    script.set("RUNNER", b"codex");
    let env = childenv::child_environment(
        &startup,
        &script,
        352,
        b"/o/grep",
        &[("LC_ALL", b"C")],
        (0, 0),
    );
    let text: Vec<String> = env
        .iter()
        .map(|e| String::from_utf8_lossy(e).into_owned())
        .collect();
    assert_eq!(
        text,
        [
            "A=2",
            "IFS= \t\n",
            "LC_ALL=C",
            "LINENO=352",
            "LOG_SWAP_PY=/py",
            "PWD=/w",
            "RUNNER=codex",
            "SHLVL=2",
            "_=/o/grep",
            "a-b=3"
        ]
    );
}
