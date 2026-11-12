use super::*;
use bytes::{section_line, zrecords};
use git::{at_path, lfs_off, working};
fn paths(included: &[Vec<u8>], excluded: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let mut args = included
        .iter()
        .map(|p| joined(&[b":(literal)", p]))
        .collect::<Vec<_>>();
    for e in excluded {
        if included.iter().any(|i| e.starts_with(&joined(&[i, b"/"]))) {
            args.push(joined(&[b":(literal,exclude)", e]));
        }
    }
    args
}
fn name_args(base: &[u8]) -> Vec<Vec<u8>> {
    [
        working(),
        words(&[
            "diff",
            "--name-only",
            "--no-renames",
            "--ignore-submodules=dirty",
            "-z",
        ]),
        vec![base.to_vec()],
    ]
    .concat()
}
fn diff_args(base: &[u8], ps: &[Vec<u8>], stat: bool, binary: bool) -> Vec<Vec<u8>> {
    let mut args = working();
    if !stat {
        args.extend(lfs_off());
    }
    args.push(b"diff".to_vec());
    if stat {
        args.extend(words(&[
            "--stat",
            "--stat-width=200",
            "--stat-name-width=500",
        ]));
    } else {
        if binary {
            args.push(b"--binary".to_vec());
        }
        args.push(b"--unified=3".to_vec());
    }
    args.extend(words(&[
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--submodule=short",
        "--ignore-submodules=dirty",
        "--text",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ]));
    args.extend([base.to_vec(), b"--".to_vec()]);
    args.extend(ps.to_vec());
    args
}
pub fn run(s: &mut Snapshot) -> Result<()> {
    let index = s.temp.0.join("index.tmp");
    let mut producer = s.git.command(&words(&["ls-files", "-s", "-z"]));
    let mut consumer = s.git.command(&words(&[
        "-c",
        "core.ignoreStat=false",
        "update-index",
        "-z",
        "--index-info",
    ]));
    consumer.env("GIT_INDEX_FILE", &index);
    // 1245: the shell forks both elements there; the second has its own
    // assignment (GIT_INDEX_FILE).
    process::fork(1245);
    consumer.envs(process::own_line(1245));
    process::pipeline_element(&mut producer);
    process::pipeline_element(&mut consumer);
    let (rc, signal) = process::pipeline(&mut producer, &mut consumer)
        .map_err(|_| Failure::unexpected(127, 1245))?;
    super::job_status(signal);
    s.checkpoint()?;
    if rc != 0 {
        return Err(Failure::unexpected(rc, 1245));
    }
    s.checkpoint()?;
    let mut tmpgit = s.git.clone();
    tmpgit.index = Some(index.clone());
    if !s.sparse.is_empty() {
        let input = s
            .sparse
            .iter()
            .flat_map(|p| p.iter().copied().chain([0]))
            .collect::<Vec<_>>();
        let out = tmpgit.with_line(1249).run_temp_input(
            &words(&[
                "-c",
                "core.ignoreStat=false",
                "update-index",
                "--skip-worktree",
                "-z",
                "--stdin",
            ]),
            &s.temp.0,
            "sparse.z",
            &input,
            git::Writer::Printf(1248),
        )?;
        if out.code != 0 {
            return Err(Failure::after(
                &out.stderr,
                Failure::unexpected(out.code, 1249),
            ));
        }
        if !out.stderr.is_empty() {
            output::stderr(&out.stderr)?;
        }
        // The assignment is disposed of after the command: Bash rebuilds with
        // the last line of the enclosing `if … fi` (1250).
        process::remake(1250);
    }
    // 1254: >"$TMPD/idxflags.z" (standard error not redirected)
    let flags = tmpgit.with_line(1254).to_file(
        &words(&["ls-files", "-z", "-v"]),
        &s.temp.0,
        "idxflags.z",
        git::Errors::Shown,
        None,
    )?;
    process::remake(1254);
    if flags.code != 0 {
        return Err(Failure::after(
            &flags.stderr,
            Failure::unexpected(flags.code, 1254),
        ));
    }
    if !flags.stderr.is_empty() {
        output::stderr(&flags.stderr)?;
    }
    for row in zrecords(&flags.stdout) {
        if row.first().is_some_and(u8::is_ascii_lowercase) {
            return Err(Failure::internal(
                "使い捨ての index に assume-unchanged が残っている(core.ignoreStat の可能性)",
            ));
        }
    }
    // `while IFS= read … idx_rec; done <idxflags.z` (1255), then
    // `export GIT_INDEX_FILE="$TMPIDX"` (1260): both mark.
    process::mark();
    process::export("GIT_INDEX_FILE", Some(index.clone().into_os_string()));
    s.git.index = Some(index);
    let temp = s.temp.0.clone();
    // 1297: >"$TMPD/tdiff.z" 2>"$TMPD/tdiff.err"
    let changed = zrecords(&s.git.at(1297).checked_to(
        &name_args(&s.args.base),
        &temp,
        "tdiff.z",
        "tdiff.err",
    )?);
    let mut included = vec![];
    let mut excluded = vec![];
    // `while IFS= read … p; do classify …; done <tdiff.z` (1300): each read marks.
    for p in changed {
        process::mark();
        match s.classify(&p, false)? {
            Class::Default(reason) => {
                section_line(
                    &mut s.sections[4],
                    &p,
                    &["(追跡)".as_bytes(), reason.as_bytes()],
                );
                excluded.push(p);
            }
            Class::Secret => {
                section_line(&mut s.sections[3], &p, &["(追跡)".as_bytes()]);
                excluded.push(p);
            }
            _ => included.push(p),
        }
    }
    process::mark();
    s.included = included.clone();
    let mut omitted = vec![];
    for p in &included {
        let ltree = s.git.at(1321).run_piped(
            &[
                words(&["ls-tree", "-z"]),
                vec![
                    s.args.base.clone(),
                    b"--".to_vec(),
                    joined(&[b":(literal)", p]),
                ],
            ]
            .concat(),
        )?;
        let base_mode = if ltree.code == 0 {
            ltree.stdout.split(|b| *b == b' ').next().unwrap_or(b"")
        } else {
            b""
        };
        let stage = s.git.at(1324).run_piped(
            &[
                words(&["ls-files", "-s", "-z", "--"]),
                vec![joined(&[b":(literal)", p])],
            ]
            .concat(),
        )?;
        let index_mode = if stage.code == 0 {
            stage.stdout.split(|b| *b == b' ').next().unwrap_or(b"")
        } else {
            b""
        };
        let absolute = at_path(&s.root, p);
        let current = if sfs::symlink(&absolute) {
            "symlink"
        } else if !absolute.exists() {
            "absent"
        } else if absolute.is_dir() && index_mode == b"160000" {
            "gitlink"
        } else if !absolute.is_file() {
            "other"
        } else {
            "file"
        };
        let base_regular = base_mode == b"100644" || base_mode == b"100755";
        let mut binary = false;
        let mut big = false;
        if base_regular {
            let object = joined(&[&s.args.base, b":", p]);
            // `git cat-file blob … 2>/dev/null | head -c 8000` without pipefail.
            signal_checkpoint()?;
            process::fork(1339);
            let mut blob = s
                .git
                .command(&[words(&["cat-file", "blob"]), vec![object.clone()]].concat());
            process::pipeline_element(&mut blob);
            let (head, signal) = process::capture_head(&mut blob, 8000).unwrap_or_default();
            super::job_status(signal);
            signal_checkpoint()?;
            binary |= head.contains(&0);
            let size = s
                .git
                .at(1342)
                .run(&[words(&["cat-file", "-s"]), vec![object]].concat(), b"")?;
            big |= size.code == 0
                && std::str::from_utf8(&trim_lf(size.stdout))
                    .ok()
                    .and_then(|v| v.parse::<u64>().ok())
                    .is_some_and(|n| n > 1048576);
        }
        if current == "file" && sfs::readable(&absolute) {
            binary |= super::head_has_nul(&absolute, p, "tracked-binary")?;
            let size = open_size(&absolute, p, "tracked-size", 1347)?;
            big |= size > 1048576;
        }
        if base_regular && (current == "symlink" || current == "other") {
            section_line(
                &mut s.sections[8],
                p,
                &["作業ツリーが通常ファイルでない".as_bytes()],
            );
        }
        if binary || big {
            omitted.push(p.clone());
            section_line(
                &mut s.sections[8],
                p,
                &[if binary {
                    "バイナリ".as_bytes()
                } else {
                    "1 MiB 超".as_bytes()
                }],
            );
        }
    }
    if !included.is_empty() {
        // 1373: >"$SEC_STAT" 2>"$TMPD/stat.err"
        s.sections[0] = s.git.at(1373).checked_to(
            &diff_args(&s.args.base, &paths(&included, &excluded), true, false),
            &temp,
            "s_stat",
            "stat.err",
        )?;
        let body = included
            .iter()
            .filter(|p| !omitted.contains(p))
            .cloned()
            .collect::<Vec<_>>();
        if body.is_empty() {
            s.body_empty = "内容は全て `## 内容を省略した追跡` へ".as_bytes().to_vec();
        } else {
            // 1389: >"$SEC_BODY" 2>"$TMPD/body.err"
            s.sections[1] = s.git.at(1389).checked_to(
                &diff_args(
                    &s.args.base,
                    &paths(&body, &[excluded, omitted].concat()),
                    false,
                    false,
                ),
                &temp,
                "s_body",
                "body.err",
            )?;
        }
    }
    Ok(())
}
pub fn patch(s: &mut Snapshot) -> Result<()> {
    if s.patch.is_none() {
        return Ok(());
    }
    let temp = s.temp.0.clone();
    // 1626: >"$TMPD/pdiff.z" 2>"$TMPD/pdiff.err"
    let changed = zrecords(&s.git.at(1626).checked_to(
        &name_args(&s.args.patch_base),
        &temp,
        "pdiff.z",
        "pdiff.err",
    )?);
    let mut included = vec![];
    let mut excluded = vec![];
    // `while IFS= read … p; do classify …; done <pdiff.z` (1629): each read marks.
    for p in changed {
        process::mark();
        if matches!(s.classify(&p, false)?, Class::Default(_) | Class::Secret) {
            excluded.push(p);
        } else {
            included.push(p);
        }
    }
    process::mark();
    s.patch_bytes = if included.is_empty() {
        vec![]
    } else {
        // 1644: >"$TMPD/patch.raw" 2>"$TMPD/patch.err"
        s.git.at(1644).checked_to(
            &diff_args(
                &s.args.patch_base,
                &paths(&included, &excluded),
                false,
                true,
            ),
            &temp,
            "patch.raw",
            "patch.err",
        )?
    };
    s.want_patch = true;
    Ok(())
}
