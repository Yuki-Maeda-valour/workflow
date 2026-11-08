#![forbid(unsafe_code)]

mod commands;

use dev_workflow_core::{CommandError, CommandOutput, CommandResult, build_info, emit};
use std::ffi::OsString;

const HELP: &str = "使い方:\n  dev-workflow --version\n  dev-workflow --help\n  dev-workflow probe --json\n  dev-workflow probe --self-sha256\n  dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]\n";

fn main() -> std::process::ExitCode {
    emit(run(&std::env::args_os().skip(1).collect::<Vec<_>>())).into()
}

fn run(args: &[OsString]) -> CommandResult {
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
    let Some(command) = commands::COMMANDS.iter().find(|entry| first == entry.name) else {
        return Err(CommandError::usage("未知のコマンドです"));
    };
    (command.run)(&args[1..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn global_options_are_exclusive() {
        for args in [
            vec![],
            vec!["unknown"],
            vec!["--help", "probe"],
            vec!["--version", "--help"],
        ] {
            let args = args.into_iter().map(OsString::from).collect::<Vec<_>>();
            assert!(matches!(run(&args), Err(CommandError::Usage(_))));
        }
    }

    #[test]
    fn version_and_help_have_the_promised_shape() {
        let version = String::from_utf8(run(&["--version".into()]).unwrap().stdout).unwrap();
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
        assert_eq!(run(&["--help".into()]).unwrap().stdout, HELP.as_bytes());
    }

    #[cfg(unix)]
    #[test]
    fn non_utf8_command_is_a_usage_error() {
        use std::os::unix::ffi::OsStringExt;
        assert!(matches!(
            run(&[OsString::from_vec(vec![0xff])]),
            Err(CommandError::Usage(_))
        ));
    }
}
