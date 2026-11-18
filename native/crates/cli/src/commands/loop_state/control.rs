use super::{
    args::Arguments,
    error::{LegacyException, Result},
    json::PyValue as V,
    runtime, state,
};
use std::path::{Path, PathBuf};
/// OS のパス(引数・Git の出力)の realpath。入力が OS のバイトなので、結果も符号化できる。
pub fn real(path: &Path, caller: super::walk_recursion::Calls) -> Result<PathBuf> {
    let text = super::realpath::real(&runtime::os_text(path.as_os_str()), caller)?;
    Ok(PathBuf::from(runtime::os(&text)?))
}
pub fn real_at(
    path: &Path,
    depth: usize,
    function: &'static str,
    line: u32,
    source: &'static str,
) -> Result<PathBuf> {
    real(path, super::walk_recursion::Calls(depth)).map_err(|e| e.at(function, line, source))
}
pub fn normalized(path: &Path) -> PathBuf {
    state::normalized(path)
}
pub fn remove_rows(state: &V, removed: &V) -> Result<V> {
    let path = state::string_path(removed)?;
    if !state::schema(state)
        || !path.is_absolute()
        || normalized(&path).as_os_str() != path.as_os_str()
    {
        return Err(LegacyException::stop("親が削除した worktree の記録が不正"));
    }
    let mut result = state.clone();
    for (key, path_key) in [
        ("worktrees", "worktree"),
        ("worktree_contents", "path"),
        ("worktree_configs", "path"),
        ("secret_patterns", "path"),
    ] {
        let rows = state
            .get(key)
            .list()
            .ok_or_else(|| LegacyException::stop("前の worktree の状態を読めない"))?;
        let mut count = 0;
        let mut kept = Vec::new();
        for row in rows {
            if !row.is_dict() || row.get(path_key).string().is_none() {
                return Err(LegacyException::stop("前の worktree の状態を読めない"));
            }
            if row.get(path_key) == removed {
                count += 1
            } else {
                kept.push(row.clone())
            }
        }
        if count != 1 {
            return Err(LegacyException::stop(
                "親が削除した worktree の記録が基準と一致しない",
            ));
        }
        result.set(key, V::List(kept));
    }
    Ok(result)
}
pub fn derive(args: &Arguments, int_limit: usize) -> Result<()> {
    if args.raw("own-worktree") != args.raw("removed-worktree") {
        return Err(LegacyException::stop(
            "親が削除した worktree の記録が基準と一致しない",
        ));
    }
    let held = state::read(Path::new(args.raw("state")), &args.limits, int_limit).map_err(|e| {
        e.at(
            "derive_removed_worktree",
            1458,
            "held = read_state(args.state, args.max_items, args.max_bytes,",
        )
    })?;
    let removed = V::from(runtime::os_text(args.raw("removed-worktree")));
    state::write(Path::new(args.raw("out")), &remove_rows(&held, &removed)?).map_err(|e| {
        e.at(
            "derive_removed_worktree",
            1460,
            "write_state(args.out, remove_exact_worktree_rows(held, args.removed_worktree))",
        )
    })
}
pub fn components(value: &V) -> Result<Vec<V>> {
    let s = value
        .string()
        .ok_or_else(|| LegacyException::stop("停止ファイルの相対パスが不正"))?;
    let codes = s.as_codepoints();
    if codes.is_empty() || codes[0] == 47 {
        return Err(LegacyException::stop("停止ファイルの相対パスが不正"));
    }
    let mut result = Vec::new();
    for part in codes.split(|&c| c == 47) {
        if part.is_empty() || part == [46] || part == [46, 46] {
            return Err(LegacyException::stop("停止ファイルの相対パスが不正"));
        }
        result.push(V::from(super::json::text(part.to_vec())));
    }
    Ok(result)
}
pub fn prefixes(parts: &[V]) -> Vec<V> {
    let mut result = Vec::new();
    let mut current = Vec::new();
    for part in parts.iter().take(parts.len().saturating_sub(1)) {
        if !current.is_empty() {
            current.push(47)
        }
        current.extend(part.string().unwrap().as_codepoints());
        result.push(V::from(super::json::text(current.clone())))
    }
    result
}
pub fn held(args: &Arguments, int_limit: usize, raw: &mut Vec<u8>) -> Result<()> {
    let prior =
        state::read(Path::new(args.raw("state")), &args.limits, int_limit).map_err(|e| {
            e.at(
                "held_stop_control",
                404,
                "prior = read_state(path, items, total, one, seconds)",
            )
        })?;
    if !state::schema(&prior) {
        return Err(LegacyException::stop("前の状態が新しい必須欄を持たない"));
    }
    let control = prior.get("stop_control");
    if matches!(control, V::Null) {
        return Ok(());
    }
    let V::Dict(rows) = control else {
        return Err(LegacyException::stop("停止ファイルの基準を読めない"));
    };
    if rows.len() != 3
        || ["worktree", "rel", "new_dirs"]
            .iter()
            .any(|k| control.get_opt(k).is_none())
    {
        return Err(LegacyException::stop("停止ファイルの基準を読めない"));
    }
    let worktree = control
        .get("worktree")
        .string()
        .ok_or_else(|| LegacyException::stop("停止ファイルの基準を読めない"))?;
    if !worktree.as_codepoints().starts_with(&[47]) || control.get("rel").string().is_none() {
        return Err(LegacyException::stop("停止ファイルの基準を読めない"));
    }
    let dirs = control
        .get("new_dirs")
        .list()
        .ok_or_else(|| LegacyException::stop("停止ファイルの基準を読めない"))?;
    let parts = components(control.get("rel"))?;
    let expected = prefixes(&parts);
    for value in dirs {
        components(value).map_err(|_| LegacyException::stop("停止ファイルの基準を読めない"))?;
    }
    if !dirs.is_empty()
        && (dirs.len() > expected.len() || dirs != &expected[expected.len() - dirs.len()..])
    {
        return Err(LegacyException::stop("停止ファイルの基準を読めない"));
    }
    let worktree =
        super::realpath::real(worktree, super::walk_recursion::Calls(3)).map_err(|e| {
            e.trace(
                "held_stop_control",
                421,
                r#"return {"worktree": os.path.realpath(worktree), "rel": rel, "new_dirs": dirs}"#,
            )
        })?;
    // 旧 main の value.encode("utf-8", "surrogateescape")(2155 行)で符号化する。
    raw.extend(runtime::bytes(&worktree)?);
    raw.push(0);
    for value in std::iter::once(control.get("rel")).chain(dirs.iter()) {
        raw.extend(runtime::bytes(value.string().unwrap())?);
        raw.push(0);
    }
    Ok(())
}
