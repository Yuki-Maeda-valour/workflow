mod args;
mod bytes;
mod paths;
mod unicode;

use bytes::{characters, concat, json, raw};
use dev_workflow_core::{CommandOutput, CommandResult, ExitCode};
use std::ffi::OsString;
use std::fs;

struct ResolutionError {
    message: Vec<u8>,
    candidates: Option<Vec<Vec<u8>>>,
}

impl From<Vec<u8>> for ResolutionError {
    fn from(message: Vec<u8>) -> Self {
        Self {
            message,
            candidates: None,
        }
    }
}

fn message(text: &str) -> ResolutionError {
    text.as_bytes().to_vec().into()
}

fn placeholder(value: &[u8]) -> bool {
    value.windows(2).enumerate().any(|(start, pair)| {
        pair == b"{{"
            && value[start + 2..]
                .iter()
                .position(|byte| matches!(byte, b'{' | b'}'))
                .is_some_and(|end| value.get(start + 2 + end..start + 4 + end) == Some(b"}}"))
    })
}

fn placeholder_error(value: &[u8], root: &[u8]) -> ResolutionError {
    let mut error = concat(&["task_dir にテンプレートの置換漏れ({{...}})が残っています: ".as_bytes(), value,
        "。task_dir を管理プロジェクトルートからの相対パスに直すか、キーを消して検出に任せてください".as_bytes()]);
    if !value.starts_with(b"/") {
        let lexical = paths::lexical(&paths::join(root, value));
        if paths::inside(&lexical, root) {
            let shown = paths::relative(&lexical, root);
            if placeholder(&shown) && fs::symlink_metadata(paths::path(&lexical)).is_ok() {
                error.extend(concat(&[
                    "。".as_bytes(),
                    &shown,
                    " が既に在ります。中身を正しい保存先へ移してから削除してください".as_bytes(),
                ]));
            }
        }
    }
    error.into()
}

fn validate(value: &[u8], root: &[u8]) -> Result<(Vec<u8>, Vec<u8>), ResolutionError> {
    if value.is_empty() {
        return Err(message("task_dir は空文字にできません"));
    }
    if value.starts_with(b"/") {
        return Err(message(
            "task_dir は管理プロジェクトルートからの相対パスで指定してください",
        ));
    }
    let mut current = root.to_vec();
    for part in paths::parts(value) {
        current = paths::join(&current, part);
        // Python は is_symlink を先に評価する。リンク切れは exists より優先する。
        let link = paths::is_link(&current)
            .map_err(|error| concat(&["task_dir を検査できません: ".as_bytes(), &error]))?;
        let exists = paths::metadata(&current, true)
            .map_err(|error| concat(&["task_dir を検査できません: ".as_bytes(), &error]))?;
        if link && exists.is_none() {
            return Err(message(
                "task_dir にリンク切れのシンボリックリンクが含まれています",
            ));
        }
        if exists.is_some_and(|metadata| !metadata.is_dir()) {
            return Err(message(
                "task_dir またはその祖先が既存ファイルを指しています",
            ));
        }
    }
    let resolved = paths::resolve(&paths::join(root, value), false)
        .map_err(|error| concat(&["task_dir を解決できません: ".as_bytes(), &error]))?;
    if !paths::inside(&resolved, root) {
        return Err(message("task_dir が管理プロジェクトの外を指しています"));
    }
    let normalized = paths::relative(&resolved, root);
    current = root.to_vec();
    for part in paths::parts(&normalized) {
        current = paths::join(&current, part);
        if paths::metadata(&current, true)?.is_some_and(|metadata| !metadata.is_dir()) {
            return Err(message(
                "task_dir またはその祖先が既存ファイルを指しています",
            ));
        }
    }
    Ok((normalized, resolved))
}

fn state_file(name: &[u8]) -> bool {
    let states = ["進行中_", "完了_", "中断_", "保留_", "候補_"];
    states.iter().any(|state| {
        name.strip_prefix(state.as_bytes()).is_some_and(|suffix| {
            suffix.ends_with(b".md") && suffix.len() > 3 && !suffix.contains(&b'\n')
        })
    })
}

fn has_state(directory: &[u8]) -> Result<bool, ResolutionError> {
    let failure = |error| {
        concat(&[
            "タスク候補を読み取れません: ".as_bytes(),
            directory,
            b": ",
            &paths::os_error(&error, directory),
        ])
    };
    for entry in fs::read_dir(paths::path(directory)).map_err(failure)? {
        let entry = entry.map_err(failure)?;
        let kind = entry.file_type().map_err(failure)?;
        if kind.is_file() && state_file(raw(&entry.file_name())) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn candidates(root: &[u8]) -> Result<Vec<Vec<u8>>, ResolutionError> {
    let mut found = Vec::new();
    for container in [
        root.to_vec(),
        paths::join(root, b"docs"),
        paths::join(root, b".claude"),
    ] {
        if paths::metadata(&container, true)?.is_none()
            || paths::is_link(&container)?
            || !paths::is_dir(&container)?
        {
            continue;
        }
        let failure = |error| {
            concat(&[
                "タスク候補の親ディレクトリを読み取れません: ".as_bytes(),
                &container,
                b": ",
                &paths::os_error(&error, &container),
            ])
        };
        let mut children = fs::read_dir(paths::path(&container))
            .map_err(failure)?
            .map(|entry| entry.map(|entry| raw(entry.file_name().as_os_str()).to_vec()))
            .collect::<Result<Vec<_>, _>>()
            .map_err(failure)?;
        children.sort_by_cached_key(|name| characters(name));
        for child in children {
            let child = paths::join(&container, &child);
            if paths::is_link(&child)? || !paths::is_dir(&child)? {
                continue;
            }
            if has_state(&child)? {
                found.push(paths::relative(&child, root));
            }
        }
    }
    found.sort_by_cached_key(|name| characters(name));
    found.dedup();
    Ok(found)
}

fn array(values: &[Vec<u8>]) -> Vec<u8> {
    concat(&[
        b"[",
        &values
            .iter()
            .map(|value| json(value))
            .collect::<Vec<_>>()
            .join(&b", "[..]),
        b"]",
    ])
}

fn resolve(arguments: args::Arguments) -> Result<Vec<u8>, ResolutionError> {
    let root = paths::resolve(raw(&arguments.root), true).map_err(|error| {
        concat(&[
            "管理プロジェクトルートを解決できません: ".as_bytes(),
            &error,
        ])
    })?;
    if !paths::is_dir(&root)? {
        return Err(message(
            "管理プロジェクトルートがディレクトリではありません",
        ));
    }
    let (selected, source) = if let Some(value) = arguments.task_dir {
        let value = raw(&value);
        if placeholder(value) {
            return Err(placeholder_error(value, &root));
        }
        (value.to_vec(), b"profile".as_slice())
    } else {
        let candidates = candidates(&root)?;
        let leftovers = candidates
            .iter()
            .filter(|candidate| placeholder(candidate))
            .cloned()
            .collect::<Vec<_>>();
        if !leftovers.is_empty() {
            return Err(concat(&[
                "テンプレートの置換漏れ({{...}})の名前のディレクトリにタスクがあります: "
                    .as_bytes(),
                &leftovers.join(&b", "[..]),
                "。中身を正しい保存先へ移してから削除してください".as_bytes(),
            ])
            .into());
        }
        if candidates.len() > 1 {
            return Err(ResolutionError {
                message: "タスクディレクトリの候補が複数あります。task_dir を明示してください"
                    .as_bytes()
                    .to_vec(),
                candidates: Some(candidates),
            });
        }
        if let Some(selected) = candidates.into_iter().next() {
            (selected, b"detected".as_slice())
        } else {
            (b"docs/tasks".to_vec(), b"default".as_slice())
        }
    };
    let (normalized, path) = validate(&selected, &root)?;
    Ok(concat(&[
        b"{\"path\": ",
        &json(&path),
        b", \"source\": ",
        &json(source),
        b", \"task_dir\": ",
        &json(&normalized),
        b"}\n",
    ]))
}

pub fn run(arguments: &[OsString]) -> CommandResult {
    let args = match args::parse(arguments) {
        Ok(args) => args,
        Err(output) => return Ok(output),
    };
    Ok(match resolve(args) {
        Ok(stdout) => CommandOutput::stdout(stdout),
        Err(error) => {
            let (start, code) = if let Some(candidates) = error.candidates {
                (
                    concat(&[b"{\"candidates\": ", &array(&candidates), b", "]),
                    ExitCode::Usage,
                )
            } else {
                (b"{".to_vec(), ExitCode::Runtime)
            };
            CommandOutput {
                stdout: concat(&[&start, b"\"error\": ", &json(&error.message), b"}\n"]),
                stderr: Vec::new(),
                code,
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_names_and_placeholders() {
        for name in ["進行中_項目.md", "完了_x.md", "候補_\r.md"] {
            assert!(state_file(name.as_bytes()));
        }
        for name in ["進行中_.md", "候補_a\nb.md", "保留_a.md\n", "x_作業.md"] {
            assert!(!state_file(name.as_bytes()));
        }
        for value in ["{{X}}", "a{{}}b", "{{{{X}}", "{{X}}/../docs"] {
            assert!(placeholder(value.as_bytes()));
        }
        for value in ["{X}", "{{x}", "{{a{b}"] {
            assert!(!placeholder(value.as_bytes()));
        }
    }
}
