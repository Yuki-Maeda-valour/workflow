#![deny(unsafe_code)]
//! R4C専用の先行検査入口。製品登録表を変更せず、純粋部品を実コンパイルする。
//! 事前検査(#472)は、S3D の `diff_snapshot` のモジュールを取り込み、自分自身を `argv[0]` が `diff-snapshot.sh` の子として
//! 起動して行う。子は、`argv[0]` が `diff-snapshot.sh` と完全に一致するときだけ、`main` の最初で S3D の入口へ入る。
//! 候補解析(#471)は、`candidate_keys` とその依存(`r4a_rest_support`・`resolve_task_dir`・`task_digest`)を `#[path]` で
//! 取り込み(複製しない)、`CandidateKeys`(下)が `check_text(本文, None)` を呼んで接続する。
#[path = "../src/commands/adopt_candidate/mod.rs"]
mod adopt_candidate;
#[path = "../src/commands/diff_snapshot/mod.rs"]
mod diff_snapshot;

// 取り込んだ R4A の部品。`allow` は、この宣言にだけ付ける(R4C 自身のコードの未使用の検出を弱めない)。
#[allow(dead_code, clippy::duplicate_mod)]
#[path = "../src/commands/candidate_keys/mod.rs"]
pub mod candidate_keys;
#[allow(dead_code, clippy::duplicate_mod)]
#[path = "../src/commands/r4a_rest_support/mod.rs"]
pub mod r4a_rest_support;
#[allow(dead_code, clippy::duplicate_mod)]
#[path = "../src/commands/resolve_task_dir/mod.rs"]
pub mod resolve_task_dir_definition;
#[allow(dead_code, clippy::duplicate_mod)]
#[path = "../src/commands/task_digest/mod.rs"]
pub mod task_digest_definition;

// 製品の `commands` の形を、この入口の中に作る(登録表には載せない。R4A の `r4a_rest_contract` と同じ形)。
// `check_text` の経路は `take_sigint` を呼ばず、R4A の信号の処理も入れない。
mod commands {
    pub use super::r4a_rest_support;

    pub use super::resolve_task_dir_definition as resolve_task_dir;
    pub use super::task_digest_definition as task_digest;

    pub mod r4a_platform {
        pub use dev_workflow_platform::r4a_rest::{read_file, take_sigint};
    }
}

use adopt_candidate::{
    Candidates, Context, Injected, InjectedPrecheck, Ops, Precheck, Problem, ProcessEnv, PyJson,
    RealOps, SelfPrecheck, Standard, run,
};
use dev_workflow_platform::adopt_candidate as os;
use r4a_rest_support::json::PyJson as KeysJson;
use std::os::unix::ffi::OsStrExt;

// この実行物だけが、`main` より前の SIGPIPE と fd の状態を記録する(S3D の example と同じ。製品の実行物は、同じ platform を
// リンクするが、この初期化子を持たない)。ローダーが呼ぶ初期化子の属性が、このファイルで唯一の unsafe の項目。
#[allow(unsafe_code)]
#[used]
#[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
#[cfg_attr(target_os = "macos", unsafe(link_section = "__DATA,__mod_init_func"))]
static REMEMBER_STARTUP_STATE: extern "C" fn() =
    dev_workflow_platform::diff_snapshot::signals::remember_startup_state;

/// 事前検査の子の `argv[0]`(完全に一致するときだけ、子のモードに入る)。
const CHILD_ARGV0: &[u8] = b"diff-snapshot.sh";

/// #471 の候補解析(`candidate_keys::check_text(body, None)`)に当たる実物。UTF-8 strict で読めた本文を Unicode の符号位置の列にして
/// 呼び、戻りの `problems` の配列を元の順のまま `Problem` へ移す。終了値は使わない(旧版も捨てる)。
struct CandidateKeys;

/// 想定外の値(辞書・文字列・配列以外、または不正な符号位置)が来たときの停止の文(`候補の採用を停止: ` の後ろ)。
const UNEXPECTED_RESULT: &str = "候補の検査の結果に想定外の値がある";

fn code_points_to_bytes(points: &[u32]) -> Result<Vec<u8>, Vec<u8>> {
    let mut text = String::new();
    for point in points {
        text.push(char::from_u32(*point).ok_or_else(|| UNEXPECTED_RESULT.as_bytes().to_vec())?);
    }
    Ok(text.into_bytes())
}

/// 移すのは辞書(キーの順のまま)・文字列(符号位置から UTF-8 へ)・配列だけ。`Null`・`Bool`・`Int`・`Float` は失敗にする。
fn convert(value: &KeysJson) -> Result<PyJson, Vec<u8>> {
    match value {
        KeysJson::String(points) => Ok(PyJson::Text(code_points_to_bytes(points)?)),
        KeysJson::Array(items) => Ok(PyJson::List(
            items.iter().map(convert).collect::<Result<_, _>>()?,
        )),
        KeysJson::Object(entries) => Ok(PyJson::Dict(
            entries
                .iter()
                .map(|(key, item)| Ok((code_points_to_bytes(key)?, convert(item)?)))
                .collect::<Result<_, Vec<u8>>>()?,
        )),
        KeysJson::Null | KeysJson::Bool(_) | KeysJson::Int { .. } | KeysJson::Float(_) => {
            Err(UNEXPECTED_RESULT.as_bytes().to_vec())
        }
    }
}

/// `check_text` の戻り(ペイロード)から `problems` の配列を取り出し、`Problem` の列にする。
fn problems_of(payload: &KeysJson) -> Result<Vec<Problem>, Vec<u8>> {
    let unexpected = || UNEXPECTED_RESULT.as_bytes().to_vec();
    let KeysJson::Object(entries) = payload else {
        return Err(unexpected());
    };
    let wanted = r4a_rest_support::chars::from_str("problems");
    let Some((_, KeysJson::Array(items))) = entries.iter().find(|(key, _)| *key == wanted) else {
        return Err(unexpected());
    };
    let code_key = r4a_rest_support::chars::from_str("code");
    items
        .iter()
        .map(|item| {
            let json = convert(item)?;
            // `code` は、要素の辞書の `code` が文字列のときだけ持つ。
            let code = match item {
                KeysJson::Object(fields) => fields.iter().find_map(|(key, value)| match value {
                    KeysJson::String(points) if *key == code_key => {
                        Some(code_points_to_bytes(points))
                    }
                    _ => None,
                }),
                _ => None,
            }
            .transpose()?;
            Ok(Problem { code, json })
        })
        .collect()
}

impl Candidates for CandidateKeys {
    fn check_text(&self, text: &str) -> Result<Vec<Problem>, Vec<u8>> {
        let body = r4a_rest_support::chars::from_str(text);
        let (_exit, payload) = candidate_keys::check_text(&body, None);
        problems_of(&payload)
    }
}

/// 起動した時点で決まる値(旧 Python は、標準の fd の種類を起動のときに調べて、`sys.stdout` などの作り方を決める)。
struct Startup {
    directory: [bool; 3],
    blksize: [i64; 3],
    stdout_terminal: bool,
    thread: u64,
}

fn execute<O: Ops, P: Precheck<O::Dir>>(
    ops: &O,
    precheck: &P,
    startup: &Startup,
) -> std::process::ExitCode {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let context = Context {
        ops,
        candidates: &CandidateKeys,
        precheck,
        env: &ProcessEnv,
        directory: startup.directory,
        blksize: startup.blksize,
        thread: startup.thread,
        // 端末の桁数は、引数の解析のとき(旧 `argparse` の整形のとき)に調べる。
        terminal_columns: dev_workflow_platform::task_digest::stdout_columns(),
        stdout_terminal: startup.stdout_terminal,
    };
    let code = run(&context, &args, &mut Standard(1), &mut Standard(2));
    std::process::ExitCode::from(u8::try_from(code).unwrap_or(1))
}

fn main() -> std::process::ExitCode {
    // 子のモード: `argv[0]` だけを見て、R4C の設定(信号・起動時の調べ・注入の読取り)より前に S3D の入口へ入る。
    if std::env::args_os()
        .next()
        .is_some_and(|name| name.as_bytes() == CHILD_ARGV0)
    {
        return diff_snapshot::main_entry().into();
    }
    // 自分の実行ファイルの絶対パスは、作業場所を変える前に決めて保持する(事前検査の子の起動に使う)。
    let precheck = SelfPrecheck::new();
    // Python と同じく、ファイルの大きさの上限の信号は無視する(書込みは EFBIG で失敗する)。
    os::ignore_file_size_signal();
    os::install_interrupt_handler();
    let startup = Startup {
        directory: [0, 1, 2].map(os::standard_is_directory),
        blksize: [0, 1, 2].map(os::standard_blksize),
        stdout_terminal: os::standard_is_terminal(1),
        thread: os::current_thread_id(),
    };
    // 検査専用の注入。この入口(example)だけにある環境変数で、製品の入口には無い(前例: R4B_OUTPUT_FAULT)。
    match std::env::var_os("R4C_FAULT_SPEC") {
        None => execute(&RealOps, &precheck, &startup),
        Some(path) => {
            let text = std::fs::read_to_string(&path).expect("R4C_FAULT_SPEC を読めない");
            let spec: serde_json::Value =
                serde_json::from_str(&text).expect("R4C_FAULT_SPEC が JSON でない");
            // 点 `start`: 起動で標準の fd の種類を決めた直後に止まり、比較器が端末の相手側を閉じるなどの窓を作る
            // (旧版は、起動器が同じ点で止まる)。通知と続行は、注入の窓と同じ環境変数の fd。
            if spec["rules"]
                .as_array()
                .is_some_and(|rules| rules.iter().any(|rule| rule["point"] == "start"))
            {
                let fd = |name: &str| {
                    std::env::var(name)
                        .ok()
                        .and_then(|value| value.parse().ok())
                };
                if let (Some(notify), Some(wait)) = (fd("R4C_FAULT_NOTIFY"), fd("R4C_FAULT_WAIT")) {
                    os::fault_injection_notify_and_wait(notify, wait);
                }
            }
            let injected = Injected::new(RealOps, &spec, std::path::Path::new(&path).parent());
            let precheck = InjectedPrecheck {
                inner: precheck,
                injected: &injected,
            };
            execute(&injected, &precheck, &startup)
        }
    }
}

#[cfg(test)]
mod candidate_adapter_tests {
    use super::*;

    fn rendered(problems: &[Problem]) -> Vec<(Option<String>, String)> {
        problems
            .iter()
            .map(|problem| {
                (
                    problem
                        .code
                        .as_ref()
                        .map(|code| String::from_utf8(code.clone()).unwrap()),
                    String::from_utf8(problem.json.render()).unwrap(),
                )
            })
            .collect()
    }

    fn check(body: &str) -> Vec<(Option<String>, String)> {
        rendered(&CandidateKeys.check_text(body).unwrap())
    }

    fn item(code: &str, detail: &str) -> (Option<String>, String) {
        (
            Some(code.to_owned()),
            format!("{{\"code\": \"{code}\", \"detail\": \"{detail}\"}}"),
        )
    }

    const SOURCE_COUNT: &str = "ヘッダの発見元の行が 0 行(照合パターンに合うもの 0 行)。照合パターンに合う行がちょうど 1 行必要";
    const KEY_COUNT: &str = "ヘッダの指摘キーの行が 0 行(値のあるもの 0 個)。ちょうど 1 行が必要";

    /// `expected=None` で出る 7 種の `code` の要素の形(辞書・キーの順 `code`・`detail`)と順を固定する。
    #[test]
    fn the_seven_codes_keep_their_shape_and_order() {
        let body = "> **無人実行**: x\n> **見送り**: 2026-01-01 — r\n> **見送り**: bad\n\n## 追加修正記録\n- [ ] x\n";
        assert_eq!(
            check(body),
            vec![
                item("source-count", SOURCE_COUNT),
                item("key-count", KEY_COUNT),
                item(
                    "unattended-meta",
                    "ヘッダに無人実行のメタ行がある(候補には書かない)"
                ),
                item("skipped", "ヘッダに見送りの行がある"),
                item(
                    "skipped-malformed",
                    "見送りの書式違いの行が 1 行ある(`> **見送り**: YYYY-MM-DD — <理由>`。区切りは em ダッシュ `—` だけ)"
                ),
                item(
                    "record-section",
                    "`## 追加修正記録` の見出しがある(候補には書かない)"
                ),
                item(
                    "checkbox",
                    "チェックボックス形の行がある(フェンスの中も数える。行 6)"
                ),
            ]
        );
    }

    #[test]
    fn a_clean_candidate_has_no_problems_and_header_less_text_has_the_two_count_problems_in_order()
    {
        assert!(check("> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n").is_empty());
        assert_eq!(
            check("x\n"),
            vec![
                item("source-count", SOURCE_COUNT),
                item("key-count", KEY_COUNT)
            ]
        );
    }

    #[test]
    fn only_the_two_skip_codes_are_the_blocking_ones_and_the_rest_stay_warnings_in_order() {
        let body = "> **発見元**: refactor\n> **指摘キー**: H41\n> **見送り**: 2026-01-01 — r\n\n## 指摘\n- [x] a\n";
        let problems = CandidateKeys.check_text(body).unwrap();
        let codes: Vec<_> = problems
            .iter()
            .map(|problem| String::from_utf8(problem.code.clone().unwrap()).unwrap())
            .collect();
        assert_eq!(codes, ["skipped", "checkbox"]);
    }

    #[test]
    fn unexpected_value_types_are_an_internal_failure() {
        let message = UNEXPECTED_RESULT.as_bytes().to_vec();
        for value in [
            KeysJson::Null,
            KeysJson::Bool(true),
            KeysJson::Int {
                negative: false,
                digits: b"1".to_vec(),
            },
            KeysJson::Float(1.5),
        ] {
            assert_eq!(convert(&value).unwrap_err(), message);
            // 辞書・配列の中の値でも止まる。
            let nested = KeysJson::Array(vec![KeysJson::Object(vec![(
                r4a_rest_support::chars::from_str("detail"),
                value,
            )])]);
            assert_eq!(convert(&nested).unwrap_err(), message);
        }
        // 不正な符号位置(サロゲート)も、成功にしない。
        assert_eq!(
            convert(&KeysJson::String(vec![0xd800])).unwrap_err(),
            message
        );
        // `problems` が無い・配列でない戻りも、成功にしない。
        assert_eq!(problems_of(&KeysJson::Null).unwrap_err(), message);
        assert_eq!(problems_of(&KeysJson::Object(vec![])).unwrap_err(), message);
        // `code` が文字列でない要素は `code` を持たない(辞書そのものは移る)。
        let payload = KeysJson::Object(vec![(
            r4a_rest_support::chars::from_str("problems"),
            KeysJson::Array(vec![KeysJson::Object(vec![(
                r4a_rest_support::chars::from_str("code"),
                KeysJson::Array(vec![]),
            )])]),
        )]);
        let moved = problems_of(&payload).unwrap();
        assert_eq!(moved[0].code, None);
        assert_eq!(moved[0].json.render(), b"{\"code\": []}");
        // 辞書のキーの順と文字列の UTF-8 への変換。
        let value = convert(&KeysJson::Object(vec![
            (r4a_rest_support::chars::from_str("b"), KeysJson::str("é")),
            (
                r4a_rest_support::chars::from_str("a"),
                KeysJson::Array(vec![KeysJson::str("x")]),
            ),
        ]))
        .unwrap();
        assert_eq!(value.render(), "{\"b\": \"é\", \"a\": [\"x\"]}".as_bytes());
    }
}
