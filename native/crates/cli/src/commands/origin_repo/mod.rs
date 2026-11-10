//! origin-repo.pyの読み取り専用判定。Gitは無期限、SSH設定の展開だけ10秒。
use super::r4b_support as support;
use dev_workflow_core::{CommandOutput, CommandResult};
use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::process::Command;
use std::time::Duration;

#[derive(Debug, PartialEq)]
struct Parsed {
    form: &'static str,
    user: Option<String>,
    host: String,
    owner: String,
    repo: String,
}
impl Parsed {
    fn value(&self) -> String {
        format!("{}/{}/{}", self.host, self.owner, self.repo)
    }
    fn comparable(&self) -> String {
        let value = self.value();
        if self.host == "github.com" {
            value.to_lowercase()
        } else {
            value
        }
    }
}
fn parse(url: &str) -> Option<Parsed> {
    // Pythonの$は末尾LFの直前にも一致する。スラッシュの省略条件は形ごとに保つ。
    let url = url.strip_suffix('\n').unwrap_or(url);
    let (form, authority, path) = if let Some(rest) = url.strip_prefix("https://") {
        let (host, path) = rest.split_once('/')?;
        ("https", host, path.strip_suffix('/').unwrap_or(path))
    } else if let Some(rest) = url.strip_prefix("ssh://") {
        let (host, path) = rest.split_once('/')?;
        ("ssh", host, path.strip_suffix('/').unwrap_or(path))
    } else {
        let (host, path) = url.split_once(':')?;
        ("scp", host, path)
    };
    let (user, host) = if let Some((user, host)) = authority.split_once('@') {
        if user.is_empty()
            || user.contains('@')
            || user.contains('/')
            || (form != "https" && user.contains(':'))
        {
            return None;
        }
        (Some(user.to_owned()), host)
    } else {
        (None, authority)
    };
    if host.is_empty()
        || !host
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b".-".contains(&c))
    {
        return None;
    }
    let (owner, repo) = path.split_once('/')?;
    let part = |s: &str| {
        !s.is_empty()
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
    };
    if !part(owner) || !part(repo) {
        return None;
    }
    let repo = repo.strip_suffix(".git").unwrap_or(repo);
    if repo.is_empty() || matches!(repo, "." | "..") {
        return None;
    }
    Some(Parsed {
        form,
        user: if form == "https" { None } else { user },
        host: host.to_lowercase(),
        owner: owner.to_owned(),
        repo: repo.to_owned(),
    })
}

enum Error {
    Git(String),
    Decode,
}
fn git(directory: &OsStr, args: &[&str]) -> Result<(i32, String), Error> {
    let done =
        support::git(directory, &support::strings(args), None).map_err(|error| match error {
            support::Failure::Io(e) => Error::Git(support::spawn_error(&e, "git")),
            support::Failure::Timeout => unreachable!(),
        })?;
    let out = support::text(&done.stdout).map_err(|_| Error::Decode)?;
    let _err = support::text(&done.stderr).map_err(|_| Error::Decode)?;
    Ok((done.code, out))
}
fn lines(directory: &OsStr, args: &[&str]) -> Result<Vec<String>, Error> {
    let (code, out) = git(directory, args)?;
    if code != 0 {
        return Err(Error::Git(format!(
            "git {} が失敗した(終了コード {code})",
            args[0]
        )));
    }
    Ok(support::splitlines(&out)
        .into_iter()
        .map(str::to_owned)
        .collect())
}
fn config(directory: &OsStr, args: &[&str]) -> Result<String, Error> {
    let args = [&["config"][..], args].concat();
    let (code, out) = git(directory, &args)?;
    if code == 1 {
        return Ok(String::new());
    }
    if code != 0 {
        return Err(Error::Git(format!(
            "git config が失敗した(終了コード {code})"
        )));
    }
    Ok(out.trim_matches(py_space).to_owned())
}
fn py_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}
fn ssh_settings() -> Result<(), String> {
    let done = support::capture(
        Command::new("ssh").args(["-G", "--", "git@github.com"]),
        Some(Duration::from_secs(10)),
    )
    .map_err(|error| match error {
        support::Failure::Timeout => "ssh -G が時間切れ".to_owned(),
        support::Failure::Io(_) => "ssh -G を起動できない".to_owned(),
    })?;
    let out = support::text(&done.stdout).map_err(|_| "ssh -G の出力を読めない")?;
    let _err = support::text(&done.stderr).map_err(|_| "ssh -G の出力を読めない")?;
    if done.code != 0 {
        return Err(format!("ssh -G が失敗した(終了コード {})", done.code));
    }
    check_ssh(&out).map_err(str::to_owned)
}
fn check_ssh(out: &str) -> Result<(), &'static str> {
    const CHECKED: &[&str] = &[
        "hostname",
        "port",
        "stricthostkeychecking",
        "userknownhostsfile",
        "globalknownhostsfile",
        "nohostauthenticationforlocalhost",
        "hostkeyalias",
    ];
    let mut settings = HashMap::new();
    for line in support::splitlines(out) {
        let line = line.trim_matches(py_space);
        let at = line.find(py_space).unwrap_or(line.len());
        let key = line[..at].to_lowercase();
        if !CHECKED.contains(&key.as_str()) {
            continue;
        }
        let value = line[at..].trim_matches(py_space).to_owned();
        if settings.insert(key, value).is_some() {
            return Err("ssh の判定に必要な設定が重複している");
        }
    }
    if CHECKED[..6]
        .iter()
        .any(|key| settings.get(*key).is_none_or(String::is_empty))
    {
        return Err("ssh の判定に必要な設定が欠落または空である");
    }
    if settings["hostname"] != "github.com" {
        return Err("ssh の設定で github.com の hostname が github.com でない");
    }
    if settings["port"] != "22" {
        return Err("ssh の設定で github.com の port が 22 でない");
    }
    let strict = settings["stricthostkeychecking"].to_lowercase();
    if !["yes", "true", "ask", "accept-new"].contains(&strict.as_str()) {
        return Err("ssh のホスト鍵検証を確認できない");
    }
    if settings
        .get("hostkeyalias")
        .is_some_and(|v| v != "github.com")
    {
        return Err("ssh のホスト鍵の照合名が github.com でない");
    }
    if !["no", "false"].contains(
        &settings["nohostauthenticationforlocalhost"]
            .to_lowercase()
            .as_str(),
    ) {
        return Err("ssh のローカル宛先のホスト鍵検証を確認できない");
    }
    let files = settings["userknownhostsfile"]
        .split(py_space)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>();
    if files[0].to_lowercase() == "none" {
        if files.len() != 1 {
            return Err("ssh のユーザー保存先を確認できない");
        }
    } else if strict == "accept-new" && files[0].starts_with('/') {
        let mut parts = Vec::new();
        for p in files[0].split('/') {
            match p {
                "" | "." => (),
                ".." => {
                    parts.pop();
                }
                _ => parts.push(p),
            }
        }
        if parts == ["dev", "null"] {
            return Err("ssh の新しいホスト鍵を保存できない");
        }
    }
    Ok(())
}
#[derive(Default)]
struct Judgment {
    origin: bool,
    same: bool,
    repo: Option<String>,
    form: Option<String>,
    vcs: bool,
    reason: String,
    push_url_sha256: Option<String>,
}
impl Judgment {
    fn json(&self) -> Vec<u8> {
        let string = |v: &Option<String>| {
            v.as_ref()
                .map(|s| String::from_utf8(support::bytes::json(s.as_bytes())).unwrap())
                .unwrap_or_else(|| "null".to_owned())
        };
        format!("{{\"origin\": {}, \"same\": {}, \"repo\": {}, \"form\": {}, \"vcs\": {}, \"reason\": {}, \"push_url_sha256\": {}}}\n",if self.origin {"true"} else {"false"},if self.same {"true"} else {"false"},string(&self.repo),string(&self.form),if self.vcs {"true"} else {"false"},string(&Some(self.reason.clone())),string(&self.push_url_sha256)).into_bytes()
    }
}
fn judge(directory: &OsStr) -> Result<Judgment, Error> {
    let mut result = Judgment::default();
    if !lines(directory, &["remote"])?.iter().any(|v| v == "origin") {
        result.reason = "origin が無い".to_owned();
        return Ok(result);
    }
    result.origin = true;
    if !config(directory, &["--get", "remote.origin.vcs"])?.is_empty() {
        result.vcs = true;
        result.reason = "remote.origin.vcs がある".to_owned();
        return Ok(result);
    }
    let fetch = lines(directory, &["remote", "get-url", "--all", "origin"])?;
    let push = lines(
        directory,
        &["remote", "get-url", "--push", "--all", "origin"],
    )?;
    if fetch.len() != 1 || push.len() != 1 {
        result.reason = "fetch か push の URL が 1 つでない".to_owned();
        return Ok(result);
    }
    let (fetch_url, push_url) = (&fetch[0], &push[0]);
    result.push_url_sha256 = Some(support::hash(push_url.as_bytes()));
    let (fetch, push) = (parse(fetch_url), parse(push_url));
    result.form = Some(push.as_ref().map_or("other", |p| p.form).to_owned());
    result.same = match (&fetch, &push) {
        (Some(a), Some(b)) => a.comparable() == b.comparable(),
        _ => fetch_url == push_url,
    };
    if !result.same {
        result.reason = "fetch と push が別のリポジトリ".to_owned();
        return Ok(result);
    }
    let Some(push) = push else {
        result.reason = "push の URL が読める形でない".to_owned();
        return Ok(result);
    };
    let refusal = if push.form == "https" {
        if std::env::var_os("GIT_SSL_NO_VERIFY").is_some()
            || config(
                directory,
                &["--type=bool", "--get-urlmatch", "http.sslverify", push_url],
            )? == "false"
        {
            Some("TLS の検証を外している".to_owned())
        } else {
            None
        }
    } else if push.user.as_deref() != Some("git") || push.host != "github.com" {
        Some("ssh の push 先が git@github.com でない".to_owned())
    } else if ["GIT_SSH_COMMAND", "GIT_SSH"]
        .iter()
        .any(|key| std::env::var_os(key).is_some_and(|s| !s.is_empty()))
        || !config(directory, &["--get", "core.sshCommand"])?.is_empty()
    {
        Some("ssh を上書きする設定がある".to_owned())
    } else {
        ssh_settings().err()
    };
    match refusal {
        Some(reason) => result.reason = reason,
        None => result.repo = Some(push.value()),
    }
    Ok(result)
}
pub fn run(args: &[OsString]) -> CommandResult {
    let args = match support::args::parse(args, "origin-repo.py", false) {
        Ok(value) => value,
        Err(out) => return Ok(out),
    };
    if !dev_workflow_platform::r4b_process::is_directory(std::path::Path::new(&args.root)) {
        return Ok(support::output("--dir がディレクトリでない", 2));
    }
    Ok(match judge(&args.root) {
        Ok(result) => CommandOutput::stdout(result.json()),
        Err(Error::Git(message)) => support::output(&message, 2),
        Err(Error::Decode) => support::output("git の出力を読めない", 1),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn url_forms_preserve_case_and_reject_unknown() {
        for url in [
            "https://secret@GitHub.com/Owner/Repo.git/",
            "ssh://git@GitHub.com/Owner/Repo.git",
            "git@GitHub.com:Owner/Repo.git",
        ] {
            let p = parse(url).unwrap();
            assert_eq!(p.value(), "github.com/Owner/Repo");
            assert_eq!(p.comparable(), "github.com/owner/repo");
        }
        for url in [
            "https://github.com:443/o/r",
            "git@github.com:o/a/b",
            "https://github.com/o/.git",
            "ssh://git@github.com:22/o/r",
        ] {
            assert!(parse(url).is_none());
        }
    }
    #[test]
    fn empty_json_has_python_order_and_spacing() {
        assert_eq!(
            String::from_utf8(Judgment::default().json()).unwrap(),
            "{\"origin\": false, \"same\": false, \"repo\": null, \"form\": null, \"vcs\": false, \"reason\": \"\", \"push_url_sha256\": null}\n"
        );
    }
}
