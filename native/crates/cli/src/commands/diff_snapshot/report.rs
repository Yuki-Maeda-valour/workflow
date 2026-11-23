use super::*;
use bytes::{path_field, zrecords};
use git::working;
const TITLES: [&str; 11] = [
    "追跡差分(stat)",
    "追跡差分",
    "未追跡ファイル",
    "除外(機密)",
    "除外(既定)",
    "基準時点から存在(対象外)",
    "含められなかった未追跡",
    "内容を省略した未追跡",
    "内容を省略した追跡",
    "改竄の疑い",
    "gitignore により除外(.gitignore 以外)",
];
pub fn header(s: &mut Snapshot) -> Result<()> {
    let h = s
        .git
        .at(1124)
        .words(&["rev-parse", "--verify", "--quiet", "HEAD"])?;
    let hbytes = trim_lf(h.stdout);
    s.head = if h.code != 0 || hbytes.is_empty() {
        "(HEAD 無し)".as_bytes().to_vec()
    } else {
        hbytes
    };
    let a = s.git.at(1127).foreground(
        &[
            words(&["merge-base", "--is-ancestor"]),
            vec![s.args.base.clone(), b"HEAD".to_vec()],
        ]
        .concat(),
    )?;
    s.ancestor = match a.code {
        0 => "基準は HEAD の祖先",
        1 => "WARNING: 基準は HEAD の祖先ではない",
        _ => "祖先判定は不能(基準が tree / HEAD 無し)",
    }
    .as_bytes()
    .to_vec();
    if s.args.pre.is_empty() {
        s.note("`--pre-untracked` の指定が無く全未追跡を対象にした");
    }
    Ok(())
}
pub fn assemble(s: &Snapshot, fixed: bool) -> Result<Vec<u8>> {
    // `date '+%Y-%m-%d %H:%M:%S'`
    let date = tools::now();
    let mut out = joined(&[
        "# diff スナップショット\n\n- 基準: ".as_bytes(),
        &s.args.base,
        "\n- 現在 HEAD: ".as_bytes(),
        &s.head,
        "\n- 生成日時: ".as_bytes(),
        &date,
        "\n- 除外 ERE: ".as_bytes(),
        &s.ere,
        "\n- 祖先判定: ".as_bytes(),
        &s.ancestor,
        "\n- 基準時点の未追跡一覧: ".as_bytes(),
        if s.args.pre.is_empty() {
            "なし".as_bytes()
        } else {
            "あり".as_bytes()
        },
        "\n- 件数の数え方: 要約行の件数は未追跡だけを数える(追跡の除外はパスだけ各節に載る)\n"
            .as_bytes(),
    ]);
    if s.tamper {
        out.extend("- WARNING: 改竄の疑いがある(`## 改竄の疑い` を参照)\n".as_bytes());
    }
    for note in &s.notes {
        out.extend(joined(&[b"- NOTE: ", note, b"\n"]));
    }
    out.push(b'\n');
    if fixed {
        out.extend("要約: 改竄の疑いがあるため未集計\n".as_bytes());
    } else {
        let [a, b1, b2, c, d, e, f] = s.counts;
        let n = a + b1 + b2 + c + d + e;
        out.extend(format!("要約: 未追跡 {n} 件(本文 {a} / 除外(機密) {b1} / 除外(既定) {b2} / 対象外 {c} / 省略 {d} / 含められず {e}) / gitignore 外 {f}\n").as_bytes());
    }
    for (i, title) in TITLES.into_iter().enumerate() {
        out.extend(format!("\n## {title}\n\n").as_bytes());
        if fixed && i != 9 {
            out.extend("(改竄の疑いがあるため生成しない)\n".as_bytes());
        } else if !s.sections[i].is_empty() {
            out.extend(&s.sections[i]);
        } else {
            out.extend(if i == 1 {
                s.body_empty.as_slice()
            } else {
                "(差分なし)".as_bytes()
            });
            out.push(b'\n');
        }
    }
    Ok(out)
}
pub fn finish_notes(s: &mut Snapshot) -> Result<()> {
    // 1557・1559: >"$TMPD/ignA.z" / >"$TMPD/ignB.z" 2>/dev/null
    let a = s.git.at(1557).to_file(
        &words(&[
            "ls-files",
            "-o",
            "--ignored",
            "--exclude-per-directory=.gitignore",
            "-z",
        ]),
        &s.temp.0,
        "ignA.z",
        git::Errors::Null,
        None,
    )?;
    let b = s.git.at(1559).to_file(
        &words(&["ls-files", "-o", "--ignored", "--exclude-standard", "-z"]),
        &s.temp.0,
        "ignB.z",
        git::Errors::Null,
        None,
    )?;
    // `while IFS= read …; done <ignA.z` (1563) and `<ignB.z` (1568).
    process::mark();
    let a = zrecords(&a.stdout);
    let aset = a.iter().cloned().collect::<HashSet<_>>();
    for p in zrecords(&b.stdout) {
        if !aset.contains(&p) {
            s.sections[10].extend(path_field(&p));
            s.sections[10].push(b'\n');
            s.counts[6] += 1;
        }
    }
    if !a.is_empty() {
        s.note(format!("gitignore 済みの未追跡 {} 件はこの snapshot の対象外(`.gitignore` が覆う領域の変更はレビューされない)",a.len()));
    }
    let excludes = s.git.at(1574).words(&[
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "info/exclude",
    ])?;
    let excludes = PathBuf::from(os(&trim_lf(excludes.stdout)));
    if !excludes.as_os_str().is_empty()
        && excludes.is_file()
        && !sfs::symlink(&excludes)
        && sfs::readable(&excludes)
    {
        // `grep -qv -e '^#' -e '^[[:space:]]*$' FILE` in the C locale. GNU grep
        // treats a file with NUL as binary and then ends lines at each NUL too.
        if let Ok(data) = sfs::read(&excludes, None, "info-exclude", &mut NoAudit) {
            let text = if data.contains(&0) {
                data.iter()
                    .map(|b| if *b == 0 { b'\n' } else { *b })
                    .collect::<Vec<_>>()
            } else {
                data
            };
            let mut lines = text.split(|b| *b == b'\n').collect::<Vec<_>>();
            if text.ends_with(b"\n") {
                lines.pop();
            }
            if lines.iter().any(|line| {
                !line.starts_with(b"#")
                    && !line
                        .iter()
                        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r'))
            }) {
                s.note("`.git/info/exclude` あり");
            }
        }
    }
    for (name, message, line) in [
        (
            b".gitignore".as_slice(),
            "`.gitignore` が基準から変更、または未追跡で追加された",
            1601,
        ),
        (b".gitattributes", "`.gitattributes` が変更された", 1606),
    ] {
        // control_changed: out="$(git … diff --name-only …)" (1593), then
        // collect_named's `while IFS= read` loops (1582・1585).
        let out = s.git.at(1593).run(
            &[
                working(),
                words(&[
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "--ignore-submodules=dirty",
                ]),
                vec![
                    s.args.base.clone(),
                    b"--".to_vec(),
                    name.to_vec(),
                    joined(&[b"**/", name]),
                ],
            ]
            .concat(),
            b"",
        )?;
        let named = s
            .untracked
            .iter()
            .chain(a.iter())
            .filter(|p| p.as_slice() == name || p.ends_with(&joined(&[b"/", name])))
            .collect::<Vec<_>>();
        process::mark();
        if (out.code == 0 && !trim_lf(out.stdout).is_empty()) || !named.is_empty() {
            let mut note = message.as_bytes().to_vec();
            if !named.is_empty() {
                // gi_list="$gi_list $(path_field "$p")" (1601・1606).
                process::fork(line);
                note.extend("(未追跡:".as_bytes());
                for p in named {
                    note.push(b' ');
                    note.extend(path_field(p));
                }
                note.push(b')');
            }
            s.note(note);
        }
    }
    // `while IFS= read …; done <untracked.z` (1615).
    process::mark();
    // Tracked inclusion was saved by tracked::run; default/secret paths do not count.
    let mut count = 0;
    for (p, v) in &s.filters {
        if v == b"lfs" && s.tracked_changed_included(p) {
            count += 1;
        }
    }
    for p in &s.untracked {
        if s.filters.get(p).is_some_and(|v| v == b"lfs") {
            count += 1;
        }
    }
    if count > 0 {
        s.note(format!(
            "`filter=lfs` のパス {count} 件は LFS ポインタではなく作業ツリーの実内容で比較した"
        ));
    }
    Ok(())
}
/// `grep -h '^Binary files ' SEC_BODY SEC_UNTRACKED` (1652) of GNU grep 3.11
/// under `LC_ALL=C`: the lines it prints. Once a buffer it has read holds a NUL
/// (counting the incomplete line kept from the previous read), the file is
/// binary: nothing more of it is printed, and grep stops at its next match.
/// The reads follow grep's `fillbuf` (96 KiB, page-aligned). Where the buffer
/// lies modulo the page decides the read sizes after an incomplete line: the
/// base (glibc 2.39) puts the first buffer at 3984 (measured with strace) and
/// a grown buffer of 128 KiB or more in its own mapping at 16 (measured). A
/// grown buffer below 128 KiB is taken to follow the old one on the heap
/// (inferred, not measured).
pub fn grep_printed(files: &[&[u8]]) -> Vec<Vec<u8>> {
    const PAGE: usize = 4096;
    const UWORD: usize = 8;
    const MIN_AFTER: usize = PAGE + UWORD;
    const MMAP_THRESHOLD: usize = 128 * 1024;
    let align = |x: usize| x.div_ceil(PAGE) * PAGE;
    let mut alloc = 96 * 1024 + PAGE + UWORD;
    let mut base = 3984usize;
    let mut printed = vec![];
    for data in files {
        let size = data.len();
        // reset(): an empty buffer just after the first page boundary.
        let mut buflim = align(base + 1) - base;
        let mut offset = 0usize;
        let mut residue = 0usize;
        let mut binary = false;
        let mut save = 0usize;
        loop {
            // fillbuf(save)
            let readbuf = if alloc - buflim >= MIN_AFTER {
                buflim
            } else {
                let minsize = save + PAGE;
                if minsize + MIN_AFTER > alloc {
                    let incr_min = minsize + MIN_AFTER - alloc;
                    let alloc_max = (size - offset + save + MIN_AFTER).max(alloc + incr_min);
                    let mut n = (alloc + alloc / 2).min(alloc_max);
                    if n - alloc < incr_min {
                        n = alloc + incr_min;
                    }
                    base = if n >= MMAP_THRESHOLD {
                        16
                    } else {
                        (base + ((alloc + UWORD + 15) & !15)) % PAGE
                    };
                    alloc = n;
                }
                align(base + 1 + save) - base
            };
            let mut readsize = alloc - UWORD - readbuf;
            readsize -= readsize % PAGE;
            let fill = readsize.min(size - offset);
            buflim = readbuf + fill;
            offset += fill;
            // The buffer is the kept bytes and the new ones.
            let start = offset - fill - save;
            if !binary && data[start..offset].contains(&0) {
                binary = true;
            }
            let beg = offset - fill;
            if fill == 0 {
                break;
            }
            let lim = match data[beg..offset].iter().rposition(|b| *b == b'\n') {
                Some(i) => beg + i + 1,
                None => beg - residue,
            };
            let first = beg - residue;
            residue = offset - lim;
            if first < lim {
                let lines = data[first..lim - 1].split(|b| *b == b'\n');
                if binary {
                    if lines.into_iter().any(|l| l.starts_with(b"Binary files ")) {
                        residue = 0;
                        break;
                    }
                } else {
                    printed.extend(
                        lines
                            .filter(|l| l.starts_with(b"Binary files "))
                            .map(<[u8]>::to_vec),
                    );
                }
            }
            save = residue;
        }
        if residue > 0 && !binary {
            let last = &data[size - residue..];
            if last.starts_with(b"Binary files ") {
                printed.push(last.to_vec());
            }
        }
    }
    printed
}
pub fn binary_tamper(s: &mut Snapshot) -> Result<()> {
    let mut bad = vec![];
    {
        for line in grep_printed(&[&s.sections[1], &s.sections[2]]) {
            if let Some(rest) = line.strip_prefix(b"Binary files ") {
                let end = rest
                    .windows(5)
                    .rposition(|v| v == b" and ")
                    .unwrap_or(rest.len());
                let p = rest[..end].strip_prefix(b"a/").unwrap_or(&rest[..end]);
                bad.push(p.to_vec());
            }
        }
    }
    if !bad.is_empty() {
        for p in bad {
            s.row(&p, "Binary files 検出".as_bytes(), b"-", b"", false);
        }
        s.show_digest = false;
        s.sections[9] = approval::rows(s);
        output::stderr(&s.sections[9])?;
        s.want_patch = false;
    }
    Ok(())
}
