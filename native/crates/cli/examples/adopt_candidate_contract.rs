#![forbid(unsafe_code)]
//! R4C専用の先行検査入口。製品登録表を変更せず、純粋部品を実コンパイルする。
//! 依存部品(#471 の候補解析・#472 の事前検査)は未接続で、呼ばれたら停止の形で知らせる。
#[path = "../src/commands/adopt_candidate/mod.rs"]
mod adopt_candidate;

use adopt_candidate::{
    Candidates, Child, Context, Injected, InjectedPrecheck, Ops, Precheck, Problem, ProcessEnv,
    RealOps, Standard, run,
};
use dev_workflow_platform::adopt_candidate as os;

/// #471・#472 の実部品が main に入るまでの境界の置き場。呼ばれたら、未接続であることを旧版の停止の形で知らせる。
struct Unconnected;

impl Candidates for Unconnected {
    fn check_text(&self, _text: &str) -> Result<Vec<Problem>, Vec<u8>> {
        Err("R4C の依存部品(#471 の候補解析)は未接続です"
            .as_bytes()
            .to_vec())
    }
}

impl<D> Precheck<D> for Unconnected {
    fn run(
        &self,
        _dir: &D,
        _root: &[u8],
        _accept: Option<&[u8]>,
        _env: &[(Vec<u8>, Vec<u8>)],
    ) -> Result<Child, Vec<u8>> {
        Err("R4C の依存部品(#472 の事前検査)は未接続です"
            .as_bytes()
            .to_vec())
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
        candidates: &Unconnected,
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
        None => execute(&RealOps, &Unconnected, &startup),
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
                inner: Unconnected,
                injected: &injected,
            };
            execute(&injected, &precheck, &startup)
        }
    }
}
