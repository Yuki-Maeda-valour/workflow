use super::{
    budget::Budget,
    error::{LegacyException as E, Result},
    json::{PyValue as V, Text},
    process, runtime, scan, state,
};
use dev_workflow_platform::{
    fs::{FileSnapshot, FsError, HeldFile},
    loop_state::{self as platform, InheritedInput, LoopArg, MemoryFile, MemoryPart},
};
use std::{
    collections::{HashMap, HashSet},
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
};
pub type Rows = Vec<(Text, Option<Text>)>;
// 旧版の dict の鍵は str。保存状態のパスは文字番号のまま鍵・比較に使い、
// OS のバイトへは旧版がファイルを操作する所でだけ変える。
pub type Trusted = Vec<(Text, V)>;
pub type Candidates = Vec<((String, Text), V)>;
/// OS のパス(Git の出力・引数・環境)を、旧版の str と同じ文字番号にする。
pub fn path_text(path: &Path) -> Text {
    runtime::os_text(path.as_os_str())
}
fn stop(reason: &str) -> E {
    E::stop(reason)
}
/// 旧 `key[len("includeif."):-len(".path")]`。Python のスライスと同じく、
/// 終わりが始まりより前なら空の条件になる(`[includeIf]` の `path`)。
pub fn includeif_condition(key: &Text) -> Text {
    let codes = key.as_codepoints();
    let start = 10.min(codes.len());
    let end = codes.len().saturating_sub(5);
    super::json::text(if end > start {
        codes[start..end].to_vec()
    } else {
        Vec::new()
    })
}
fn key(value: &Text) -> String {
    runtime::utf8(value).to_lowercase()
}
pub fn config_rows(raw: &[u8]) -> Rows {
    raw.split(|b| *b == 0)
        .filter(|r| !r.is_empty())
        .map(|r| {
            if let Some(at) = r.iter().position(|b| *b == 10) {
                (
                    runtime::decode(&r[..at]),
                    Some(runtime::decode(&r[at + 1..])),
                )
            } else {
                (runtime::decode(r), None)
            }
        })
        .collect()
}
fn origins(raw: &[u8], top: &Path) -> Vec<PathBuf> {
    let mut result = Vec::new();
    for part in raw.split(|b| *b == 0) {
        if let Some(path) = part.strip_prefix(b"file:") {
            let path = path.split(|b| *b == 9).next().unwrap();
            let path = PathBuf::from(runtime::os(&runtime::decode(path)).unwrap());
            let path = if path.is_absolute() {
                path
            } else {
                top.join(path)
            };
            if !result
                .iter()
                .any(|p: &PathBuf| p.as_os_str() == path.as_os_str())
            {
                result.push(path)
            }
        }
    }
    result
}
fn isolated() -> Vec<(OsString, OsString)> {
    vec![
        ("GIT_CONFIG_NOSYSTEM".into(), "1".into()),
        ("GIT_CONFIG_GLOBAL".into(), "/dev/null".into()),
    ]
}
fn memory<'a>(name: &str, parts: &[MemoryPart<'a>]) -> Result<MemoryFile<'a>> {
    MemoryFile::create(name, parts).map_err(|e| match e {
        platform::MemoryError::Create(_) => stop("includeIf の安全な条件判定を作れない"),
        // 旧 memory_file の os.write の失敗(RLIMIT_FSIZE の EFBIG など)。except Exception で
        // fd を閉じて上げ直すので、817 行のフレームを持つ OSError になる。
        platform::MemoryError::Write(error) => E::Os {
            error,
            path: None,
            location: "memory_file",
        }
        .trace("memory_file", 817, "offset += os.write(fd, raw[offset:])"),
    })
}
/// 旧 config_quote(str)。文字番号の上で置き換え、符号化は呼出元の .encode() で行う。
fn quote_text(value: &Text) -> Vec<u32> {
    let mut result = vec![34];
    for &c in value.as_codepoints() {
        match c {
            92 => result.extend([92, 92]),
            34 => result.extend([92, 34]),
            10 => result.extend([92, 110]),
            9 => result.extend([92, 116]),
            _ => result.push(c),
        }
    }
    result.push(34);
    result
}
fn ascii(value: &str) -> impl Iterator<Item = u32> + '_ {
    value.chars().map(u32::from)
}
/// 旧版の明示の `.encode()`(UTF-8 strict)。標準出力の符号化(location "output")と分ける。
fn encode_strict(codes: Vec<u32>) -> Result<Vec<u8>> {
    runtime::encode(&super::json::text(codes), "utf-8", "strict").map_err(|e| match e {
        E::Python { kind, reason, .. } => E::Python {
            kind,
            reason,
            location: "encode",
        },
        other => other,
    })
}
pub fn regular(path: &Path, budget: &mut Budget) -> Result<(HeldFile, FileSnapshot)> {
    let (parent, name) = state::raw_parent(path)?;
    let before = parent
        .metadata(&name)
        .map_err(|_| stop("到達する config が無い"))?;
    budget.tick(0)?;
    if !before.is_regular() {
        return Err(stop("到達する config が通常ファイルでない"));
    }
    let file = super::observe::open(&parent, &name, &before).map_err(config_open_error)?;
    Ok((file, before))
}
fn config_open_error(error: FsError) -> E {
    match error {
        FsError::Changed | FsError::NotRegular => stop("config が lstat と open の間に変わった"),
        e => stop("到達する config を安全に開けない").contextual(e.into(), true),
    }
}
pub fn safe(
    top: &Path,
    path: &Path,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
    exact: bool,
    parse: bool,
) -> Result<(V, Rows)> {
    safe_keyed(top, path, &path_text(path), budget, trusted, exact, parse)
}
/// 旧 safe_config_file(top, path)。path は保存状態の str(key)のまま鍵・記録に使い、
/// OS の操作には呼出元が変えたバイト(path)を使う。
pub fn safe_keyed(
    top: &Path,
    path: &Path,
    key: &Text,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
    exact: bool,
    parse: bool,
) -> Result<(V, Rows)> {
    safe_inner(top, path, key, budget, trusted, exact, parse).map_err(|e| {
        if e.is_os() {
            stop("到達する config を安全に開けない").contextual(e, true)
        } else {
            e
        }
    })
}
fn safe_inner(
    top: &Path,
    path: &Path,
    key: &Text,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
    exact: bool,
    parse: bool,
) -> Result<(V, Rows)> {
    super::observe::budget("file", budget)?;
    // 旧 os.path.split(path) と open_dir_path(parent_path)(721 行)。相対は Stop。
    let (parent, name) = state::raw_parent(path).map_err(|e| {
        e.at(
            "safe_config_file",
            721,
            "parent = open_dir_path(parent_path)",
        )
    })?;
    if std::os::unix::ffi::OsStrExt::as_bytes(name.as_os_str()).contains(&0) {
        // os.stat(name, dir_fd=parent) の ValueError は except OSError に当たらない(725 行)。
        return Err(E::Python {
            kind: "ValueError",
            reason: "embedded null byte".into(),
            location: "os.stat",
        }
        .trace(
            "safe_config_file",
            725,
            "before = os.stat(name, dir_fd=parent, follow_symlinks=False)",
        ));
    }
    let before = parent
        .metadata(&name)
        .map_err(|_| stop("到達する config が無い"))?;
    budget.tick(0)?;
    let expected = trusted.and_then(|t| t.iter().find(|(p, _)| p == key).map(|(_, v)| v));
    if trusted.is_some() && expected.is_none() {
        return Err(stop("config の新しい origin は基準が無いため開けない"));
    }
    if expected
        .is_some_and(|e| !e.is_dict() || (!e.get("kind").is("file") && !e.get("kind").is("link")))
    {
        return Err(stop("保存済み config の形式を読めない"));
    }
    let mut link = None;
    let (mut file, target_before) = if before.is_regular() {
        if expected.is_some_and(|e| !e.get("kind").is("file")) {
            return Err(stop("config の link 構造が基準から変わった"));
        }
        (
            super::observe::open(&parent, &name, &before).map_err(config_open_error)?,
            before,
        )
    } else if scan::kind(&before) == "link" {
        if expected
            .is_some_and(|e| !e.get("kind").is("link") || e.get("meta") != &scan::meta(&before))
        {
            return Err(stop("config の link 構造が基準から変わった"));
        }
        let target =
            platform::read_link(&parent, &name).map_err(|_| stop("config の link を読めない"))?;
        let target_value = V::from(runtime::os_text(&target));
        if expected.is_some_and(|e| e.get("target") != &target_value) {
            return Err(stop("config の link 先が基準から変わった"));
        }
        // 旧 os.path.join(parent_path, link_target)(parent_path は os.path.split の頭)。
        let target_path = state::join_raw(&state::dirname(path), Path::new(&target));
        let (file, st) = regular(&target_path, budget)?;
        if let Some(expected) = expected {
            let target = expected.get("target_entry").get("meta");
            if !target.is_dict()
                || target.get("dev") != &V::from(st.dev)
                || target.get("ino") != &V::from(st.ino)
                || target.get("mode") != &V::from(i128::from(st.mode & 0o7777))
            {
                return Err(stop("config の link 先の実体が基準から変わった"));
            }
        }
        link = Some(target);
        (file, st)
    } else {
        return Err(stop("到達する config が通常ファイルでも link でもない"));
    };
    budget.one(target_before.size.max(0) as u128)?;
    let raw = state::read_held(&mut file, budget).map_err(|e| {
        if e.is_os() {
            stop("通常ファイルを読めない").contextual(e, true)
        } else {
            e
        }
    })?;
    let target_entry = V::object([
        ("kind", V::from("file")),
        ("meta", scan::meta(&target_before)),
        ("sha256", V::from(state::hash(&raw))),
    ]);
    let mut observed = if let Some(target) = &link {
        V::object([
            ("kind", V::from("link")),
            ("meta", scan::meta(&before)),
            ("target", V::from(runtime::os_text(target))),
            ("target_entry", target_entry),
        ])
    } else {
        target_entry
    };
    let mut recorded = "config:".chars().map(u32::from).collect::<Vec<_>>();
    recorded.extend_from_slice(key.as_codepoints());
    observed.set("path", V::from(super::json::text(recorded)));
    if exact && expected != Some(&observed) {
        return Err(stop("保持済み config がイテレーションの途中で変わった"));
    }
    let verify = || -> Result<()> {
        if let Some(target) = &link
            && (parent.metadata(&name)? != before
                || platform::read_link(&parent, &name)? != *target)
        {
            return Err(stop("config の link が読取中に変わった"));
        }
        Ok(())
    };
    verify()?;
    if !parse {
        return Ok((observed, Vec::new()));
    }
    let input = InheritedInput::Held(&file);
    let raw_args = ["config", "--file"];
    let tail = ["--no-includes", "--null", "--list"];
    let args = raw_args
        .iter()
        .map(|s| LoopArg::Raw(OsStr::new(s)))
        .chain(std::iter::once(LoopArg::InputPath(input)))
        .chain(tail.iter().map(|s| LoopArg::Raw(OsStr::new(s))))
        .collect::<Vec<_>>();
    let parsed = process::run(OsStr::new("/"), &args, &[input], budget, false, &isolated())?.1;
    if file.snapshot()? != target_before {
        return Err(stop("config が Git の解析中に変わった"));
    }
    verify()?;
    let _ = top;
    Ok((observed, config_rows(&parsed)))
}
const INCLUDE_LINE: &str = r#"fd = memory_file("loop-state-include-path", ("[loop-state-path]\n\tvalue = " + config_quote(value) + "\n").encode())"#;
const MARKER_LINE: &str = r#"marker_fd = memory_file("loop-state-include-marker", b"[loop-state-probe]\n\tactive = yes\n")"#;
const ENCODE_LINE: &str = r#"f"\tpath = /proc/self/fd/{marker_fd}", "")).encode()"#;
const CONDITION_LINE: &str = r#"probe_fd = memory_file("loop-state-include-condition", probe)"#;
pub fn include(
    top: &Path,
    value: Option<&Text>,
    origin: &Path,
    budget: &mut Budget,
) -> Result<PathBuf> {
    // 実効値のグラフの origin は Git の出力から読んだパスなので、結果も符号化できる。
    let target = include_text(top, value, &path_text(origin), budget)?;
    Ok(PathBuf::from(runtime::os(&target)?))
}
/// 旧 include_target(top, value, origin)。相対パスは、文字列の origin の
/// os.path.dirname へ os.path.join する(保存状態の origin の文字番号を保つ)。
pub fn include_text(
    top: &Path,
    value: Option<&Text>,
    origin: &Text,
    budget: &mut Budget,
) -> Result<Text> {
    super::observe::budget("include-path", budget)?;
    let value = value
        .filter(|v| !v.as_codepoints().is_empty() && !v.as_codepoints().contains(&0))
        .ok_or_else(|| stop("include の path が不正である"))?;
    // 旧版は全文を組み立ててから .encode() する。失敗の位置は全文の中の文字番号になる。
    let mut text = ascii("[loop-state-path]\n\tvalue = ").collect::<Vec<_>>();
    text.extend(quote_text(value));
    text.push(10);
    let raw = encode_strict(text).map_err(|e| e.trace("include_target", 1065, INCLUDE_LINE))?;
    let fd = memory("loop-state-include-path", &[MemoryPart::Raw(&raw)])
        .map_err(|e| e.at("include_target", 1065, INCLUDE_LINE))?;
    let input = InheritedInput::Memory(&fd);
    let args = [
        LoopArg::Raw(OsStr::new("config")),
        LoopArg::Raw(OsStr::new("--file")),
        LoopArg::InputPath(input),
        LoopArg::Raw(OsStr::new("--no-includes")),
        LoopArg::Raw(OsStr::new("--type=path")),
        LoopArg::Raw(OsStr::new("--get")),
        LoopArg::Raw(OsStr::new("loop-state-path.value")),
    ];
    let raw = process::run(top.as_os_str(), &args, &[input], budget, false, &[])?.1;
    drop(fd);
    let expanded = raw.strip_suffix(b"\n").unwrap_or(&raw);
    if expanded.is_empty() || expanded.starts_with(b"~") {
        return Err(stop("include の path を Git と同じ規則で展開できない"));
    }
    let expanded = runtime::decode(expanded);
    if expanded.as_codepoints().starts_with(&[47]) {
        return Ok(expanded);
    }
    // os.path.join(os.path.dirname(origin), expanded)。
    Ok(super::json::text(state::join_codes(
        &state::dirname_codes(origin.as_codepoints()),
        expanded.as_codepoints(),
    )))
}
pub fn probe(
    top: &Path,
    condition: &Text,
    origin: &Path,
    effective: &Rows,
    budget: &mut Budget,
    execution: Option<&Path>,
    extra: &[(OsString, OsString)],
) -> Result<bool> {
    super::observe::budget("condition", budget)?;
    // 旧 probe_condition: condition[:len(prefix)] の字面(大文字・小文字)を保ち、
    // os.path.join(os.path.dirname(origin), condition[len(prefix) + 2:]) をつなぐ。
    let mut condition = condition.clone();
    let lower = key(&condition);
    for prefix in ["gitdir/i:", "gitdir:"] {
        if lower.starts_with(prefix)
            && condition
                .as_codepoints()
                .get(prefix.len()..)
                .is_some_and(|s| s.starts_with(&[46, 47]))
        {
            let codes = condition.as_codepoints();
            let mut rewritten = codes[..prefix.len()].to_vec();
            rewritten.extend(state::join_codes(
                &state::dirname_codes(path_text(origin).as_codepoints()),
                &codes[prefix.len() + 2..],
            ));
            condition = super::json::text(rewritten);
            break;
        }
    }
    // remote_lines は文字番号のまま作る(符号化は後の .encode() の 1 回だけ)。
    let mut text: Vec<u32> = Vec::new();
    for (k, v) in effective {
        let folded = key(k);
        if let Some(v) = v
            && folded.starts_with("remote.")
            && folded.ends_with(".url")
            && k.as_codepoints().len() > "remote..url".len()
        {
            text.extend(ascii("[remote "));
            text.extend(quote_text(&super::json::text(
                k.as_codepoints()[7..k.as_codepoints().len() - 4].to_vec(),
            )));
            text.extend(ascii("]\n\turl = "));
            text.extend(quote_text(v));
            text.push(10);
        }
    }
    text.extend(ascii("[includeIf "));
    text.extend(quote_text(&condition));
    text.extend(ascii("]\n\tpath = "));
    // 旧版は marker の memfd を先に作り、その後で全文を .encode() する。
    let marker = memory(
        "loop-state-include-marker",
        &[MemoryPart::Raw(b"[loop-state-probe]\n\tactive = yes\n")],
    )
    .map_err(|e| e.at("probe_condition", 842, MARKER_LINE))?;
    let marker_input = InheritedInput::Memory(&marker);
    // 残り("/proc/self/fd/<番号>" と改行)は ASCII なので、失敗の位置は前の部分で決まる。
    let prefix = encode_strict(text).map_err(|e| e.trace("probe_condition", 846, ENCODE_LINE))?;
    let probe = memory(
        "loop-state-include-condition",
        &[
            MemoryPart::Raw(&prefix),
            MemoryPart::InputPath(marker_input),
            MemoryPart::Raw(b"\n"),
        ],
    )
    .map_err(|e| e.at("probe_condition", 847, CONDITION_LINE))?;
    let probe_input = InheritedInput::Memory(&probe);
    let args = [
        LoopArg::Raw(OsStr::new("config")),
        LoopArg::Raw(OsStr::new("--file")),
        LoopArg::InputPath(probe_input),
        LoopArg::Raw(OsStr::new("--includes")),
        LoopArg::Raw(OsStr::new("--get")),
        LoopArg::Raw(OsStr::new("loop-state-probe.active")),
    ];
    let (rc, out) = process::run(
        execution.unwrap_or(top).as_os_str(),
        &args,
        &[probe_input, marker_input],
        budget,
        true,
        extra,
    )?;
    drop(probe);
    drop(marker);
    match rc {
        1 => Ok(false),
        0 => Ok(runtime::py_bytes_strip(&out) == b"yes"),
        _ => Err(stop("includeIf の条件を Git で判定できない")),
    }
}
pub fn candidates_paths(
    common: &Path,
    git_dir: &Path,
    budget: &mut Budget,
    held_system: Option<&Text>,
) -> Result<Vec<(String, Text)>> {
    let mut paths: Vec<(String, Text)> = vec![
        ("common".into(), path_text(&common.join("config"))),
        ("common".into(), path_text(&common.join("config.worktree"))),
    ];
    // 旧 os.path.abspath(git_dir) != os.path.abspath(common) は str の比較(先頭の「//」を「/」と同じに見ない)。
    if state::absolute(git_dir)?.as_os_str() != state::absolute(common)?.as_os_str() {
        paths.push((
            "worktree".into(),
            path_text(&git_dir.join("config.worktree")),
        ))
    }
    if let Some(global) = std::env::var_os("GIT_CONFIG_GLOBAL") {
        if global != "/dev/null" {
            paths.push((
                "global".into(),
                path_text(&state::absolute(Path::new(&global))?),
            ));
        }
    } else if let Some(home) = std::env::var_os("HOME").filter(|s| !s.is_empty()) {
        let home = PathBuf::from(home);
        let xdg = std::env::var_os("XDG_CONFIG_HOME")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".config"));
        paths.push(("global".into(), path_text(&xdg.join("git/config"))));
        paths.push(("global".into(), path_text(&home.join(".gitconfig"))));
    }
    if !matches!(
        std::env::var("GIT_CONFIG_NOSYSTEM").as_deref(),
        Ok("1" | "true" | "yes")
    ) {
        // 保存状態の system の場所は str のまま(符号化できなくても、旧版は lexists で無いと見る)。
        let system = if let Some(value) =
            std::env::var_os("GIT_CONFIG_SYSTEM").filter(|s| !s.is_empty())
        {
            path_text(&state::absolute(Path::new(&value))?)
        } else if let Some(path) = held_system {
            path.clone()
        } else {
            let args = process::strings(&["var", "GIT_CONFIG_SYSTEM"]);
            let refs = args.iter().map(|s| LoopArg::Raw(s)).collect::<Vec<_>>();
            let raw=process::run(OsStr::new("/"),&refs,&[],budget,false,&[("GIT_CONFIG_GLOBAL".into(),"/dev/null".into())]).map_err(|e| if e.stop_reason().is_some() { stop("Git が system config の既定パス取得(git var GIT_CONFIG_SYSTEM)に対応しない。GIT_CONFIG_SYSTEM で実パスを明示する") } else { e })?.1;
            // 旧 raw.decode("utf-8", "surrogateescape").strip()(str.strip)。
            let value = runtime::py_text_strip(&runtime::decode(&raw));
            if value.as_codepoints().is_empty() || value.as_codepoints().contains(&0) {
                return Err(stop(
                    "Git の system config の既定パスを読めない。GIT_CONFIG_SYSTEM で実パスを明示する",
                ));
            }
            path_text(&state::absolute(Path::new(&runtime::os(&value)?))?)
        };
        paths.push(("system".into(), system));
    }
    let mut result = Vec::new();
    for (kind, path) in paths {
        // dict.fromkeys((kind, os.path.abspath(path)) ...): str の組で重複を除く。
        let pair = (kind, state::abspath_text(&path));
        if !result.contains(&pair) {
            result.push(pair)
        }
    }
    Ok(result)
}
/// 旧 os.path.lexists(str): 符号化できないパス(ValueError)も False。
pub fn lexists_text(path: &Text) -> bool {
    runtime::os(path).is_ok_and(|p| Path::new(&p).symlink_metadata().is_ok())
}
pub fn candidates(top: &Path, common: &Path, git_dir: &Path, budget: &mut Budget) -> Result<V> {
    super::observe::budget("candidates", budget)?;
    let mut result = Vec::new();
    for (kind, text) in candidates_paths(common, git_dir, budget, None)? {
        let mut row = V::object([("kind", V::from(kind)), ("path", V::from(text.clone()))]);
        // 保存状態を使わない候補は OS から得たパスなので、必ず符号化できる。
        let path = PathBuf::from(runtime::os(&text)?);
        if path.symlink_metadata().is_err() {
            row.set("state", V::from("absent"));
        } else {
            let (item, _) = safe(top, &path, budget, None, false, false)?;
            row.set("state", V::from("present"));
            row.set("entry", item);
        }
        result.push(row)
    }
    Ok(V::List(result))
}
pub fn trusted(prior: &V, fresh: bool) -> Result<Option<Trusted>> {
    if matches!(prior, V::Null) || fresh {
        return Ok(None);
    }
    let rows = prior
        .get("worktree_configs")
        .list()
        .ok_or_else(|| stop("前の config の状態を読めない"))?;
    let blocks =
        std::iter::once(prior.get("config")).chain(rows.iter().map(|r| r.get("effective")));
    let mut result: Trusted = Vec::new();
    for block in blocks {
        let origins = block
            .get("origins")
            .list()
            .ok_or_else(|| stop("前の config の状態を読めない"))?;
        for item in origins {
            let path = item
                .get("path")
                .string()
                .ok_or_else(|| stop("前の config の状態を読めない"))?;
            let codes = path.as_codepoints();
            let prefix = "config:".chars().map(u32::from).collect::<Vec<_>>();
            if !codes.starts_with(&prefix) {
                return Err(stop("前の config の状態を読めない"));
            }
            let path = super::json::text(codes[prefix.len()..].to_vec());
            if let Some((_, old)) = result.iter().find(|(p, _)| *p == path) {
                if old != item {
                    return Err(stop("前の config の状態が矛盾している"));
                }
            } else {
                result.push((path, item.clone()))
            }
        }
    }
    Ok(Some(result))
}
pub fn trusted_candidates(prior: &V, fresh: bool) -> Result<Option<Candidates>> {
    if matches!(prior, V::Null) || fresh {
        return Ok(None);
    }
    let rows = prior
        .get("worktree_configs")
        .list()
        .ok_or_else(|| stop("前の config 候補の状態を読めない"))?;
    let mut result: Candidates = Vec::new();
    for block in std::iter::once(prior.get("config")).chain(rows.iter().map(|r| r.get("effective")))
    {
        let candidates = block
            .get("candidates")
            .list()
            .ok_or_else(|| stop("前の config 候補の状態を読めない"))?;
        for item in candidates {
            let kind = item
                .get("kind")
                .string()
                .map(runtime::utf8)
                .filter(|s| ["common", "worktree", "global", "system"].contains(&s.as_str()))
                .ok_or_else(|| stop("前の config 候補の状態を読めない"))?;
            let path = item
                .get("path")
                .string()
                .cloned()
                .ok_or_else(|| stop("前の config 候補の状態を読めない"))?;
            if (!item.get("state").is("absent") && !item.get("state").is("present"))
                || (item.get("state").is("present") && !item.get("entry").is_dict())
                || (item.get("state").is("absent") && !matches!(item,V::Dict(v)if v.len()==3))
            {
                return Err(stop("前の config 候補の状態を読めない"));
            }
            let key = (kind, path);
            if let Some((_, old)) = result.iter().find(|(k, _)| k == &key) {
                if old != item {
                    return Err(stop("前の config 候補の状態が矛盾している"));
                }
            } else {
                result.push((key, item.clone()));
            }
        }
    }
    Ok(Some(result))
}

#[derive(Clone, Copy, Debug)]
pub enum ConfigEvent<'a> {
    RootsStart,
    RootsDone,
    ReadStart(&'a Path),
    ReadDone(&'a Path),
    ConditionStart(&'a Path),
    IncludePath(&'a Path),
    EffectiveStart,
    EffectiveDone,
    CandidatesStart,
    CandidatesDone,
}
pub trait Observer {
    fn event(
        &mut self,
        event: ConfigEvent<'_>,
        budget: &mut Budget,
        raw: Option<&[u8]>,
    ) -> Result<()>;
}
struct Quiet;
impl Observer for Quiet {
    fn event(
        &mut self,
        event: ConfigEvent<'_>,
        _budget: &mut Budget,
        _raw: Option<&[u8]>,
    ) -> Result<()> {
        match event {
            ConfigEvent::ReadStart(path)
            | ConfigEvent::ReadDone(path)
            | ConfigEvent::ConditionStart(path)
            | ConfigEvent::IncludePath(path) => {
                let _ = path;
            }
            _ => (),
        }
        Ok(())
    }
}
// 同じグラフ処理を、実 IO と独立した制御順検査の双方から使う。
// 製品入口は RealOperations のみを渡す。
pub(super) trait Operations {
    fn roots(&mut self, top: &Path, budget: &mut Budget) -> Result<Vec<u8>>;
    fn file(
        &mut self,
        top: &Path,
        path: &Path,
        budget: &mut Budget,
        trusted: Option<&Trusted>,
    ) -> Result<(V, Rows, (i128, i128, u32))>;
    fn include(
        &mut self,
        top: &Path,
        value: Option<&Text>,
        origin: &Path,
        budget: &mut Budget,
    ) -> Result<PathBuf>;
    fn condition(
        &mut self,
        top: &Path,
        condition: &Text,
        origin: &Path,
        effective: &Rows,
        budget: &mut Budget,
    ) -> Result<bool>;
    fn effective(&mut self, top: &Path, budget: &mut Budget) -> Result<Vec<u8>>;
    fn candidates(
        &mut self,
        top: &Path,
        common: &Path,
        git_dir: &Path,
        budget: &mut Budget,
    ) -> Result<V>;
}
struct RealOperations;
impl Operations for RealOperations {
    fn roots(&mut self, top: &Path, budget: &mut Budget) -> Result<Vec<u8>> {
        process::git(
            top.as_os_str(),
            &process::strings(&[
                "config",
                "--show-origin",
                "--show-scope",
                "--no-includes",
                "--null",
                "--list",
            ]),
            budget,
        )
    }
    fn file(
        &mut self,
        top: &Path,
        path: &Path,
        budget: &mut Budget,
        trusted: Option<&Trusted>,
    ) -> Result<(V, Rows, (i128, i128, u32))> {
        let (item, rows) = safe(top, path, budget, trusted, false, true)?;
        // 旧 open_dir_path(os.path.dirname(path))。Path::parent と違い "." の部分を保つ。
        let st = state::directory(&state::dirname(path))?.snapshot()?;
        Ok((item, rows, (st.dev, st.ino, st.mode & 0o7777)))
    }
    fn include(
        &mut self,
        top: &Path,
        value: Option<&Text>,
        origin: &Path,
        budget: &mut Budget,
    ) -> Result<PathBuf> {
        include(top, value, origin, budget)
    }
    fn condition(
        &mut self,
        top: &Path,
        condition: &Text,
        origin: &Path,
        effective: &Rows,
        budget: &mut Budget,
    ) -> Result<bool> {
        probe(top, condition, origin, effective, budget, None, &[])
    }
    fn effective(&mut self, top: &Path, budget: &mut Budget) -> Result<Vec<u8>> {
        process::git(
            top.as_os_str(),
            &process::strings(&[
                "config",
                "--show-origin",
                "--show-scope",
                "--includes",
                "--null",
                "--list",
            ]),
            budget,
        )
    }
    fn candidates(
        &mut self,
        top: &Path,
        common: &Path,
        git_dir: &Path,
        budget: &mut Budget,
    ) -> Result<V> {
        candidates(top, common, git_dir, budget)
    }
}
type ParentIdentity = (i128, i128);
type FileIdentity = (i128, i128, u32);
type PreparedFile = (V, Rows, ParentIdentity, FileIdentity);
struct Graph<'a> {
    operations: &'a mut dyn Operations,
    observer: &'a mut dyn Observer,
    top: &'a Path,
    budget: &'a mut Budget,
    trusted: Option<&'a Trusted>,
    prepared: HashMap<OsString, PreparedFile>,
    registered: HashSet<OsString>,
    done: HashSet<OsString>,
    active: HashSet<(ParentIdentity, FileIdentity)>,
    // 旧 pending_seen は (str, str, str | None) の集合。Path の == を使わずバイトで比べる。
    pending: Vec<(OsString, Text, Option<Text>)>,
    observed: Vec<V>,
    effective: Rows,
    edges: HashMap<OsString, HashSet<OsString>>,
}
impl Graph<'_> {
    fn register(&mut self, path: &Path) -> Result<PreparedFile> {
        if !self.prepared.contains_key(path.as_os_str()) {
            self.observer
                .event(ConfigEvent::ReadStart(path), self.budget, None)?;
            let (item, rows, parent_identity) =
                self.operations
                    .file(self.top, path, self.budget, self.trusted)?;
            self.observer
                .event(ConfigEvent::ReadDone(path), self.budget, None)?;
            let meta = item.get("meta");
            let integer = |key: &str| {
                if let V::Integer(v) = meta.get(key) {
                    v.to_string().parse().unwrap()
                } else {
                    0
                }
            };
            let identity = (integer("dev"), integer("ino"));
            self.prepared.insert(
                path.as_os_str().to_owned(),
                (item, rows, identity, parent_identity),
            );
        }
        let (item, rows, identity, parent) = self.prepared[path.as_os_str()].clone();
        if self.registered.insert(path.as_os_str().to_owned()) {
            self.observed.push(item.clone());
            self.effective.extend(
                rows.iter()
                    .filter(|(k, _)| key(k) != "include.path" && !key(k).starts_with("includeif."))
                    .cloned(),
            );
        }
        Ok((item, rows, identity, parent))
    }
    fn follow(&mut self, parent: &Path, value: Option<&Text>) -> Result<()> {
        self.observer
            .event(ConfigEvent::IncludePath(parent), self.budget, None)?;
        let target = self
            .operations
            .include(self.top, value, parent, self.budget)
            .map_err(|e| {
                e.at(
                    "follow",
                    1270,
                    "target = include_target(top, value, origin, budget)",
                )
            })?;
        self.register(&target)
            .map_err(|e| e.at("follow", 1271, "register(target)"))?;
        self.edges
            .entry(parent.as_os_str().to_owned())
            .or_default()
            .insert(target.as_os_str().to_owned());
        self.visit(&target)
            .map_err(|e| e.at("follow", 1274, "non_hasconfig(target)"))
    }
    fn visit(&mut self, path: &Path) -> Result<()> {
        let (_, rows, identity, parent) = self.register(path)?;
        if self.done.contains(path.as_os_str()) {
            return Ok(());
        }
        if !self.active.insert((identity, parent)) {
            return Err(stop("include が循環している"));
        }
        for (k, v) in rows {
            let folded = key(&k);
            if folded == "include.path" {
                self.follow(path, v.as_ref())
                    .map_err(|e| e.at("non_hasconfig", 1288, "follow(path, path, value)"))?;
            } else if folded.starts_with("includeif.") && folded.ends_with(".path") {
                let condition = includeif_condition(&k);
                if key(&condition).starts_with("hasconfig:") {
                    let marker = (path.as_os_str().to_owned(), condition, v);
                    if !self.pending.contains(&marker) {
                        self.pending.push(marker)
                    }
                } else {
                    self.observer
                        .event(ConfigEvent::ConditionStart(path), self.budget, None)?;
                    if self
                        .operations
                        .condition(self.top, &condition, path, &self.effective, self.budget)
                        .map_err(|e| {
                            e.at(
                                "non_hasconfig",
                                1295,
                                "elif probe_condition(top, condition, path, effective, budget):",
                            )
                        })?
                    {
                        self.follow(path, v.as_ref()).map_err(|e| {
                            e.at("non_hasconfig", 1296, "follow(path, path, value)")
                        })?;
                    }
                }
            }
        }
        self.active.remove(&(identity, parent));
        self.done.insert(path.as_os_str().to_owned());
        Ok(())
    }
    fn cycle(
        &self,
        path: &Path,
        visiting: &mut HashSet<OsString>,
        visited: &mut HashSet<OsString>,
    ) -> Result<()> {
        if visiting.contains(path.as_os_str()) {
            return Err(stop("include が循環している"));
        }
        if visited.contains(path.as_os_str()) {
            return Ok(());
        }
        visiting.insert(path.as_os_str().to_owned());
        if let Some(children) = self.edges.get(path.as_os_str()) {
            for child in children {
                self.cycle(Path::new(child), visiting, visited)?
            }
        }
        visiting.remove(path.as_os_str());
        visited.insert(path.as_os_str().to_owned());
        Ok(())
    }
}
pub fn snapshot(
    top: &Path,
    common: &Path,
    git_dir: &Path,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
) -> Result<V> {
    snapshot_observed(top, common, git_dir, budget, trusted, &mut Quiet)
}
pub fn snapshot_observed(
    top: &Path,
    common: &Path,
    git_dir: &Path,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
    observer: &mut dyn Observer,
) -> Result<V> {
    snapshot_with_operations(
        top,
        common,
        git_dir,
        budget,
        trusted,
        observer,
        &mut RealOperations,
    )
}
pub(super) fn snapshot_with_operations(
    top: &Path,
    common: &Path,
    git_dir: &Path,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
    observer: &mut dyn Observer,
    operations: &mut dyn Operations,
) -> Result<V> {
    observer.event(ConfigEvent::RootsStart, budget, None)?;
    let base = operations.roots(top, budget)?;
    observer.event(ConfigEvent::RootsDone, budget, Some(&base))?;
    let mut roots = origins(&base, top);
    let common_config = common.join("config");
    if !roots
        .iter()
        .any(|p| p.as_os_str() == common_config.as_os_str())
    {
        roots.push(common_config)
    }
    let mut graph = Graph {
        operations,
        observer,
        top,
        budget,
        trusted,
        prepared: HashMap::new(),
        registered: HashSet::new(),
        done: HashSet::new(),
        active: HashSet::new(),
        pending: Vec::new(),
        observed: Vec::new(),
        effective: Vec::new(),
        edges: HashMap::new(),
    };
    for root in &roots {
        graph
            .register(root)
            .map_err(|e| e.at("config_snapshot", 1308, "register(root)"))?;
    }
    for root in &roots {
        graph
            .visit(root)
            .map_err(|e| e.at("config_snapshot", 1310, "non_hasconfig(root)"))?;
    }
    let mut cursor = 0;
    while cursor < graph.pending.len() {
        let (origin, condition, value) = graph.pending[cursor].clone();
        let origin = PathBuf::from(origin);
        cursor += 1;
        graph
            .observer
            .event(ConfigEvent::ConditionStart(&origin), graph.budget, None)?;
        if graph
            .operations
            .condition(top, &condition, &origin, &graph.effective, graph.budget)
            .map_err(|e| {
                e.at(
                    "config_snapshot",
                    1314,
                    "if probe_condition(top, condition, origin, effective, budget):",
                )
            })?
        {
            graph
                .follow(&origin, value.as_ref())
                .map_err(|e| e.at("config_snapshot", 1315, "follow(origin, origin, value)"))?;
        }
    }
    for node in graph.edges.keys() {
        graph.cycle(Path::new(node), &mut HashSet::new(), &mut HashSet::new())?;
    }
    graph
        .observer
        .event(ConfigEvent::EffectiveStart, graph.budget, None)?;
    let raw = graph.operations.effective(top, graph.budget)?;
    graph
        .observer
        .event(ConfigEvent::EffectiveDone, graph.budget, Some(&raw))?;
    let actual = origins(&raw, top);
    if actual
        .iter()
        .any(|p| !graph.registered.contains(p.as_os_str()))
    {
        return Err(stop("include の到達先を事前に安全に検査できない"));
    }
    for path in actual {
        graph
            .observer
            .event(ConfigEvent::ReadStart(&path), graph.budget, None)?;
        let (after, _, _) = graph.operations.file(top, &path, graph.budget, trusted)?;
        graph
            .observer
            .event(ConfigEvent::ReadDone(&path), graph.budget, None)?;
        if after != graph.prepared[path.as_os_str()].0 {
            return Err(stop("config が実効値の読取中に変わった"));
        }
    }
    graph.observed.sort_by(|a, b| {
        a.get("path")
            .string()
            .unwrap()
            .as_codepoints()
            .cmp(b.get("path").string().unwrap().as_codepoints())
    });
    let observed = std::mem::take(&mut graph.observed);
    graph
        .observer
        .event(ConfigEvent::CandidatesStart, graph.budget, None)?;
    let candidates = graph
        .operations
        .candidates(top, common, git_dir, graph.budget)?;
    graph
        .observer
        .event(ConfigEvent::CandidatesDone, graph.budget, None)?;
    Ok(V::object([
        ("effective_sha256", V::from(state::hash(&raw))),
        ("origins", V::List(observed)),
        ("candidates", candidates),
    ]))
}
pub fn file_snapshot(
    top: &Path,
    path: &Path,
    rel: &str,
    budget: &mut Budget,
    trusted: Option<&Trusted>,
) -> Result<V> {
    if path.symlink_metadata().is_err() {
        return Ok(V::object([("state", V::from("absent"))]));
    }
    let (mut observed, rows) = safe(top, path, budget, trusted, false, true)?;
    observed.set("path", V::from(rel));
    let rows = rows
        .into_iter()
        .map(|(k, v)| V::List(vec![V::from(k), v.map(V::from).unwrap_or(V::Null)]))
        .collect();
    Ok(V::object([
        ("state", V::from("ok")),
        ("entry", observed),
        ("entries", V::List(rows)),
    ]))
}
