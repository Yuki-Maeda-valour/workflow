mod probe;
mod resolve_task_dir;
// D2(#542): S2L と S2E を製品へ登録する。製品の bin のテストのビルドでは 2 つのモジュールを組まず、
// 下の小さな置き換えを使う(2 つのモジュールの単体テストは、検査専用の入口を根にしたパスで別のプロセスを起動するので、
// `cargo test --example loop_state_fixture`・`--example s2e_contract` で流す)。
#[cfg(not(test))]
mod environment_guard;
#[cfg(not(test))]
mod loop_state;

use dev_workflow_core::{CommandIo, CommandResult};
use std::ffi::OsString;

pub struct Command {
    pub name: &'static str,
    pub run: fn(&[OsString], &mut CommandIo<'_>) -> CommandResult,
}

// 移植する機能は commands/<名前>/mod.rs に置き、この固定表に加える。
pub const COMMANDS: &[Command] = &[
    Command {
        name: "probe",
        run: |args, _io| probe::run(args),
    },
    Command {
        name: "resolve-task-dir",
        run: |args, _io| resolve_task_dir::run(args),
    },
    // 3 つは標準出力・標準エラーへ旧版と同じ順に直接書き、終了コードだけを返す(最終出力は空)。
    Command {
        name: "loop-state",
        run: |args, _io| Ok(exit_only(loop_state::main(args))),
    },
    Command {
        name: "environment-guard",
        run: |args, _io| {
            Ok(exit_only(environment_guard::run_product(
                args,
                environment_guard::Form::Direct,
            )))
        },
    },
    Command {
        name: "environment-guard-held",
        run: |args, _io| {
            Ok(exit_only(environment_guard::run_product(
                args,
                environment_guard::Form::Held,
            )))
        },
    },
];

/// 出力を返さず、終了コードだけを渡す最終出力。
fn exit_only(code: u8) -> dev_workflow_core::CommandOutput {
    dev_workflow_core::CommandOutput {
        stdout: Vec::new(),
        stderr: Vec::new(),
        code: dev_workflow_core::ExitCode::new(code),
    }
}

/// 製品の bin のテストのビルドだけで使う、登録表の名前と呼出し口だけの置き換え(D2 の決めたことの 10)。
#[cfg(test)]
mod environment_guard {
    #[derive(Clone, Copy)]
    pub enum Form {
        Direct,
        Held,
    }

    pub fn run_product(_args: &[std::ffi::OsString], form: Form) -> u8 {
        match form {
            Form::Direct => 200,
            Form::Held => 201,
        }
    }
}

/// 製品の bin のテストのビルドだけで使う、登録表の名前と呼出し口だけの置き換え(D2 の決めたことの 10)。
#[cfg(test)]
mod loop_state {
    pub fn main(_args: &[std::ffi::OsString]) -> u8 {
        202
    }
}
