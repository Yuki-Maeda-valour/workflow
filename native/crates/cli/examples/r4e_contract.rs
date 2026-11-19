#![deny(unsafe_code)]
//! R4E 専用の検査入口。製品の登録表を変えず、`md_tables_to_xlsx` を `#[path]` で取り込む。
//!
//! 起動前の状態の記録だけのために、`main` より前に呼ばれる関数の一覧の項目を 1 つ置く
//! ([`RECORD_STARTUP_STATE`])。項目を置く属性(`link_section`)は unsafe の属性なので、この
//! ファイルは `forbid` ではなく `deny(unsafe_code)` にし、その 1 つの静的変数だけを許す。
//! 記録する関数の本体と、中の unsafe は platform の `r4e` にある。この example は製品とは別の
//! 実行ファイルなので、製品の `dev-workflow` の起動前の関数は増えない。
#[path = "../src/commands/md_tables_to_xlsx/mod.rs"]
mod md_tables_to_xlsx;

use dev_workflow_platform::r4e;
use std::ffi::OsStr;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("R4E の起動前の記録は Linux と Mac だけに対応する");

/// 起動前の関数の一覧の項目。Linux は `.init_array`、Mac は `__DATA,__mod_init_func`
/// (種類 `mod_init_funcs`)に置く。`#[used]` で、最適化と結合で消えないようにする。
#[allow(unsafe_code)]
#[used]
#[cfg_attr(target_os = "linux", unsafe(link_section = ".init_array"))]
#[cfg_attr(
    target_os = "macos",
    unsafe(link_section = "__DATA,__mod_init_func,mod_init_funcs")
)]
static RECORD_STARTUP_STATE: extern "C" fn() = r4e::record_startup_state;

/// 検査専用の隠しの切り替え。値があるときだけ、起動前の記録を 1 行で出して終わる。
/// `1` は標準出力、`stderr` は標準エラー、`file:<パス>` はそのファイルへ書く。
const STARTUP_PROBE: &str = "R4E_CONTRACT_STARTUP_PROBE";

/// 検査専用の隠しの切り替え。値があるときだけ、呼出履歴の段の表(固定名・行番号・関数・ソースの行)
/// を JSON の行で標準出力へ出して終わる。旧版の環境の実物(旧スクリプト・標準ライブラリ・openpyxl)
/// と照らすために使う。
const FRAMES_PROBE: &str = "R4E_CONTRACT_FRAMES";

fn main() -> std::process::ExitCode {
    let startup = r4e::startup_state();
    if let Some(mode) = std::env::var_os(STARTUP_PROBE) {
        return startup_probe(&mode, startup.as_ref()).into();
    }
    if std::env::var_os(FRAMES_PROBE).is_some() {
        return frames_probe(&mut std::io::stdout()).into();
    }
    let Some(startup) = startup else {
        eprintln!("r4e_contract: 起動前の記録がありません");
        return md_tables_to_xlsx::HARNESS_FAILURE.into();
    };
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    md_tables_to_xlsx::run(&args, &startup).into()
}

/// 呼出履歴の段の表のすべて(入出力・入力・ブック・引数・出力先の事前検査・ZIP の書き出し)。
fn all_frames() -> impl Iterator<Item = &'static md_tables_to_xlsx::errors::Frame> {
    md_tables_to_xlsx::errors::frames::ALL
        .iter()
        .chain(md_tables_to_xlsx::args::frames::ALL)
        .chain(md_tables_to_xlsx::paths::frames::ALL)
        .chain(md_tables_to_xlsx::zip::frames::ALL)
}

/// 呼出履歴の段の表を JSON の行で書く。
fn frames_probe(out: &mut dyn Write) -> dev_workflow_core::ExitCode {
    let mut text = String::new();
    for frame in all_frames() {
        let line = serde_json::json!({
            "file": frame.file,
            "line": frame.line,
            "function": frame.function,
            "source": frame.source,
        });
        text.push_str(&line.to_string());
        text.push('\n');
    }
    match out.write_all(text.as_bytes()).and_then(|()| out.flush()) {
        Ok(()) => dev_workflow_core::ExitCode::SUCCESS,
        Err(_) => md_tables_to_xlsx::HARNESS_FAILURE,
    }
}

/// 起動前の記録と、標準ライブラリが埋めた後の今の fd の種類を 1 行にする。
fn startup_line(startup: Option<&r4e::StartupState>) -> String {
    let Some(state) = startup else {
        return "r4e-startup not-recorded\n".to_owned();
    };
    let now = [
        r4e::fd_kind(std::io::stdin()),
        r4e::fd_kind(std::io::stdout()),
        r4e::fd_kind(std::io::stderr()),
    ]
    .map(|kind| kind.map_or("error", r4e::FdKind::name))
    .join(" ");
    format!("r4e-startup {} now={now}\n", state.summary())
}

fn startup_probe(mode: &OsStr, startup: Option<&r4e::StartupState>) -> dev_workflow_core::ExitCode {
    let line = startup_line(startup);
    let written = match mode.as_bytes() {
        b"1" => std::io::stdout().write_all(line.as_bytes()),
        b"stderr" => std::io::stderr().write_all(line.as_bytes()),
        bytes => match bytes.strip_prefix(b"file:") {
            Some(path) => std::fs::write(OsStr::from_bytes(path), line.as_bytes()),
            None => {
                eprintln!("r4e_contract: {STARTUP_PROBE} の値が違います");
                return md_tables_to_xlsx::HARNESS_FAILURE;
            }
        },
    };
    match written {
        Ok(()) => dev_workflow_core::ExitCode::SUCCESS,
        Err(_) => md_tables_to_xlsx::HARNESS_FAILURE,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_startup_record_is_placed_before_main() {
        // テストの実行ファイルにも一覧の項目が入るので、記録がある。
        let state = r4e::startup_state().expect("起動前の記録がある");
        let line = startup_line(Some(&state));
        assert!(line.starts_with("r4e-startup "), "{line}");
        assert!(line.contains(" sigpipe="), "{line}");
        assert!(line.ends_with('\n'), "{line}");
        assert_eq!(startup_line(None), "r4e-startup not-recorded\n");
    }

    #[test]
    fn the_frames_probe_lists_every_frame_as_json() {
        let mut out = Vec::new();
        assert_eq!(frames_probe(&mut out), dev_workflow_core::ExitCode::SUCCESS);
        let text = String::from_utf8(out).unwrap();
        let lines = text.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), all_frames().count());
        let first: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(first["file"], "md_tables_to_xlsx.py");
        assert_eq!(first["line"], 241);
        assert_eq!(first["function"], "<module>");
        assert_eq!(first["source"], "    sys.exit(main())");
        let last: serde_json::Value = serde_json::from_str(lines[lines.len() - 1]).unwrap();
        assert_eq!(last["file"], "zipfile.py");
        assert_eq!(last["line"], 1943);
    }

    #[test]
    fn an_unknown_probe_mode_is_rejected() {
        assert_eq!(
            startup_probe(OsStr::new("2"), None),
            md_tables_to_xlsx::HARNESS_FAILURE
        );
    }
}
