//! ブックの組み立て(旧 `write_xlsx` の 177〜194 行と、openpyxl 3.1.5 の `Workbook`・`Worksheet`・
//! `Cell` の働き)と、ZIP に入れる前の部分の並び。
//!
//! 出典と許諾: 部分の名前・順・各部分の XML の形は openpyxl 3.1.5(MIT ライセンス。
//! Copyright (c) 2010 openpyxl)の出力に合わせた。許諾の全文は同じディレクトリの `NOTICE.md`。
//!
//! 表ごとに次の順で進める(旧版と同じ。例外と警告の順もこれで決まる)。
//! 1. シート名(`unique_sheet_name` → `create_sheet` の `avoid_duplicate_name`)。32 文字以上なら
//!    `UserWarning` を実行中の最初の 1 回だけ。
//! 2. 行を足す(`ws.append`)。各値は 32767 文字で切ってから制御文字を照らし、あれば
//!    `IllegalCharacterError`。2 文字以上で `=` で始まれば数式、誤りの 7 語なら誤りの型、ほかは文字列。
//! 3. 列幅(`ws.cell(row, col)` を全部の行と列で読む)。1048577 行目で `ValueError`(行の上限)、
//!    18279 列目で `ValueError: Invalid column index`(`get_column_letter`)。
//!
//! 1 行目の太字・`A2` の固定・寸法は、部分の XML([`super::xml`])で書く。

use super::errors::{ExcType, Frame, PyException, frames, types};
use super::markdown::Table;
use super::pyio::{PyStr, Runtime};
use super::sheet_name::{self, MAX_TITLE, TITLE_WARNING};
use super::xml;
use std::collections::HashSet;

/// `openpyxl.utils.exceptions.IllegalCharacterError`。
pub const ILLEGAL_CHARACTER_ERROR: ExcType = ExcType {
    module: "openpyxl.utils.exceptions",
    name: "IllegalCharacterError",
    bases: &["Exception", "BaseException"],
};

/// セルの値の長さの上限(`Cell.check_string` の切り方)。
pub const MAX_CELL_LENGTH: usize = 32767;
/// 行の上限(`Worksheet._get_cell`)。
pub const MAX_ROW: usize = 1_048_576;
/// `get_column_letter` が受ける列の上限。
pub const MAX_COLUMN_INDEX: usize = 18278;
/// 誤りの型になる語(`openpyxl.cell.cell.ERROR_CODES`)。
pub const ERROR_CODES: [&str; 7] = [
    "#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A",
];

/// セルの値(openpyxl の `data_type`)。値は 32767 文字で切った後のもの。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    /// `s`(空文字も含む)。
    Text(PyStr),
    /// `f`(先頭の `=` を含む値)。
    Formula(PyStr),
    /// `e`。
    Error(PyStr),
}

impl Value {
    /// `cell.value`。
    pub fn text(&self) -> &PyStr {
        match self {
            Self::Text(text) | Self::Formula(text) | Self::Error(text) => text,
        }
    }
}

/// 1 つのシート。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sheet {
    /// openpyxl が決めたシート名。
    pub title: PyStr,
    /// 足した行(見出しの行が 1 行目)。各行は表の行のセルの数のまま。
    pub rows: Vec<Vec<Value>>,
    /// `ws.max_column`(全部の行の最大のセルの数)。
    pub max_column: usize,
    /// 列ごとの幅(8〜60)。
    pub widths: Vec<u32>,
}

/// 組み立てたブック。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Workbook {
    pub sheets: Vec<Sheet>,
}

/// ZIP に入れる前の 1 つの部分。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Part {
    /// ZIP の中の名前。
    pub name: String,
    pub data: Vec<u8>,
    /// シートの部分か(旧版はシートを一時ファイルから入れるので、ZIP の外部属性が違う)。
    pub worksheet: bool,
}

/// `docProps/core.xml` の 2 つの時刻(UTC の Unix 時刻の秒。秒未満は切り捨て)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CoreTimes {
    /// `Workbook()` を作った時刻。
    pub created: i64,
    /// 保存の時刻(`wb.save` の始め)。
    pub modified: i64,
}

/// 例外が起きた `write_xlsx` の中の段の前に付ける、外側の段。
const WRITE_XLSX: &[Frame] = &[frames::MODULE, frames::MAIN_CALLS_WRITE_XLSX];

impl Workbook {
    /// 旧 `write_xlsx` の 177〜194 行(表ごとのシート・行・太字・固定・列幅)。
    pub fn build(py: &mut Runtime, tables: &[Table]) -> Result<Self, PyException> {
        let mut used = HashSet::new();
        let mut titles: Vec<PyStr> = Vec::with_capacity(tables.len());
        let mut sheets = Vec::with_capacity(tables.len());
        let mut warned = false;
        for table in tables {
            py.check_signals().map_err(|error| {
                error
                    .called_from(&[frames::WRITE_XLSX_LOOP])
                    .called_from(WRITE_XLSX)
            })?;
            let candidate = sheet_name::unique_sheet_name(&table.name, &mut used);
            let title = sheet_name::avoid_duplicate_name(&titles, &candidate);
            if title.len() > MAX_TITLE && !warned {
                // 警告の既定の扱い(場所ごとに 1 回)。書けたかに依らず、2 回目は出さない。
                warned = true;
                py.show_warning(&PyStr::from(TITLE_WARNING))
                    .map_err(|error| {
                        error
                            .called_from(&[frames::WRITE_XLSX_CREATES_SHEET])
                            .called_from(WRITE_XLSX)
                    })?;
            }
            titles.push(title.clone());
            // 行を足す・列幅を測る繰り返し(旧版は Python)の中で届いた SIGINT は、誤りより先に
            // KeyboardInterrupt になる。
            let interrupted = |py: &mut Runtime, error: PyException| match py.check_signals() {
                Err(interrupt) => interrupt
                    .called_from(&[frames::WRITE_XLSX_LOOP])
                    .called_from(WRITE_XLSX),
                Ok(()) => error,
            };
            let rows = table
                .rows
                .iter()
                .map(|row| row.iter().map(bind_value).collect::<Result<Vec<_>, _>>())
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| interrupted(py, error))?;
            let max_column = rows.iter().map(Vec::len).max().unwrap_or(1).max(1);
            let widths =
                column_widths(&rows, max_column).map_err(|error| interrupted(py, error))?;
            sheets.push(Sheet {
                title,
                rows,
                max_column,
                widths,
            });
        }
        Ok(Self { sheets })
    }

    /// シート名の並び(`wb.sheetnames`)。
    pub fn sheet_names(&self) -> Vec<&PyStr> {
        self.sheets.iter().map(|sheet| &sheet.title).collect()
    }

    /// ZIP に入れる部分を、旧版の ZIP の順で並べる(`ExcelWriter.write_data`)。
    pub fn parts(&self, times: CoreTimes) -> Vec<Part> {
        let part = |name: &str, data: Vec<u8>| Part {
            name: name.to_owned(),
            data,
            worksheet: false,
        };
        let mut parts = vec![
            part("docProps/app.xml", xml::APP_XML.as_bytes().to_vec()),
            part(
                "docProps/core.xml",
                xml::core_xml(times.created, times.modified),
            ),
            part("xl/theme/theme1.xml", xml::THEME_XML.to_vec()),
        ];
        for (index, sheet) in self.sheets.iter().enumerate() {
            parts.push(Part {
                name: format!("xl/worksheets/sheet{}.xml", index + 1),
                data: xml::sheet_xml(sheet),
                worksheet: true,
            });
        }
        let count = self.sheets.len();
        parts.extend([
            part("xl/styles.xml", xml::STYLES_XML.as_bytes().to_vec()),
            part("_rels/.rels", xml::ROOT_RELS.as_bytes().to_vec()),
            part("xl/workbook.xml", xml::workbook_xml(&self.sheet_names())),
            part("xl/_rels/workbook.xml.rels", xml::workbook_rels(count)),
            part("[Content_Types].xml", xml::content_types(count)),
        ]);
        parts
    }
}

/// `Cell(value=…)` の `_bind_value`: 32767 文字で切り、制御文字(`\x00-\x08`・`\x0b-\x0c`・
/// `\x0e-\x1f`)があれば `IllegalCharacterError`。型を決める。
fn bind_value(value: &PyStr) -> Result<Value, PyException> {
    let points = value.as_code_points();
    let points = &points[..points.len().min(MAX_CELL_LENGTH)];
    if points.iter().any(|&code| is_illegal(code)) {
        let mut message = PyStr::from_code_points(points.to_vec());
        message.push_str(" cannot be used in worksheets.");
        return Err(
            PyException::new(ILLEGAL_CHARACTER_ERROR, message).called_from(&[
                frames::MODULE,
                frames::MAIN_CALLS_WRITE_XLSX,
                frames::WRITE_XLSX_APPENDS,
                frames::WORKSHEET_APPEND,
                frames::CELL_INIT,
                frames::CELL_VALUE_SETTER,
                frames::CELL_BIND_VALUE,
                frames::CELL_CHECK_STRING,
            ]),
        );
    }
    let text = PyStr::from_code_points(points.to_vec());
    Ok(if points.len() > 1 && points[0] == u32::from('=') {
        Value::Formula(text)
    } else if ERROR_CODES
        .iter()
        .any(|code| code.chars().map(u32::from).eq(points.iter().copied()))
    {
        Value::Error(text)
    } else {
        Value::Text(text)
    })
}

/// `ILLEGAL_CHARACTERS_RE`(`[\000-\010]|[\013-\014]|[\016-\037]`)。
fn is_illegal(code: u32) -> bool {
    matches!(code, 0x00..=0x08 | 0x0b..=0x0c | 0x0e..=0x1f)
}

/// 旧 `write_xlsx` の 187〜194 行: 列ごとに、全部の行の値の最初の行(`\n` まで)の幅
/// (0x7F を超える文字は 2)+ 2 を 60 で抑え、8 以上の最大にする。
fn column_widths(rows: &[Vec<Value>], max_column: usize) -> Result<Vec<u32>, PyException> {
    if rows.len() > MAX_ROW {
        // 1 列目の 1048577 行目の `ws.cell(row=…)` が先に失敗する。
        return Err(PyException::new(
            types::VALUE_ERROR,
            format!(
                "Row numbers must be between 1 and 1048576. Row number supplied was {}",
                MAX_ROW + 1
            ),
        )
        .called_from(&[
            frames::WRITE_XLSX_READS_CELL,
            frames::WORKSHEET_CELL,
            frames::WORKSHEET_GET_CELL,
        ])
        .called_from(WRITE_XLSX));
    }
    let mut widths = Vec::with_capacity(max_column.min(MAX_COLUMN_INDEX));
    for column in 1..=max_column {
        let mut width: u32 = 8;
        for row in rows {
            let text = row
                .get(column - 1)
                .map_or(&[][..], |value| value.text().as_code_points());
            let first_line = text
                .split(|&code| code == u32::from('\n'))
                .next()
                .unwrap_or_default();
            let display = first_line
                .iter()
                .map(|&code| if code > 0x7f { 2u64 } else { 1 })
                .sum::<u64>();
            width = width.max(display.saturating_add(2).min(60) as u32);
        }
        if column > MAX_COLUMN_INDEX {
            return Err(PyException::new(
                types::VALUE_ERROR,
                format!("Invalid column index {column}"),
            )
            .called_from(&[
                frames::WRITE_XLSX_SETS_WIDTH,
                frames::UTILS_GET_COLUMN_LETTER,
            ])
            .called_from(WRITE_XLSX));
        }
        widths.push(width);
    }
    Ok(widths)
}

#[cfg(test)]
mod tests {
    use super::super::pyio::Stream;
    use super::super::pyio::fake::FakeOs;
    use super::*;
    use dev_workflow_platform::r4e;

    fn runtime() -> (Runtime, FakeOs) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        let runtime = Runtime::start_with(&state, &|_| None, Box::new(os.clone())).unwrap();
        (runtime, os)
    }

    fn table(name: &str, rows: &[&[&str]]) -> Table {
        Table {
            name: PyStr::from(name),
            rows: rows
                .iter()
                .map(|row| row.iter().map(|cell| PyStr::from(*cell)).collect())
                .collect(),
        }
    }

    #[test]
    fn values_get_the_openpyxl_types() {
        let bound = |text: &str| bind_value(&PyStr::from(text)).unwrap();
        assert_eq!(bound("="), Value::Text(PyStr::from("=")));
        assert_eq!(bound("==1"), Value::Formula(PyStr::from("==1")));
        assert_eq!(bound("=A1"), Value::Formula(PyStr::from("=A1")));
        assert_eq!(bound(""), Value::Text(PyStr::new()));
        for code in ERROR_CODES {
            assert_eq!(bound(code), Value::Error(PyStr::from(code)));
        }
        assert_eq!(bound("#n/a"), Value::Text(PyStr::from("#n/a")));
        assert_eq!(bound("#N/A "), Value::Text(PyStr::from("#N/A ")));
        assert_eq!(bound("1"), Value::Text(PyStr::from("1")));
        // タブ・改行・\r は制御文字に入らない。
        assert_eq!(bound("a\tb\nc\rd"), Value::Text(PyStr::from("a\tb\nc\rd")));
    }

    #[test]
    fn values_are_cut_before_the_control_characters_are_checked() {
        let long = format!("{}\u{1}b", "a".repeat(MAX_CELL_LENGTH));
        let value = bind_value(&PyStr::from(long.as_str())).unwrap();
        assert_eq!(value.text().len(), MAX_CELL_LENGTH);
        let long = format!("{}\u{1}", "a".repeat(MAX_CELL_LENGTH - 1));
        let error = bind_value(&PyStr::from(long.as_str())).unwrap_err();
        assert_eq!(error.kind, ILLEGAL_CHARACTER_ERROR);
        assert_eq!(error.message.len(), MAX_CELL_LENGTH + 30);
        for code in [0x00, 0x08, 0x0b, 0x0c, 0x0e, 0x1b, 0x1f] {
            let text = PyStr::from_code_points(vec![0x61, code, 0x62]);
            let error = bind_value(&text).unwrap_err();
            let mut expected = text.clone();
            expected.push_str(" cannot be used in worksheets.");
            assert_eq!(error.message, expected, "{code:x}");
            assert_eq!(
                error.type_line().as_code_points()[..47],
                PyStr::from("openpyxl.utils.exceptions.IllegalCharacterError").as_code_points()[..]
            );
            assert_eq!(
                error
                    .traceback
                    .iter()
                    .map(|frame| (frame.file, frame.line))
                    .collect::<Vec<_>>(),
                [
                    ("md_tables_to_xlsx.py", 241),
                    ("md_tables_to_xlsx.py", 233),
                    ("md_tables_to_xlsx.py", 183),
                    ("openpyxl/worksheet/worksheet.py", 673),
                    ("openpyxl/cell/cell.py", 119),
                    ("openpyxl/cell/cell.py", 218),
                    ("openpyxl/cell/cell.py", 197),
                    ("openpyxl/cell/cell.py", 165),
                ]
            );
        }
    }

    #[test]
    fn widths_count_the_first_line_and_wide_characters() {
        let rows = vec![
            vec![Value::Text(PyStr::from("項目")), Value::Text(PyStr::new())],
            vec![
                Value::Text(PyStr::from("日本語の項目名")),
                Value::Text(PyStr::from(format!("1行目\n{}", "z".repeat(80)).as_str())),
            ],
            vec![Value::Text(PyStr::from("x".repeat(100).as_str()))],
            vec![Value::Formula(PyStr::from("=SUM(1,2)"))],
        ];
        assert_eq!(column_widths(&rows, 3).unwrap(), [60, 8, 8]);
        let rows = vec![
            vec![Value::Text(PyStr::from("日本語の項目名"))],
            vec![Value::Text(PyStr::from_code_points(vec![0x1f600, 0xdcff]))],
        ];
        assert_eq!(column_widths(&rows, 1).unwrap(), [16]);
        let rows = vec![vec![Value::Text(PyStr::from("a".repeat(57).as_str()))]];
        assert_eq!(column_widths(&rows, 1).unwrap(), [59]);
    }

    #[test]
    fn column_and_row_limits_raise_value_errors() {
        let header = vec![Value::Text(PyStr::new()); 18279];
        let error = column_widths(std::slice::from_ref(&header), 18279).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "ValueError: Invalid column index 18279"
        );
        assert_eq!(
            error
                .traceback
                .iter()
                .map(|frame| (frame.file, frame.line))
                .collect::<Vec<_>>(),
            [
                ("md_tables_to_xlsx.py", 241),
                ("md_tables_to_xlsx.py", 233),
                ("md_tables_to_xlsx.py", 194),
                ("openpyxl/utils/cell.py", 94),
            ]
        );
        assert_eq!(
            column_widths(&[header[..18278].to_vec()], 18278)
                .unwrap()
                .len(),
            18278
        );
        let rows = vec![vec![Value::Text(PyStr::new())]; MAX_ROW + 1];
        let error = column_widths(&rows, 1).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "ValueError: Row numbers must be between 1 and 1048576. Row number supplied was 1048577"
        );
        assert_eq!(error.traceback.last().unwrap().line, 257);
    }

    #[test]
    fn the_title_warning_is_shown_once() {
        // 31 文字ちょうどの名前と、その大文字小文字違い(cases.json の sheets/warning-31-exact-case)。
        let (mut runtime, os) = runtime();
        let lower = "a".repeat(29);
        let upper = "A".repeat(29);
        let mixed = format!("{}A", "a".repeat(28));
        let tables = [
            table(&format!("v {lower}"), &[&["x"]]),
            table(&format!("v {upper}"), &[&["x"]]),
            table(&format!("v {mixed}"), &[&["x"]]),
        ];
        let book = Workbook::build(&mut runtime, &tables).unwrap();
        let names = book
            .sheet_names()
            .into_iter()
            .map(PyStr::to_string_lossy)
            .collect::<Vec<_>>();
        assert_eq!(
            names,
            [
                format!("v {lower}"),
                format!("v {upper}1"),
                format!("v {mixed}2"),
            ]
        );
        assert_eq!(os.written(Stream::Stderr), TITLE_WARNING);
    }

    #[test]
    fn the_first_error_stops_the_book() {
        let (mut runtime, _os) = runtime();
        let tables = [
            table("v a", &[&["x"]]),
            table("v b", &[&["ok"], &["bad\u{1}"]]),
            table("v c", &[&["\u{2}"]]),
        ];
        let error = Workbook::build(&mut runtime, &tables).unwrap_err();
        assert_eq!(
            error.message.to_string_lossy(),
            "bad\u{1} cannot be used in worksheets."
        );
    }

    #[test]
    fn parts_follow_the_legacy_order_and_bytes() {
        let (mut runtime, _os) = runtime();
        let tables = [table(
            "basic 概要",
            &[&["名前", "値", "備考"], &["a", "1", "x"], &["b", "2", "y"]],
        )];
        let book = Workbook::build(&mut runtime, &tables).unwrap();
        let parts = book.parts(CoreTimes {
            created: 0,
            modified: 0,
        });
        let names = parts
            .iter()
            .map(|part| part.name.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            names,
            [
                "docProps/app.xml",
                "docProps/core.xml",
                "xl/theme/theme1.xml",
                "xl/worksheets/sheet1.xml",
                "xl/styles.xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels",
                "[Content_Types].xml",
            ]
        );
        assert_eq!(
            parts.iter().map(|part| part.worksheet).collect::<Vec<_>>(),
            [false, false, false, true, false, false, false, false, false]
        );
        // 固定期待(native/fixtures/r4e/expected/01-basic)のバイトと同じ。
        let expected = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../fixtures/r4e/expected/01-basic");
        for part in &parts {
            let mut data = std::fs::read(expected.join(&part.name)).unwrap();
            if part.name == "docProps/core.xml" {
                let text = String::from_utf8(data).unwrap();
                data = text
                    .replace("@CREATED@", "1970-01-01T00:00:00Z")
                    .replace("@MODIFIED@", "1970-01-01T00:00:00Z")
                    .into_bytes();
            }
            assert_eq!(
                String::from_utf8_lossy(&part.data),
                String::from_utf8_lossy(&data),
                "{}",
                part.name
            );
        }
    }
}
