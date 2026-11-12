//! 呼出履歴に出す旧スクリプトの行の固定表。
//!
//! 行番号とソースの行は、Rustのソースからではなく旧版の`pending-implementation.py`の行から取る。
//! 表の各行は、旧版のその行の字面(行頭の字下げを含む全体)を持ち、旧版と食い違えば検査で止める。
//! 所在地の欄は固定のファイル名にする(2026-11-11 17:58・21:35の利用者決定)。

/// 呼出履歴の所在地の欄に出す、旧スクリプトの固定のファイル名。
pub const SCRIPT_NAME: &str = "pending-implementation.py";

/// 旧スクリプトの1行。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Line {
    pub name: &'static str,
    pub line: u32,
    /// 旧版のその行の全体(改行を除く)。
    pub source: &'static str,
}

/// 行の固定表。
pub struct Lines(pub &'static [Line]);

impl Lines {
    /// 名前の行。表に無い名前は実装の誤りなので止める。
    pub fn point(&self, name: &str) -> Line {
        *self
            .0
            .iter()
            .find(|line| line.name == name)
            .unwrap_or_else(|| panic!("行の固定表に無い地点: {name}"))
    }

    /// 旧スクリプトの本文と照らす。名前の重複・範囲の外・字面の違いを報告する。
    #[cfg(test)]
    pub fn verify(&self, source: &str) -> Result<(), String> {
        let lines: Vec<&str> = source.split('\n').collect();
        for (index, line) in self.0.iter().enumerate() {
            if self.0[..index].iter().any(|other| other.name == line.name) {
                return Err(format!("地点の名前が重複: {}", line.name));
            }
            match (line.line as usize)
                .checked_sub(1)
                .and_then(|i| lines.get(i))
            {
                Some(text) if *text == line.source => {}
                Some(_) => return Err(format!("行の字面が違う: {} ({})", line.name, line.line)),
                None => return Err(format!("行が範囲の外: {} ({})", line.name, line.line)),
            }
        }
        Ok(())
    }
}

pub const LINES: Lines = Lines(&[
    Line {
        name: "help",
        line: 825,
        source: "        sys.stdout.write(HELP)",
    },
    Line {
        name: "run",
        line: 827,
        source: "    code, result, notes = run_detail(argv)",
    },
    Line {
        name: "result",
        line: 828,
        source: "    sys.stdout.write(canonical_json(result) + \"\\n\")",
    },
    Line {
        name: "issue",
        line: 832,
        source: "            sys.stderr.write(\"pending-implementation: \" + issue[\"code\"] + \" \"",
    },
    Line {
        name: "note",
        line: 837,
        source: "        sys.stderr.write(note + \"\\n\")",
    },
    Line {
        name: "confirm",
        line: 839,
        source: "        sys.stderr.write(\"保護記録候補の一覧について、人の確認が必要です。\\n\")",
    },
    Line {
        name: "module",
        line: 844,
        source: "    sys.exit(main())",
    },
]);

/// 旧スクリプトの所在(検査だけが使う)。
#[cfg(test)]
pub fn old_script_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../plugins/dev-workflow/skills/do-task/scripts/pending-implementation.py")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_table_matches_the_old_script() {
        let source = std::fs::read_to_string(old_script_path()).unwrap();
        LINES.verify(&source).unwrap();
        // 照らし合わせが字面の違いを見逃さないこと。
        let changed = source.replace("    sys.exit(main())", "    sys.exit(main( ))");
        assert!(LINES.verify(&changed).is_err());
    }
}
