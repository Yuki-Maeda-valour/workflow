use super::{
    args::Arguments,
    budget::Budget,
    config::{self, Candidates, Trusted},
    control,
    error::{LegacyException as E, Result},
    json::{PyValue as V, Text},
    process, runtime, scan, state,
};
use dev_workflow_platform::fs::{FsError, SnapshotMatch};
use std::{
    collections::{HashMap, HashSet},
    ffi::OsStr,
    os::unix::{ffi::OsStrExt, fs::DirBuilderExt},
    path::{Path, PathBuf},
};
fn stop(reason: &str) -> E {
    E::stop(reason)
}
/// 旧 `except OSError as exc: raise Stop(reason) from exc`。Stop とほかの例外はそのまま上げる。
fn os_stop(reason: &'static str) -> impl Fn(E) -> E {
    move |e| if e.is_os() { stop(reason) } else { e }
}
pub fn git_dir(top: &Path, budget: &mut Budget) -> Result<PathBuf> {
    let root = state::directory(top)
        .map_err(|e| e.at("held_git_dir", 865, "root = open_dir_path(top)"))?;
    let dotgit = root
        .metadata(OsStr::new(".git"))
        .map_err(|_| stop("worktree の git-dir を安全に読めない"))?;
    budget.tick(0)?;
    if dotgit.is_directory() {
        // 旧 os.open と os.fstat の OSError は except OSError(安全に読めない)、same の不一致だけが差し替わった。
        root.open_dir(OsStr::new(".git"), &dotgit, SnapshotMatch::Full)
            .map_err(|e| match e {
                FsError::Io(_) | FsError::InvalidInput => {
                    stop("worktree の git-dir を安全に読めない")
                }
                _ => stop("worktree の git-dir が差し替わった"),
            })?;
        return state::absolute(&top.join(".git"));
    }
    if dotgit.is_regular() {
        let raw = state::read_at(&root, OsStr::new(".git"), &dotgit, budget)
            .map_err(os_stop("worktree の git-dir を安全に読めない"))?
            .1;
        if !raw.starts_with(b"gitdir: ") || raw.iter().filter(|&&c| c == 10).count() > 1 {
            return Err(stop("worktree の git-dir を読めない"));
        }
        let value = raw[8..].strip_suffix(b"\n").unwrap_or(&raw[8..]);
        if value.is_empty() || value.contains(&0) {
            return Err(stop("worktree の git-dir を読めない"));
        }
        let target = PathBuf::from(runtime::os(&runtime::decode(value))?);
        return Ok(if target.is_absolute() {
            target
        } else {
            control::normalized(&state::absolute(&top.join(target))?)
        });
    }
    Err(stop("worktree の git-dir を安全に開けない"))
}
/// 旧 held_head・held_common_dir の前半。`actual_git_dir != os.path.abspath(expected_git_dir)` は str の比較
/// (Path の == は末尾の「/」・「.」・重なった「/」を無視するので使わない)。
fn exact_admin(top: &Path, expected: &Path, budget: &mut Budget) -> Result<PathBuf> {
    let actual = git_dir(top, budget)?;
    if actual.as_os_str() != state::absolute(expected)?.as_os_str() {
        return Err(stop("worktree の git-dir が基準から変わった"));
    }
    Ok(actual)
}
pub fn common_dir(top: &Path, expected: &Path, common: &Path, budget: &mut Budget) -> Result<()> {
    let actual = exact_admin(top, expected, budget)?;
    let reason = "worktree の commondir を安全に読めない";
    let admin = state::directory(&actual).map_err(os_stop(reason))?;
    let before = match admin.metadata(OsStr::new("commondir")) {
        Ok(v) => v,
        Err(e) if e.is_not_found() => {
            if actual.as_os_str() != state::absolute(common)?.as_os_str() {
                return Err(stop("worktree の commondir が基準から変わった"));
            }
            return Ok(());
        }
        Err(_) => return Err(stop(reason)),
    };
    if !before.is_regular() {
        return Err(stop(reason));
    }
    let raw = state::read_at(&admin, OsStr::new("commondir"), &before, budget)
        .map_err(os_stop(reason))?
        .1;
    if raw.iter().filter(|&&c| c == 10).count() > 1 {
        return Err(stop(reason));
    }
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    if raw.is_empty() || raw.contains(&0) {
        return Err(stop(reason));
    }
    let target = PathBuf::from(runtime::os(&runtime::decode(raw))?);
    let resolved = if target.is_absolute() {
        target
    } else {
        control::normalized(&state::absolute(&actual.join(target))?)
    };
    if resolved.as_os_str() != state::absolute(common)?.as_os_str() {
        return Err(stop("worktree の commondir が基準から変わった"));
    }
    Ok(())
}
pub(super) fn branch(
    top: &Path,
    expected: &Path,
    common: &Path,
    budget: &mut Budget,
) -> Result<Option<Text>> {
    let actual = exact_admin(top, expected, budget)?;
    let reason = "worktree の HEAD を安全に読めない";
    let admin = state::directory(&actual).map_err(os_stop(reason))?;
    let head = admin
        .metadata(OsStr::new("HEAD"))
        .map_err(|_| stop(reason))?;
    if !head.is_regular() {
        return Err(stop(reason));
    }
    let head = state::read_at(&admin, OsStr::new("HEAD"), &head, budget)
        .map_err(os_stop(reason))?
        .1;
    if !head.starts_with(b"ref: refs/heads/") {
        return Ok(None);
    }
    // 旧 head[len(b"ref: "):].strip()(bytes.strip は \x0b も除く)。
    let mut current = runtime::decode(runtime::py_bytes_strip(&head[5..]));
    let mut seen = HashSet::new();
    for _ in 0..32 {
        budget.tick(0)?;
        let raw = runtime::bytes(&current)?;
        let parts = raw.split(|b| *b == 47).collect::<Vec<_>>();
        if !raw.starts_with(b"refs/heads/")
            || !seen.insert(current.clone())
            || parts
                .iter()
                .any(|p| p.is_empty() || *p == b"." || *p == b"..")
        {
            return Err(stop("worktree の HEAD を読めない"));
        }
        let mut parent =
            state::directory(common).map_err(os_stop("worktree の HEAD を読めない"))?;
        let mut missing = false;
        let nul = |line: u32, source: &'static str| {
            E::Python {
                kind: "ValueError",
                reason: "embedded null byte".into(),
                location: "os.stat",
            }
            .trace("held_branch", line, source)
        };
        for part in &parts[..parts.len() - 1] {
            use std::os::unix::ffi::OsStrExt;
            let name = OsStr::from_bytes(part);
            if part.contains(&0) {
                // os.stat の ValueError は except OSError・FileNotFoundError に当たらない。
                return Err(nul(
                    973,
                    "before_parent = os.stat(part, dir_fd=parent, follow_symlinks=False)",
                ));
            }
            let st = match parent.metadata(name) {
                Ok(v) => v,
                Err(e) if e.is_not_found() => {
                    missing = true;
                    break;
                }
                _ => return Err(stop("worktree の HEAD を読めない")),
            };
            if !st.is_directory() {
                return Err(stop("worktree の HEAD を読めない"));
            }
            parent = parent
                .open_dir(name, &st, SnapshotMatch::DirectoryPath)
                .map_err(|_| stop("worktree の HEAD を読めない"))?;
        }
        if missing {
            return Ok(Some(current));
        }
        use std::os::unix::ffi::OsStrExt;
        let name = OsStr::from_bytes(parts.last().unwrap());
        if parts.last().unwrap().contains(&0) {
            return Err(nul(
                994,
                "before = os.stat(name, dir_fd=parent, follow_symlinks=False)",
            ));
        }
        let st = match parent.metadata(name) {
            Ok(v) => v,
            Err(e) if e.is_not_found() => return Ok(Some(current)),
            _ => return Err(stop("worktree の HEAD を読めない")),
        };
        if !st.is_regular() {
            return Err(stop("worktree の HEAD を読めない"));
        }
        let raw = state::read_at(&parent, name, &st, budget)
            .map_err(os_stop("worktree の HEAD を読めない"))?
            .1;
        if !raw.starts_with(b"ref: ") {
            return Ok(Some(current));
        }
        current = runtime::decode(runtime::py_bytes_strip(&raw[5..]));
    }
    Err(stop("worktree の HEAD の symbolic ref が深すぎる"))
}
/// 旧 prior_worktree_git_dirs。鍵と値は保存状態の str のまま(値は os.path.abspath の str)。
pub fn git_dirs(prior: &V) -> Result<HashMap<Text, Text>> {
    if matches!(prior, V::Null) {
        return Ok(HashMap::new());
    }
    let rows = prior
        .get("worktree_configs")
        .list()
        .ok_or_else(|| stop("前の worktree の git-dir 状態を読めない"))?;
    let mut result = HashMap::new();
    for row in rows {
        let (true, Some(path), Some(admin)) = (
            row.is_dict(),
            row.get("path").string(),
            row.get("git_dir").string(),
        ) else {
            return Err(stop("前の worktree の git-dir 状態を読めない"));
        };
        if result.contains_key(path) || !admin.as_codepoints().starts_with(&[47]) {
            return Err(stop("前の worktree の git-dir 状態が重複している"));
        }
        result.insert(path.clone(), state::abspath_text(admin));
    }
    Ok(result)
}
/// 旧 prior_worktree_branches。鍵は保存状態の str のまま。
pub fn branches(prior: &V) -> Result<HashMap<Text, Text>> {
    if matches!(prior, V::Null) {
        return Ok(HashMap::new());
    }
    let rows = prior
        .get("worktrees")
        .list()
        .ok_or_else(|| stop("前の worktree の branch 状態を読めない"))?;
    let mut result = HashMap::new();
    for row in rows {
        let (true, Some(path), Some(branch)) = (
            row.is_dict(),
            row.get("worktree").string(),
            row.get("branch").string(),
        ) else {
            return Err(stop("前の worktree の branch 状態を読めない"));
        };
        if result.insert(path.clone(), branch.clone()).is_some() {
            return Err(stop("前の worktree の branch 状態が重複している"));
        }
    }
    Ok(result)
}
pub(super) fn candidate_preflight(
    top: &Path,
    common: &Path,
    git_dir: &Path,
    expected: &Candidates,
    budget: &mut Budget,
    own_new: bool,
    copy_source: Option<&Path>,
) -> Result<Trusted> {
    let systems = expected
        .iter()
        .filter(|((kind, _), _)| kind == "system")
        .map(|((_, p), _)| p)
        .collect::<HashSet<_>>();
    if systems.len() > 1 {
        return Err(stop("保存済み system config の場所が矛盾している"));
    }
    let mut paths =
        config::candidates_paths(common, git_dir, budget, systems.iter().next().copied())?;
    let missing = paths
        .iter()
        .filter(|p| !expected.iter().any(|(k, _)| k == *p))
        .cloned()
        .collect::<Vec<_>>();
    let own_root = (
        "worktree".to_owned(),
        config::path_text(&state::absolute(&git_dir.join("config.worktree"))?),
    );
    let mut added = Vec::new();
    if !missing.is_empty() {
        if !own_new || missing != vec![own_root.clone()] {
            return Err(stop("config 候補がイテレーションの途中で変わった"));
        }
        // 自分の新しい管理先の候補は OS から得たパスなので、符号化できる。
        let path = &PathBuf::from(runtime::os(&own_root.1)?);
        if path.symlink_metadata().is_ok() {
            let source =
                copy_source.ok_or_else(|| stop("config 候補がイテレーションの途中で変わった"))?;
            let source = state::absolute(source)?;
            // 旧 source_admin == os.path.abspath(common) は str の比較。
            let source_kind = if source.as_os_str() == state::absolute(common)?.as_os_str() {
                "common"
            } else {
                "worktree"
            };
            let source_key = (
                source_kind.to_owned(),
                config::path_text(&state::absolute(&source.join("config.worktree"))?),
            );
            let source = expected
                .iter()
                .find(|(k, _)| k == &source_key)
                .map(|(_, v)| v)
                .filter(|v| v.get("state").is("present"))
                .ok_or_else(|| stop("config 候補がイテレーションの途中で変わった"))?
                .get("entry");
            if !source.is_dict() {
                return Err(stop("保存済み config の形式を読めない"));
            }
            // 旧 except Stop だけを替える(1184 行)。read_open_regular の OSError は「通常ファイルを読めない」(688 行)。
            let (mut file, before) = config::regular(path, budget).map_err(|e| {
                if e.stop_reason().is_some() {
                    stop("config 候補がイテレーションの途中で変わった")
                } else {
                    e
                }
            })?;
            budget.one(before.size.max(0) as u128)?;
            let raw =
                state::read_held(&mut file, budget).map_err(os_stop("通常ファイルを読めない"))?;
            drop(file);
            let source_file = if source.get("kind").is("link") {
                source.get("target_entry")
            } else {
                source
            };
            if !source_file.is_dict()
                || !source_file.get("kind").is("file")
                || source_file.get("sha256") != &V::from(state::hash(&raw))
                || source_file.get("meta").get("mode") != scan::meta(&before).get("mode")
            {
                return Err(stop("config 候補がイテレーションの途中で変わった"));
            }
            let mut name = std::ffi::OsString::from("config:");
            name.push(path);
            added.push((
                own_root.1.clone(),
                V::object([
                    ("path", V::from(runtime::os_text(&name))),
                    ("kind", V::from("file")),
                    ("meta", scan::meta(&before)),
                    ("sha256", V::from(state::hash(&raw))),
                ]),
            ));
        }
        paths.retain(|k| k != &own_root);
    }
    for (kind, path) in paths {
        let saved = &expected
            .iter()
            .find(|(k, _)| k == &(kind.clone(), path.clone()))
            .unwrap()
            .1;
        // 旧 os.path.lexists(str): 符号化できない保存済みのパスも「無い」になる。
        let exists = config::lexists_text(&path);
        if saved.get("state").is("absent") {
            if exists {
                return Err(stop("config 候補がイテレーションの途中で変わった"));
            }
            continue;
        }
        if !exists {
            return Err(stop("config 候補がイテレーションの途中で変わった"));
        }
        let trust = vec![(path.clone(), saved.get("entry").clone())];
        let os_path = PathBuf::from(runtime::os(&path)?);
        let (item, _) =
            config::safe_keyed(top, &os_path, &path, budget, Some(&trust), true, false)?;
        if &V::object([
            ("kind", V::from(kind)),
            ("path", V::from(path.clone())),
            ("state", V::from("present")),
            ("entry", item),
        ]) != saved
        {
            return Err(stop("config 候補がイテレーションの途中で変わった"));
        }
    }
    Ok(added)
}
// 旧条件評価の入力を、事前検査・実効値・分岐基準に分けて明示する。
#[allow(clippy::too_many_arguments)]
fn condition(
    top: &Path,
    expected: &Path,
    common: &Path,
    condition: &Text,
    origin: &Path,
    effective: &config::Rows,
    budget: &mut Budget,
    expected_branch: Option<&Text>,
) -> Result<Option<bool>> {
    let lower = runtime::utf8(condition).to_lowercase();
    let isolated = vec![
        ("GIT_CONFIG_NOSYSTEM".into(), "1".into()),
        ("GIT_CONFIG_GLOBAL".into(), "/dev/null".into()),
    ];
    if lower.starts_with("hasconfig:remote.*.url:") {
        return config::probe(
            top,
            condition,
            origin,
            effective,
            budget,
            Some(Path::new("/")),
            &isolated,
        )
        .map(Some)
        .map_err(|e| {
            e.at(
                "preflight_condition",
                1020,
                r#"return probe_condition(top, condition, origin, effective, budget, execution_top="/", env_extra=isolated)"#,
            )
        });
    }
    if !lower.starts_with("onbranch:") {
        return Ok(
            if lower.starts_with("gitdir:") || lower.starts_with("gitdir/i:") {
                None
            } else {
                Some(false)
            },
        );
    }
    let Some(branch) = branch(top, expected, common, budget).map_err(|e| {
        e.at(
            "preflight_condition",
            1023,
            "branch = held_branch(top, expected_git_dir, common, budget)",
        )
    })?
    else {
        return Ok(Some(false));
    };
    if expected_branch.is_some_and(|v| v != &branch) {
        return Err(stop("worktree の branch が基準から変わった"));
    }
    onbranch(top, condition, origin, effective, budget, &branch, isolated)
}
const MKDTEMP_LINE: &str = r#"parent = tempfile.mkdtemp(prefix="loop-state-branch-")"#;
/// 旧 preflight_condition の onbranch の一時の Git の置き場(1029〜1057 行)。
fn onbranch(
    top: &Path,
    condition: &Text,
    origin: &Path,
    effective: &config::Rows,
    budget: &mut Budget,
    branch: &Text,
    isolated: Vec<(std::ffi::OsString, std::ffi::OsString)>,
) -> Result<Option<bool>> {
    use dev_workflow_platform::loop_state::{OpenFlags, open_file, write_once};
    let os_error = |error: std::io::Error, path: Option<&Path>| E::Os {
        error,
        path: path.map(|p| p.as_os_str().as_bytes().to_vec()),
        location: "preflight_condition",
    };
    let parent = super::legacy_tempfile::mkdtemp("loop-state-branch-")
        .map_err(|e| e.at("preflight_condition", 1029, MKDTEMP_LINE))?;
    let temp = parent.join("git");
    let body = (|| -> Result<Option<bool>> {
        let args = ["init".into(), "--bare".into(), temp.as_os_str().to_owned()];
        let refs = args
            .iter()
            .map(|a| dev_workflow_platform::loop_state::LoopArg::Raw(a))
            .collect::<Vec<_>>();
        process::run(OsStr::new("/"), &refs, &[], budget, false, &isolated).map_err(|e| {
            e.at(
                "preflight_condition",
                1032,
                r#"run_git("/", "init", "--bare", temp, budget=budget, env_extra=isolated)"#,
            )
        })?;
        let head_path = temp.join("HEAD");
        let head = open_file(
            &head_path,
            OpenFlags {
                read: false,
                create_exclusive: false,
                truncate: true,
                nofollow: false,
            },
        )
        .map_err(|e| {
            os_error(e, Some(&head_path)).at(
                "preflight_condition",
                1034,
                "fd = os.open(head_path, os.O_WRONLY | os.O_TRUNC, 0o600)",
            )
        })?;
        let mut content = b"ref: ".to_vec();
        content.extend(runtime::bytes(branch)?);
        content.push(10);
        // os.write は 1 回だけ呼び、途中までの書込みの数は見ない。
        write_once(&head, &content).map_err(|e| {
            os_error(e, None).at(
                "preflight_condition",
                1036,
                r#"os.write(fd, ("ref: " + branch + "\n").encode("utf-8", "surrogateescape"))"#,
            )
        })?;
        drop(head);
        let mut ref_path = temp.clone();
        for part in branch.as_codepoints().split(|c| *c == 47) {
            if !part.is_empty() {
                ref_path.push(runtime::os(&super::json::text(part.to_vec()))?);
            }
        }
        makedirs(ref_path.parent().unwrap_or(&temp)).map_err(|e| {
            e.at(
                "preflight_condition",
                1040,
                "os.makedirs(os.path.dirname(ref_path), mode=0o700, exist_ok=True)",
            )
        })?;
        let reference = open_file(
            &ref_path,
            OpenFlags {
                read: false,
                create_exclusive: true,
                truncate: false,
                nofollow: false,
            },
        )
        .map_err(|e| {
            os_error(e, Some(&ref_path)).at(
                "preflight_condition",
                1041,
                "ref_fd = os.open(ref_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
            )
        })?;
        write_once(&reference, b"0000000000000000000000000000000000000000\n").map_err(|e| {
            os_error(e, None).at(
                "preflight_condition",
                1043,
                r#"os.write(ref_fd, b"0" * 40 + b"\n")"#,
            )
        })?;
        drop(reference);
        let mut extra = isolated.clone();
        extra.push(("GIT_DIR".into(), temp.as_os_str().to_owned()));
        config::probe(top, condition, origin, effective, budget, Some(Path::new("/")), &extra)
            .map(Some)
            .map_err(|e| {
                e.at(
                    "preflight_condition",
                    1046,
                    r#"return probe_condition(top, condition, origin, effective, budget, execution_top="/","#,
                )
            })
    })();
    // finally: init が temp を作る前に失敗したときは、後始末で元の失敗を置き換えない。
    match (body, cleanup_temporary(&temp, &parent)) {
        (body, Ok(())) => body,
        (Ok(_), Err(error)) => Err(error),
        (Err(first), Err(error)) => Err(error.contextual(first, false)),
    }
}
/// 旧 preflight_condition の finally(1051〜1057 行)。失敗はファイル名つきの OSError と旧の行。
pub(super) fn cleanup_temporary(temp: &Path, parent: &Path) -> Result<()> {
    fn failure(error: std::io::Error, path: &Path, line: u32, source: &'static str) -> E {
        E::Os {
            error,
            path: Some(path.as_os_str().as_bytes().to_vec()),
            location: "preflight_condition.cleanup",
        }
        .trace("preflight_condition", line, source)
    }
    // os.path.isdir は辿る stat。失敗は False。
    if temp.is_dir() {
        walk_bottom_up(temp, &mut |base, directories, files| {
            for name in files {
                let path = base.join(name);
                std::fs::remove_file(&path).map_err(|e| {
                    failure(
                        e,
                        &path,
                        1053,
                        "for name in files: os.unlink(os.path.join(base, name))",
                    )
                })?;
            }
            for name in directories {
                let path = base.join(name);
                std::fs::remove_dir(&path).map_err(|e| {
                    failure(
                        e,
                        &path,
                        1054,
                        "for name in directories: os.rmdir(os.path.join(base, name))",
                    )
                })?;
            }
            Ok(())
        })?;
        std::fs::remove_dir(temp).map_err(|e| failure(e, temp, 1055, "os.rmdir(temp)"))?;
    }
    if parent.is_dir() {
        std::fs::remove_dir(parent).map_err(|e| failure(e, parent, 1057, "os.rmdir(parent)"))?;
    }
    Ok(())
}
type WalkVisit<'a> =
    dyn FnMut(&Path, Vec<std::ffi::OsString>, Vec<std::ffi::OsString>) -> Result<()> + 'a;
/// os.walk(top, topdown=False, onerror=None, followlinks=False) の順。
/// - 子の分類は DirEntry.is_dir()(リンクを辿る)。ディレクトリへのリンクも directories に入る。
/// - 降りるかは DirEntry.is_symlink()(辿らない)。リンクの先へは降りない。
/// - 列挙の失敗は無視して、その段を出さない。降りた先を処理してから、その段を出す。
fn walk_bottom_up(top: &Path, visit: &mut WalkVisit<'_>) -> Result<()> {
    let Ok(entries) = std::fs::read_dir(top) else {
        return Ok(());
    };
    let mut directories = Vec::new();
    let mut files = Vec::new();
    let mut walk_into = Vec::new();
    for entry in entries {
        let Ok(entry) = entry else {
            return Ok(());
        };
        let path = entry.path();
        let is_directory = std::fs::metadata(&path).is_ok_and(|m| m.is_dir());
        if is_directory {
            directories.push(entry.file_name());
            if !entry.file_type().is_ok_and(|t| t.is_symlink()) {
                walk_into.push(path);
            }
        } else {
            files.push(entry.file_name());
        }
    }
    for path in walk_into {
        walk_bottom_up(&path, visit)?;
    }
    visit(top, directories, files)
}
/// os.makedirs(name, mode=0o700, exist_ok=True)。途中のディレクトリは既定の 0o777。
fn makedirs(name: &Path) -> Result<()> {
    fn make(name: &Path, mode: u32, exist_ok: bool) -> std::io::Result<()> {
        if let Some(head) = name.parent()
            && !head.as_os_str().is_empty()
            && name.file_name().is_some()
            && std::fs::metadata(head).is_err()
        {
            match make(head, 0o777, exist_ok) {
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                other => other?,
            }
        }
        match std::fs::DirBuilder::new().mode(mode).create(name) {
            Ok(()) => Ok(()),
            Err(e) => {
                if !exist_ok || !name.is_dir() {
                    Err(e)
                } else {
                    Ok(())
                }
            }
        }
    }
    make(name, 0o700, true).map_err(|error| E::Os {
        error,
        path: Some(name.as_os_str().as_bytes().to_vec()),
        location: "os.makedirs",
    })
}
pub(super) trait Operations {
    /// path は保存状態の str(trusted の鍵)。OS のバイトへは実装の中で変える。
    fn file(
        &mut self,
        top: &Path,
        path: &Text,
        budget: &mut Budget,
        trusted: &Trusted,
        parse: bool,
    ) -> Result<(V, config::Rows)>;
    /// origin は保存状態の str(trusted の鍵)。結果も str のまま返す。
    fn include(
        &mut self,
        top: &Path,
        value: Option<&Text>,
        origin: &Text,
        budget: &mut Budget,
    ) -> Result<Text>;
    #[allow(clippy::too_many_arguments)]
    fn condition(
        &mut self,
        top: &Path,
        expected: &Path,
        common: &Path,
        condition: &Text,
        origin: &Path,
        effective: &config::Rows,
        budget: &mut Budget,
        expected_branch: Option<&Text>,
    ) -> Result<Option<bool>>;
}
struct RealOperations;
impl Operations for RealOperations {
    fn file(
        &mut self,
        top: &Path,
        path: &Text,
        budget: &mut Budget,
        trusted: &Trusted,
        parse: bool,
    ) -> Result<(V, config::Rows)> {
        let os_path = state::encode_config_path(path)?;
        config::safe_keyed(top, &os_path, path, budget, Some(trusted), true, parse)
    }
    fn include(
        &mut self,
        top: &Path,
        value: Option<&Text>,
        origin: &Text,
        budget: &mut Budget,
    ) -> Result<Text> {
        config::include_text(top, value, origin, budget)
    }
    fn condition(
        &mut self,
        top: &Path,
        expected: &Path,
        common: &Path,
        c: &Text,
        origin: &Path,
        effective: &config::Rows,
        budget: &mut Budget,
        expected_branch: Option<&Text>,
    ) -> Result<Option<bool>> {
        condition(
            top,
            expected,
            common,
            c,
            origin,
            effective,
            budget,
            expected_branch,
        )
    }
}
#[allow(clippy::too_many_arguments)]
pub fn context(
    top: &Path,
    trusted: Option<&mut Trusted>,
    candidates: Option<&Candidates>,
    budget: &mut Budget,
    expected: Option<&Text>,
    common: &Path,
    expected_branch: Option<&Text>,
    own_new: bool,
    copy_source: Option<&Path>,
) -> Result<()> {
    context_with_operations(
        top,
        trusted,
        candidates,
        budget,
        expected,
        common,
        expected_branch,
        own_new,
        copy_source,
        &mut RealOperations,
    )
}
#[allow(clippy::too_many_arguments)]
pub(super) fn context_with_operations(
    top: &Path,
    trusted: Option<&mut Trusted>,
    candidates: Option<&Candidates>,
    budget: &mut Budget,
    expected: Option<&Text>,
    common: &Path,
    expected_branch: Option<&Text>,
    own_new: bool,
    copy_source: Option<&Path>,
    operations: &mut dyn Operations,
) -> Result<()> {
    let Some(trusted) = trusted else {
        return Ok(());
    };
    let expected = expected.ok_or_else(|| stop("worktree の git-dir の基準が無い"))?;
    let candidates = candidates.ok_or_else(|| stop("worktree の git-dir の基準が無い"))?;
    // 旧版は str で比べる(保存状態の基準を OS のバイトへ変えない)。
    let held = git_dir(top, budget).map_err(|e| {
        e.at(
            "preflight_trusted_configs",
            1643,
            "if held_git_dir(top, budget) != os.path.abspath(expected_git_dir):",
        )
    })?;
    if config::path_text(&held) != state::abspath_text(expected) {
        return Err(stop("worktree の git-dir が基準から変わった"));
    }
    // 一致したので、基準は OS から読んだ git-dir と同じ文字番号で、符号化できる。
    let expected = &PathBuf::from(runtime::os(expected)?);
    common_dir(top, expected, common, budget)?;
    for (path, item) in candidate_preflight(
        top,
        common,
        expected,
        candidates,
        budget,
        own_new,
        copy_source,
    )? {
        if let Some((_, old)) = trusted.iter().find(|(p, _)| p == &path) {
            if old != &item {
                return Err(stop("保存済み config の状態が矛盾している"));
            }
        } else {
            trusted.push((path, item));
        }
    }
    for (path, _) in trusted.iter() {
        operations.file(top, path, budget, trusted, false).map_err(|e| {
            e.at(
                "preflight_trusted_configs",
                1659,
                "item, _ = safe_config_file(top, path, budget, trusted, require_exact=True, parse=False)",
            )
        })?;
    }
    let mut prepared = Vec::new();
    let mut effective = Vec::new();
    for (path, _) in trusted.iter() {
        let (item, rows) = operations
            .file(top, path, budget, trusted, true)
            .map_err(|e| {
                e.at(
                    "preflight_trusted_configs",
                    1664,
                    "item, rows = safe_config_file(top, path, budget, trusted, require_exact=True)",
                )
            })?;
        effective.extend(
            rows.iter()
                .filter(|(key, _)| {
                    let k = runtime::utf8(key).to_lowercase();
                    k != "include.path" && !k.starts_with("includeif.")
                })
                .cloned(),
        );
        prepared.push((path.clone(), item, rows));
    }
    for (origin_text, item, rows) in prepared {
        // 開けた保存済みの origin なので符号化できる(include の相対パスの起点に使う)。
        let origin = PathBuf::from(runtime::os(&origin_text)?);
        for (key, value) in rows {
            let folded = runtime::utf8(&key).to_lowercase();
            let target = if folded == "include.path" {
                operations
                    .include(top, value.as_ref(), &origin_text, budget)
                    .map_err(|e| {
                        e.at(
                            "preflight_trusted_configs",
                            1672,
                            "target = include_target(top, value, origin, budget)",
                        )
                    })?
            } else if folded.starts_with("includeif.") && folded.ends_with(".path") {
                let condition_text = config::includeif_condition(&key);
                match operations
                    .condition(
                        top,
                        expected,
                        common,
                        &condition_text,
                        &origin,
                        &effective,
                        budget,
                        expected_branch,
                    )
                    .map_err(|e| {
                        e.at(
                            "preflight_trusted_configs",
                            1675,
                            "active = preflight_condition(top, expected_git_dir, common, condition, origin, effective, budget,",
                        )
                    })? {
                    None => {
                        let target = operations
                            .include(top, value.as_ref(), &origin_text, budget)
                            .map_err(|e| e.at("preflight_trusted_configs", 1682, "target = include_target(top, value, origin, budget)"))?;
                        if !trusted
                            .iter()
                            .any(|(p, _)| *p == target)
                            && trusted
                                .iter()
                                .find(|(p, _)| *p == origin_text)
                                .map(|(_, v)| v)
                                != Some(&item)
                        {
                            return Err(stop("config の条件付き origin を安全に判定できない"));
                        }
                        continue;
                    }
                    Some(false) => continue,
                    Some(true) => operations
                        .include(top, value.as_ref(), &origin_text, budget)
                        .map_err(|e| e.at("preflight_trusted_configs", 1688, "target = include_target(top, value, origin, budget)"))?,
                }
            } else {
                continue;
            };
            if !trusted.iter().any(|(p, _)| *p == target) {
                return Err(stop("config の新しい origin は基準が無いため開けない"));
            }
        }
    }
    Ok(())
}
pub fn run(args: &Arguments, int_limit: usize) -> Result<()> {
    let mut budget = Budget::new(args.limits.clone()).map_err(|e|e.at("preflight",1927,"budget = Budget(args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)"))?;
    let prior = state::read(Path::new(args.raw("state")), &args.limits, int_limit).map_err(|e| {
        e.at(
            "preflight",
            1928,
            "prior = read_state(args.state, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)",
        )
    })?;
    if !state::schema(&prior) {
        return Err(stop("前の状態が新しい必須欄を持たない"));
    }
    let top = control::real_at(
        Path::new(args.raw("top")),
        3,
        "preflight",
        1931,
        r#"top = os.path.realpath(args.top)"#,
    )?;
    let common = control::real_at(
        Path::new(args.raw("common")),
        3,
        "preflight",
        1932,
        r#"common = os.path.realpath(args.common)"#,
    )?;
    let admin = state::absolute(Path::new(args.raw("repo-admin")))?;
    let mut trusted = config::trusted(&prior, false)?;
    let candidates = config::trusted_candidates(&prior, false)?;
    let git_dirs = git_dirs(&prior)?;
    let branches = branches(&prior)?;
    let rows = prior
        .get("worktrees")
        .list()
        .ok_or_else(|| stop("前の config の状態を読めない"))?;
    if git_dirs.get(&config::path_text(&top)) != Some(&config::path_text(&admin)) {
        return Err(stop("保存済み状態の起動元と現在の管理パスが一致しない"));
    }
    for path in [common.join("config"), common.join("config.worktree")] {
        if !candidates.as_ref().is_some_and(|c| {
            c.iter()
                .any(|((k, p), _)| k == "common" && *p == config::path_text(&path))
        }) {
            return Err(stop(
                "保存済み状態の common config が現在の管理パスと一致しない",
            ));
        }
    }
    for row in rows {
        // 旧版の行は str のまま dict を引く(OS のバイトへは held_git_dir の open で変える)。
        let path = row
            .get("worktree")
            .string()
            .cloned()
            .ok_or_else(|| stop("前の worktree の状態を読めない"))?;
        let expected = git_dirs
            .get(&path)
            .ok_or_else(|| stop("worktree の git-dir の基準が無い"))?;
        let own = row
            .get("locked")
            .string()
            .is_some_and(|s| runtime::utf8(s).starts_with("dev-workflow-loop: "));
        let branch = if own { None } else { branches.get(&path) };
        // 最初の OS の操作は held_git_dir(top) の open_dir_path(1643・865 行)。
        let context_path = state::encode_dir_path(&path)
            .map_err(|e| {
                e.at("held_git_dir", 865, "root = open_dir_path(top)").at(
                    "preflight_trusted_configs",
                    1643,
                    "if held_git_dir(top, budget) != os.path.abspath(expected_git_dir):",
                )
            })
            .and_then(|context_path| {
                context(
                    &context_path,
                    trusted.as_mut(),
                    candidates.as_ref(),
                    &mut budget,
                    Some(expected),
                    &common,
                    branch,
                    false,
                    None,
                )
            });
        context_path.map_err(|e| {
            e.at(
                "preflight",
                1955,
                "preflight_trusted_configs(context, trusted, candidates, budget, expected,",
            )
        })?;
    }
    Ok(())
}
