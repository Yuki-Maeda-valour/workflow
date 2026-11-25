//! 引数から改名の計画までの純粋な部分。旧 `main()` と `adopt()` の先頭から、OS を触る前までと同じ順序。
//! 順序: 環境の印 → 無人実行 → root と候補の絶対化 → rootからの相対化 → 候補名 → 採用名。
use super::args::{ArgValue, Arguments};
use super::diagnostics as text;
use super::paths::PyPath;

/// 捕捉される失敗(`候補の採用を停止: ` に続く表示)と、捕捉されない Python の例外。
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Stop {
    /// 旧 `except (OSError, ValueError, UnicodeError, SubprocessError)` が捕まえる失敗の表示。
    Caught(Vec<u8>),
    /// 旧版が捕まえずに traceback で終わる例外。
    Uncaught(Uncaught),
    /// 保護区間の後に SIGINT が見つかった(旧版では `try` の外へ出た KeyboardInterrupt)。信号で終わる。
    Interrupted,
}

/// 旧版が捕まえない例外。明示値が `--`(空のリスト)になる引数で起きる。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Uncaught {
    /// `--root=--`: `Path([])` の `TypeError`。
    RootNotPath,
    /// `--expect-sha256=--`: `re.fullmatch` の `TypeError`。
    DigestNotString,
}

fn caught(message: &str) -> Stop {
    Stop::Caught(message.as_bytes().to_vec())
}

/// OS を触る前に決まる計画。
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Plan {
    pub(crate) root: PyPath,
    pub(crate) source: PyPath,
    /// `source.relative_to(root)`。Git へ渡す pathspec の元になる。
    pub(crate) relative: PyPath,
    pub(crate) new_name: Vec<u8>,
}

impl Plan {
    /// `source.with_name(f'進行中_{new_name}.md')`。
    pub(crate) fn destination(&self) -> Result<PyPath, Stop> {
        let name = [
            "進行中_".as_bytes(),
            self.new_name.as_slice(),
            b".md".as_slice(),
        ]
        .concat();
        self.source.with_name(&name).map_err(Stop::Caught)
    }
}

/// `os.getcwd()` の結果。失敗は `OSError` の表示。
pub(crate) type Cwd<'a> = &'a dyn Fn() -> Result<Vec<u8>, Vec<u8>>;

fn path_argument(value: &ArgValue, cwd: Cwd<'_>) -> Result<PyPath, Stop> {
    match value {
        ArgValue::Text(text) => {
            let path = PyPath::parse(text);
            if path.is_absolute() {
                Ok(path)
            } else {
                Ok(path.absolute(&cwd().map_err(Stop::Caught)?))
            }
        }
        ArgValue::EmptyList => Err(Stop::Uncaught(Uncaught::RootNotPath)),
    }
}

/// `loop_iteration` は環境変数 `DEV_WORKFLOW_LOOP_ITER` の値(空文字は未設定と同じ)。
/// `cwd` は相対のパスの数だけ呼ぶ。
pub(crate) fn plan(
    arguments: &Arguments,
    loop_iteration: Option<&[u8]>,
    cwd: Cwd<'_>,
) -> Result<Plan, Stop> {
    let refuse_loop = || {
        if loop_iteration.is_some_and(|value| !value.is_empty()) {
            Err(caught(text::LOOP_REFUSAL))
        } else {
            Ok(())
        }
    };
    refuse_loop()?;
    if arguments.unattended {
        return Err(caught(text::UNATTENDED_REFUSAL));
    }
    refuse_loop()?;
    let root = path_argument(&arguments.root, cwd)?;
    let source = path_argument(&arguments.candidate, cwd)?;
    let relative = source.relative_to(&root).map_err(Stop::Caught)?;
    let name = source.name();
    if !name.starts_with("候補_".as_bytes())
        || !name.ends_with(b".md")
        || name == "候補_.md".as_bytes()
    {
        return Err(caught(text::NOT_A_CANDIDATE_NAME));
    }
    let new_name = match &arguments.name {
        Some(ArgValue::Text(value)) => value.clone(),
        Some(ArgValue::EmptyList) => Vec::new(),
        None => name["候補_".len()..name.len() - 3].to_vec(),
    };
    if new_name.is_empty()
        || new_name
            .iter()
            .any(|byte| matches!(byte, b'/' | b'\\' | 0 | b'\n' | b'\r'))
        || new_name == b"."
        || new_name == b".."
    {
        return Err(caught(text::INVALID_ADOPT_NAME));
    }
    Ok(Plan {
        root,
        source,
        relative,
        new_name,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> ArgValue {
        ArgValue::Text(value.as_bytes().to_vec())
    }

    fn arguments(root: &str, candidate: &str, name: Option<ArgValue>) -> Arguments {
        Arguments {
            root: text(root),
            expect_sha256: text("s"),
            name,
            accept: None,
            unattended: false,
            candidate: text(candidate),
        }
    }

    fn here() -> Result<Vec<u8>, Vec<u8>> {
        Ok(b"/w".to_vec())
    }

    fn refusal(arguments: &Arguments, loop_iteration: Option<&[u8]>) -> Vec<u8> {
        match plan(arguments, loop_iteration, &here) {
            Err(Stop::Caught(message)) => message,
            other => panic!("拒否のはず: {other:?}"),
        }
    }

    #[test]
    fn derives_the_adopted_name_from_the_candidate_file_name() {
        let given = arguments("/r", "/r/d/候補_a b.md", None);
        let planned = plan(&given, None, &here).unwrap();
        assert_eq!(planned.new_name, b"a b");
        assert_eq!(planned.relative.to_bytes(), "d/候補_a b.md".as_bytes());
        assert_eq!(
            planned.destination().unwrap().to_bytes(),
            "/r/d/進行中_a b.md".as_bytes()
        );
    }

    #[test]
    fn relative_arguments_use_the_working_directory_and_keep_dot_dot() {
        let given = arguments("r", "r/./x/../候補_a.md", None);
        let planned = plan(&given, None, &here).unwrap();
        assert_eq!(planned.root.to_bytes(), b"/w/r");
        assert_eq!(planned.relative.to_bytes(), "x/../候補_a.md".as_bytes());
        assert_eq!(
            planned.destination().unwrap().to_bytes(),
            "/w/r/x/../進行中_a.md".as_bytes()
        );
        let failing = plan(&given, None, &|| {
            Err(b"[Errno 2] No such file or directory".to_vec())
        });
        assert_eq!(
            failing,
            Err(Stop::Caught(
                b"[Errno 2] No such file or directory".to_vec()
            ))
        );
    }

    #[test]
    fn checks_run_in_the_old_order() {
        let mut given = arguments("/r", "/other/候補_a.md", Some(text("")));
        given.unattended = true;
        assert_eq!(refusal(&given, Some(b"1")), text::LOOP_REFUSAL.as_bytes());
        assert_eq!(
            refusal(&given, Some(b"")),
            text::UNATTENDED_REFUSAL.as_bytes()
        );
        given.unattended = false;
        assert!(
            refusal(&given, None).ends_with(b"OR one path is relative and the other is absolute.")
        );
        let given = arguments("/r", "/r/x.md", Some(text("")));
        assert_eq!(refusal(&given, None), text::NOT_A_CANDIDATE_NAME.as_bytes());
        let given = arguments("/r", "/r/候補_.md", None);
        assert_eq!(refusal(&given, None), text::NOT_A_CANDIDATE_NAME.as_bytes());
        for name in ["", ".", "..", "a/b", "a\\b", "a\nb", "a\rb", "a\0b"] {
            let given = arguments("/r", "/r/候補_a.md", Some(text(name)));
            assert_eq!(
                refusal(&given, None),
                text::INVALID_ADOPT_NAME.as_bytes(),
                "{name:?}"
            );
        }
        let given = arguments("/r", "/r/候補_a.md", Some(ArgValue::EmptyList));
        assert_eq!(refusal(&given, None), text::INVALID_ADOPT_NAME.as_bytes());
        assert_eq!(
            plan(
                &arguments("/r", "/r/候補_a.md", Some(text(".hidden"))),
                None,
                &here
            )
            .unwrap()
            .new_name,
            b".hidden"
        );
    }

    #[test]
    fn candidate_name_may_hold_raw_bytes_and_the_root_itself_is_not_a_candidate() {
        let planned = plan(&arguments("/r", "/r/候補_\u{e9}.md", None), None, &here).unwrap();
        assert_eq!(planned.new_name, "\u{e9}".as_bytes());
        let given = arguments("/r", "/r", None);
        assert_eq!(refusal(&given, None), text::NOT_A_CANDIDATE_NAME.as_bytes());
    }

    #[test]
    fn a_list_valued_path_is_an_uncaught_type_error() {
        let mut given = arguments("/r", "/r/候補_a.md", None);
        given.candidate = ArgValue::EmptyList;
        assert_eq!(
            plan(&given, None, &here),
            Err(Stop::Uncaught(Uncaught::RootNotPath))
        );
    }
}
