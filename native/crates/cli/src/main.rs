#![forbid(unsafe_code)]

mod commands;

// 受け継いだ`SIGPIPE`の扱いを`main`より前に控える(D2・#542のQ2のA)。`loop-state`・`environment-guard`の設定の段階の失敗と
// 1つ目の地域設定の警告は、旧Pythonと同じく受け継いだ扱いで書く。`main`の前の処理は扱いを読むだけで、書き込みも確保もしない。
dev_workflow_platform::s2e_record_startup_state!();

use dev_workflow_core::{
    CommandError, CommandIo, CommandOutput, CommandResult, build_info, execute,
};
use std::ffi::OsString;

const HELP: &str = "使い方:\n  dev-workflow --version\n  dev-workflow --help\n  dev-workflow probe --json\n  dev-workflow probe --self-sha256\n  dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]\n  dev-workflow loop-state COMMAND [ARGS...]\n  dev-workflow environment-guard COMMAND [ARGS...]\n  dev-workflow environment-guard-held COMMAND [ARGS...]\n";

fn main() -> std::process::ExitCode {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    execute(|io| run(&args, io, commands::COMMANDS)).into()
}

fn run(args: &[OsString], io: &mut CommandIo<'_>, commands: &[commands::Command]) -> CommandResult {
    let Some(first) = args.first() else {
        return Err(CommandError::usage("コマンドを指定してください"));
    };
    if first == "--version" || first == "--help" {
        if args.len() != 1 {
            return Err(CommandError::usage(
                "--version・--help は単独で指定してください",
            ));
        }
        return if first == "--version" {
            Ok(CommandOutput::stdout(
                format!(
                    "dev-workflow {} (source_input_sha256: {})\n",
                    build_info::NATIVE_VERSION,
                    build_info::SOURCE_INPUT_SHA256
                )
                .into_bytes(),
            ))
        } else {
            Ok(CommandOutput::stdout(HELP.as_bytes()))
        };
    }
    let Some(command) = commands.iter().find(|entry| first == entry.name) else {
        return Err(CommandError::usage("未知のコマンドです"));
    };
    (command.run)(&args[1..], io)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_core::{ExitCode, execute_to};
    use std::io;

    fn run(args: &[OsString]) -> CommandOutput {
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        let code = execute_to(&mut io::empty(), &mut stdout, &mut stderr, |io| {
            super::run(args, io, commands::COMMANDS)
        });
        CommandOutput {
            stdout,
            stderr,
            code,
        }
    }

    #[test]
    fn global_options_are_exclusive() {
        for args in [
            vec![],
            vec!["unknown"],
            vec!["--help", "probe"],
            vec!["--version", "--help"],
        ] {
            let args = args.into_iter().map(OsString::from).collect::<Vec<_>>();
            let output = run(&args);
            assert_eq!(output.code, ExitCode::USAGE);
            assert!(output.stdout.is_empty());
            assert!(output.stderr.starts_with(b"dev-workflow: "));
        }
    }

    #[test]
    fn version_and_help_have_the_promised_shape() {
        let output = run(&["--version".into()]);
        assert_eq!(output.code, ExitCode::SUCCESS);
        assert!(output.stderr.is_empty());
        let version = String::from_utf8(output.stdout).unwrap();
        assert_eq!(version.lines().count(), 1);
        assert!(version.ends_with('\n'));
        assert!(version.contains(build_info::NATIVE_VERSION));
        assert!(version.contains(build_info::SOURCE_INPUT_SHA256));
        assert_eq!(build_info::SOURCE_INPUT_SHA256.len(), 64);
        assert!(
            build_info::SOURCE_INPUT_SHA256
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        );
        assert!(build_info::RUSTC.starts_with("rustc 1.99.1 "));
        let output = run(&["--help".into()]);
        assert_eq!(output.code, ExitCode::SUCCESS);
        assert!(output.stderr.is_empty());
        assert_eq!(output.stdout, HELP.as_bytes());
    }

    #[cfg(unix)]
    #[test]
    fn non_utf8_command_is_a_usage_error() {
        use std::os::unix::ffi::OsStringExt;
        let output = run(&[OsString::from_vec(vec![0xff])]);
        assert_eq!(output.code, ExitCode::USAGE);
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            "dev-workflow: 未知のコマンドです\n".as_bytes()
        );
    }

    fn stand_in_code(name: &str) -> ExitCode {
        let output = run(&[name.into(), "argument".into()]);
        assert!(output.stdout.is_empty() && output.stderr.is_empty());
        output.code
    }

    #[test]
    fn the_direct_environment_guard_reaches_the_direct_form() {
        assert_eq!(stand_in_code("environment-guard"), ExitCode::new(200));
    }

    #[test]
    fn the_held_environment_guard_reaches_the_held_form() {
        assert_eq!(stand_in_code("environment-guard-held"), ExitCode::new(201));
    }

    #[test]
    fn loop_state_reaches_the_loop_state_entry() {
        assert_eq!(stand_in_code("loop-state"), ExitCode::new(202));
    }

    #[test]
    fn registered_commands_receive_arguments_and_the_same_io() {
        let commands = [commands::Command {
            name: "test-only",
            run: |args, io| {
                assert_eq!(args, [OsString::from("argument")]);
                io.write_stderr(b"ready\0").unwrap();
                let mut input = [0; 2];
                assert_eq!(io.read_input(&mut input).unwrap(), 2);
                assert_eq!(input, [0xff, 0]);
                io.write_stdout(&input).unwrap();
                Ok(CommandOutput {
                    stdout: b"done".to_vec(),
                    stderr: Vec::new(),
                    code: ExitCode::new(255),
                })
            },
        }];
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        let code = execute_to(&mut b"\xff\0".as_slice(), &mut stdout, &mut stderr, |io| {
            super::run(&["test-only".into(), "argument".into()], io, &commands)
        });
        assert_eq!(code, ExitCode::new(255));
        assert_eq!(stdout, b"\xff\0done");
        assert_eq!(stderr, b"ready\0");
        assert_eq!(
            commands::COMMANDS
                .iter()
                .map(|command| command.name)
                .collect::<Vec<_>>(),
            [
                "probe",
                "resolve-task-dir",
                "loop-state",
                "environment-guard",
                "environment-guard-held"
            ]
        );
    }
}
