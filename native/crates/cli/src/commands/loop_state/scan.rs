use super::{
    budget::Budget,
    control,
    error::{LegacyException, Result, fs_reason},
    json::{PyValue as V, Text},
    runtime, state,
    walk_recursion::{Calls, RegexCache},
};
use dev_workflow_core::loop_state::{self, LegacyIntLimit};
use dev_workflow_platform::{
    fs::{FileSnapshot, HeldDir, SnapshotMatch},
    loop_state::DirectoryNames,
};
use std::{
    ffi::{OsStr, OsString},
    path::Path,
};
pub fn meta(st: &FileSnapshot) -> V {
    V::object([
        ("mode", V::from(i128::from(st.mode & 0o7777))),
        ("size", V::from(st.size)),
        (
            "mtime_ns",
            V::from(st.mtime_seconds * 1_000_000_000 + st.mtime_nanoseconds),
        ),
        (
            "ctime_ns",
            V::from(st.ctime_seconds * 1_000_000_000 + st.ctime_nanoseconds),
        ),
        ("dev", V::from(st.dev)),
        ("ino", V::from(st.ino)),
    ])
}
pub fn kind(st: &FileSnapshot) -> &'static str {
    match st.mode & 0o170000 {
        0o100000 => "file",
        0o040000 => "dir",
        0o120000 => "link",
        0o010000 => "fifo",
        0o140000 => "socket",
        0o020000 => "char",
        0o060000 => "block",
        _ => "other",
    }
}
fn message(prefix: &str, value: &Text, suffix: &str) -> Text {
    let mut text = prefix.chars().map(u32::from).collect::<Vec<_>>();
    text.extend_from_slice(value.as_codepoints());
    text.extend(suffix.chars().map(u32::from));
    super::json::text(text)
}
#[cfg(test)]
pub fn names(parent: &HeldDir, budget: &mut Budget, label: &str) -> Result<Vec<OsString>> {
    names_text(parent, budget, &Text::from(label))
}
fn names_text(parent: &HeldDir, budget: &mut Budget, label: &Text) -> Result<Vec<OsString>> {
    let failure = |error: dev_workflow_platform::fs::FsError, line, source| {
        let reason = LegacyException::stop_text(message(
            "",
            label,
            &format!(" を列挙できない: {}", fs_reason(&error)),
        ))
        .trace(
            "bounded_names",
            318,
            r#"raise Stop(f"{label} を列挙できない: {exc.strerror}") from exc"#,
        );
        reason.contextual(
            LegacyException::from(error).trace("bounded_names", line, source),
            true,
        )
    };
    let mut names = Vec::new();
    super::observe::names(label)
        .map_err(|e| failure(e, 311, "with os.scandir(parent) as children:"))?;
    let entries = DirectoryNames::new(parent)
        .map_err(|e| failure(e, 311, "with os.scandir(parent) as children:"))?;
    for child in entries {
        let child = child.map_err(|e| failure(e, 312, "for child in children:"))?;
        budget.tick(0)?;
        names.push(child);
    }
    names.sort_by_key(|name| runtime::os_text(name).as_codepoints().to_vec());
    Ok(names)
}

fn hash_at(
    parent: &HeldDir,
    name: &OsStr,
    before: &FileSnapshot,
    budget: &mut Budget,
) -> Result<String> {
    hash_at_with_calls(parent, name, before, budget, None)
}
fn hash_at_with_calls(
    parent: &HeldDir,
    name: &OsStr,
    before: &FileSnapshot,
    budget: &mut Budget,
    calls: Option<Calls>,
) -> Result<String> {
    if let Some(calls) = calls {
        calls
            .tick()
            .map_err(|e| e.trace("hash_at", 233, "budget.tick(); got = 0"))?;
    }
    budget.tick(0)?;
    let convert = |e: LegacyException| {
        match e {
        LegacyException::Os{error,..}=>LegacyException::Os{error,path:Some(std::os::unix::ffi::OsStrExt::as_bytes(name).to_vec()),location:"hash_at.open"}.at("hash_at",234,r#"fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)"#),e=>e
    }
    };
    let mut file = if let Some(calls) = calls {
        super::observe::open_tree(parent, name, before, || {
            calls
                .builtin()
                .map_err(|e| e.trace("hash_at", 236, "opened = os.fstat(fd)"))?;
            calls.same().map_err(|e| {
                e.trace(
                    "hash_at",
                    237,
                    "if not stat.S_ISREG(opened.st_mode) or not same(before, opened):",
                )
            })
        })
        .map_err(convert)?
    } else {
        super::observe::open(parent, name, before)
            .map_err(LegacyException::from)
            .map_err(convert)?
    };
    let raw = state::read_held_with(&mut file, budget, true)?;
    Ok(state::hash(&raw))
}
pub fn entry_at(
    parent: &HeldDir,
    name: &OsStr,
    rel: &Text,
    patterns: &[Text],
    budget: &mut Budget,
) -> Result<V> {
    let before = parent.metadata(name).map_err(|e| {
        LegacyException::stop_text(message(
            "パスを lstat できない: ",
            rel,
            &format!(": {}", fs_reason(&e)),
        ))
    })?;
    budget.tick(0)?;
    let k = kind(&before);
    let mut result = V::object([
        ("path", V::from(rel.clone())),
        ("kind", V::from(k)),
        ("meta", meta(&before)),
    ]);
    if loop_state::secret_path(rel, patterns) {
        result.set("secret", V::from(true));
    } else if k == "file" {
        result.set(
            "sha256",
            V::from(hash_at(parent, name, &before, budget).map_err(|e| {
                e.at(
                    "entry_at",
                    286,
                    r#"result["sha256"] = hash_at(parent, name, before, budget)"#,
                )
            })?),
        );
    } else if k == "link" {
        // 旧 entry_at は readlink の OSError を Stop に変える(終了 20)。rel の文字番号は保つ。
        let target = super::observe::read_link(parent, name).map_err(|e| {
            LegacyException::stop_text(message(
                "symlink を読めない: ",
                rel,
                &format!(": {}", fs_reason(&e)),
            ))
        })?;
        result.set("target", V::from(runtime::os_text(&target)));
    } else if k == "char" || k == "block" {
        result.set(
            "rdev",
            V::from(dev_workflow_platform::loop_state::device_number(
                parent, name,
            )?),
        );
    }
    Ok(result)
}
/// 旧 entry(300・302 行)。呼出元のフレームは呼出元で足す。
pub fn entry(path: &Path, rel: &str, budget: &mut Budget) -> Result<V> {
    let (parent, name) = state::parent(path)
        .map_err(|e| e.at("entry", 300, "parent = open_dir_path(parent_path)"))?;
    entry_at(&parent, &name, &Text::from(rel), &[], budget).map_err(|e| {
        e.at(
            "entry",
            302,
            "return entry_at(parent, name, rel, patterns, budget)",
        )
    })
}
/// 旧 lexists が偽なら absent、真なら entry。旧 optional_entry(1786 行)と、lexists の後に entry を
/// 直接呼ぶ所(index_snapshot・snapshot の config_worktree)のフレームは、呼出元が渡す。
pub fn optional_at(
    path: &Path,
    rel: &str,
    budget: &mut Budget,
    frame: Option<(&'static str, u32, &'static str)>,
) -> Result<V> {
    if !path.symlink_metadata().is_ok() {
        Ok(V::object([("state", V::from("absent"))]))
    } else {
        entry(path, rel, budget).map_err(|e| match frame {
            Some((function, line, source)) => e.at(function, line, source),
            None => e,
        })
    }
}
const OPTIONAL_ENTRY: (&str, u32, &str) = (
    "optional_entry",
    1786,
    "return entry(path, rel, [], budget)",
);
/// 旧 optional_entry。
pub fn optional(path: &Path, rel: &str, budget: &mut Budget) -> Result<V> {
    optional_at(path, rel, budget, Some(OPTIONAL_ENTRY))
}
pub fn tree(
    root: &Path,
    patterns: &[Text],
    budget: &mut Budget,
    stop: Option<&V>,
) -> Result<Vec<V>> {
    tree_with_calls(root, patterns, budget, stop, Calls(4))
}
pub fn tree_with_calls(
    root: &Path,
    patterns: &[Text],
    budget: &mut Budget,
    stop: Option<&V>,
    tree_calls: Calls,
) -> Result<Vec<V>> {
    let mut regex_cache = RegexCache::default();
    // 保持親と現在行、除外指定、共有予算を再帰の各段へ明示的に渡す。
    #[allow(clippy::too_many_arguments)]
    fn walk(
        parent: &HeldDir,
        name: &OsStr,
        rel: Text,
        patterns: &[Text],
        budget: &mut Budget,
        stop: Option<&V>,
        inherited: bool,
        result: &mut Vec<V>,
        calls: Calls,
        regex_cache: &mut RegexCache,
    ) -> Result<()> {
        let calls = calls.enter()?;
        calls.builtin().map_err(|e| {
            e.trace(
                "walk",
                475,
                "try: before = os.stat(name, dir_fd=parent, follow_symlinks=False)",
            )
        })?;
        let before = parent.metadata(name).map_err(|e| {
            LegacyException::stop_text(message(
                "パスを lstat できない: ",
                &rel,
                &format!(": {}", fs_reason(&e)),
            ))
        })?;
        calls.tick().map_err(|e|e.trace("walk",477,r#"budget.tick(); k = kind(before); item = {"path": rel, "kind": k, "meta": meta(before)}"#))?;
        budget.tick(0)?;
        let k = kind(&before);
        let mut row = V::object([
            ("path", V::from(rel.clone())),
            ("kind", V::from(k)),
            ("meta", meta(&before)),
        ]);
        let secret = inherited
            || regex_cache.secret(calls, &rel, patterns).map_err(|e| {
                e.trace(
                    "walk",
                    481,
                    "secret = inherited_secret or secret_path(rel, patterns)",
                )
            })?;
        if stop.is_some_and(|s| s.get("rel") == &V::from(rel.clone())) && k == "file" {
        } else if secret {
            row.set("secret", V::from(true));
        } else if k == "file" {
            row.set("sha256",V::from(hash_at_with_calls(parent,name,&before,budget,Some(calls.enter()?)).map_err(|e|e.at("walk",488,r#"elif k == "file": item["sha256"] = hash_at(parent, name, before, budget)"#))?));
        } else if k == "link" {
            // 旧 walk の os.readlink は捕捉しない。ファイル名つきの OSError になる。
            let line = r#"elif k == "link": item["target"] = os.readlink(name, dir_fd=parent)"#;
            calls.builtin().map_err(|e| e.trace("walk", 489, line))?;
            let target = super::observe::read_link(parent, name).map_err(|e| match e {
                dev_workflow_platform::fs::FsError::Io(error) => LegacyException::Os {
                    error,
                    path: Some(std::os::unix::ffi::OsStrExt::as_bytes(name).to_vec()),
                    location: "walk.readlink",
                }
                .trace("walk", 489, line),
                other => LegacyException::from(other),
            })?;
            row.set("target", V::from(runtime::os_text(&target)));
        } else if k == "char" || k == "block" {
            row.set(
                "rdev",
                V::from(dev_workflow_platform::loop_state::device_number(
                    parent, name,
                )?),
            );
        }
        result.push(row);
        if k != "dir" {
            return Ok(());
        }
        calls
            .builtin()
            .map_err(|e| e.trace("walk", 493, "fd = os.open(name, DIR_FLAGS, dir_fd=parent)"))?;
        let directory = dev_workflow_platform::loop_state::open_tree_directory(
            parent,
            name,
            &before,
            || {
                calls.builtin().and_then(|_|calls.same()).map_err(|e|e.trace("walk",495,r#"if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが差し替わった: {rel}")"#))
            },
        ).map_err(|e: LegacyException| match e {
            LegacyException::Os { error, .. } => LegacyException::Os { error, path: Some(std::os::unix::ffi::OsStrExt::as_bytes(name).to_vec()), location: "walk.open" }.trace("walk",493,"fd = os.open(name, DIR_FLAGS, dir_fd=parent)"),
            LegacyException::Stop(_)=>LegacyException::stop_text(message("ディレクトリが差し替わった: ",&rel,"")).trace("walk",495,r#"if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが差し替わった: {rel}")"#),
            other => other,
        })?;
        let label = message("ディレクトリ ", &rel, "");
        for child in names_text(&directory, budget, &label).map_err(|e| {
            e.at(
                "walk",
                496,
                r#"for child in bounded_names(fd, budget, f"ディレクトリ {rel}"):"#,
            )
        })? {
            if rel == Text::from(".") && child == ".git" {
                continue;
            }
            let mut codes = if rel == Text::from(".") {
                Vec::new()
            } else {
                let mut c = rel.as_codepoints().to_vec();
                c.push(47);
                c
            };
            codes.extend(runtime::os_text(&child).as_codepoints());
            walk(
                &directory,
                &child,
                super::json::text(codes),
                patterns,
                budget,
                stop,
                secret,
                result,
                calls,
                regex_cache,
            )
            .map_err(|e| {
                e.at(
                    "walk",
                    498,
                    r#"walk(fd, child, child if rel == "." else rel + "/" + child, secret)"#,
                )
            })?;
        }
        calls.same().map_err(|e|e.trace("walk",499,r#"if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが読取中に変わった: {rel}")"#))?;
        if directory.snapshot()? != before {
            return Err(LegacyException::stop_text(message("ディレクトリが読取中に変わった: ",&rel,"")).trace("walk",499,r#"if not same(before, os.fstat(fd)): raise Stop(f"ディレクトリが読取中に変わった: {rel}")"#));
        }
        Ok(())
    }
    // 旧 tree は os.path.dirname(root)・os.path.basename(root) をそのまま open_dir_path へ渡す
    // (abspath しない)。
    let (parent, name) = state::raw_parent(root)?;
    let mut result = Vec::new();
    walk(
        &parent,
        &name,
        Text::from("."),
        patterns,
        budget,
        stop,
        false,
        &mut result,
        tree_calls,
        &mut regex_cache,
    )
    .map_err(|e| e.at("tree", 505, r#"walk(root_parent, name, ".")"#))?;
    if let Some(stop) = stop {
        normalize(&mut result, stop);
    }
    Ok(result)
}
fn normalize(rows: &mut Vec<V>, control: &V) {
    let rel = control.get("rel");
    let dirs = control.get("new_dirs").list().unwrap_or(&[]);
    let first = dirs.first().unwrap_or(rel);
    let codes = first.string().unwrap().as_codepoints();
    let parent = codes
        .iter()
        .rposition(|c| *c == 47)
        .map(|at| super::json::text(codes[..at].to_vec()))
        .unwrap_or_else(|| Text::from("."));
    let mut stable = vec![V::from(".")];
    if parent != Text::from(".") {
        let mut p = parent.as_codepoints().to_vec();
        p.extend([47, 120]);
        stable.extend(control::prefixes(
            &control::components(&V::from(super::json::text(p))).unwrap(),
        ));
    }
    for row in rows.iter_mut() {
        if stable.contains(row.get("path"))
            && row.get("kind").is("dir")
            && row.get("meta").is_dict()
        {
            let old = row.get("meta");
            row.set(
                "meta",
                V::object([
                    ("mode", old.get("mode").clone()),
                    ("dev", old.get("dev").clone()),
                    ("ino", old.get("ino").clone()),
                ]),
            );
        }
    }
    let Some(leaf) = rows.iter().find(|r| r.get("path") == rel) else {
        return;
    };
    if !leaf.get("kind").is("file")
        || leaf.get("meta").get("size") != &V::from(0i128)
        || leaf.get_opt("sha256").is_some()
        || leaf.get("secret").truth()
    {
        return;
    }
    let mut remove = vec![rel.clone()];
    for (i, path) in dirs.iter().enumerate().rev() {
        let Some(row) = rows.iter().find(|r| r.get("path") == path) else {
            return;
        };
        if !row.get("kind").is("dir") {
            return;
        }
        let next = dirs.get(i + 1).unwrap_or(rel);
        let mut prefix = path.string().unwrap().as_codepoints().to_vec();
        prefix.push(47);
        let children = rows
            .iter()
            .filter(|r| {
                r.get("path").string().is_some_and(|s| {
                    s.as_codepoints().starts_with(&prefix)
                        && !s.as_codepoints()[prefix.len()..].contains(&47)
                })
            })
            .collect::<Vec<_>>();
        if children.len() != 1 || children[0].get("path") != next {
            return;
        }
        remove.push(path.clone());
    }
    rows.retain(|r| !remove.contains(r.get("path")));
}
pub fn profile(root: &Path, budget: &mut Budget, int_limit: usize) -> Result<Vec<Text>> {
    // 旧 profile_patterns の os.stat・os.open・read_at の OSError は捕まえない(未捕捉の例外)。
    let uncaught =
        |error: dev_workflow_platform::fs::FsError, line: u32, source: &'static str| match error {
            dev_workflow_platform::fs::FsError::Io(error) => LegacyException::Os {
                error,
                path: Some(b".claude".to_vec()),
                location: "profile_patterns",
            }
            .trace("profile_patterns", line, source),
            _ => LegacyException::stop("secret_paths の親ディレクトリが差し替わった"),
        };
    let raw = {
        let directory = state::directory(root)
            .map_err(|e| e.at("profile_patterns", 1349, "root_fd = open_dir_path(root)"))?;
        let claude = match directory.metadata(OsStr::new(".claude")) {
            Ok(s) => s,
            Err(e) if e.is_not_found() => return Ok(loop_state::default_secret_patterns()),
            Err(e) => {
                return Err(uncaught(
                    e,
                    1353,
                    r#"claude = os.stat(".claude", dir_fd=root_fd, follow_symlinks=False)"#,
                ));
            }
        };
        if !claude.is_directory() {
            return Err(LegacyException::stop("secret_paths を安全に読めない"));
        }
        // open の OSError(1358 行)は未捕捉。same の不一致だけが差し替わった(1360 行)。
        let parent = directory
            .open_dir(OsStr::new(".claude"), &claude, SnapshotMatch::Full)
            .map_err(|e| {
                uncaught(
                    e,
                    1358,
                    r#"parent = os.open(".claude", DIR_FLAGS, dir_fd=root_fd)"#,
                )
            })?;
        let st = match parent.metadata(OsStr::new("project-profile.yml")) {
            Ok(s) => s,
            Err(e) if e.is_not_found() => return Ok(loop_state::default_secret_patterns()),
            Err(_) => return Err(LegacyException::stop("secret_paths を安全に読めない")),
        };
        if !st.is_regular() {
            return Err(LegacyException::stop("secret_paths を安全に読めない"));
        }
        state::read_at(&parent, OsStr::new("project-profile.yml"), &st, budget)
            .map_err(|e| {
                e.at(
                    "profile_patterns",
                    1371,
                    r#"raw = read_at(parent, "project-profile.yml", st, budget)"#,
                )
            })?
            .1
    };
    super::observe::profile_parse()?;
    loop_state::profile_patterns_for_loop(
        &raw,
        LegacyIntLimit::from_python_value(int_limit as i64).unwrap(),
        loop_state::legacy_recursion::LegacyRecursionContext::cpython310_pyyaml603(),
    )
    .map_err(|e| LegacyException::stop(e.message()))
}
