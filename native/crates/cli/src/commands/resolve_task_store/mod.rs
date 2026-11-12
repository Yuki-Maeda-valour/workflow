//! `resolve-task-store.py` の移植。YAML自体は読まない。

use crate::commands::r4a_rest_support::args::{self, Action, Env, Kind, NArgs, Parser, Value};
use crate::commands::r4a_rest_support::chars::{self, PyStr};
use crate::commands::r4a_rest_support::driver;
use crate::commands::r4a_rest_support::exception::{Frame, PyException};
use crate::commands::r4a_rest_support::json::{self, JsonPolicy, PyJson};
use crate::commands::r4a_rest_support::stream::{Program, Raised};

pub const PROG: &str = "resolve-task-store.py";
const DESCRIPTION: &str =
    "解析済みの profile 値からタスクの置き場を選ぶ。YAML 自体は呼出側で解析する。";
const VALUE_JSON_HELP: &str = "YAML 解析後の task_store の JSON 値。省略時は file";
const INVALID: &str =
    "task_store は file または github-issue を指定する(無い・null・空文字は file)";
const NOT_JSON: &str =
    "--value-json が JSON ではない。task_store は file または github-issue を指定する";

/// 旧版の呼出履歴(行番号とソースの行は旧スクリプトのまま)。
const MODULE: Frame = Frame {
    file: PROG,
    line: 41,
    func: "<module>",
    source: Some("sys.exit(main())"),
};
const MAIN_PARSE: Frame = Frame {
    file: PROG,
    line: 26,
    func: "main",
    source: Some("args = parser.parse_args()"),
};
const MAIN_LOADS: Frame = Frame {
    file: PROG,
    line: 28,
    func: "main",
    source: Some("value = json.loads(args.value_json) if args.value_json is not None else None"),
};
const MAIN_PRINT_NOT_JSON: Frame = Frame {
    file: PROG,
    line: 31,
    func: "main",
    source: Some(
        "print(\"--value-json が JSON ではない。task_store は file または github-issue を指定する\", file=sys.stderr)",
    ),
};
const MAIN_PRINT_INVALID: Frame = Frame {
    file: PROG,
    line: 34,
    func: "main",
    source: Some("print(str(exc), file=sys.stderr)"),
};
const MAIN_PRINT_OUTPUT: Frame = Frame {
    file: PROG,
    line: 36,
    func: "main",
    source: Some("print(json.dumps({\"task_store\": store}, ensure_ascii=False))"),
};

/// `--value-json` の JSON を読むときの再帰の深さ(`<module>`→`main`→`loads` の呼出の分を引いた値)。
pub const JSON_DEPTH: usize = 994;

/// `-OO` 相当(`PYTHONOPTIMIZE` が 2 以上)では `__doc__` が `None` になり、説明の段落が無くなる。
pub fn parser(optimize: i32) -> Parser {
    Parser {
        prog: PROG,
        description: (optimize < 2).then_some(DESCRIPTION),
        actions: vec![
            Action {
                option_strings: &["-h", "--help"],
                dest: "help",
                nargs: NArgs::Zero,
                kind: Kind::Help,
                help: Some("show this help message and exit"),
            },
            Action {
                option_strings: &["--value-json"],
                dest: "value_json",
                nargs: NArgs::One,
                kind: Kind::Store,
                help: Some(VALUE_JSON_HELP),
            },
        ],
    }
}

/// `resolve(value)`。値は解析済みの JSON(`None` は引数なし)。
pub fn resolve(value: Option<&PyJson>) -> Result<&'static str, &'static str> {
    match value {
        None | Some(PyJson::Null) => Ok("file"),
        Some(PyJson::String(text)) if text.is_empty() => Ok("file"),
        Some(PyJson::String(text)) if *text == chars::from_str("file") => Ok("file"),
        Some(PyJson::String(text)) if *text == chars::from_str("github-issue") => {
            Ok("github-issue")
        }
        _ => Err(INVALID),
    }
}

fn kind_name(value: &Value) -> &'static str {
    match value {
        Value::List(_) => "list",
        Value::None => "NoneType",
        Value::Str(_) => "str",
        Value::Bool(_) => "bool",
    }
}

/// `main()`。戻り値は旧版の `return` の値、または未処理の例外・`SystemExit`。
pub fn main(program: &mut Program, env: &Env, argv: &[PyStr]) -> Result<i32, Raised> {
    let base = [MODULE, MAIN_PARSE];
    let namespace = match parser(env.optimize).parse(env, argv) {
        Ok(namespace) => namespace,
        Err(exit) => return Err(driver::handle_exit(program, exit, &base)),
    };
    let loads_caller = [MODULE, MAIN_LOADS];
    let value = match args::get(&namespace, "value_json") {
        Value::None => None,
        Value::Str(text) => {
            let policy = JsonPolicy {
                int_max_digits: env.int_max_digits,
                depth: JSON_DEPTH,
            };
            match json::loads_text(text, policy) {
                Ok(value) => Some(value),
                Err(failure) if failure.is_value_error() => {
                    let handled = driver::json_exception(&failure, &[MAIN_LOADS]);
                    return print_error(program, NOT_JSON, MAIN_PRINT_NOT_JSON, handled);
                }
                Err(failure) => {
                    return Err(Raised::Exception(driver::json_exception(
                        &failure,
                        &loads_caller,
                    )));
                }
            }
        }
        other => {
            return Err(Raised::Exception(driver::not_str_exception(
                kind_name(other),
                &loads_caller,
            )));
        }
    };
    let store = match resolve(value.as_ref()) {
        Ok(store) => store,
        Err(message) => {
            return print_error_plain(program, message);
        }
    };
    let body = chars::from_str(&format!("{{\"task_store\": \"{store}\"}}"));
    program
        .print_stdout(&body, &[MODULE, MAIN_PRINT_OUTPUT])
        .map_err(Raised::Exception)?;
    Ok(0)
}

/// `except ValueError:` の中の `print(..., file=sys.stderr); return 2`。
fn print_error(
    program: &mut Program,
    message: &str,
    site: Frame,
    handled: PyException,
) -> Result<i32, Raised> {
    program
        .print_stderr(&chars::from_str(message), &[MODULE, site])
        .map_err(|mut exception| {
            exception.context = Some(Box::new(handled));
            Raised::Exception(exception)
        })?;
    Ok(2)
}

/// `except TaskStoreError as exc:` の中。保持する例外の呼出履歴は `resolve` の中の `raise`。
fn print_error_plain(program: &mut Program, message: &str) -> Result<i32, Raised> {
    let handled = PyException::new(
        "__main__.TaskStoreError",
        message,
        &[
            MAIN_RESOLVE,
            Frame {
                file: PROG,
                line: 20,
                func: "resolve",
                source: Some(
                    "raise TaskStoreError(\"task_store は file または github-issue を指定する(無い・null・空文字は file)\")",
                ),
            },
        ],
    );
    print_error(program, message, MAIN_PRINT_INVALID, handled)
}

const MAIN_RESOLVE: Frame = Frame {
    file: PROG,
    line: 29,
    func: "main",
    source: Some("store = resolve(value)"),
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_accepts_only_the_closed_set() {
        assert_eq!(resolve(None), Ok("file"));
        assert_eq!(resolve(Some(&PyJson::String(vec![]))), Ok("file"));
        assert_eq!(resolve(Some(&PyJson::Null)), Ok("file"));
        assert_eq!(
            resolve(Some(&PyJson::String(chars::from_str("github-issue")))),
            Ok("github-issue")
        );
        assert_eq!(resolve(Some(&PyJson::Bool(false))), Err(INVALID));
    }
}
