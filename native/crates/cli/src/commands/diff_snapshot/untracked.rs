use super::*;
use bytes::{lower, path_field, section_line};
use git::{at_path, lfs_off, working};
use std::os::unix::fs::MetadataExt;
fn notinc(s: &mut Snapshot, p: &[u8], reason: &str) {
    section_line(&mut s.sections[6], p, &[reason.as_bytes()]);
    s.counts[5] += 1;
}
fn omit(s: &mut Snapshot, p: &[u8], reason: &str) {
    section_line(&mut s.sections[7], p, &[reason.as_bytes()]);
    s.counts[4] += 1;
}
fn outside(s: &mut Snapshot, p: &[u8]) -> Result<()> {
    let absolute = at_path(&s.root, p);
    let (kind, hash, size) = if p.ends_with(b"/") {
        ("ネスト repo", b"-".to_vec(), b"-".to_vec())
    } else if sfs::symlink(&absolute) {
        // lnk="$(readlink -- "$f" 2>/dev/null; printf x)": a failure leaves it empty.
        let link = sfs::read_link(&absolute, "outside-readlink").unwrap_or_default();
        let bytes = raw(link.as_os_str());
        ("symlink", sha(bytes), bytes.len().to_string().into_bytes())
    } else if !absolute.exists() {
        ("消失", b"-".to_vec(), b"-".to_vec())
    } else if !absolute.is_file() {
        ("通常ファイルでない", b"-".to_vec(), b"-".to_vec())
    } else if !sfs::readable(&absolute) {
        ("読めない", b"-".to_vec(), b"-".to_vec())
    } else {
        let size = open_size(&absolute, p, "outside-size", 1416)?;
        if size > 1048576 {
            (
                "通常ファイル(1 MiB 超)",
                b"-".to_vec(),
                size.to_string().into_bytes(),
            )
        } else {
            (
                "通常ファイル",
                // sha="$(sha256_file "$f")" (1418).
                sfs::read_and_hash(&absolute, "outside-hash", &mut NoAudit, sha)
                    .map_err(|failure| sha_failure(p, failure, 1418))?,
                size.to_string().into_bytes(),
            )
        }
    };
    section_line(&mut s.sections[5], p, &[kind.as_bytes(), &hash, &size]);
    Ok(())
}
// `[ TOTAL -gt "$MAX_BYTES" ]`: Bash accepts any number of leading zeroes and
// rejects values above intmax_t with a diagnostic; the condition is then false.
fn above(value: u64, limit: &[u8]) -> bool {
    let digits = &limit[limit.iter().take_while(|b| **b == b'0').count()..];
    let parsed = if digits.len() > 19 {
        None
    } else {
        std::str::from_utf8(digits)
            .ok()
            .map(|t| {
                if t.is_empty() {
                    Ok(0)
                } else {
                    t.parse::<u64>()
                }
            })
            .and_then(|r| r.ok())
            .filter(|n| *n <= i64::MAX as u64)
    };
    match parsed {
        Some(n) => value > n,
        None => {
            let _ = output::stderr(&joined(&[
                b"diff-snapshot.sh: line 1477: [: ",
                limit,
                b": integer expression expected\n",
            ]));
            false
        }
    }
}
pub fn run(s: &mut Snapshot) -> Result<()> {
    // `LC_ALL=C tr … <tracked.z >tracked_lower.z` (1395): Bash rebuilds after
    // the command; then `while IFS= read` over tracked.z (1399) and
    // untracked.z (1427) mark.
    process::remake(1395);
    process::mark();
    let mut tracked_lower = HashMap::new();
    for p in &s.tracked {
        tracked_lower.entry(lower(p)).or_insert_with(|| p.clone());
    }
    let mut total = 0u64;
    let mut cap = false;
    let mut aliases = 0;
    // `while IFS= read … f <&3; do classify …; done` (1439): each read marks.
    for p in s.untracked.clone() {
        process::mark();
        match s.classify(&p, true)? {
            Class::Default(reason) => {
                section_line(
                    &mut s.sections[4],
                    &p,
                    &["(未追跡)".as_bytes(), reason.as_bytes()],
                );
                s.counts[2] += 1;
                continue;
            }
            Class::Secret => {
                section_line(&mut s.sections[3], &p, &["(未追跡)".as_bytes()]);
                s.counts[1] += 1;
                continue;
            }
            Class::Outside => {
                outside(s, &p)?;
                s.counts[3] += 1;
                continue;
            }
            Class::Include => (),
        }
        if p.ends_with(b"/") {
            notinc(s, &p, "ネスト repo");
            continue;
        }
        let absolute = at_path(&s.root, &p);
        let symlink = sfs::symlink(&absolute);
        if symlink {
            if !absolute.exists() {
                notinc(s, &p, "リンク切れ");
                continue;
            }
            if !absolute.is_file() {
                notinc(s, &p, "ディレクトリ等へのリンク");
                continue;
            }
        } else if !absolute.exists() {
            notinc(s, &p, "列挙後に消失");
            continue;
        } else if !absolute.is_file() {
            notinc(s, &p, "通常ファイルでない");
            continue;
        } else if !sfs::readable(&absolute) {
            notinc(s, &p, "読めない");
            s.failed += 1;
            continue;
        }
        let size = if symlink {
            // u_link="$(readlink -- "$f" 2>/dev/null; printf x)" (1462): empty on failure.
            sfs::read_link(&absolute, "untracked-readlink")
                .unwrap_or_default()
                .as_os_str()
                .len() as u64
        } else {
            if let Some(tp) = tracked_lower.get(&lower(&p)) {
                let first = std::fs::metadata(&absolute);
                let second = std::fs::metadata(at_path(&s.root, tp));
                if let (Ok(a), Ok(b)) = (first, second)
                    && a.dev() == b.dev()
                    && a.ino() == b.ino()
                {
                    notinc(s, &p, "追跡ファイルの別表記(同一 inode)");
                    aliases += 1;
                    continue;
                }
            }
            let size = open_size(&absolute, &p, "untracked-size", 1472)?;
            if size > 1048576 {
                omit(s, &p, "1 MiB 超");
                continue;
            }
            if super::head_has_nul(&absolute, &p, "untracked-binary")? {
                omit(s, &p, "バイナリ");
                continue;
            }
            size
        };
        if cap {
            omit(s, &p, "総量上限");
            continue;
        }
        if above(total.saturating_add(size), &s.args.max_bytes) {
            cap = true;
            omit(s, &p, "総量上限");
            continue;
        }
        if p == b"-" {
            notinc(s, &p, "diff 失敗");
            s.failed += 1;
            continue;
        }
        total = total.wrapping_add(size);
        let args = [
            working(),
            lfs_off(),
            words(&[
                "diff",
                "--unified=3",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--submodule=short",
                "--ignore-submodules=dirty",
                "--text",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--no-index",
                "--",
                "/dev/null",
            ]),
            vec![p.clone()],
        ]
        .concat();
        // 1490: >"$TMPD/ud" 2>"$TMPD/uderr"
        let result =
            s.git
                .at(1490)
                .to_file(&args, &s.temp.0, "ud", git::Errors::File("uderr"), None)?;
        if result.code == 0 || (result.code == 1 && !result.stdout.is_empty()) {
            s.sections[2].extend(result.stdout);
            s.counts[0] += 1;
        } else {
            notinc(s, &p, "diff 失敗");
            s.failed += 1;
        }
        s.checkpoint()?;
    }
    // The last read of 1439, and `while IFS= read … rec; done <stage.z` (1505).
    process::mark();
    if cap {
        s.note("未追跡の総量上限に達した");
    }
    if aliases > 0 {
        s.note(format!("追跡ファイルの別表記(同一 inode): {aliases} 件"));
    }
    if !s.sections[5].is_empty() {
        s.note("対象外の分類はリポジトリ内の一覧に基づく");
    }
    walk(s)
}
fn inside(path: &[u8], set: &[Vec<u8>]) -> bool {
    set.iter()
        .any(|k| path == k || path.starts_with(&joined(&[k, b"/"])))
}
fn walk(s: &mut Snapshot) -> Result<()> {
    let nested = s
        .untracked
        .iter()
        .filter_map(|p| p.strip_suffix(b"/").map(<[u8]>::to_vec))
        .collect::<Vec<_>>();
    let gitlinks = s
        .stage
        .iter()
        .filter(|r| r.starts_with(b"160000 "))
        .filter_map(|r| {
            r.iter()
                .position(|b| *b == b'\t')
                .map(|n| r[n + 1..].to_vec())
        })
        .collect::<Vec<_>>();
    // `( CDPATH= cd -P -- "$TOP" && find . -path ./.git -prune -o -name .git
    // -prune -print0 -o -type d -print0 ) … || FIND_RC=$?`, in readdir order.
    let (scanned, scan_ok) = tools::find_scan(&s.root);
    if !scan_ok {
        s.note("`.git` 名エントリ・ディレクトリの走査に失敗した経路あり");
    }
    // The subshell `( … find … )` (1526) rebuilds only in its child; then
    // `while IFS= read … e; done <find.z` (1530) marks.
    process::mark();
    let mut subgit = vec![];
    let mut names = 0;
    for e in scanned {
        if e == b"." {
            continue;
        }
        let e = e.strip_prefix(b"./").unwrap_or(&e).to_vec();
        if e == b".git" || e.ends_with(b"/.git") {
            if inside(&e, &nested) {
                continue;
            }
            if inside(&e, &gitlinks) {
                subgit.push(e);
                continue;
            }
            let absolute = at_path(&s.root, &e);
            let path = if !sfs::symlink(&absolute) && absolute.is_dir() {
                joined(&[&e, b"/"])
            } else {
                e
            };
            notinc(s, &path, ".git という名前のエントリ");
            names += 1;
        } else if !inside(&e, &nested)
            && !inside(&e, &gitlinks)
            && !sfs::readable(&at_path(&s.root, &e))
        {
            notinc(s, &joined(&[&e, b"/"]), "読めない");
            s.failed += 1;
        }
    }
    if names > 0 {
        s.note(format!("`.git` という名前のエントリ: {names} 件"));
    }
    if !subgit.is_empty() {
        // sg_list="$sg_list $(path_field "$p")" (1551).
        process::fork(1551);
        let list = subgit
            .iter()
            .map(|p| path_field(p))
            .collect::<Vec<_>>()
            .join(&b' ');
        s.note(joined(&[
            format!(
                "サブモジュール配下の `.git` 名エントリ: {} 件(",
                subgit.len()
            )
            .as_bytes(),
            &list,
            b")",
        ]));
    }
    Ok(())
}
