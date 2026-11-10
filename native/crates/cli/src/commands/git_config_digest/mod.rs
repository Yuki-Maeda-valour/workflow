//! ローカルGit設定の字面・種類・項目順から旧Pythonと同じ確認値を作る。
use super::r4b_support as support;
use dev_workflow_core::{CommandOutput, CommandResult};
use dev_workflow_platform::r4b_process;
use std::ffi::{OsStr, OsString};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::Path;
use std::time::Duration;

fn git(directory: &OsStr, args: &[OsString]) -> Result<Vec<u8>, String> {
    let operation = args[0].to_str().unwrap();
    let done =
        support::git(directory, args, Some(Duration::from_secs(10))).map_err(
            |error| match error {
                support::Failure::Timeout => format!("git {operation} が時間切れ(10 秒)"),
                support::Failure::Io(e) => support::spawn_error(&e, "git"),
            },
        )?;
    if done.code != 0 {
        return Err(format!(
            "git {operation} が失敗した(終了コード {})",
            done.code
        ));
    }
    Ok(done.stdout)
}
fn locations(directory: &OsStr) -> Result<[OsString; 3], String> {
    let out = git(
        directory,
        &support::strings(&[
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
            "--git-path",
            "config.worktree",
        ]),
    )?;
    let paths = out.split(|&c| c == b'\n').collect::<Vec<_>>();
    if paths.len() != 4 || !paths[3].is_empty() || paths[..3].iter().any(|p| p.is_empty()) {
        return Err("git rev-parse の出力が 3 行でない".to_owned());
    }
    Ok(std::array::from_fn(|index| {
        OsString::from_vec(paths[index].to_vec())
    }))
}
fn entries(directory: &OsStr, path: &OsStr) -> Result<String, String> {
    let mut args = support::strings(&["config", "--file"]);
    args.push(path.to_owned());
    args.extend(support::strings(&["--no-includes", "--list", "-z"]));
    let out = git(directory, &args)?;
    let records = out
        .split(|&c| c == 0)
        .filter(|r| !r.is_empty())
        .map(|record| {
            let split = record.iter().position(|&c| c == b'\n');
            let (key, value) = split.map_or((record, "null".to_owned()), |at| {
                (&record[..at], support::ascii_json(&record[at + 1..]))
            });
            format!("[{},{}]", support::ascii_json(key), value)
        })
        .collect::<Vec<_>>();
    Ok(format!("[{}]", records.join(",")))
}
fn describe(directory: &OsStr, path: &OsStr) -> Result<String, String> {
    let quoted = support::ascii_json(path.as_bytes());
    let Some(meta) = r4b_process::metadata(Path::new(path), false)
        .map_err(|e| format!("設定ファイルを調べられない: {}", support::reason(&e)))?
    else {
        return Ok(format!("{{\"kind\":\"absent\",\"path\":{quoted}}}"));
    };
    let (kind, regular, target) = if meta.file_type().is_symlink() {
        let target = r4b_process::read_link(Path::new(path))
            .map_err(|e| format!("symlink の字面を読めない: {}", support::reason(&e)))?;
        let regular = r4b_process::metadata(Path::new(path), true)
            .map_err(|e| format!("symlink の指す先を調べられない: {}", support::reason(&e)))?
            .is_some_and(|m| m.is_file());
        (
            "symlink",
            regular,
            Some(support::ascii_json(target.as_os_str().as_bytes())),
        )
    } else if meta.is_file() {
        ("file", true, None)
    } else {
        ("other", false, None)
    };
    // sort_keys=True。entries・kind・path・targetの順に置く。
    let mut fields = Vec::new();
    if regular {
        fields.push(format!("\"entries\":{}", entries(directory, path)?));
    }
    fields.push(format!("\"kind\":\"{kind}\""));
    fields.push(format!("\"path\":{quoted}"));
    if let Some(target) = target {
        fields.push(format!("\"target\":{target}"));
    }
    Ok(format!("{{{}}}", fields.join(",")))
}
fn join(base: &OsStr, name: &[u8]) -> OsString {
    // os.path.joinは末尾/を保つ。PathBufの部品正規化を使わない。
    let mut bytes = base.as_bytes().to_vec();
    if !bytes.is_empty() && !bytes.ends_with(b"/") {
        bytes.push(b'/');
    }
    bytes.extend_from_slice(name);
    OsString::from_vec(bytes)
}
fn digest(directory: &OsStr) -> Result<String, String> {
    let [git_dir, common_dir, worktree] = locations(directory)?;
    let config = describe(directory, &join(&common_dir, b"config"))?;
    let config_worktree = describe(directory, &join(&git_dir, b"config.worktree"))?;
    let material = format!(
        "{{\"config\":{config},\"config.worktree\":{config_worktree},\"config.worktree-git-path\":{},\"git-common-dir\":{},\"git-dir\":{}}}",
        support::ascii_json(worktree.as_bytes()),
        support::ascii_json(common_dir.as_bytes()),
        support::ascii_json(git_dir.as_bytes())
    );
    Ok(support::hash(material.as_bytes()))
}
fn valid_expect(value: &OsStr) -> bool {
    let value = value.as_bytes();
    value.len() == 71
        && value.starts_with(b"sha256:")
        && value[7..]
            .iter()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c))
}
pub fn run(args: &[OsString]) -> CommandResult {
    let args = match support::args::parse(args, "git-config-digest.py", true) {
        Ok(value) => value,
        Err(output) => return Ok(output),
    };
    if args
        .task_dir
        .as_ref()
        .is_some_and(|value| !valid_expect(value))
    {
        return Ok(support::output(
            "--expect の形が sha256:<64 桁の 16 進の小文字> でない",
            2,
        ));
    }
    if !r4b_process::is_directory(Path::new(&args.root)) {
        return Ok(support::output("--dir がディレクトリでない", 2));
    }
    let value = match digest(&args.root) {
        Ok(value) => value,
        Err(message) => return Ok(support::output(&message, 2)),
    };
    Ok(match args.task_dir {
        None => CommandOutput::stdout(format!("{value}\n").into_bytes()),
        Some(expected) if expected == OsStr::new(&value) => CommandOutput::stdout(Vec::new()),
        Some(_) => support::output("git 設定のダイジェストが --expect と一致しない", 1),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn expect_is_strict() {
        assert!(valid_expect(OsStr::new(&format!(
            "sha256:{}",
            "a".repeat(64)
        ))));
        for v in ["sha256:A", "sha256:", "sha256:00\n"] {
            assert!(!valid_expect(OsStr::new(v)));
        }
    }
    #[test]
    fn joins_preserve_lexical_path() {
        assert_eq!(
            join(OsStr::new("//a/./"), b"config"),
            OsStr::new("//a/./config")
        );
    }
}
