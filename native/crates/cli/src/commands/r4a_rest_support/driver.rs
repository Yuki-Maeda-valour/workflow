//! argparse の終了・JSON の失敗を、旧版と同じ出力と例外の呼出履歴へつなぐ。

use super::args::{CauseSite, ErrorOrigin, Exit};
use super::exception::{Frame, PyException};
use super::frames::*;
use super::json::JsonFailure;
use super::stream::{Program, Raised};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Target {
    Stdout,
    Stderr,
    /// `print_usage(sys.stderr)`: `sys.stderr` が `None` なら `sys.stdout` へ書く(`argparse.py` の `print_usage`)。
    StderrThenStdout,
}

/// `parser._print_message(text, file)`。`file` が `None` なら標準エラーへ書く。
fn print_message(
    program: &mut Program,
    text: &[u32],
    first: Target,
    frames: &[Frame],
) -> Result<(), PyException> {
    let wants_stdout = match first {
        Target::Stdout => program.stdout.is_some(),
        Target::StderrThenStdout => program.stderr.is_none() && program.stdout.is_some(),
        Target::Stderr => false,
    };
    if wants_stdout {
        program.write_stdout(text, frames)
    } else {
        program.write_stderr(text, frames)
    }
}

fn join(base: &[Frame], more: &[Frame]) -> Vec<Frame> {
    base.iter().chain(more.iter()).copied().collect()
}

/// `parse_args()` が終了したときの書込みと `SystemExit`。`base` は呼出元までのフレーム
/// (`<module>` と `main` の `parse_args` の行)。
pub fn handle_exit(program: &mut Program, exit: Exit, base: &[Frame]) -> Raised {
    match exit {
        Exit::Help { text } => {
            let frames = join(
                base,
                &[
                    ARGPARSE_PARSE_ARGS_HELP,
                    ARGPARSE_PARSE_KNOWN,
                    ARGPARSE_CONSUME_OPTIONAL_CALL,
                    ARGPARSE_TAKE_ACTION_CALL,
                    ARGPARSE_ACTION_CALL,
                    ARGPARSE_HELP_ACTION,
                    ARGPARSE_PRINT_HELP,
                    ARGPARSE_PRINT_MESSAGE,
                ],
            );
            match print_message(program, &text, Target::Stdout, &frames) {
                Ok(()) => Raised::Exit(0),
                Err(exception) => Raised::Exception(exception),
            }
        }
        Exit::Error {
            usage,
            message,
            origin,
            cause,
        } => {
            // `ArgumentError` を捕まえた後の `error()` の失敗は、元の例外の連鎖つきで表示される。
            let context = cause.map(|(text, site)| {
                let raise = match site {
                    CauseSite::ExpectedOne => vec![ARGPARSE_CONSUME_MATCH, ARGPARSE_MATCH_RAISE],
                    CauseSite::ExplicitBundled => vec![ARGPARSE_RAISE_BUNDLED],
                    CauseSite::ExplicitLong => vec![ARGPARSE_RAISE_LONG],
                };
                let mut frames = vec![ARGPARSE_PARSE_KNOWN, ARGPARSE_CONSUME_OPTIONAL_CALL];
                frames.extend(raise);
                let mut e = PyException::new("argparse.ArgumentError", "", &frames);
                e.message = text;
                e
            });
            let entry: Vec<Frame> = match origin {
                ErrorOrigin::Extra => vec![ARGPARSE_PARSE_ARGS_EXTRA],
                ErrorOrigin::Argument => vec![ARGPARSE_PARSE_ARGS_HELP, ARGPARSE_PARSE_KNOWN_ERROR],
                ErrorOrigin::Ambiguous => vec![
                    ARGPARSE_PARSE_ARGS_HELP,
                    ARGPARSE_PARSE_KNOWN,
                    ARGPARSE_CLASSIFY,
                    ARGPARSE_PARSE_OPTIONAL_AMBIGUOUS,
                ],
            };
            let usage_frames = join(
                &join(base, &entry),
                &[
                    ARGPARSE_ERROR_USAGE,
                    ARGPARSE_PRINT_USAGE,
                    ARGPARSE_PRINT_MESSAGE,
                ],
            );
            if let Err(mut exception) =
                print_message(program, &usage, Target::StderrThenStdout, &usage_frames)
            {
                exception.context = context.clone().map(Box::new);
                return Raised::Exception(exception);
            }
            let message_frames = join(
                &join(base, &entry),
                &[ARGPARSE_ERROR_EXIT, ARGPARSE_EXIT, ARGPARSE_PRINT_MESSAGE],
            );
            match print_message(program, &message, Target::Stderr, &message_frames) {
                Ok(()) => Raised::Exit(2),
                Err(mut exception) => {
                    exception.context = context.map(Box::new);
                    Raised::Exception(exception)
                }
            }
        }
    }
}

/// `json.loads` の失敗を、旧版の呼出履歴付きの例外にする。`caller` は `loads` を呼んだ行まで。
pub fn json_exception(failure: &JsonFailure, caller: &[Frame]) -> PyException {
    let mut frames: Vec<Frame> = caller.to_vec();
    let message = failure.message();
    match failure {
        JsonFailure::Decode { message: text, .. } if text.starts_with("Unexpected UTF-8 BOM") => {
            frames.push(JSON_LOADS_BOM);
        }
        JsonFailure::Decode { message: text, .. } if *text == "Extra data" => {
            frames.extend([JSON_LOADS_DECODE, JSON_DECODE_EXTRA]);
        }
        JsonFailure::Decode { message: text, .. } if *text == "Expecting value" => {
            frames.extend([JSON_LOADS_DECODE, JSON_DECODE_RAW, JSON_RAW_EXPECTING]);
        }
        _ => frames.extend([JSON_LOADS_DECODE, JSON_DECODE_RAW, JSON_RAW_SCAN]),
    }
    let mut exception = PyException::new(failure.exception_name(), &message, &frames);
    exception.message = super::chars::from_str(&message);
    exception
}

pub fn not_str_exception(type_name: &str, caller: &[Frame]) -> PyException {
    let mut frames = caller.to_vec();
    frames.push(JSON_LOADS_NOT_STR);
    PyException::new(
        "TypeError",
        &format!("the JSON object must be str, bytes or bytearray, not {type_name}"),
        &frames,
    )
}
