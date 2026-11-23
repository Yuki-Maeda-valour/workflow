use super::{
    args::Arguments,
    error::{LegacyException, Result},
    json::{PyValue as V, Text},
    runtime, state,
};
use std::{collections::BTreeSet, path::Path};
const REQUIRED: &[&str] = &[
    "schema",
    "refs",
    "worktrees",
    "worktree_contents",
    "worktree_configs",
    "secret_patterns",
    "stop_control",
    "config",
    "common_admin",
    "common_config",
    "common_config_worktree",
    "repo_admin",
];
fn config_diagnostic(name: &str, left: &V, right: &V) -> String {
    let tail = if !left.is_dict() || !right.is_dict() {
        "の状態を読めない"
    } else if left.get("state") != right.get("state") {
        "の有無または種類が変わった(内容は出さない)"
    } else if !left.get("state").is("ok") {
        "が変わった(内容は出さない)"
    } else {
        let (l, r) = (left.get("entry"), right.get("entry"));
        if !l.is_dict() || !r.is_dict() {
            "の構造が変わった(内容は出さない)"
        } else {
            let (lh, rh) = (l.get("sha256"), r.get("sha256"));
            let valid = |v: &V| {
                v.string().is_some_and(|s| {
                    s.as_codepoints().len() == 64
                        && s.as_codepoints()
                            .iter()
                            .all(|c| matches!(c,48..=57|97..=102))
                })
            };
            if valid(lh) && valid(rh) && lh != rh {
                return format!(
                    "{name} の内容が変わった(sha256:{} → {}; 設定値は出さない)",
                    runtime::utf8(lh.string().unwrap()),
                    runtime::utf8(rh.string().unwrap())
                );
            }
            if l.get("kind") != r.get("kind") || l.get("meta") != r.get("meta") {
                "の構造が変わった(内容は出さない)"
            } else {
                "が変わった(内容は出さない)"
            }
        }
    };
    format!("{name} {tail}")
}
pub fn diagnostics(before: &V, after: &V) -> Result<Vec<Text>> {
    for value in [before.get_opt("repo_admin"), after.get_opt("repo_admin")]
        .into_iter()
        .flatten()
    {
        if !value.is_dict() {
            return Err(LegacyException::Python{kind:"AttributeError",reason:format!("'{}' object has no attribute 'get'",value.python_type()),location:"diagnostic_diffs"}.trace("diagnostic_diffs",2076,r#"("repo:config.worktree", before.get("repo_admin", {}).get("config_worktree"), after.get("repo_admin", {}).get("config_worktree")))"#));
        }
    }
    let mut out = Vec::new();
    if before.get("repo_admin").get("path") != after.get("repo_admin").get("path") {
        out.push("repo:git-dir: 人の管理パスが変わった(前の起動元の設定を確認する。未変更とは判定しない)".into());
    }
    for (name, left, right) in [
        (
            "config",
            before.get("common_config"),
            after.get("common_config"),
        ),
        (
            "config.worktree",
            before.get("common_config_worktree"),
            after.get("common_config_worktree"),
        ),
        (
            "repo:config.worktree",
            before.get("repo_admin").get("config_worktree"),
            after.get("repo_admin").get("config_worktree"),
        ),
    ] {
        if left != right {
            out.push(Text::from(config_diagnostic(name, left, right).as_str()));
        }
    }
    let mut keys = REQUIRED.to_vec();
    keys.sort();
    for key in keys {
        if !["common_config", "common_config_worktree", "repo_admin"].contains(&key)
            && before.get(key) != after.get(key)
        {
            out.push(Text::from(format!("{key} が変わった").as_str()));
        }
    }
    if before.get("refs") != after.get("refs") {
        let mut names = BTreeSet::new();
        for value in [before.get("refs"), after.get("refs")] {
            for row in value.list().unwrap_or(&[]) {
                if let Some(name) = row.get("name").string() {
                    let codes = name.as_codepoints();
                    if codes.starts_with(&[114, 101, 102, 115, 47])
                        && codes.len() <= 1024
                        && codes.iter().all(|c| *c >= 33 && *c != 127)
                    {
                        names.insert(codes.to_vec());
                    }
                }
            }
        }
        let changed = names
            .into_iter()
            .filter(|n| {
                let n = V::from(super::json::text(n.clone()));
                find(before, "refs", "name", &n) != find(after, "refs", "name", &n)
            })
            .collect::<Vec<_>>();
        for name in changed.iter().take(50) {
            let mut codes = name.clone();
            codes.extend(": ref が変わった".chars().map(u32::from));
            out.push(super::json::text(codes));
        }
        if changed.len() > 50 {
            out.push("ほかの ref の差分は保存した状態で確認する".into());
        }
    }
    Ok(out)
}
fn find<'a>(state: &'a V, key: &str, path_key: &str, path: &V) -> Option<&'a V> {
    state
        .get(key)
        .list()?
        .iter()
        .rfind(|row| row.get(path_key) == path)
}
fn strict_find<'a>(
    state: &'a V,
    key: &str,
    path_key: &str,
    path: &V,
    line: u32,
    source: &'static str,
) -> Result<Option<&'a V>> {
    let exception = |kind, reason: String, generator| {
        let e = LegacyException::Python {
            kind,
            reason,
            location: "compare.generator",
        };
        let e = if generator {
            e.trace("<genexpr>", line, source)
        } else {
            e
        };
        e.trace("compare", line, source)
    };
    let rows = state.get_opt(key).ok_or_else(|| {
        exception(
            "KeyError",
            super::json::quote(&super::json::Text::from(key)).replace('"', "'"),
            false,
        )
    })?;
    match rows {
        V::List(rows) => {
            for row in rows {
                let value = if let V::Dict(_) = row {
                    row.get_opt(path_key)
                        .ok_or_else(|| exception("KeyError", format!("'{path_key}'"), true))?
                } else {
                    return Err(exception(
                        "TypeError",
                        match row {
                            V::String(_) => "string indices must be integers".into(),
                            V::List(_) => "list indices must be integers or slices, not str".into(),
                            _ => format!("'{}' object is not subscriptable", row.python_type()),
                        },
                        true,
                    ));
                };
                if value == path {
                    return Ok(Some(row));
                }
            }
            Ok(None)
        }
        V::String(value) if value.as_codepoints().is_empty() => Ok(None),
        V::Dict(value) if value.is_empty() => Ok(None),
        V::String(_) | V::Dict(_) => Err(exception(
            "TypeError",
            "string indices must be integers".into(),
            true,
        )),
        _ => Err(exception(
            "TypeError",
            format!("'{}' object is not iterable", rows.python_type()),
            false,
        )),
    }
}
fn required<'a>(row: &'a V, key: &str, line: u32, source: &'static str) -> Result<&'a V> {
    row.get_opt(key).ok_or_else(|| {
        LegacyException::Python {
            kind: "KeyError",
            reason: format!("'{key}'"),
            location: "compare.required",
        }
        .trace("compare", line, source)
    })
}
fn at(state: &V, key: &str, path_key: &str, path: &V) -> Option<usize> {
    state
        .get(key)
        .list()?
        .iter()
        .position(|row| row.get(path_key) == path)
}
fn row_mut<'a>(state: &'a mut V, key: &str, index: usize) -> &'a mut V {
    &mut state.get_mut(key).unwrap().list_mut().unwrap()[index]
}
fn lines(rows: Vec<String>) -> Vec<u8> {
    rows.into_iter()
        .map(|line| format!("差分: {line}\n"))
        .collect::<String>()
        .into_bytes()
}
fn site(args: &Arguments, result: (u8, Vec<u8>), line: u32, source: &'static str) -> (u8, Vec<u8>) {
    args.print_sites
        .borrow_mut()
        .push((result.1.len(), line, source));
    result
}
// Each Python print encodes separately. A later invalid surrogate must not
// discard bytes that earlier prints have already placed in stdout's buffer.
fn diagnostic_lines(
    args: &Arguments,
    before: &V,
    after: &V,
    partial: &mut Vec<u8>,
    line: u32,
) -> Result<Vec<u8>> {
    let source = r#"for line in diagnostic_diffs(before, after, required): print("差分: " + line)"#;
    let rows = diagnostics(before, after).map_err(|e| e.trace("compare", line, source))?;
    let mut output = Vec::new();
    for row in rows {
        let mut codes = "差分: ".chars().map(u32::from).collect::<Vec<_>>();
        codes.extend_from_slice(row.as_codepoints());
        // print writes the value before its terminating newline.
        match runtime::encode(&super::json::text(codes), "utf-8", "surrogateescape") {
            Ok(bytes) => {
                output.extend(bytes);
                output.push(b'\n');
            }
            Err(error) => {
                args.print_sites
                    .borrow_mut()
                    .push((output.len(), line, source));
                partial.extend(output);
                return Err(error.trace("compare", line, source));
            }
        }
    }
    args.print_sites
        .borrow_mut()
        .push((output.len(), line, source));
    Ok(output)
}
fn failure(
    args: &Arguments,
    before: &V,
    after: &V,
    message: &str,
    partial: &mut Vec<u8>,
) -> Result<(u8, Vec<u8>)> {
    let mut out = diagnostic_lines(args, before, after, partial, 1991)?;
    out.extend(lines(vec![message.into()]));
    Ok(site(
        args,
        (1, out),
        1992,
        r#"print("差分: 自分の worktree/ref の期待状態に一致しない"); return 1"#,
    ))
}
fn clean_index(content: &V) -> Result<bool> {
    let Some(index) = content.get_opt("index") else {
        return Ok(false);
    };
    if !index.is_dict() {
        return Err(LegacyException::Python {
            kind: "AttributeError",
            reason: format!("'{}' object has no attribute 'get'", index.python_type()),
            location: "compare.index",
        }
        .trace(
            "compare",
            1990,
            r#"or not ac.get("index", {}).get("head_clean")):"#,
        ));
    }
    Ok(index.get("head_clean").truth())
}
pub fn compare(
    args: &Arguments,
    int_limit: usize,
    active: &mut Option<LegacyException>,
    partial: &mut Vec<u8>,
) -> Result<(u8, Vec<u8>)> {
    let read = || -> Result<(V, V)> {
        Ok((
            state::read(Path::new(args.raw("before")), &args.limits, int_limit).map_err(|e|e.at("compare",1961,"before = read_state(args.before, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)"))?,
            state::read(Path::new(args.raw("after")), &args.limits, int_limit).map_err(|e|e.at("compare",1962,"after = read_state(args.after, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds)"))?,
        ))
    };
    let (before, mut after) = match read() {
        Ok(v) => v,
        Err(e) if e.stop_reason().is_some() => {
            *active = Some(e);
            return Ok(site(
                args,
                (1, "差分: 状態を読めない\n".as_bytes().to_vec()),
                1964,
                r#"print("差分: 状態を読めない", file=sys.stdout); return 1"#,
            ));
        }
        Err(e) => return Err(e),
    };
    if !state::schema(&before) || REQUIRED.iter().any(|k| before.get_opt(k).is_none()) {
        let mut out = Vec::new();
        if before.get_opt("repo_admin").is_none() {
            out.push("repo:git-dir: 保存済み状態に人の管理パスが無い(前の起動元の設定を確認する。現在の設定で中断前の基準を補完しない)".into());
            args.print_sites.borrow_mut().push((lines(out.clone()).len(),1968,r#"print("差分: repo:git-dir: 保存済み状態に人の管理パスが無い(前の起動元の設定を確認する。現在の設定で中断前の基準を補完しない)")"#));
        }
        out.push("保存済み状態が新しい必須欄を持たない(補完せず停止)".into());
        return Ok(site(
            args,
            (1, lines(out)),
            1969,
            r#"print("差分: 保存済み状態が新しい必須欄を持たない(補完せず停止)"); return 1"#,
        ));
    }
    if before == after {
        return Ok((0, Vec::new()));
    }
    if !args.raw("self-worktree").is_empty() && !args.raw("self-ref").is_empty() {
        let path = V::from(runtime::os_text(args.raw("self-worktree")));
        let reference = V::from(runtime::os_text(args.raw("self-ref")));
        let bw = strict_find(
            &before,
            "worktrees",
            "worktree",
            &path,
            1979,
            "bw = next((w for w in before[\"worktrees\"] if w[\"worktree\"] == args.self_worktree), None)",
        )?;
        let aw = strict_find(&after, "worktrees", "worktree", &path,1980,"aw = next((w for w in after[\"worktrees\"] if w[\"worktree\"] == args.self_worktree), None)")?.cloned();
        let ar = strict_find(
            &after,
            "refs",
            "name",
            &reference,
            1981,
            "ar = next((r for r in after[\"refs\"] if r[\"name\"] == args.self_ref), None)",
        )?
        .cloned();
        let bc = strict_find(
            &before,
            "worktree_contents",
            "path",
            &path,
            1982,
            "bc = next((c for c in before[\"worktree_contents\"] if c[\"path\"] == args.self_worktree), None)",
        )?;
        let ac = strict_find(&after, "worktree_contents", "path", &path,1983,"ac = next((c for c in after[\"worktree_contents\"] if c[\"path\"] == args.self_worktree), None)")?.cloned();
        let detached = bw.is_some_and(|bw| {
            aw.as_ref().is_some_and(|aw| {
                bw.get("branch").is("")
                    && aw.get("branch").is("")
                    && bw.get("HEAD") != aw.get("HEAD")
                    && ar.is_none()
            })
        });
        if bw.is_none()
            || aw.is_none()
            || bc.is_none()
            || ac.is_none()
            || bw.unwrap().get("locked") != aw.as_ref().unwrap().get("locked")
            || (!bw.unwrap().get("branch").is("") && bw.unwrap().get("branch") != &reference)
            || (!detached
                && (ar.is_none()
                    || aw.as_ref().unwrap().get("branch") != &reference
                    || aw.as_ref().unwrap().get("HEAD") != ar.as_ref().unwrap().get("oid")
                    || ar.as_ref().unwrap().get("symref").truth()))
            || !clean_index(ac.as_ref().unwrap())?
        {
            return failure(
                args,
                &before,
                &after,
                "自分の worktree/ref の期待状態に一致しない",
                partial,
            );
        }
        let bw = bw.unwrap();
        let expected = required(
            aw.as_ref().unwrap(),
            "HEAD",
            1995,
            r#"expected_oid = aw["HEAD"]"#,
        )?
        .clone();
        let ai = at(&after, "worktrees", "worktree", &path).unwrap();
        let target = row_mut(&mut after, "worktrees", ai);
        target.set(
            "HEAD",
            required(bw, "HEAD", 1996, r#"aw["HEAD"] = bw["HEAD"]"#)?.clone(),
        );
        target.set(
            "branch",
            required(bw, "branch", 1997, r#"aw["branch"] = bw["branch"]"#)?.clone(),
        );
        if let Some(detached) = bw.get_opt("detached") {
            target.set("detached", detached.clone())
        } else {
            target.remove("detached")
        };
        let ci = at(&after, "worktree_contents", "path", &path).unwrap();
        row_mut(&mut after, "worktree_contents", ci).set(
            "index",
            required(bc.unwrap(), "index", 2002, r#"ac["index"] = bc["index"]"#)?.clone(),
        );
        let bcfg = strict_find(
            &before,
            "worktree_configs",
            "path",
            &path,
            2006,
            "bcfg = next((c for c in before[\"worktree_configs\"] if c[\"path\"] == args.self_worktree), None)",
        )?;
        let acfg = strict_find(
            &after,
            "worktree_configs",
            "path",
            &path,
            2007,
            "acfg = next((c for c in after[\"worktree_configs\"] if c[\"path\"] == args.self_worktree), None)",
        )?;
        let acfg = acfg.and_then(|_| at(&after, "worktree_configs", "path", &path));
        if bcfg.is_none()
            || acfg.is_none()
            || !bcfg.unwrap().get("private_refs").is_dict()
            || !after.get("worktree_configs").list().unwrap()[acfg.unwrap()]
                .get("private_refs")
                .is_dict()
        {
            return Ok(site(
                args,
                (
                    1,
                    lines(vec!["自分の private HEAD の期待状態に一致しない".into()]),
                ),
                2009,
                r#"print("差分: 自分の private HEAD の期待状態に一致しない"); return 1"#,
            ));
        }
        row_mut(&mut after, "worktree_configs", acfg.unwrap())
            .get_mut("private_refs")
            .unwrap()
            .set(
                "head",
                bcfg.unwrap().get("private_refs").get("head").clone(),
            );
        if detached {
            return Ok(if before == after {
                (0, Vec::new())
            } else {
                (1, diagnostic_lines(args, &before, &after, partial, 2013)?)
            });
        }
        let br = strict_find(
            &before,
            "refs",
            "name",
            &reference,
            2015,
            "br = next((r for r in before[\"refs\"] if r[\"name\"] == args.self_ref), None)",
        )?;
        let ri = at(&after, "refs", "name", &reference).unwrap();
        if br.is_some_and(|br| after.get("refs").list().unwrap()[ri].get("oid") == br.get("oid")) {
            return Ok(site(
                args,
                (
                    1,
                    lines(vec!["自分の branch が新しい commit へ進んでいない".into()]),
                ),
                2023,
                r#"print("差分: 自分の branch が新しい commit へ進んでいない"); return 1"#,
            ));
        }
        if let Some(br) = br {
            row_mut(&mut after, "refs", ri).set(
                "oid",
                required(br, "oid", 2025, r#"ar["oid"] = br["oid"]"#)?.clone(),
            )
        } else {
            after
                .get_mut("refs")
                .unwrap()
                .list_mut()
                .unwrap()
                .remove(ri);
        }
        let name = reference.string().unwrap().as_codepoints();
        let prefix = "refs/heads/".chars().map(u32::from).collect::<Vec<_>>();
        let mut remote = "refs/remotes/origin/"
            .chars()
            .map(u32::from)
            .collect::<Vec<_>>();
        remote.extend_from_slice(name.strip_prefix(prefix.as_slice()).unwrap_or(name));
        let remote = V::from(super::json::text(remote));
        let br = strict_find(
            &before,
            "refs",
            "name",
            &remote,
            2032,
            "br = next((r for r in before[\"refs\"] if r[\"name\"] == remote), None)",
        )?;
        let ar = strict_find(
            &after,
            "refs",
            "name",
            &remote,
            2033,
            r#"ar = next((r for r in after["refs"] if r["name"] == remote), None)"#,
        )?;
        let ri = ar.and_then(|_| at(&after, "refs", "name", &remote));
        if let Some(ri) = ri {
            let ar = &after.get("refs").list().unwrap()[ri];
            if br != Some(ar) {
                if ar.get("oid") != &expected || ar.get("symref").truth() {
                    return Ok(site(
                        args,
                        (
                            1,
                            lines(vec![
                                "自分の remote-tracking ref の期待状態に一致しない".into(),
                            ]),
                        ),
                        2036,
                        r#"print("差分: 自分の remote-tracking ref の期待状態に一致しない"); return 1"#,
                    ));
                }
                if let Some(br) = br {
                    row_mut(&mut after, "refs", ri).set(
                        "oid",
                        required(br, "oid", 2037, r#"if br: ar["oid"] = br["oid"]"#)?.clone(),
                    )
                } else {
                    after
                        .get_mut("refs")
                        .unwrap()
                        .list_mut()
                        .unwrap()
                        .remove(ri);
                }
            }
        }
    }
    Ok(if before == after {
        (0, Vec::new())
    } else {
        (1, diagnostic_lines(args, &before, &after, partial, 2040)?)
    })
}
