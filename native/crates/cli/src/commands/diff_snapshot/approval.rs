use super::*;
use bytes::sanitize;
pub fn apply(s: &mut Snapshot) -> Result<()> {
    // `LC_ALL=C sort -z <dg >dgs` (1016): the temporary assignment is disposed
    // of after the command, and Bash rebuilds its export environment there.
    process::remake(1016);
    let mut records = Vec::new();
    let mut bad = 0;
    for r in &s.rows {
        if r.allowed {
            records.push(joined(&[&r.one, b"\t", &r.two, b"\t", &r.three]));
        } else {
            bad += 1;
        }
    }
    records.sort();
    let data = records
        .iter()
        .flat_map(|r| r.iter().copied().chain([0]))
        .collect::<Vec<_>>();
    s.digest = sha(&data);
    s.show_digest = s.tamper && bad == 0 && !records.is_empty();
    let mut names = vec![];
    if s.tamper && s.args.accept_given && bad == 0 {
        if s.args.accept == s.digest && !records.is_empty() {
            s.note(joined(&[
                format!(
                    "承認済みとして扱った項目: {} 件(承認ダイジェスト ",
                    records.len()
                )
                .as_bytes(),
                &s.digest,
                b")",
            ]));
            for row in s.rows.clone() {
                s.note(joined(&[
                    "承認済み: ".as_bytes(),
                    &sanitize(&row.one),
                    b"\t",
                    &sanitize(&row.two),
                ]));
                if let Some(key) = row.two.strip_prefix("ローカル設定: filter.".as_bytes()) {
                    // ${ap_key%.*}: without a dot, the whole key is the name.
                    let end = key.iter().rposition(|b| *b == b'.').unwrap_or(key.len());
                    names.push(key[..end].to_vec());
                } else if let Some(name) = row.two.strip_prefix("filter 属性: ".as_bytes()) {
                    names.push(name.to_vec());
                }
            }
            s.rows.clear();
            s.tamper = false;
            s.show_digest = false;
        } else {
            s.mismatch = true;
        }
    }
    let mut seen = HashSet::new();
    let names = names
        .into_iter()
        .filter(|v| seen.insert(v.clone()))
        .collect::<Vec<_>>();
    if !names.is_empty() {
        // `export GIT_CONFIG_COUNT=… GIT_CONFIG_KEY_n=… …` reaches every later child.
        s.git.env.clear();
        s.git.env.push((
            "GIT_CONFIG_COUNT".into(),
            (names.len() * 4).to_string().into(),
        ));
        for (i, name) in names.iter().enumerate() {
            for (j, field) in [b"clean".as_slice(), b"smudge", b"process", b"required"]
                .into_iter()
                .enumerate()
            {
                let number = i * 4 + j;
                s.git.env.push((
                    format!("GIT_CONFIG_KEY_{number}").into(),
                    os(&joined(&[b"filter.", name, b".", field])),
                ));
                s.git.env.push((
                    format!("GIT_CONFIG_VALUE_{number}").into(),
                    if field == b"required" {
                        "false".into()
                    } else {
                        "".into()
                    },
                ));
            }
        }
        // `export GIT_CONFIG_COUNT=…` (1067) marks the export environment.
        for (key, value) in &s.git.env {
            process::export(&key.to_string_lossy(), Some(value.clone()));
        }
        for name in &names {
            for field in [b"clean".as_slice(), b"smudge", b"process"] {
                let key = joined(&[b"filter.", name, b".", field]);
                let out = s.git.at(1068).run(
                    &[words(&["config", "--get"]), vec![key.clone()]].concat(),
                    b"",
                )?;
                if out.code != 0 || !trim_lf(out.stdout).is_empty() {
                    return Err(Failure::raw(
                        22,
                        joined(&[
                            "ERROR [tamper] 承認した filter を無効化できない(".as_bytes(),
                            &key,
                            b")\n",
                        ]),
                    ));
                }
            }
        }
    }
    for name in names {
        s.note(joined(&["承認済みの filter を実行せず実内容で比較した: ".as_bytes(),&sanitize(&name),"(この filter の対象パスは内容が同じでも変更ありに出ることがあり、clean フィルタで伏せている内容は snapshot に出る)".as_bytes()]));
    }
    Ok(())
}
pub fn rows(s: &Snapshot) -> Vec<u8> {
    let mut out = vec![];
    for r in &s.rows {
        out.extend(joined(&[
            &sanitize(&r.one),
            b"\t",
            &sanitize(&r.two),
            b"\t",
            &sanitize(&r.three),
        ]));
        if !r.four.is_empty() {
            out.extend(joined(&[b"\t", &sanitize(&r.four)]));
        }
        out.push(b'\n');
    }
    if s.show_digest {
        out.extend(joined(&["承認ダイジェスト: ".as_bytes(), &s.digest, b"\n"]));
    }
    out
}
pub fn mismatch(s: &Snapshot) -> Result<()> {
    if s.mismatch {
        output::stderr(
            "承認ダイジェストが一致しない(疑いの内容が承認時から変わった)\n".as_bytes(),
        )?;
    }
    Ok(())
}
pub fn notes(s: &Snapshot) -> Result<()> {
    for note in &s.notes {
        output::stderr(&joined(&[b"NOTE: ", note, b"\n"]))?;
    }
    Ok(())
}
pub fn tokens(s: &Snapshot) -> Result<()> {
    for (i, (k, v)) in s.git.env.iter().enumerate() {
        output::stdout(
            &joined(&[raw(k), b"=", raw(v), b"\0"]),
            if i == 0 { 1092 } else { 1093 },
        )?;
    }
    Ok(())
}
