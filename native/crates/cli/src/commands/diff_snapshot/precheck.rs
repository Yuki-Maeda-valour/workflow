use super::*;
use bytes::{display_value, lower, path_field, sanitize, zrecords};
use git::at_path;
pub fn run(s: &mut Snapshot) -> Result<()> {
    // 720: >"$TMPD/cfg.z" 2>"$TMPD/cfg.err"
    let cfg = s.git.at(720).to_file(
        &words(&["config", "--show-scope", "--includes", "--list", "-z"]),
        &s.temp.0,
        "cfg.z",
        git::Errors::File("cfg.err"),
        None,
    )?;
    if cfg.code >= 2 {
        return Err(Failure::raw(
            20,
            joined(&[
                &cfg.stderr,
                format!(
                    "ERROR [internal] ローカル設定を読めない(終了コード {})\n",
                    cfg.code
                )
                .as_bytes(),
            ]),
        ));
    }
    // `while IFS= read … cf_scope && IFS= read … cf_kv; do … done <cfg.z` (722).
    process::mark();
    let mut disabled: [Vec<Vec<u8>>; 3] = std::array::from_fn(|_| vec![]);
    let mut promisor = Vec::new();
    let cfg = zrecords(&cfg.stdout);
    for pair in cfg.as_chunks::<2>().0 {
        if pair[0] != b"local" && pair[0] != b"worktree" {
            continue;
        }
        let (key, value) = match pair[1].iter().position(|b| *b == b'\n') {
            Some(n) => (&pair[1][..n], &pair[1][n + 1..]),
            None => (pair[1].as_slice(), b"".as_slice()),
        };
        let low = lower(key);
        let digest = sha(value);
        if low == b"include.path" || low.starts_with(b"includeif.") {
            s.row(
                key,
                &joined(&[b"include: ", key]),
                &digest,
                &display_value(value),
                true,
            );
            continue;
        }
        if low == b"core.fsmonitor" || low == b"core.hookspath" {
            disabled[0].push(low);
            continue;
        }
        if [b"core.filemode".as_slice(), b"core.symlinks"].contains(&low.as_slice()) {
            if value == b"false" {
                disabled[1].push(low);
            }
            continue;
        }
        if [b"core.ignorecase".as_slice(), b"core.ignorestat"].contains(&low.as_slice()) {
            if value == b"true" {
                disabled[1].push(low);
            }
            continue;
        }
        if low == b"core.splitindex" {
            continue;
        }
        if low == b"core.pager" || low.starts_with(b"pager.") {
            disabled[2].push(low);
            continue;
        }
        if low == b"extensions.partialclone" || glob_middle(&low, b"remote.", b".promisor") {
            promisor.push((key.to_vec(), digest, display_value(value)));
            continue;
        }
        if [
            (
                b"filter.lfs.clean".as_slice(),
                b"git-lfs clean -- %f".as_slice(),
            ),
            (b"filter.lfs.smudge", b"git-lfs smudge -- %f"),
            (b"filter.lfs.process", b"git-lfs filter-process"),
            (b"filter.lfs.required", b"true"),
        ]
        .iter()
        .any(|(k, v)| low == *k && value == *v)
        {
            continue;
        }
        if [
            b"core.excludesfile".as_slice(),
            b"core.attributesfile",
            b"core.bigfilethreshold",
            b"diff.external",
        ]
        .contains(&low.as_slice())
            || low.starts_with(b"filter.")
            || glob_middle(&low, b"diff.", b".textconv")
            || glob_middle(&low, b"diff.", b".command")
        {
            s.row(
                key,
                &joined(&["ローカル設定: ".as_bytes(), key]),
                &digest,
                &display_value(value),
                true,
            );
        }
    }
    for (i, group) in disabled.iter().enumerate() {
        if !group.is_empty() {
            if i == 2 {
                // add_note "…$(sanitize "${DIS3[*]}")" (758).
                process::fork(758);
            }
            let names = group.join(&b' ');
            s.note(joined(&[
                "無効化して実行: ".as_bytes(),
                &if i == 2 { sanitize(&names) } else { names },
            ]));
        }
    }
    if !promisor.is_empty() {
        // pnames="$pnames $(sanitize …)" (767), then git_ge_245's
        // v="$(git … version …)" (625).
        process::fork(767);
        let version = s.git.at(625).words(&["version"])?;
        let v = trim_lf(version.stdout);
        let v = v.strip_prefix(b"git version ").unwrap_or(&v);
        let parts = v.split(|b| *b == b'.').collect::<Vec<_>>();
        let n = |b: Option<&&[u8]>| {
            b.and_then(|b| std::str::from_utf8(b).ok())
                .and_then(|s| s.parse::<u64>().ok())
        };
        let major = n(parts.first());
        let minor = n(parts.get(1));
        let supported =
            major.is_some_and(|n| n > 2) || (major == Some(2) && minor.is_some_and(|n| n >= 45));
        if supported {
            let names = promisor
                .iter()
                .map(|(k, _, _)| sanitize(k))
                .collect::<Vec<_>>()
                .join(&b' ');
            s.note(joined(&[
                "promisor 構成: 遅延取得を無効化して実行(".as_bytes(),
                &names,
                b")",
            ]));
        } else {
            let mut err = vec![];
            for (key, hash, value) in promisor {
                err.extend(joined(&[
                    &sanitize(&key),
                    b"\t",
                    &sanitize(&joined(&["ローカル設定: ".as_bytes(), &key])),
                    b"\t",
                    &hash,
                    b"\t",
                    &value,
                    b"\n",
                ]));
            }
            err.extend(
                "WARNING: promisor 構成で遅延取得を止められない(git 2.45 以上が要る)\n".as_bytes(),
            );
            return Err(Failure::raw(22, err));
        }
    }
    // 784: >"$TMPD/replace.txt" 2>/dev/null
    let replace = s.git.at(784).to_file(
        &words(&["for-each-ref", "refs/replace/"]),
        &s.temp.0,
        "replace.txt",
        git::Errors::Null,
        None,
    )?;
    if replace.code == 0 && !replace.stdout.is_empty() {
        s.note("置換参照を無効化して実行");
    }
    if !s.args.precheck {
        for (base, name) in [
            (&s.args.base, "--base"),
            (&s.args.patch_base, "--patch-base"),
        ] {
            if name == "--patch-base" && s.patch.is_none() {
                continue;
            }
            let o = s
                .git
                .at(if name == "--base" { 790 } else { 794 })
                .foreground(
                    &[
                        words(&["rev-parse", "--verify", "--quiet"]),
                        vec![joined(&[base, b"^{tree}"])],
                    ]
                    .concat(),
                )?;
            if o.code != 0 {
                return Err(Failure::raw(
                    4,
                    joined(&[
                        b"ERROR [no-base] ",
                        name.as_bytes(),
                        " を解決できない: ".as_bytes(),
                        base,
                        b"\n",
                    ]),
                ));
            }
        }
    }
    let info = s.git.at(801).words(&[
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "info/attributes",
    ])?;
    let info = PathBuf::from(os(&trim_lf(info.stdout)));
    if !info.as_os_str().is_empty() && sfs::exists_or_link(&info) {
        if !info.is_file() || sfs::symlink(&info) {
            s.row(
                b".git/info/attributes",
                ".git/info/attributes が通常ファイルでない".as_bytes(),
                b"-",
                b"",
                false,
            );
            s.stop = true;
        } else {
            // ia_sha="$(sha256_file "$INFO_ATTR")" (807).
            process::fork(807);
            let data = sfs::read(&info, None, "info-attributes", &mut NoAudit)
                .map_err(|failure| super::sha_failure(raw(info.as_os_str()), failure, 807))?;
            // `while IFS= read -r ia_line || …; done <"$INFO_ATTR"` (809).
            process::mark();
            let hit = data.split(|b| *b == b'\n').any(attribute_line);
            if hit {
                s.row(
                    b".git/info/attributes",
                    b".git/info/attributes",
                    &sha(&data),
                    b"",
                    true,
                );
            }
        }
    }
    if s.stop {
        return Ok(());
    }
    // 853・856・859: >"$TMPD/NAME.z" 2>"$TMPD/NAME.err"
    let temp = s.temp.0.clone();
    s.tracked = zrecords(&s.git.at(853).checked_to(
        &words(&["ls-files", "-z"]),
        &temp,
        "tracked.z",
        "tracked.err",
    )?);
    s.untracked = zrecords(&s.git.at(856).checked_to(
        &words(&["ls-files", "-o", "--exclude-standard", "-z"]),
        &temp,
        "untracked.z",
        "untracked.err",
    )?);
    s.stage = zrecords(&s.git.at(859).checked_to(
        &words(&["ls-files", "-s", "-z"]),
        &temp,
        "stage.z",
        "stage.err",
    )?);
    gitlinks(s)?;
    // `while IFS= read … gl_rec; do … done <stage.z` (866).
    process::mark();
    let join = |v: &[Vec<u8>]| {
        v.iter()
            .flat_map(|p| p.iter().copied().chain([0]))
            .collect::<Vec<u8>>()
    };
    // 897: cat "$TMPD/tracked.z" "$TMPD/untracked.z" >"$TMPD/attrin.z"
    // 900: <attrin.z >attr.z 2>attr.err
    let attrin = s.temp.0.join("attrin.z");
    process::fork(897);
    git::cat_into(
        &attrin,
        &[
            (&s.temp.0.join("tracked.z"), &join(&s.tracked)),
            (&s.temp.0.join("untracked.z"), &join(&s.untracked)),
        ],
        897,
    )?;
    let attrs = zrecords(&git::check(s.git.at(900).to_file(
        &words(&[
            "check-attr",
            "--stdin",
            "-z",
            "filter",
            "working-tree-encoding",
            "ident",
        ]),
        &s.temp.0,
        "attr.z",
        git::Errors::File("attr.err"),
        Some(&attrin),
    )?)?);
    // `while IFS= read … at_path && … at_name && … at_val; do … done <attr.z` (904).
    process::mark();
    let mut pending = vec![];
    for row in attrs.as_chunks::<3>().0 {
        let p = &row[0];
        let name = &row[1];
        let value = &row[2];
        match name.as_slice() {
            b"filter" => {
                s.filters.insert(p.clone(), value.clone());
                match value.as_slice() {
                    b"unspecified" | b"unset" => pending.push((p.clone(), value.clone())),
                    b"lfs" => (),
                    _ => s.row(
                        p,
                        &joined(&["filter 属性: ".as_bytes(), value]),
                        b"-",
                        b"",
                        true,
                    ),
                }
            }
            b"working-tree-encoding" | b"ident" if value != b"unspecified" && value != b"unset" => {
                s.row(
                    p,
                    &joined(&[name, " 属性: ".as_bytes(), value]),
                    b"-",
                    b"",
                    false,
                );
            }
            _ => (),
        }
    }
    if !pending.is_empty() {
        // 929: >"$TMPD/unsetf.txt" 2>/dev/null
        let o = s.git.at(929).to_file(
            &words(&["config", "--get-regexp", r"^filter\.(unset|unspecified)\."]),
            &s.temp.0,
            "unsetf.txt",
            git::Errors::Null,
            None,
        )?;
        if o.code == 0 {
            // `while IFS= read -r uf_line; do … done <unsetf.txt` (932).
            process::mark();
            let defined = o
                .stdout
                .split(|b| *b == b'\n')
                .filter_map(|line| {
                    if line.starts_with(b"filter.unset.") {
                        Some(b"unset".to_vec())
                    } else if line.starts_with(b"filter.unspecified.") {
                        Some(b"unspecified".to_vec())
                    } else {
                        None
                    }
                })
                .collect::<HashSet<_>>();
            for (p, v) in pending {
                if defined.contains(&v) {
                    s.row(
                        &p,
                        &joined(&["filter 属性: ".as_bytes(), &v]),
                        b"-",
                        b"",
                        true,
                    );
                }
            }
        }
    }
    flags(s)
}
/// Bash `case` pattern `PREFIX*SUFFIX`: the two fixed parts cannot overlap.
fn glob_middle(value: &[u8], prefix: &[u8], suffix: &[u8]) -> bool {
    value.len() >= prefix.len() + suffix.len()
        && value.starts_with(prefix)
        && value.ends_with(suffix)
}
/// The C locale `[[:space:]]` class used by the old pattern matching.
fn shell_space(b: &u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}
fn attribute_line(line: &[u8]) -> bool {
    // `read -r` drops NUL bytes before the line is examined.
    let line = line.iter().copied().filter(|b| *b != 0).collect::<Vec<_>>();
    let line = line.as_slice();
    if line.is_empty() || line.starts_with(b"#") {
        return false;
    }
    let rest = if let Some(mut rest) = line.strip_prefix(b"\"") {
        while !rest.is_empty() {
            if rest.starts_with(b"\\\\") || rest.starts_with(b"\\\"") {
                rest = &rest[2..];
            } else if rest[0] == b'"' {
                rest = &rest[1..];
                break;
            } else {
                rest = &rest[1..];
            }
        }
        rest
    } else {
        line.iter()
            .position(shell_space)
            .map_or(b"".as_slice(), |i| &line[i + 1..])
    };
    rest.split(|b| matches!(b, b' ' | b'\t' | b'\n'))
        .any(|token| {
            token.starts_with(b"filter=")
                || token.starts_with(b"diff=")
                || token == b"-diff"
                || token == b"binary"
                || token.starts_with(b"working-tree-encoding=")
        })
}
fn gitlinks(s: &mut Snapshot) -> Result<()> {
    for rec in s.stage.clone() {
        if !rec.starts_with(b"160000 ") {
            continue;
        }
        let Some(i) = rec.iter().position(|b| *b == b'\t') else {
            continue;
        };
        let path = &rec[i + 1..];
        let absolute = at_path(&s.root, path);
        if !absolute.is_dir() {
            let mut probe = absolute.clone();
            while !sfs::exists_or_link(&probe) && probe != s.root {
                if !probe.pop() {
                    break;
                }
            }
            if (probe.is_dir() && !sfs::searchable(&probe))
                || (sfs::symlink(&probe) && !probe.exists())
                || (probe == s.root && !probe.exists())
            {
                s.row(
                    path,
                    "gitlink のパスを解決できない".as_bytes(),
                    b"-",
                    b"",
                    false,
                );
            }
            continue;
        }
        if sfs::exists_or_link(&absolute.join(".git")) {
            continue;
        }
        // `find "./P/." -mindepth 1 -maxdepth 1 -print -quit`: an unreadable
        // directory fails; any entry makes it non-empty.
        let listed = std::fs::read_dir(at_path(&s.root, path).join("."))
            .map(|mut entries| entries.next().map(|e| e.is_ok()));
        if matches!(listed, Err(_) | Ok(Some(false))) {
            s.row(
                path,
                "gitlink のディレクトリを列挙できない".as_bytes(),
                b"-",
                b"",
                false,
            );
        } else if matches!(listed, Ok(Some(true))) {
            s.row(
                path,
                "gitlink に .git 名エントリが無い非空ディレクトリ".as_bytes(),
                b"-",
                b"",
                false,
            );
        }
    }
    Ok(())
}
fn flags(s: &mut Snapshot) -> Result<()> {
    // 950: >"$TMPD/flags.z" 2>"$TMPD/flags.err"
    let flags = zrecords(&s.git.at(950).checked_to(
        &words(&["ls-files", "-v", "-z"]),
        &s.temp.0,
        "flags.z",
        "flags.err",
    )?);
    let sp = s
        .git
        .at(954)
        .words(&["config", "--get", "core.sparsecheckout"])?;
    let enabled = if sp.code == 0 && trim_lf(sp.stdout) == b"true" {
        let p = s.git.at(956).words(&[
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/sparse-checkout",
        ])?;
        Path::new(&os(&trim_lf(p.stdout))).exists()
    } else {
        false
    };
    // `while IFS= read … fl_rec; do … done <flags.z` (960).
    process::mark();
    let mut entries = vec![];
    let mut candidates = vec![];
    for rec in flags {
        if rec.len() < 2 {
            continue;
        }
        let ch = rec[0];
        let p = rec[2..].to_vec();
        let reason = match ch {
            b's' => "index フラグ: assume-unchanged,skip-worktree",
            b'S' => "index フラグ: skip-worktree",
            b'a'..=b'z' => "index フラグ: assume-unchanged",
            _ => continue,
        };
        if enabled && ch == b'S' && !sfs::exists_or_link(&at_path(&s.root, &p)) {
            candidates.push(p.clone());
        }
        entries.push((p, reason));
    }
    let mut exceptions = HashSet::new();
    if !candidates.is_empty() {
        let input = candidates
            .iter()
            .flat_map(|p| p.iter().copied().chain([0]))
            .collect::<Vec<_>>();
        // 977: : >"$TMPD/cand.z"; 978: printf … >>"$TMPD/cand.z"; 980: <cand.z >rules.z 2>/dev/null
        let cand = s.temp.0.join("cand.z");
        sfs::write_input(&cand, &input, "cand.z")
            .map_err(|e| git::Writer::Printf(978).failure(&e))?;
        let o = s.git.at(980).to_file(
            &words(&["sparse-checkout", "check-rules", "-z"]),
            &s.temp.0,
            "rules.z",
            git::Errors::Null,
            Some(&cand),
        )?;
        if o.code == 0 {
            // `while IFS= read … p; do IN_RULES["$p"]=1; done <rules.z` (983).
            process::mark();
            let inside = zrecords(&o.stdout).into_iter().collect::<HashSet<_>>();
            for p in candidates {
                if !inside.contains(&p) {
                    exceptions.insert(p);
                }
            }
        }
    }
    for (p, reason) in entries {
        if exceptions.contains(&p) {
            s.sparse.push(p);
        } else {
            s.row(&p, reason.as_bytes(), b"-", b"", true);
        }
    }
    if !s.sparse.is_empty() {
        // sp_list="$sp_list $(path_field …)" (998).
        process::fork(998);
        let names = s
            .sparse
            .iter()
            .take(20)
            .map(|p| path_field(p))
            .collect::<Vec<_>>()
            .join(&b' ');
        s.note(joined(&[
            format!(
                "sparse-checkout で作業ツリーに無い skip-worktree: {} 件(",
                s.sparse.len()
            )
            .as_bytes(),
            &names,
            b")",
        ]));
    }
    Ok(())
}
