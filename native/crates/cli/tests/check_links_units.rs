#![forbid(unsafe_code)]
//! R4U(#480)の文字列だけの部品の単体検査。固定期待は本家 CPython 3.10.11 の実測
//! (native/fixtures/r4u/ の gen_unit_fixtures.py・gen_matcher_corpus.py が作る)。
#[path = "../src/commands/check_links/mod.rs"]
// 入口(run)と OS の部品は tests/check_links.rs が別プロセスで検査する。ここでは文字列の部品だけを使う。
#[allow(dead_code)]
mod check_links;

use check_links::matcher::{self, Found};
use check_links::oserror::{self, Os};
use check_links::path::{self, PurePath};
use check_links::traceback::{self, Exception, Frame, PieceKind, Resolve, Site, Traceback};
use check_links::{posix, text};
use serde_json::Value;

const UNITS: &str = include_str!("../../../fixtures/r4u/units.json");
const LINUX: &str = include_str!("../../../fixtures/r4u/linux.json");
const CORPUS: &str = include_str!("../../../fixtures/r4u/matcher-corpus.json");

fn load(source: &str) -> Value {
    serde_json::from_str(source).expect("固定期待の JSON を読めない")
}

/// 固定期待のバイト列(文字列は UTF-8、`{"hex": ...}` は 16 進)。
fn raw(value: &Value) -> Vec<u8> {
    match value {
        Value::String(text) => text.as_bytes().to_vec(),
        Value::Object(map) if map.len() == 1 => {
            let hex = map["hex"].as_str().expect("hex は文字列");
            (0..hex.len())
                .step_by(2)
                .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).expect("16 進でない"))
                .collect()
        }
        other => panic!("バイト列の形が違う: {other}"),
    }
}

fn list(value: &Value) -> &Vec<Value> {
    value.as_array().expect("配列でない")
}

fn int(value: &Value) -> i64 {
    value.as_i64().expect("整数でない")
}

/// `[[開始, 終わり], ...]` の範囲を、全符号位置の真偽の表にする。
fn membership(ranges: &Value) -> Vec<bool> {
    let mut table = vec![false; 0x110000];
    for range in list(ranges) {
        let (low, high) = (int(&range[0]) as usize, int(&range[1]) as usize);
        table[low..=high].fill(true);
    }
    table
}

fn fixture_python_is_upstream(fixture: &Value) {
    let python = &fixture["python"];
    assert_eq!(python["version"], "3.10.11");
    assert_eq!(
        python["stdlib_sha256"]["pathlib.py"],
        "936f14a3ecc4ac5e896e4d673666d76b25a9d581f3b4c8d8d424e830fda2a69b"
    );
    assert_eq!(
        python["stdlib_sha256"]["posixpath.py"],
        "2c770bac0680a5a180b1310102d14e1e120f2f2e8e47925b1574ec808cb8ad6d"
    );
}

#[test]
fn pure_path_matches_pathlib() {
    let units = load(UNITS);
    fixture_python_is_upstream(&units);
    let excluded: Vec<Vec<u8>> = list(&units["exclude_dirs"]).iter().map(raw).collect();
    let mut ours: Vec<Vec<u8>> = path::EXCLUDE_DIRS
        .iter()
        .map(|name| name.to_vec())
        .collect();
    ours.sort();
    assert_eq!(ours, excluded);
    let cases = list(&units["pure_path"]);
    assert!(cases.len() >= 40);
    let mut joins = 0;
    for case in cases {
        let input = raw(&case["input"]);
        let path = PurePath::new(&input);
        let label = String::from_utf8_lossy(&input).into_owned();
        assert_eq!(path.to_bytes(), raw(&case["str"]), "str: {label}");
        let parts: Vec<Vec<u8>> = list(&case["parts"]).iter().map(raw).collect();
        let ours: Vec<Vec<u8>> = path.parts().iter().map(|part| part.to_vec()).collect();
        assert_eq!(ours, parts, "parts: {label}");
        assert_eq!(
            path.parent().to_bytes(),
            raw(&case["parent"]),
            "parent: {label}"
        );
        let parents: Vec<Vec<u8>> = list(&case["parents"]).iter().map(raw).collect();
        let ours: Vec<Vec<u8>> = path.parents().iter().map(PurePath::to_bytes).collect();
        assert_eq!(ours, parents, "parents: {label}");
        assert_eq!(
            path.has_excluded_part(),
            case["excluded"].as_bool().unwrap(),
            "除外: {label}"
        );
        assert_eq!(
            path.child(b"c.md").to_bytes(),
            raw(&case["child"]),
            "child: {label}"
        );
        for pair in list(&case["join"]) {
            let other = raw(&pair[0]);
            assert_eq!(path.join(&other).to_bytes(), raw(&pair[1]), "join: {label}");
            joins += 1;
        }
    }
    assert!(joins >= 400);
}

#[test]
fn sort_matches_sorted_paths() {
    let units = load(UNITS);
    let cases = list(&units["sort"]);
    assert!(cases.len() >= 300);
    for case in cases {
        let mut paths: Vec<PurePath> = list(&case["input"])
            .iter()
            .map(|item| PurePath::new(&raw(item)))
            .collect();
        let mut by_ord = paths.clone();
        path::sort(&mut paths);
        by_ord.sort();
        let expected: Vec<Vec<u8>> = list(&case["sorted"]).iter().map(raw).collect();
        assert_eq!(
            paths.iter().map(PurePath::to_bytes).collect::<Vec<_>>(),
            expected
        );
        assert_eq!(by_ord, paths);
    }
}

#[test]
fn posixpath_matches_python() {
    let units = load(UNITS);
    let cases = &units["posixpath"];
    let mut count = 0;
    for case in list(&cases["join"]) {
        let parts: Vec<Vec<u8>> = list(&case["input"]).iter().map(raw).collect();
        let rest: Vec<&[u8]> = parts[1..].iter().map(Vec::as_slice).collect();
        assert_eq!(posix::join(&parts[0], &rest), raw(&case["result"]));
        count += 1;
    }
    for case in list(&cases["split"]) {
        let (head, tail) = posix::split(&raw(&case["input"]));
        assert_eq!(head, raw(&case["result"][0]));
        assert_eq!(tail, raw(&case["result"][1]));
        count += 1;
    }
    for case in list(&cases["normpath"]) {
        assert_eq!(posix::normpath(&raw(&case["input"])), raw(&case["result"]));
        count += 1;
    }
    for case in list(&cases["isabs"]) {
        assert_eq!(
            posix::isabs(&raw(&case["input"])),
            case["result"].as_bool().unwrap()
        );
        count += 1;
    }
    assert!(count >= 60);
}

#[test]
fn decode_matches_read_text() {
    let units = load(UNITS);
    let cases = list(&units["decode"]);
    assert!(cases.len() >= 3000);
    for case in cases {
        let input = raw(&serde_json::json!({ "hex": case["input"] }));
        assert_eq!(
            text::decode(&input),
            case["text"].as_str().unwrap(),
            "入力 {}",
            case["input"]
        );
    }
}

#[test]
fn repr_and_printable_match_python() {
    let units = load(UNITS);
    let cases = list(&units["repr"]);
    assert!(cases.len() >= 400);
    for case in cases {
        assert_eq!(
            check_links::repr(&raw(&case["input"])),
            case["repr"].as_str().unwrap()
        );
    }
    let nonprintable = membership(&units["nonprintable_ranges"]);
    for (code, &nonprintable) in (0..=0x10ffff_u32).zip(&nonprintable) {
        assert_eq!(check_links::printable(code), !nonprintable, "U+{code:04X}");
    }
    assert_eq!(check_links::code_points(b"a\xff"), [0x61, 0xdcff]);
}

#[test]
fn spaces_match_python_whitespace() {
    let corpus = load(CORPUS);
    let space = &corpus["space_ranges"];
    assert_eq!(space["re_s"], space["str_isspace"]);
    let spaces = membership(&space["re_s"]);
    let mut checked = 0;
    for (code, &expected) in (0..=0x10ffff_u32).zip(&spaces) {
        match char::from_u32(code) {
            Some(c) => {
                assert_eq!(matcher::is_space(c), expected, "U+{code:04X}");
                checked += 1;
            }
            None => assert!(!expected, "サロゲートは空白でない"),
        }
    }
    assert_eq!(checked, 0x110000 - 0x800);
}

fn char_offset(text: &str, byte: usize) -> i64 {
    text[..byte].chars().count() as i64
}

#[test]
fn matchers_match_legacy_regexes() {
    let corpus = load(CORPUS);
    assert_eq!(corpus["unicode"], "13.0.0");
    assert_eq!(corpus["patterns"]["link"], r"\[[^\]]*\]\(([^)\s]+)\)");
    assert_eq!(
        corpus["patterns"]["codepath"],
        r"`([A-Za-z0-9_.@/-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})`"
    );
    let cases = list(&corpus["cases"]);
    assert_eq!(cases.len() as i64, int(&corpus["case_count"]));
    assert!(cases.len() >= 10_000);
    let (mut links, mut codes) = (0, 0);
    for case in cases {
        let text = case["text"].as_str().unwrap();
        let ours: Vec<(i64, i64, &str, Option<&str>)> = matcher::links(text)
            .into_iter()
            .map(|Found { start, group }| {
                (
                    char_offset(text, start),
                    matcher::line_number(text, start) as i64,
                    group,
                    matcher::link_target(group),
                )
            })
            .collect();
        let expected: Vec<(i64, i64, &str, Option<&str>)> = list(&case["links"])
            .iter()
            .map(|item| {
                (
                    int(&item[0]),
                    int(&item[1]),
                    item[2].as_str().unwrap(),
                    item[3].as_str(),
                )
            })
            .collect();
        assert_eq!(ours, expected, "リンク: {text:?}");
        links += expected.len();
        let ours: Vec<(i64, i64, &str, bool)> = matcher::code_paths(text)
            .into_iter()
            .map(|Found { start, group }| {
                (
                    char_offset(text, start),
                    matcher::line_number(text, start) as i64,
                    group,
                    matcher::code_skipped(group),
                )
            })
            .collect();
        let expected: Vec<(i64, i64, &str, bool)> = list(&case["codes"])
            .iter()
            .map(|item| {
                (
                    int(&item[0]),
                    int(&item[1]),
                    item[2].as_str().unwrap(),
                    item[3].as_bool().unwrap(),
                )
            })
            .collect();
        assert_eq!(ours, expected, "コード中のパス: {text:?}");
        codes += expected.len();
    }
    assert_eq!(links as i64, int(&corpus["link_match_count"]));
    assert_eq!(codes as i64, int(&corpus["code_match_count"]));
    assert!(links >= 1000 && codes >= 1000);
}

/// 定型のフレームと、固定期待のキー(gen_unit_fixtures.py の FRAMES)の対応。
const FRAMES: [(&str, Frame); 46] = [
    ("legacy.module", traceback::MODULE),
    ("legacy.main_iter", traceback::MAIN_ITER),
    ("legacy.main_doc", traceback::MAIN_DOC),
    ("legacy.main_read", traceback::MAIN_READ),
    ("legacy.main_link_resolve", traceback::MAIN_LINK_RESOLVE),
    ("legacy.main_link_exists", traceback::MAIN_LINK_EXISTS),
    ("legacy.main_code_resolve", traceback::MAIN_CODE_RESOLVE),
    ("legacy.main_code_alt", traceback::MAIN_CODE_ALT),
    ("legacy.main_code_exists", traceback::MAIN_CODE_EXISTS),
    ("legacy.main_print_count", traceback::MAIN_PRINT_COUNT),
    ("legacy.main_print_warn", traceback::MAIN_PRINT_WARN),
    ("legacy.main_print_error", traceback::MAIN_PRINT_ERROR),
    ("legacy.main_print_result", traceback::MAIN_PRINT_RESULT),
    ("legacy.iter_is_dir", traceback::ITER_IS_DIR),
    ("legacy.iter_rglob", traceback::ITER_RGLOB),
    ("legacy.iter_is_file", traceback::ITER_IS_FILE),
    ("legacy.iter_warn", traceback::ITER_WARN),
    ("pathlib.is_dir", traceback::IS_DIR),
    ("pathlib.is_file", traceback::IS_FILE),
    ("pathlib.exists", traceback::EXISTS),
    ("pathlib.stat", traceback::STAT),
    ("pathlib.read_text_open", traceback::READ_TEXT_OPEN),
    ("pathlib.read_text_read", traceback::READ_TEXT_READ),
    ("pathlib.open", traceback::OPEN),
    ("pathlib.resolve_realpath", traceback::RESOLVE_REALPATH),
    ("pathlib.resolve_stat", traceback::RESOLVE_STAT),
    (
        "pathlib.resolve_check_eloop",
        traceback::RESOLVE_CHECK_ELOOP,
    ),
    ("pathlib.check_eloop", traceback::CHECK_ELOOP),
    ("pathlib.rglob", traceback::RGLOB),
    ("pathlib.select_from_is_dir", traceback::SELECT_FROM_IS_DIR),
    ("pathlib.recursive_iterate", traceback::RECURSIVE_ITERATE),
    (
        "pathlib.recursive_successor",
        traceback::RECURSIVE_SUCCESSOR,
    ),
    ("pathlib.iterate_scandir", traceback::ITERATE_SCANDIR),
    (
        "pathlib.iterate_entry_is_dir",
        traceback::ITERATE_ENTRY_IS_DIR,
    ),
    (
        "pathlib.iterate_entry_is_symlink",
        traceback::ITERATE_ENTRY_IS_SYMLINK,
    ),
    ("pathlib.iterate_recurse", traceback::ITERATE_RECURSE),
    ("pathlib.wildcard_scandir", traceback::WILDCARD_SCANDIR),
    ("pathlib.fspath", traceback::FSPATH),
    ("posixpath.realpath_join", traceback::REALPATH_JOIN),
    ("posixpath.realpath_abspath", traceback::REALPATH_ABSPATH),
    ("posixpath.abspath_getcwd", traceback::ABSPATH_GETCWD),
    (
        "posixpath.joinrealpath_isabs",
        traceback::JOINREALPATH_ISABS,
    ),
    (
        "posixpath.joinrealpath_lstat",
        traceback::JOINREALPATH_LSTAT,
    ),
    (
        "posixpath.joinrealpath_recurse",
        traceback::JOINREALPATH_RECURSE,
    ),
    ("posixpath.isabs_sep", traceback::ISABS_SEP),
    ("posixpath.get_sep", traceback::GET_SEP),
];

#[test]
fn frames_match_cpython_sources() {
    let units = load(UNITS);
    let fixture = list(&units["frames"]);
    assert_eq!(fixture.len(), FRAMES.len());
    let keys: Vec<&str> = fixture
        .iter()
        .map(|item| item["key"].as_str().unwrap())
        .collect();
    for (name, _) in &FRAMES {
        assert_eq!(keys.iter().filter(|key| *key == name).count(), 1, "{name}");
    }
    for item in fixture {
        let key = item["key"].as_str().unwrap();
        let (_, frame) = FRAMES
            .iter()
            .find(|(name, _)| *name == key)
            .unwrap_or_else(|| panic!("定型に無いフレーム: {key}"));
        assert_eq!(frame.file, item["file"].as_str().unwrap(), "{key}");
        assert_eq!(i64::from(frame.line), int(&item["line"]), "{key}");
        assert_eq!(frame.name, item["name"].as_str().unwrap(), "{key}");
        assert_eq!(frame.source, item["source"].as_str().unwrap(), "{key}");
    }
}

#[test]
fn site_frames_follow_cpython_call_paths() {
    use traceback::*;
    let tail = |site: Site, skip: usize| site.frames()[skip..].to_vec();
    assert_eq!(
        Site::TargetIsFile.frames(),
        [MODULE, MAIN_ITER, ITER_IS_FILE, IS_FILE, STAT]
    );
    assert_eq!(Site::TargetWarn.frames(), [MODULE, MAIN_ITER, ITER_WARN]);
    assert_eq!(
        tail(Site::RglobSelectIsDir, 3),
        [RGLOB, SELECT_FROM_IS_DIR, IS_DIR, STAT]
    );
    assert_eq!(
        tail(Site::RglobIterateScandir { depth: 2 }, 3),
        [
            RGLOB,
            RECURSIVE_ITERATE,
            ITERATE_RECURSE,
            ITERATE_RECURSE,
            ITERATE_SCANDIR
        ]
    );
    assert_eq!(
        tail(Site::RglobEntryIsSymlink { depth: 0 }, 3),
        [RGLOB, RECURSIVE_ITERATE, ITERATE_ENTRY_IS_SYMLINK]
    );
    assert_eq!(Site::ReadRead.frames(), [MODULE, MAIN_READ, READ_TEXT_READ]);
    assert_eq!(
        Site::ResolveReadlink {
            call: Resolve::Code,
            depth: 1
        }
        .frames(),
        [
            MODULE,
            MAIN_CODE_RESOLVE,
            RESOLVE_REALPATH,
            REALPATH_JOIN,
            JOINREALPATH_RECURSE,
            JOINREALPATH_RECURSE
        ]
    );
    assert_eq!(
        Site::ResolveLstat {
            call: Resolve::Code,
            depth: 0
        }
        .frames()[1],
        MAIN_CODE_RESOLVE
    );
    assert_eq!(
        Site::ResolveGetcwd(Resolve::Code).frames()[1],
        MAIN_CODE_RESOLVE
    );
    assert_eq!(
        Site::ResolveLoop(Resolve::Code).frames()[1],
        MAIN_CODE_RESOLVE
    );
    for (site, frame) in [
        (Site::PrintDoc, MAIN_DOC),
        (Site::PrintCount, MAIN_PRINT_COUNT),
        (Site::PrintWarn, MAIN_PRINT_WARN),
        (Site::PrintError, MAIN_PRINT_ERROR),
        (Site::PrintResult, MAIN_PRINT_RESULT),
    ] {
        assert_eq!(site.frames(), [MODULE, frame]);
    }
}

#[test]
fn glibc_strerror_matches_measured_table() {
    let linux = load(LINUX);
    let table = list(&linux["strerror"]);
    assert_eq!(table.len(), 201);
    for item in table {
        let errno = int(&item[0]) as i32;
        assert_eq!(
            oserror::glibc_strerror(errno),
            item[1].as_str().unwrap(),
            "errno {errno}"
        );
    }
    assert_eq!(oserror::glibc_strerror(-1), "Unknown error -1");
    assert_eq!(
        oserror::os_error_text(13, "Permission denied", Some(b"a'b")),
        "[Errno 13] Permission denied: \"a'b\""
    );
    assert_eq!(
        oserror::os_error_text(2, "No such file or directory", None),
        "[Errno 2] No such file or directory"
    );
}

#[test]
fn exception_types_match_python_errnomap() {
    let linux = load(LINUX);
    for item in list(&linux["exception_type"]) {
        let errno = int(&item[0]) as i32;
        assert_eq!(
            oserror::exception_type_on(Os::Linux, errno),
            item[1].as_str().unwrap(),
            "errno {errno}"
        );
    }
    // Mac の番号は実測でないので、名前ごとの種類が Linux の実測と同じことと、番号の重複が無いことを確かめる。
    let by_name = list(&linux["exception_type_by_name"]);
    for &(name, kind, linux_errno, macos_errno) in &oserror::NAMED {
        let measured = by_name
            .iter()
            .find(|item| item[0] == name)
            .unwrap_or_else(|| panic!("{name} が実測に無い"));
        assert_eq!(measured[1], kind, "{name}");
        assert_eq!(oserror::exception_type_on(Os::Linux, linux_errno), kind);
        assert_eq!(oserror::exception_type_on(Os::Macos, macos_errno), kind);
    }
    assert_eq!(oserror::exception_type_on(Os::Macos, 62), "OSError");
    let expected = if cfg!(target_os = "macos") {
        Os::Macos
    } else {
        Os::Linux
    };
    assert_eq!(oserror::CURRENT, expected);
    assert_eq!(
        oserror::exception_type(13),
        oserror::exception_type_on(expected, 13)
    );
    // 実行物の OS で選ぶ入口と、OS を指定する入口が同じ結果になる。
    // errno 36 は Linux では ENAMETOOLONG(OSError)、Mac では EINPROGRESS(BlockingIOError)。
    assert_eq!(
        Exception::os(36, "x", None),
        Exception::os_on(expected, 36, "x", None)
    );
    assert_eq!(
        Exception::os_on(Os::Linux, 36, "x", None).type_name,
        "OSError"
    );
    assert_eq!(
        Exception::os_on(Os::Macos, 36, "x", None).type_name,
        "BlockingIOError"
    );
}

fn site(name: &str, depth: usize) -> Site {
    match name {
        "target-is-dir" => Site::TargetIsDir,
        "rglob-wildcard-scandir" => Site::RglobWildcardScandir,
        "rglob-recursion" => Site::RglobRecursion { depth },
        "rglob-entry-is-dir" => Site::RglobEntryIsDir { depth },
        "read-open" => Site::ReadOpen,
        "link-resolve-lstat" => Site::ResolveLstat {
            call: Resolve::Link,
            depth,
        },
        "resolve-stat" => Site::ResolveStat,
        "link-resolve-loop" => Site::ResolveLoop(Resolve::Link),
        "link-resolve-getcwd" => Site::ResolveGetcwd(Resolve::Link),
        "link-resolve-recursion" => Site::ResolveRecursion {
            call: Resolve::Link,
            depth,
        },
        "code-resolve-recursion" => Site::ResolveRecursion {
            call: Resolve::Code,
            depth,
        },
        "link-exists" => Site::LinkExists,
        "code-alt-exists" => Site::CodeAltExists,
        "code-exists" => Site::CodeExists,
        "print-error" => Site::PrintError,
        other => panic!("未知の所: {other}"),
    }
}

fn exception(spec: &Value) -> Exception {
    match spec["kind"].as_str().unwrap() {
        "os" => {
            let errno = int(&spec["errno"]) as i32;
            let filename = (!spec["filename"].is_null()).then(|| raw(&spec["filename"]));
            // linux.json は Linux の実測なので、どの OS で流しても種類は Linux の番号で選ぶ。
            Exception::os_on(
                Os::Linux,
                errno,
                &oserror::glibc_strerror(errno),
                filename.as_deref(),
            )
        }
        "value-null" => Exception::embedded_null(),
        "runtime-loop" => Exception::symlink_loop(&raw(&spec["filename"])),
        "recursion" => Exception::recursion(spec["c_call"].as_bool().unwrap()),
        "keyboard" => Exception::keyboard_interrupt(),
        other => panic!("未知の例外: {other}"),
    }
}

#[test]
fn tracebacks_match_legacy_on_linux() {
    let linux = load(LINUX);
    fixture_python_is_upstream(&linux);
    let cases = list(&linux["tracebacks"]);
    assert!(cases.len() >= 20);
    for case in cases {
        let chain: Vec<Traceback> = list(&case["chain"])
            .iter()
            .map(|item| Traceback {
                frames: site(
                    item["site"].as_str().unwrap(),
                    item["repeat"].as_u64().unwrap_or(0) as usize,
                )
                .frames(),
                exception: exception(&item["exception"]),
            })
            .collect();
        let mut pieces = traceback::print_exception(&chain);
        if !case["unraisable"].is_null() {
            pieces.extend(traceback::unraisable_stdout(&exception(
                &case["unraisable"]["exception"],
            )));
        }
        let text: String = pieces.iter().map(|piece| piece.text.as_str()).collect();
        assert_eq!(text, case["stderr"].as_str().unwrap(), "{}", case["id"]);
    }
}

#[test]
fn pieces_follow_cpython_write_calls() {
    let chain = [
        Traceback {
            frames: Site::ResolveStat.frames(),
            exception: Exception::os_on(
                Os::Linux,
                40,
                "Too many levels of symbolic links",
                Some(b"/x"),
            ),
        },
        Traceback {
            frames: Site::ResolveLoop(Resolve::Link).frames(),
            exception: Exception::symlink_loop(b"/x"),
        },
    ];
    let kinds: Vec<PieceKind> = traceback::print_exception(&chain)
        .iter()
        .map(|piece| piece.kind)
        .collect();
    use PieceKind::*;
    let frame = [FileLine, Indent, Source, SourceEnd];
    let mut expected = vec![Header];
    expected.extend(frame.repeat(2));
    expected.extend([TypeName, Colon, Value, End, Separator, Header]);
    expected.extend(frame.repeat(4));
    expected.extend([TypeName, Colon, Value, End]);
    assert_eq!(kinds, expected);
    // 本文が空の KeyboardInterrupt は `: ` を書かない。unraisable は書く。
    let interrupted = traceback::print_exception(&[Traceback {
        frames: vec![],
        exception: Exception::keyboard_interrupt(),
    }]);
    let text: String = interrupted
        .iter()
        .map(|piece| piece.text.as_str())
        .collect();
    assert_eq!(text, "KeyboardInterrupt\n");
    let text: String = traceback::unraisable_stdout(&Exception::keyboard_interrupt())
        .iter()
        .map(|piece| piece.text.as_str())
        .collect();
    assert_eq!(
        text,
        format!(
            "Exception ignored in: {}\nKeyboardInterrupt: \n",
            traceback::STDOUT_REPR
        )
    );
    // 1 回だけ多いときは単数、1000 より古いフレームは出さない。
    let frames = Site::RglobIterateScandir { depth: 4 }.frames();
    let text: String = traceback::print_exception(&[Traceback {
        frames,
        exception: Exception::recursion(false),
    }])
    .iter()
    .map(|piece| piece.text.as_str())
    .collect();
    assert!(text.contains("  [Previous line repeated 1 more time]\n"));
    let deep = Site::RglobRecursion { depth: 1200 }.frames();
    let pieces = traceback::print_exception(&[Traceback {
        frames: deep,
        exception: Exception::recursion(false),
    }]);
    assert!(!pieces.iter().any(|piece| piece.text.contains("<module>")));
    assert!(
        pieces
            .iter()
            .any(|piece| piece.text == "  [Previous line repeated 995 more times]\n")
    );
}

#[test]
fn doc_is_the_legacy_docstring() {
    // print(__doc__) で出す本文は、旧版の先頭の docstring(三重引用符の間)と同じ。
    let legacy = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
    ))
    .expect("旧版を読めない");
    let start = legacy.find("\"\"\"").expect("docstring の始まりが無い") + 3;
    let end = start
        + legacy[start..]
            .find("\"\"\"")
            .expect("docstring の終わりが無い");
    assert_eq!(check_links::legacy::DOC, &legacy[start..end]);
}

#[test]
fn startup_flags_follow_cpython_parsing() {
    use check_links::startup::{env_flag, hash_seed_valid, int_max_str_digits_valid, str_to_int};
    // _Py_str_to_int(strtol を全体で読み、int に収まるとき)。
    for (text, expected) in [
        ("0", Some(0)),
        ("2", Some(2)),
        (" 2", Some(2)),
        ("\t\n\x0b\x0c\r+7", Some(7)),
        ("-3", Some(-3)),
        ("-0", Some(0)),
        ("2 ", None),
        ("+", None),
        ("- 1", None),
        ("0x10", None),
        ("2147483647", Some(i32::MAX)),
        ("2147483648", None),
        ("-2147483648", Some(i32::MIN)),
        ("99999999999999999999", None),
        ("", None),
    ] {
        assert_eq!(str_to_int(text.as_bytes()), expected, "{text:?}");
    }
    // _Py_get_env_flag(読めない値と負の値は 1)。
    for (value, expected) in [
        (None, 0),
        (Some("0"), 0),
        (Some("000"), 0),
        (Some(" 0"), 0),
        (Some("2"), 2),
        (Some("x"), 1),
        (Some("-1"), 1),
        (Some("2 "), 1),
    ] {
        assert_eq!(env_flag(value.map(str::as_bytes)), expected, "{value:?}");
    }
    // config_init_hash_seed(random か strtoul で 0〜4294967295)。
    for (value, expected) in [
        (None, true),
        (Some("random"), true),
        (Some("Random"), false),
        (Some("0"), true),
        (Some(" 7"), true),
        (Some("+7"), true),
        (Some("-0"), true),
        (Some("-1"), false),
        (Some("4294967295"), true),
        (Some("4294967296"), false),
        (Some("5 "), false),
        (Some("999999999999999999999999999999"), false),
    ] {
        assert_eq!(
            hash_seed_valid(value.map(str::as_bytes)),
            expected,
            "{value:?}"
        );
    }
    // config_init_int_max_str_digits(0 か 640 以上)。
    for (value, expected) in [
        (None, true),
        (Some("0"), true),
        (Some("640"), true),
        (Some("639"), false),
        (Some("-1"), false),
        (Some("x"), false),
        (Some("2147483648"), false),
    ] {
        assert_eq!(
            int_max_str_digits_valid(value.map(str::as_bytes)),
            expected,
            "{value:?}"
        );
    }
}

#[test]
fn recursion_limits_match_measured_tracebacks() {
    // linux.json の深い木(995 段)と長いリンクの列(1100 個)で、最初に RecursionError になった深さが境目。
    let linux = load(LINUX);
    let repeat = |id: &str| {
        list(&linux["tracebacks"])
            .iter()
            .find(|case| case["id"] == id)
            .and_then(|case| case["chain"][0]["repeat"].as_u64())
            .expect("呼出履歴の固定期待が無い") as usize
    };
    assert_eq!(
        check_links::fsops::RGLOB_RECURSION_DEPTH,
        repeat("rglob-recursion")
    );
    assert_eq!(
        check_links::fsops::LINK_RECURSION_DEPTH,
        repeat("link-resolve-recursion")
    );
    assert_eq!(
        check_links::fsops::LINK_RECURSION_DEPTH,
        repeat("code-resolve-recursion")
    );
}

#[test]
#[should_panic(expected = "main の前の控えが無い")]
fn run_without_capture_panics_before_any_output() {
    // この単体検査の実行物は main の前の控えを置かない。入口は何も書かずに panic する(呼出元の誤り)。
    assert!(!dev_workflow_platform::check_links::captured_before_main());
    assert_eq!(
        dev_workflow_platform::check_links::sigpipe_ignored_at_start(),
        None
    );
    assert_eq!(
        dev_workflow_platform::check_links::std_valid_at_start(1),
        None
    );
    check_links::run(&[]);
}
