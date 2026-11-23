use super::{
    args::Arguments,
    budget::Budget,
    config, control,
    error::{LegacyException as E, Result},
    json::{PyValue as V, Text},
    preflight, process, runtime, scan, state,
};
use dev_workflow_core::loop_state::{self, PatternInput, PatternItem};
use dev_workflow_platform::fs::{FsError, HeldDir, SnapshotMatch};
use std::{
    collections::{HashMap, HashSet},
    ffi::OsStr,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
};
fn stop(reason: &str) -> E {
    E::stop(reason)
}
fn git(top: &Path, args: &[&str], budget: &mut Budget) -> Result<Vec<u8>> {
    process::git(top.as_os_str(), &process::strings(args), budget)
}
pub fn worktrees(top: &Path, budget: &mut Budget) -> Result<Vec<V>> {
    let raw = git(top, &["worktree", "list", "--porcelain", "-z"], budget)?;
    let mut result = Vec::new();
    let mut current = V::object(Vec::<(&str, V)>::new());
    for line in raw.split(|b| *b == 0) {
        if line.is_empty() {
            if current.truth() {
                result.push(current);
                current = V::object(Vec::<(&str, V)>::new());
            }
            continue;
        }
        budget.tick(0)?;
        let at = line.iter().position(|b| *b == 32).unwrap_or(line.len());
        let key = String::from_utf8_lossy(&line[..at]);
        let value = runtime::decode(if at < line.len() {
            &line[at + 1..]
        } else {
            &[]
        });
        if key == "worktree" && current.truth() {
            result.push(current);
            current = V::object(Vec::<(&str, V)>::new());
        }
        current.set(&key, V::from(value));
    }
    if current.truth() {
        result.push(current)
    }
    if result.is_empty() {
        return Err(stop("worktree 一覧が空"));
    }
    for row in &mut result {
        if row.get_opt("worktree").is_none() || row.get_opt("HEAD").is_none() {
            return Err(stop("worktree の必須欄が無い"));
        }
        for key in ["locked", "branch"] {
            if row.get_opt(key).is_none() {
                row.set(key, V::from(""))
            }
        }
    }
    result.sort_by_key(|r| r.get("worktree").string().unwrap().as_codepoints().to_vec());
    Ok(result)
}
pub(super) fn refs(top: &Path, budget: &mut Budget) -> Result<Vec<V>> {
    let raw = git(
        top,
        &[
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(symref)%00",
        ],
        budget,
    )?;
    let mut result = Vec::new();
    for record in raw.split(|b| *b == 10) {
        if record.is_empty() {
            continue;
        }
        budget.tick(0)?;
        let mut fields = record.split(|b| *b == 0).collect::<Vec<_>>();
        if fields.last() == Some(&b"".as_slice()) {
            fields.pop();
        }
        if fields.len() != 3 {
            return Err(stop("ref の出力が壊れている"));
        }
        runtime::strict_ascii(fields[1]).map_err(|e| {
            e.at(
                "refs",
                575,
                r#""oid": fields[1].decode("ascii", "strict"),"#,
            )
        })?;
        result.push(V::object([
            ("name", V::from(runtime::decode(fields[0]))),
            ("oid", V::from(runtime::decode(fields[1]))),
            ("symref", V::from(runtime::decode(fields[2]))),
        ]));
    }
    let common = git(
        top,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
        budget,
    )?;
    let common=runtime::strict_utf8(&common).map_err(|e|e.at("refs",583,r#"common = run_git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", budget=budget).decode().strip()"#))?.trim_matches(|c: char| runtime::py_isspace(u32::from(c)));
    let mut names = result
        .iter()
        .map(|r| r.get("name").string().unwrap().clone())
        .collect::<HashSet<_>>();
    // The loose-ref walker has its own Python context: module/main/snapshot/refs
    // followed by walk. It has no tree metadata/secret-path calls.
    fn loose_names(
        parent: &HeldDir,
        budget: &mut Budget,
        calls: super::walk_recursion::Calls,
    ) -> Result<Vec<std::ffi::OsString>> {
        let calls = calls.enter()?;
        calls
            .builtin()
            .map_err(|e| e.trace("bounded_names", 311, "with os.scandir(parent) as children:"))?;
        let failure = |error: dev_workflow_platform::fs::FsError, line, source| {
            let reason = super::error::fs_reason(&error);
            let raised = stop(&format!("loose ref を列挙できない: {reason}")).trace(
                "bounded_names",
                318,
                r#"raise Stop(f"{label} を列挙できない: {exc.strerror}") from exc"#,
            );
            raised.contextual(E::from(error).trace("bounded_names", line, source), true)
        };
        let entries = dev_workflow_platform::loop_state::DirectoryNames::new(parent)
            .map_err(|e| failure(e, 311, "with os.scandir(parent) as children:"))?;
        let mut names = Vec::new();
        for name in entries {
            let name = name.map_err(|e| failure(e, 312, "for child in children:"))?;
            calls
                .tick()
                .map_err(|e| e.trace("bounded_names", 315, "budget.tick()"))?;
            budget.tick(0)?;
            calls
                .builtin()
                .map_err(|e| e.trace("bounded_names", 316, "names.append(child.name)"))?;
            names.push(name);
        }
        calls
            .builtin()
            .and_then(|_| super::walk_recursion::Calls(calls.0 + 1).builtin())
            .map_err(|e| e.trace("bounded_names", 319, "return sorted(names)"))?;
        // sorted holds its builtin call while comparing directory-name strings.
        if names.len() > 1 {
            super::walk_recursion::Calls(calls.0 + 1)
                .compare()
                .map_err(|e| e.trace("bounded_names", 319, "return sorted(names)"))?;
        }
        names.sort_by_key(|name| runtime::os_text(name).as_codepoints().to_vec());
        Ok(names)
    }
    fn os_at(e: E, name: &std::ffi::OsStr, line: u32, source: &'static str) -> E {
        let e = match e {
            E::Os { error, .. } => E::Os {
                error,
                path: Some(name.as_bytes().to_vec()),
                location: "refs.walk",
            },
            other => other,
        };
        e.at("walk", line, source)
    }
    fn walk(
        parent: &HeldDir,
        rel: &[u8],
        budget: &mut Budget,
        result: &mut Vec<V>,
        known: &mut HashSet<Text>,
        caller: super::walk_recursion::Calls,
    ) -> Result<()> {
        let calls = caller.enter()?;
        calls
            .tick()
            .map_err(|e| e.trace("walk", 585, "budget.tick()"))?;
        budget.tick(0)?;
        for name in loose_names(parent, budget, calls).map_err(|e| {
            e.at(
                "walk",
                586,
                r#"for name in bounded_names(parent, budget, "loose ref"):"#,
            )
        })? {
            calls
                .tick()
                .map_err(|e| e.trace("walk", 587, "budget.tick()"))?;
            budget.tick(0)?;
            let mut child = rel.to_vec();
            if !child.is_empty() {
                child.push(47)
            }
            child.extend(name.as_bytes());
            calls.builtin().map_err(|e| {
                e.trace(
                    "walk",
                    589,
                    "st = os.stat(name, dir_fd=parent, follow_symlinks=False)",
                )
            })?;
            let st = parent.metadata(&name).map_err(|e| {
                os_at(
                    e.into(),
                    &name,
                    589,
                    "st = os.stat(name, dir_fd=parent, follow_symlinks=False)",
                )
            })?;
            calls
                .builtin()
                .map_err(|e| e.trace("walk", 590, "if stat.S_ISDIR(st.st_mode):"))?;
            if st.is_directory() {
                calls.builtin().map_err(|e| {
                    e.trace("walk", 591, "fd = os.open(name, DIR_FLAGS, dir_fd=parent)")
                })?;
                let fd = dev_workflow_platform::loop_state::open_tree_directory(parent,&name,&st,|| {
                    calls.builtin().and_then(|_|calls.same()).map_err(|e|e.trace("walk",593,r#"if not same(st, os.fstat(fd)): raise Stop("loose ref の親が差し替わった")"#))
                }).map_err(|e:E|match e { E::Stop(_)=>stop("loose ref の親が差し替わった").trace("walk",593,r#"if not same(st, os.fstat(fd)): raise Stop("loose ref の親が差し替わった")"#),E::Os{..}=>os_at(e,&name,591,"fd = os.open(name, DIR_FLAGS, dir_fd=parent)"),other=>other })?;
                walk(&fd, &child, budget, result, known, calls)
                    .map_err(|e| e.at("walk", 594, "walk(fd, child_rel)"))?;
            } else {
                calls
                    .builtin()
                    .map_err(|e| e.trace("walk", 596, "elif stat.S_ISREG(st.st_mode):"))?;
                if st.is_regular() {
                    let read = calls.enter().map_err(|e| {
                        e.trace("walk", 597, "raw_ref = read_at(parent, name, st, budget)")
                    })?;
                    let raw=(|| -> Result<Vec<u8>> {
                        read.compare().map_err(|e|e.trace("read_at",254,"if before.st_size > budget.one:"))?;
                        budget.one(st.size.max(0) as u128)?;
                        read.tick().map_err(|e|e.trace("read_at",256,"budget.tick(); got = 0; blocks: list[bytes] = []"))?;
                        budget.tick(0)?;
                        read.builtin().map_err(|e|e.trace("read_at",257,"fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, \"O_NOFOLLOW\", 0), dir_fd=parent)"))?;
                        let mut file=super::observe::open_tree(parent,&name,&st,|| {
                            read.builtin().map_err(|e|e.trace("read_at",259,"opened = os.fstat(fd)"))?;
                            read.builtin().and_then(|_|read.same()).map_err(|e|e.trace("read_at",260,"if not stat.S_ISREG(opened.st_mode) or not same(before, opened):"))
                        }).map_err(|e:E|match e { E::Os{error,..}=>E::Os{error,path:Some(name.as_bytes().to_vec()),location:"read_at.open"}.trace("read_at",257,"fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, \"O_NOFOLLOW\", 0), dir_fd=parent)"),e=>e })?;
                        // All later read/len/read-budget/fstat/same calls have at
                        // most this already checked same() peak. The actual shared
                        // budget is still consumed only by the real reads.
                        state::read_held(&mut file,budget)
                    })().map_err(|e|e.at("walk",597,"raw_ref = read_at(parent, name, st, budget)"))?;
                    calls
                        .builtin()
                        .map_err(|e| e.trace("walk", 598, r#"if raw_ref.startswith(b"ref: "):"#))?;
                    if raw.starts_with(b"ref: ") {
                        calls.builtin().map_err(|e|e.trace("walk",599,r#"target = raw_ref[5:].strip().decode("utf-8", "surrogateescape")"#))?;
                        // 旧 raw_ref[5:].strip()(bytes.strip は \x0b も除く)。
                        let target = runtime::decode(runtime::py_bytes_strip(&raw[5..]));
                        let mut logical = b"refs/".to_vec();
                        logical.extend(&child);
                        let logical = runtime::decode(&logical);
                        calls.builtin().map_err(|e| {
                            e.trace(
                                "walk",
                                601,
                                r#"if not target.startswith("refs/") or logical in names:"#,
                            )
                        })?;
                        if !runtime::bytes(&target)?.starts_with(b"refs/")
                            || known.contains(&logical)
                        {
                            continue;
                        }
                        calls.builtin().map_err(|e| {
                            e.trace(
                                "walk",
                                603,
                                r#"result.append({"name": logical, "oid": "", "symref": target})"#,
                            )
                        })?;
                        result.push(V::object([
                            ("name", V::from(logical.clone())),
                            ("oid", V::from("")),
                            ("symref", V::from(target)),
                        ]));
                        calls
                            .builtin()
                            .map_err(|e| e.trace("walk", 604, "names.add(logical)"))?;
                        known.insert(logical);
                    }
                } else {
                    calls
                        .builtin()
                        .map_err(|e| e.trace("walk", 605, "elif stat.S_ISLNK(st.st_mode):"))?;
                    if scan::kind(&st) == "link" {
                        return Err(stop("loose ref が symlink である"));
                    }
                }
            }
        }
        Ok(())
    }
    let path = Path::new(common).join("refs");
    match state::directory(&path) {
        Ok(root) => walk(
            &root,
            &[],
            budget,
            &mut result,
            &mut names,
            super::walk_recursion::Calls(4),
        )
        .map_err(|e| e.at("refs", 614, r#"try: walk(root, "")"#))?,
        // 旧版は except Stop だけで、lexists が偽なら loose ref を見ない。Stop 以外は上げる。
        Err(e) => {
            if e.stop_reason().is_none() || path.symlink_metadata().is_ok() {
                return Err(e);
            }
        }
    }
    result.sort_by_key(|r| r.get("name").string().unwrap().as_codepoints().to_vec());
    Ok(result)
}
// 旧版の dict のキーは str。保存状態の文字番号のまま比べ、OS のバイトへ変えない。
type PatternMap = HashMap<Text, Vec<Text>>;
fn prior_patterns(prior: &V) -> Result<(PatternMap, PatternMap)> {
    if matches!(prior, V::Null) {
        return Ok((HashMap::new(), HashMap::new()));
    }
    let rows = prior
        .get("secret_patterns")
        .list()
        .ok_or_else(|| stop("前の secret_paths の状態が新しい必須欄を持たない"))?;
    let defaults = loop_state::default_secret_patterns();
    let mut found = HashMap::new();
    for row in rows {
        if !matches!(row,V::Dict(r)if r.len()==2) {
            return Err(stop("前の secret_paths の状態を読めない"));
        }
        let path = row
            .get("path")
            .string()
            .cloned()
            .ok_or_else(|| stop("前の secret_paths の状態を読めない"))?;
        if found.contains_key(&path) {
            return Err(stop("前の secret_paths の状態が重複している"));
        }
        let patterns = row
            .get("patterns")
            .list()
            .ok_or_else(|| stop("前の secret_paths の状態を読めない"))?
            .iter()
            .map(|v| {
                v.string()
                    .cloned()
                    .ok_or_else(|| stop("前の secret_paths の状態を読めない"))
            })
            .collect::<Result<Vec<_>>>()?;
        if defaults.iter().any(|p| !patterns.contains(p)) {
            return Err(stop("前の secret_paths の状態に既定値が無い"));
        }
        let custom = patterns
            .iter()
            .filter(|p| !defaults.contains(p))
            .cloned()
            .map(PatternItem::Text)
            .collect();
        let canonical = loop_state::canonical_secret_patterns(&PatternInput::List(custom))
            .map_err(|e| stop(e.message()))?;
        if canonical != patterns {
            return Err(stop("前の secret_paths の状態が正規形でない"));
        }
        found.insert(path, canonical);
    }
    let configs = prior
        .get("worktree_configs")
        .list()
        .ok_or_else(|| stop("前の secret_paths の worktree 状態を読めない"))?;
    let mut by_admin = HashMap::new();
    let mut paths = HashSet::new();
    for config in configs {
        if !config.is_dict() {
            return Err(stop("前の secret_paths の worktree 状態を読めない"));
        }
        let (Some(path), Some(admin)) = (
            config.get("path").string().cloned(),
            config.get("git_dir").string().cloned(),
        ) else {
            return Err(stop("前の secret_paths の worktree 状態を読めない"));
        };
        let patterns = found
            .get(&path)
            .ok_or_else(|| stop("前の secret_paths の worktree 対応が無い"))?;
        let admin =
            super::realpath::real(&admin, super::walk_recursion::Calls(4)).map_err(|e| {
                e.at(
                    "prior_secret_patterns",
                    1524,
                    r#"key = os.path.realpath(config["git_dir"])"#,
                )
            })?;
        if by_admin.get(&admin).is_some_and(|v| v != patterns) {
            return Err(stop("前の secret_paths の worktree 対応が矛盾している"));
        }
        by_admin.insert(admin, patterns.clone());
        paths.insert(path);
    }
    if found.keys().collect::<HashSet<_>>() != paths.iter().collect::<HashSet<_>>() {
        return Err(stop("前の secret_paths の worktree 対応が矛盾している"));
    }
    Ok((found, by_admin))
}
fn stop_control(args: &Arguments, prior: &V, wts: &[V], budget: &mut Budget) -> Result<Option<V>> {
    let raw = args.raw("stop-control-worktree");
    let rel = args.raw("stop-control-rel");
    let dirs = args.many("stop-control-new-dir");
    let fresh = args.flag("fresh-config-baseline");
    if raw.is_empty() && rel.is_empty() && dirs.is_empty() {
        if !matches!(prior, V::Null) && !fresh && !matches!(prior.get("stop_control"), V::Null) {
            return Err(stop("停止ファイルの基準が欠けている"));
        }
        return Ok(None);
    }
    if raw.is_empty() || rel.is_empty() {
        return Err(stop("停止ファイルの基準が不完全"));
    }
    let worktree = control::real_at(
        Path::new(raw),
        4,
        "stop_control_from_args",
        350,
        r#"worktree = os.path.realpath(raw_worktree)"#,
    )?;
    let mut observed = HashSet::new();
    if Path::new(raw).is_absolute() {
        for row in wts {
            let path = state::string_path(row.get("worktree"))?;
            let resolved=control::real_at(&path,5,"<setcomp>",351,r#"if not os.path.isabs(raw_worktree) or worktree not in {os.path.realpath(path) for path in worktree_paths}:"#).map_err(|e|e.at("stop_control_from_args",351,r#"if not os.path.isabs(raw_worktree) or worktree not in {os.path.realpath(path) for path in worktree_paths}:"#))?;
            observed.insert(resolved.into_os_string());
        }
    }
    // 旧 {os.path.realpath(path) ...} は str の集合。Path の == と Hash を使わずバイトで比べる。
    if !Path::new(raw).is_absolute() || !observed.contains(worktree.as_os_str()) {
        return Err(stop("停止ファイルの worktree が観察対象でない"));
    }
    let rel = V::from(runtime::os_text(rel));
    let parts = control::components(&rel)?;
    let mut new_dirs = Vec::new();
    for value in dirs {
        let value = V::from(runtime::os_text(value));
        let dparts = control::components(&value)?;
        if dparts.len() >= parts.len() || dparts != parts[..dparts.len()] {
            return Err(stop("停止ファイルの新規ディレクトリが不正"));
        }
        new_dirs.push(value);
    }
    let expected = control::prefixes(&parts);
    if !new_dirs.is_empty()
        && (new_dirs.len() > expected.len()
            || new_dirs != expected[expected.len() - new_dirs.len()..])
    {
        return Err(stop("停止ファイルの新規ディレクトリが連続していない"));
    }
    let descriptor = V::object([
        ("worktree", V::from(runtime::os_text(worktree.as_os_str()))),
        ("rel", rel),
        ("new_dirs", V::List(new_dirs.clone())),
    ]);
    if !matches!(prior, V::Null) && !fresh {
        if prior.get("stop_control") != &descriptor {
            return Err(stop("停止ファイルの基準がイテレーションの途中で変わった"));
        }
        return Ok(Some(descriptor));
    }
    let mut fd = state::directory(&worktree).map_err(|e| {
        e.at(
            "stop_control_from_args",
            374,
            "fd = open_dir_path(worktree)",
        )
    })?;
    let mut missing = Vec::new();
    // 旧 os.stat の ENOENT 以外(379 行)と os.open(387 行)の OSError は捕まえない。fstat の不一致だけが Stop。
    let uncaught = |error: FsError, name: &OsStr, line: u32, source: &'static str| match error {
        FsError::Io(error) => E::Os {
            error,
            path: Some(name.as_bytes().to_vec()),
            location: "stop_control_from_args",
        }
        .trace("stop_control_from_args", line, source),
        _ => stop("停止ファイルの親が差し替わった"),
    };
    for (index, part) in parts.iter().enumerate() {
        let name = runtime::os(part.string().unwrap())?;
        let before = match fd.metadata(&name) {
            Ok(s) => s,
            Err(e) if e.is_not_found() => {
                missing = expected[index..].to_vec();
                break;
            }
            Err(e) => {
                return Err(uncaught(
                    e,
                    &name,
                    379,
                    "before = os.stat(part, dir_fd=fd, follow_symlinks=False)",
                ));
            }
        };
        if index == parts.len() - 1 {
            return Err(stop("停止ファイルは開始時に無い必要がある"));
        }
        if !before.is_directory() {
            return Err(stop("停止ファイルの親が通常ディレクトリでない"));
        }
        fd = fd
            .open_dir(&name, &before, SnapshotMatch::Full)
            .map_err(|e| uncaught(e, &name, 387, "nxt = os.open(part, DIR_FLAGS, dir_fd=fd)"))?;
    }
    if missing != new_dirs {
        return Err(stop("停止ファイルの新規ディレクトリが開始時と合わない"));
    }
    let _ = budget;
    Ok(Some(descriptor))
}
fn common_admin(common: &Path, budget: &mut Budget) -> Result<V> {
    let path = common.join("hooks");
    let hooks = if path.symlink_metadata().is_ok_and(|s| s.is_dir()) {
        V::object([
            ("state", V::from("dir")),
            (
                "entries",
                V::List(
                    scan::tree_with_calls(
                        &path,
                        &[],
                        budget,
                        None,
                        super::walk_recursion::Calls(5),
                    )
                    .map_err(|e| {
                        e.at(
                            "common_admin_snapshot",
                            1796,
                            r#"hooks_value = {"state": "dir", "entries": tree(hooks, [], budget)}"#,
                        )
                    })?,
                ),
            ),
        ])
    } else {
        scan::optional(&path, "common:hooks", budget).map_err(|e| {
            e.at(
                "common_admin_snapshot",
                1798,
                r#"hooks_value = optional_entry(hooks, "common:hooks", budget)"#,
            )
        })?
    };
    let mut info = V::object(Vec::<(&str, V)>::new());
    for name in ["exclude", "attributes", "sparse-checkout", "grafts"] {
        info.set(
            name,
            scan::optional(
                &common.join("info").join(name),
                &format!("common:info/{name}"),
                budget,
            )
            .map_err(|e| {
                e.at("<dictcomp>", 1799, INFO_LINE)
                    .at("common_admin_snapshot", 1799, INFO_LINE)
            })?,
        );
    }
    Ok(V::object([("hooks", hooks), ("info", info)]))
}
const INFO_LINE: &str = r#"info = {name: optional_entry(os.path.join(common, "info", name), "common:info/" + name, budget)"#;
fn private(admin: &Path, budget: &mut Budget) -> Result<V> {
    let head = scan::entry(&admin.join("HEAD"), "private:HEAD", budget).map_err(|e| {
        e.at(
            "private_ref_snapshot",
            1806,
            r#"head = entry(os.path.join(git_dir, "HEAD"), "private:HEAD", [], budget)"#,
        )
    })?;
    let path = admin.join("refs");
    let refs = if path.symlink_metadata().is_ok() {
        V::List(
            scan::tree_with_calls(&path, &[], budget, None, super::walk_recursion::Calls(5))
                .map_err(|e| {
                    e.at(
                        "private_ref_snapshot",
                        1810,
                        "refs = tree(refs_path, [], budget)",
                    )
                })?,
        )
    } else {
        V::object([("state", V::from("absent"))])
    };
    Ok(V::object([("head", head), ("refs", refs)]))
}
pub fn run(args: &Arguments, int_limit: usize) -> Result<()> {
    let mut budget = Budget::new(args.limits.clone()).map_err(|e|e.at("snapshot",1826,"budget = Budget(args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)"))?;
    let top = Path::new(args.raw("top"));
    let common = Path::new(args.raw("common"));
    let repo_admin = Path::new(args.raw("repo-admin"));
    let fresh = args.flag("fresh-config-baseline");
    let mut prior = if args.raw("secret-patterns-from").is_empty() {
        V::Null
    } else {
        let prior = state::read_at_depth(
            Path::new(args.raw("secret-patterns-from")),
            &args.limits,
            int_limit,
            5,
        )
        .map_err(|e| {
            e.at(
                "prior_state",
                1414,
                "prior = read_state(args.secret_patterns_from, args.max_items, args.max_bytes,",
            )
            .at(
                "snapshot",
                1827,
                "prior = without_exact_removed_parent_worktree(prior_state(args), args)",
            )
        })?;
        if !state::schema(&prior) {
            return Err(stop("前の状態が新しい必須欄を持たない"));
        }
        prior
    };
    let removed = args.many("removed-parent-worktree");
    if !removed.is_empty() {
        if matches!(prior, V::Null)
            || args.raw("wt-admin") != "-"
            || args.raw("exclude-worktree").is_empty()
        {
            return Err(stop("親が削除した worktree の記録が基準と一致しない"));
        }
        if removed.len() != 1 || !Path::new(&removed[0]).is_absolute() {
            return Err(stop("親が削除した worktree の記録が不正"));
        }
        let removed = control::real_at(
            Path::new(&removed[0]),
            4,
            "without_exact_removed_parent_worktree",
            1478,
            r#"removed = os.path.realpath(removed_args[0])"#,
        )
        .map_err(|e| {
            e.at(
                "snapshot",
                1827,
                r#"prior = without_exact_removed_parent_worktree(prior_state(args), args)"#,
            )
        })?;
        if removed.as_os_str() != control::real_at(Path::new(args.raw("exclude-worktree")),4,"without_exact_removed_parent_worktree",1479,r#"if removed != os.path.realpath(args.exclude_worktree) or os.path.lexists(removed):"#).map_err(|e|e.at("snapshot",1827,r#"prior = without_exact_removed_parent_worktree(prior_state(args), args)"#))?.as_os_str()
            || removed.symlink_metadata().is_ok()
        {
            return Err(stop("親が削除した worktree の記録が基準と一致しない"));
        }
        // 旧版は prior.get("worktrees", []) を回し、同じ字面の行があるときだけ取り除く。
        let removed_text = V::from(runtime::os_text(removed.as_os_str()));
        let frame = |e: E| {
            e.at(
                "snapshot",
                1827,
                "prior = without_exact_removed_parent_worktree(prior_state(args), args)",
            )
        };
        let found = match prior.get_opt("worktrees") {
            None => false,
            Some(V::List(rows)) => rows
                .iter()
                .any(|row| row.is_dict() && row.get_opt("worktree") == Some(&removed_text)),
            // dict のキーと str の文字は dict でないので、一致しない。
            Some(V::Dict(_) | V::String(_)) => false,
            Some(other) => {
                return Err(frame(
                    E::Python {
                        kind: "TypeError",
                        reason: format!("'{}' object is not iterable", other.python_type()),
                        location: "without_exact_removed_parent_worktree",
                    }
                    .trace(
                        "without_exact_removed_parent_worktree",
                        1484,
                        r#"for row in prior.get("worktrees", []):"#,
                    ),
                ));
            }
        };
        if !found {
            return Err(stop("親が削除した worktree の記録が基準と一致しない"));
        }
        prior = control::remove_rows(&prior, &removed_text).map_err(|e| {
            frame(e.at(
                "without_exact_removed_parent_worktree",
                1486,
                "return remove_exact_worktree_rows(prior, removed)",
            ))
        })?;
    }
    let (previous, by_admin) = prior_patterns(&prior).map_err(|e| {
        e.at(
            "snapshot",
            1828,
            r#"previous_patterns, previous_patterns_by_git_dir = prior_secret_patterns(prior)"#,
        )
    })?;
    let mut trusted = config::trusted(&prior, fresh)?;
    let candidates = config::trusted_candidates(&prior, fresh)?;
    let admins = preflight::git_dirs(&prior)?;
    let branches = preflight::branches(&prior)?;
    preflight::context(
        top,
        trusted.as_mut(),
        candidates.as_ref(),
        &mut budget,
        admins.get(&config::path_text(top)),
        common,
        branches.get(&config::path_text(top)),
        false,
        None,
    )
    .map_err(|e| {
        e.at(
            "snapshot",
            1836,
            "preflight_trusted_configs(args.top, trusted_configs, trusted_candidates, budget, top_expected,",
        )
    })?;
    let wts = worktrees(top, &mut budget)?;
    let control=stop_control(args,&prior,&wts,&mut budget).map_err(|e|e.at("snapshot",1839,r#"control = stop_control_from_args(args, prior, {row["worktree"] for row in wts}, budget)"#))?;
    if !matches!(prior, V::Null) && !fresh {
        let old = prior
            .get("worktrees")
            .list()
            .ok_or_else(|| stop("前の worktree 状態を読めない"))?;
        // 旧 old_paths は str の集合(OS のバイトへ変えない)。
        let paths = old
            .iter()
            .filter(|row| row.is_dict())
            .filter_map(|row| row.get("worktree").string().cloned())
            .collect::<HashSet<_>>();
        if paths.len() != old.len() {
            return Err(stop("前の worktree 状態を読めない"));
        }
        let own = if args.raw("exclude-worktree").is_empty() {
            None
        } else {
            Some(control::real_at(
                Path::new(args.raw("exclude-worktree")),
                3,
                "snapshot",
                1845,
                r#"own = os.path.realpath(args.exclude_worktree) if args.exclude_worktree else None"#,
            )?)
        };
        for wt in &wts {
            let path = state::string_path(wt.get("worktree"))?;
            if !paths.contains(&config::path_text(&path))
                && own.as_ref().map(|p| p.as_os_str())
                    != Some(
                        control::real_at(
                            &path,
                            3,
                            "snapshot",
                            1848,
                            r#"if path not in old_paths and os.path.realpath(path) != own:"#,
                        )?
                        .as_os_str(),
                    )
            {
                return Err(stop(
                    "人の worktree がイテレーションの途中で追加または移動した",
                ));
            }
        }
    }
    let top_real = control::real_at(
        top,
        3,
        "snapshot",
        1855,
        r#"top_real = os.path.realpath(args.top)"#,
    )?;
    for wt in &wts {
        let path = state::string_path(wt.get("worktree"))?;
        if control::real_at(
            &path,
            3,
            "snapshot",
            1858,
            r#"if os.path.realpath(path) == top_real:"#,
        )?
        .as_os_str()
            == top_real.as_os_str()
        {
            continue;
        }
        let own_path = !args.raw("exclude-worktree").is_empty()
            && control::real_at(
                &path,
                3,
                "snapshot",
                1861,
                r#"own_path = bool(args.exclude_worktree and os.path.realpath(path) == os.path.realpath(args.exclude_worktree))"#,
            )?
            .as_os_str()
                == control::real_at(
                    Path::new(args.raw("exclude-worktree")),
                    3,
                    "snapshot",
                    1861,
                    r#"own_path = bool(args.exclude_worktree and os.path.realpath(path) == os.path.realpath(args.exclude_worktree))"#,
                )?
                .as_os_str();
        let key = config::path_text(&path);
        let new_admin = if !admins.contains_key(&key) && own_path && args.raw("wt-admin") != "-" {
            Some(config::path_text(Path::new(args.raw("wt-admin"))))
        } else {
            None
        };
        let expected = admins.get(&key).or(new_admin.as_ref());
        preflight::context(
            &path,
            trusted.as_mut(),
            candidates.as_ref(),
            &mut budget,
            expected,
            common,
            if own_path { None } else { branches.get(&key) },
            new_admin.is_some(),
            if new_admin.is_some() {
                Some(repo_admin)
            } else {
                None
            },
        )
        .map_err(|e| {
            e.at(
                "snapshot",
                1870,
                "preflight_trusted_configs(path, trusted_configs, trusted_candidates, budget, expected,",
            )
        })?;
    }
    let (mut contents, mut configs, mut patterns_rows) = (Vec::new(), Vec::new(), Vec::new());
    for wt in &wts {
        let path = state::string_path(wt.get("worktree"))?;
        let raw = git(
            &path,
            &["rev-parse", "--path-format=absolute", "--git-dir"],
            &mut budget,
        )?;
        let admin = PathBuf::from(
            runtime::strict_utf8(&raw).map_err(|e|e.at("snapshot",1880,r#"git_dir = run_git(path, "rev-parse", "--path-format=absolute", "--git-dir", budget=budget).decode().strip()"#))?
                .trim_matches(|c: char| runtime::py_isspace(u32::from(c))),
        );
        let inherited=match previous.get(&runtime::os_text(path.as_os_str())){Some(value)=>value.clone(),None if fresh=>by_admin.get(&runtime::os_text(control::real_at(&admin,3,"snapshot",1886,r#"inherited = previous_patterns_by_git_dir.get(os.path.realpath(git_dir), [])"#)?.as_os_str())).cloned().unwrap_or_default(),None=>Vec::new()};
        let mut patterns = inherited;
        for pattern in scan::profile(&path, &mut budget, int_limit).map_err(|e| {
            e.at(
                "snapshot",
                1887,
                "patterns = list(dict.fromkeys((inherited or []) + profile_patterns(path, budget)))",
            )
        })? {
            if !patterns.contains(&pattern) {
                patterns.push(pattern)
            }
        }
        patterns_rows.push(V::object([
            ("path", wt.get("worktree").clone()),
            (
                "patterns",
                V::List(patterns.iter().cloned().map(V::from).collect()),
            ),
        ]));
        let mut index = scan::optional_at(
            &admin.join("index"),
            "index",
            &mut budget,
            Some((
                "index_snapshot",
                1698,
                r#"result = {"state": "absent"} if not os.path.lexists(path) else entry(path, "index", [], budget)"#,
            )),
        )
        .map_err(|e| {
            e.at(
                "snapshot",
                1889,
                r#"content: dict[str, Any] = {"path": path, "index": index_snapshot(git_dir, path, budget)}"#,
            )
        })?;
        index.set(
            "head_clean",
            V::from(process::index(path.as_os_str(), &budget).map_err(|e| {
                e.at(
                    "snapshot",
                    1889,
                    r#"content: dict[str, Any] = {"path": path, "index": index_snapshot(git_dir, path, budget)}"#,
                )
            })?),
        );
        let mut content = V::object([("path", wt.get("worktree").clone()), ("index", index)]);
        let config_worktree = scan::optional_at(
            &admin.join("config.worktree"),
            "config.worktree",
            &mut budget,
            Some((
                "snapshot",
                1891,
                r#""config_worktree": (entry(os.path.join(git_dir, "config.worktree"), "config.worktree", [], budget)"#,
            )),
        )?;
        let private = if control::real_at(
            &admin,
            3,
            "snapshot",
            1897,
            r#"if os.path.realpath(git_dir) != os.path.realpath(args.common)"#,
        )?
        .as_os_str()
            != control::real_at(
                common,
                3,
                "snapshot",
                1897,
                r#"if os.path.realpath(git_dir) != os.path.realpath(args.common)"#,
            )?
            .as_os_str()
        {
            private(&admin, &mut budget).map_err(|e| {
                e.at(
                    "snapshot",
                    1896,
                    r#""private_refs": (private_ref_snapshot(git_dir, budget)"#,
                )
            })?
        } else {
            V::object([("state", V::from("common-refs-covered"))])
        };
        let effective = config::snapshot(&path, common, &admin, &mut budget, trusted.as_ref())
            .map_err(|e| {
                e.at(
                    "snapshot",
                    1899,
                    r#""effective": config_snapshot(path, args.common, budget, trusted_configs, git_dir=git_dir)})"#,
                )
            })?;
        configs.push(V::object([
            ("path", wt.get("worktree").clone()),
            ("git_dir", V::from(runtime::os_text(admin.as_os_str()))),
            ("config_worktree", config_worktree),
            ("private_refs", private),
            ("effective", effective),
        ]));
        if control::real_at(
            &path,
            3,
            "snapshot",
            1900,
            r#"if os.path.realpath(path) != os.path.realpath(args.exclude_worktree or ""):"#,
        )?
        .as_os_str()
            != control::real_at(
                Path::new(args.raw("exclude-worktree")),
                3,
                "snapshot",
                1900,
                r#"if os.path.realpath(path) != os.path.realpath(args.exclude_worktree or ""):"#,
            )?
            .as_os_str()
        {
            let matching = if let Some(value) = control.as_ref() {
                if V::from(runtime::os_text(control::real_at(&path,3,"snapshot",1901,r#"matching_control = control if control is not None and os.path.realpath(path) == control["worktree"] else None"#)?.as_os_str()))==*value.get("worktree"){Some(value)}else{None}
            } else {
                None
            };
            content.set(
                "files",
                V::List(
                    scan::tree(&path, &patterns, &mut budget, matching).map_err(|e| {
                        e.at(
                            "snapshot",
                            1902,
                            r#"content["files"] = tree(path, patterns, budget, matching_control)"#,
                        )
                    })?,
                ),
            );
        } else {
            content.set(
                "files",
                V::object([("state", V::from("self-worktree-content-excluded"))]),
            );
        }
        contents.push(content);
    }
    let state = V::object([
        ("schema", V::from(7i128)),
        ("refs", V::List(refs(top, &mut budget).map_err(|e|e.at("snapshot",1906,r#"state = {"schema": SCHEMA, "refs": refs(args.top, budget), "worktrees": wts,"#))?)),
        ("worktrees", V::List(wts)),
        ("worktree_contents", V::List(contents)),
        ("worktree_configs", V::List(configs)),
        ("secret_patterns", V::List(patterns_rows)),
        ("stop_control", control.unwrap_or(V::Null)),
        (
            "config",
            config::snapshot(top, common, repo_admin, &mut budget, trusted.as_ref()).map_err(|e| {
                e.at(
                    "snapshot",
                    1910,
                    r#""config": config_snapshot(args.top, args.common, budget, trusted_configs, git_dir=args.repo_admin),"#,
                )
            })?,
        ),
        (
            "common_admin",
            common_admin(common, &mut budget).map_err(|e| {
                e.at(
                    "snapshot",
                    1911,
                    r#""common_admin": common_admin_snapshot(args.common, budget),"#,
                )
            })?,
        ),
        (
            "common_config",
            config::file_snapshot(
                top,
                &common.join("config"),
                "config",
                &mut budget,
                trusted.as_ref(),
            )?,
        ),
        (
            "common_config_worktree",
            config::file_snapshot(
                top,
                &common.join("config.worktree"),
                "config.worktree",
                &mut budget,
                trusted.as_ref(),
            )?,
        ),
        (
            "repo_admin",
            V::object([
                ("path", V::from(runtime::os_text(repo_admin.as_os_str()))),
                (
                    "config_worktree",
                    config::file_snapshot(
                        top,
                        &repo_admin.join("config.worktree"),
                        "repo:config.worktree",
                        &mut budget,
                        trusted.as_ref(),
                    )?,
                ),
            ]),
        ),
    ]);
    state::write(Path::new(args.raw("out")), &state)
        .map_err(|e| e.at("snapshot", 1916, "write_state(args.out, state)"))
}
