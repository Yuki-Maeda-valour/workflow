//! 各部分の XML のバイト(openpyxl 3.1.5・et_xmlfile 2.0.0・CPython 3.10.11 の ElementTree が
//! 旧版の環境〈lxml なし〉で出すものと同じ)。
//!
//! 出典と許諾: 既定テーマ(`theme1.xml`。openpyxl の `openpyxl/writer/theme.py` の `theme_xml`)・
//! `docProps/app.xml` の文(`openpyxl/packaging/extended.py`)・書式(`xl/styles.xml`)と、各部分の
//! 要素と属性の並びは、openpyxl 3.1.5 から転記した。openpyxl は MIT ライセンス
//! (Copyright (c) 2010 openpyxl)。許諾の全文は同じディレクトリの `NOTICE.md` にある。
//!
//! - 書き方: 宣言(`<?xml …?>`)は付けない。子の無い要素は `<a b="c" />`(空白と `/>`)。
//! - エスケープ: 文字は `&`・`<`・`>`(`_escape_cdata`)。属性はそれに `"`・`\r`・`\n`・`\t`
//!   (`_escape_attrib`。`\t` は `&#09;`)。`'` はそのまま。制御文字もそのまま書く(旧版の出力のまま)。
//! - 文字コード: UTF-8 で、書けない文字(サロゲート)は `&#NNNNN;`(10 進。`xmlcharrefreplace`)。

use super::pyio::PyStr;
use super::workbook::{Sheet, Value};

/// 既定テーマ(openpyxl 3.1.5 の `theme_xml` を UTF-8 にした 10140 バイト)。
pub const THEME_XML: &[u8] = include_bytes!("theme1.xml");

/// `docProps/app.xml`(openpyxl 3.1.5 の `ExtendedProperties()`。205 バイト)。
pub const APP_XML: &str = concat!(
    "<Properties xmlns=\"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties\">",
    "<Application>Microsoft Excel Compatible / Openpyxl 3.1.5</Application>",
    "<AppVersion>3.1</AppVersion></Properties>",
);

/// `xl/styles.xml`(既定の書式と、1 行目の太字の書式の 2 つ)。
pub const STYLES_XML: &str = concat!(
    "<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">",
    "<numFmts count=\"0\" /><fonts count=\"2\"><font><name val=\"Calibri\" /><family val=\"2\" />",
    "<color theme=\"1\" /><sz val=\"11\" /><scheme val=\"minor\" /></font><font><b val=\"1\" />",
    "</font></fonts><fills count=\"2\"><fill><patternFill /></fill><fill>",
    "<patternFill patternType=\"gray125\" /></fill></fills><borders count=\"1\"><border><left />",
    "<right /><top /><bottom /><diagonal /></border></borders><cellStyleXfs count=\"1\">",
    "<xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" /></cellStyleXfs><cellXfs count=\"2\">",
    "<xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" pivotButton=\"0\" quotePrefix=\"0\" xfId=\"0\" />",
    "<xf numFmtId=\"0\" fontId=\"1\" fillId=\"0\" borderId=\"0\" pivotButton=\"0\" quotePrefix=\"0\" xfId=\"0\" />",
    "</cellXfs><cellStyles count=\"1\">",
    "<cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\" hidden=\"0\" /></cellStyles>",
    "<tableStyles count=\"0\" defaultTableStyle=\"TableStyleMedium9\" defaultPivotStyle=\"PivotStyleLight16\" />",
    "<colors><indexedColors><rgbColor rgb=\"00000000\" /><rgbColor rgb=\"00FFFFFF\" />",
    "<rgbColor rgb=\"00FF0000\" /><rgbColor rgb=\"0000FF00\" /><rgbColor rgb=\"000000FF\" />",
    "<rgbColor rgb=\"00FFFF00\" /><rgbColor rgb=\"00FF00FF\" /><rgbColor rgb=\"0000FFFF\" />",
    "<rgbColor rgb=\"00000000\" /><rgbColor rgb=\"00FFFFFF\" /><rgbColor rgb=\"00FF0000\" />",
    "<rgbColor rgb=\"0000FF00\" /><rgbColor rgb=\"000000FF\" /><rgbColor rgb=\"00FFFF00\" />",
    "<rgbColor rgb=\"00FF00FF\" /><rgbColor rgb=\"0000FFFF\" /><rgbColor rgb=\"00800000\" />",
    "<rgbColor rgb=\"00008000\" /><rgbColor rgb=\"00000080\" /><rgbColor rgb=\"00808000\" />",
    "<rgbColor rgb=\"00800080\" /><rgbColor rgb=\"00008080\" /><rgbColor rgb=\"00C0C0C0\" />",
    "<rgbColor rgb=\"00808080\" /><rgbColor rgb=\"009999FF\" /><rgbColor rgb=\"00993366\" />",
    "<rgbColor rgb=\"00FFFFCC\" /><rgbColor rgb=\"00CCFFFF\" /><rgbColor rgb=\"00660066\" />",
    "<rgbColor rgb=\"00FF8080\" /><rgbColor rgb=\"000066CC\" /><rgbColor rgb=\"00CCCCFF\" />",
    "<rgbColor rgb=\"00000080\" /><rgbColor rgb=\"00FF00FF\" /><rgbColor rgb=\"00FFFF00\" />",
    "<rgbColor rgb=\"0000FFFF\" /><rgbColor rgb=\"00800080\" /><rgbColor rgb=\"00800000\" />",
    "<rgbColor rgb=\"00008080\" /><rgbColor rgb=\"000000FF\" /><rgbColor rgb=\"0000CCFF\" />",
    "<rgbColor rgb=\"00CCFFFF\" /><rgbColor rgb=\"00CCFFCC\" /><rgbColor rgb=\"00FFFF99\" />",
    "<rgbColor rgb=\"0099CCFF\" /><rgbColor rgb=\"00FF99CC\" /><rgbColor rgb=\"00CC99FF\" />",
    "<rgbColor rgb=\"00FFCC99\" /><rgbColor rgb=\"003366FF\" /><rgbColor rgb=\"0033CCCC\" />",
    "<rgbColor rgb=\"0099CC00\" /><rgbColor rgb=\"00FFCC00\" /><rgbColor rgb=\"00FF9900\" />",
    "<rgbColor rgb=\"00FF6600\" /><rgbColor rgb=\"00666699\" /><rgbColor rgb=\"00969696\" />",
    "<rgbColor rgb=\"00003366\" /><rgbColor rgb=\"00339966\" /><rgbColor rgb=\"00003300\" />",
    "<rgbColor rgb=\"00333300\" /><rgbColor rgb=\"00993300\" /><rgbColor rgb=\"00993366\" />",
    "<rgbColor rgb=\"00333399\" /><rgbColor rgb=\"00333333\" /></indexedColors></colors>",
    "</styleSheet>",
);

/// `_rels/.rels`(`WorkbookWriter.write_root_rels`)。
pub const ROOT_RELS: &str = concat!(
    "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">",
    "<Relationship Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" ",
    "Target=\"xl/workbook.xml\" Id=\"rId1\" />",
    "<Relationship Type=\"http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties\" ",
    "Target=\"docProps/core.xml\" Id=\"rId2\" />",
    "<Relationship Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties\" ",
    "Target=\"docProps/app.xml\" Id=\"rId3\" /></Relationships>",
);

/// シートの部分の、行より前の固定の要素(性質・寸法の後の表示・行の書式)。`A2` で固定する。
const SHEET_VIEWS_AND_FORMAT: &str = concat!(
    "<sheetViews><sheetView workbookViewId=\"0\">",
    "<pane ySplit=\"1\" topLeftCell=\"A2\" activePane=\"bottomLeft\" state=\"frozen\" />",
    "<selection pane=\"bottomLeft\" activeCell=\"A1\" sqref=\"A1\" /></sheetView></sheetViews>",
    "<sheetFormatPr baseColWidth=\"8\" defaultRowHeight=\"15\" />",
);

/// XML の出力の溜め。
#[derive(Default)]
pub struct XmlWriter {
    bytes: Vec<u8>,
}

impl XmlWriter {
    pub fn new() -> Self {
        Self::default()
    }

    /// エスケープの要らない固定の文(ASCII)。
    pub fn raw(&mut self, text: &str) {
        self.bytes.extend_from_slice(text.as_bytes());
    }

    /// 要素の文字(`_escape_cdata`: `&`・`<`・`>`)。
    pub fn text(&mut self, points: &[u32]) {
        for &code in points {
            match code {
                0x26 => self.raw("&amp;"),
                0x3c => self.raw("&lt;"),
                0x3e => self.raw("&gt;"),
                _ => self.code_point(code),
            }
        }
    }

    /// 属性の値(`_escape_attrib`: `&`・`<`・`>`・`"`・`\r`・`\n`・`\t`)。
    pub fn attribute(&mut self, points: &[u32]) {
        for &code in points {
            match code {
                0x26 => self.raw("&amp;"),
                0x3c => self.raw("&lt;"),
                0x3e => self.raw("&gt;"),
                0x22 => self.raw("&quot;"),
                0x0d => self.raw("&#13;"),
                0x0a => self.raw("&#10;"),
                0x09 => self.raw("&#09;"),
                _ => self.code_point(code),
            }
        }
    }

    /// 1 文字を UTF-8 で書く。書けない文字(サロゲート)は `&#NNNNN;`。
    fn code_point(&mut self, code: u32) {
        match char::from_u32(code) {
            Some(c) => {
                let mut buffer = [0; 4];
                self.bytes
                    .extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
            }
            None => self.raw(&format!("&#{code};")),
        }
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}

/// `get_column_letter(index)`(1〜18278。範囲の外は `None`)。
pub fn column_letter(index: usize) -> Option<String> {
    if !(1..=18278).contains(&index) {
        return None;
    }
    let mut letters = Vec::new();
    let mut rest = index;
    while rest > 0 {
        let digit = (rest - 1) % 26;
        letters.push(b'A' + digit as u8);
        rest = (rest - 1) / 26;
    }
    letters.reverse();
    String::from_utf8(letters).ok()
}

/// `datetime.isoformat(timespec="seconds") + "Z"`(UTC の Unix 時刻の秒から)。
pub fn w3cdtf(unix_seconds: i64) -> String {
    let days = unix_seconds.div_euclid(86_400);
    let seconds = unix_seconds.rem_euclid(86_400);
    // 1970-03-01 を起点にした暦の計算(H. Hinnant の civil_from_days)。
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        seconds / 3600,
        seconds % 3600 / 60,
        seconds % 60
    )
}

/// `docProps/core.xml`。`created` は `Workbook()` を作った時刻、`modified` は保存の時刻
/// (どちらも UTC の Unix 時刻の秒。秒未満は切り捨てて渡す)。
pub fn core_xml(created: i64, modified: i64) -> Vec<u8> {
    format!(
        concat!(
            "<cp:coreProperties xmlns:cp=\"http://schemas.openxmlformats.org/package/2006/metadata/core-properties\" ",
            "xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:dcterms=\"http://purl.org/dc/terms/\" ",
            "xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"><dc:creator>openpyxl</dc:creator>",
            "<dcterms:created xsi:type=\"dcterms:W3CDTF\">{}</dcterms:created>",
            "<dcterms:modified xsi:type=\"dcterms:W3CDTF\">{}</dcterms:modified></cp:coreProperties>",
        ),
        w3cdtf(created),
        w3cdtf(modified)
    )
    .into_bytes()
}

/// `xl/workbook.xml`(`WorkbookWriter.write`)。シート名は属性としてエスケープする。
pub fn workbook_xml(titles: &[&PyStr]) -> Vec<u8> {
    let mut xml = XmlWriter::new();
    xml.raw(concat!(
        "<workbook xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" ",
        "xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><workbookPr />",
        "<workbookProtection /><bookViews><workbookView visibility=\"visible\" minimized=\"0\" ",
        "showHorizontalScroll=\"1\" showVerticalScroll=\"1\" showSheetTabs=\"1\" tabRatio=\"600\" ",
        "firstSheet=\"0\" activeTab=\"0\" autoFilterDateGrouping=\"1\" /></bookViews><sheets>",
    ));
    for (index, title) in titles.iter().enumerate() {
        let number = index + 1;
        xml.raw("<sheet name=\"");
        xml.attribute(title.as_code_points());
        xml.raw(&format!(
            "\" sheetId=\"{number}\" state=\"visible\" r:id=\"rId{number}\" />"
        ));
    }
    xml.raw(
        "</sheets><definedNames /><calcPr calcId=\"124519\" fullCalcOnLoad=\"1\" /></workbook>",
    );
    xml.into_bytes()
}

/// `xl/_rels/workbook.xml.rels`(シート、書式、テーマの順)。
pub fn workbook_rels(sheets: usize) -> Vec<u8> {
    const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    let mut xml = XmlWriter::new();
    xml.raw(
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">",
    );
    for number in 1..=sheets {
        xml.raw(&format!(
            "<Relationship Type=\"{REL}/worksheet\" Target=\"/xl/worksheets/sheet{number}.xml\" Id=\"rId{number}\" />"
        ));
    }
    xml.raw(&format!(
        "<Relationship Type=\"{REL}/styles\" Target=\"styles.xml\" Id=\"rId{}\" />",
        sheets + 1
    ));
    xml.raw(&format!(
        "<Relationship Type=\"{REL}/theme\" Target=\"theme/theme1.xml\" Id=\"rId{}\" />",
        sheets + 2
    ));
    xml.raw("</Relationships>");
    xml.into_bytes()
}

/// `[Content_Types].xml`(`Manifest._write`)。
pub fn content_types(sheets: usize) -> Vec<u8> {
    const OFFICE: &str = "application/vnd.openxmlformats-officedocument";
    let mut xml = XmlWriter::new();
    xml.raw(concat!(
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">",
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\" />",
        "<Default Extension=\"xml\" ContentType=\"application/xml\" />",
    ));
    xml.raw(&format!(
        "<Override PartName=\"/xl/styles.xml\" ContentType=\"{OFFICE}.spreadsheetml.styles+xml\" />"
    ));
    xml.raw(&format!(
        "<Override PartName=\"/xl/theme/theme1.xml\" ContentType=\"{OFFICE}.theme+xml\" />"
    ));
    xml.raw(concat!(
        "<Override PartName=\"/docProps/core.xml\" ",
        "ContentType=\"application/vnd.openxmlformats-package.core-properties+xml\" />",
    ));
    xml.raw(&format!(
        "<Override PartName=\"/docProps/app.xml\" ContentType=\"{OFFICE}.extended-properties+xml\" />"
    ));
    for number in 1..=sheets {
        xml.raw(&format!(
            "<Override PartName=\"/xl/worksheets/sheet{number}.xml\" ContentType=\"{OFFICE}.spreadsheetml.worksheet+xml\" />"
        ));
    }
    xml.raw(&format!(
        "<Override PartName=\"/xl/workbook.xml\" ContentType=\"{OFFICE}.spreadsheetml.sheet.main+xml\" />"
    ));
    xml.raw("</Types>");
    xml.into_bytes()
}

/// `xl/worksheets/sheetN.xml`(`WorksheetWriter.write`)。
///
/// - 寸法は `A1:<最後の列><最後の行>`。列幅は列ごとに 1 つの `<col>`(`%.16g` の整数)。
/// - 1 行目は表の列数(全部の行の最大)まで太字のセル(`s="1"`)。値の無いセルは `t="n"` の空の要素。
/// - 2 行目からは、行にあるセルだけを書く。空文字は `t="inlineStr"` の空の要素。
pub fn sheet_xml(sheet: &Sheet) -> Vec<u8> {
    let mut xml = XmlWriter::new();
    xml.raw(concat!(
        "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">",
        "<sheetPr><outlinePr summaryBelow=\"1\" summaryRight=\"1\" /><pageSetUpPr /></sheetPr>",
    ));
    let last_column = letter(sheet.max_column);
    xml.raw(&format!(
        "<dimension ref=\"A1:{last_column}{}\" />",
        sheet.rows.len()
    ));
    xml.raw(SHEET_VIEWS_AND_FORMAT);
    xml.raw("<cols>");
    for (index, width) in sheet.widths.iter().enumerate() {
        let number = index + 1;
        xml.raw(&format!(
            "<col width=\"{width}\" customWidth=\"1\" min=\"{number}\" max=\"{number}\" />"
        ));
    }
    xml.raw("</cols><sheetData>");
    for (index, row) in sheet.rows.iter().enumerate() {
        let number = index + 1;
        xml.raw(&format!("<row r=\"{number}\">"));
        if number == 1 {
            for column in 1..=sheet.max_column {
                cell(&mut xml, column, number, row.get(column - 1), true);
            }
        } else {
            for (offset, value) in row.iter().enumerate() {
                cell(&mut xml, offset + 1, number, Some(value), false);
            }
        }
        xml.raw("</row>");
    }
    xml.raw(concat!(
        "</sheetData><pageMargins left=\"0.75\" right=\"0.75\" top=\"1\" bottom=\"1\" ",
        "header=\"0.5\" footer=\"0.5\" /></worksheet>",
    ));
    xml.into_bytes()
}

/// 表の列の範囲(1〜18278)の列名。範囲は組み立ての段で確かめてある。
fn letter(column: usize) -> String {
    column_letter(column).unwrap_or_default()
}

/// `etree_write_cell`。`bold` は 1 行目(書式 1)。
fn cell(xml: &mut XmlWriter, column: usize, row: usize, value: Option<&Value>, bold: bool) {
    xml.raw(&format!("<c r=\"{}{row}\"", letter(column)));
    if bold {
        xml.raw(" s=\"1\"");
    }
    match value {
        None => xml.raw(" t=\"n\" />"),
        Some(Value::Text(text)) if text.is_empty() => xml.raw(" t=\"inlineStr\" />"),
        Some(Value::Text(text)) => {
            xml.raw(" t=\"inlineStr\"><is><t");
            if needs_preserve(text.as_code_points()) {
                xml.raw(" xml:space=\"preserve\"");
            }
            xml.raw(">");
            xml.text(text.as_code_points());
            xml.raw("</t></is></c>");
        }
        Some(Value::Formula(text)) => {
            xml.raw("><f>");
            xml.text(&text.as_code_points()[1..]);
            xml.raw("</f><v /></c>");
        }
        Some(Value::Error(text)) => {
            xml.raw(" t=\"e\"><v>");
            xml.text(text.as_code_points());
            xml.raw("</v></c>");
        }
    }
}

/// `openpyxl.xml.functions.whitespace`: `strip()` が空でなく、元と違うときだけ `xml:space`。
fn needs_preserve(text: &[u32]) -> bool {
    let stripped = super::markdown::strip(text);
    !stripped.is_empty() && stripped.len() != text.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn escaped_text(text: &[u32]) -> Vec<u8> {
        let mut xml = XmlWriter::new();
        xml.text(text);
        xml.into_bytes()
    }

    fn escaped_attribute(text: &[u32]) -> Vec<u8> {
        let mut xml = XmlWriter::new();
        xml.attribute(text);
        xml.into_bytes()
    }

    fn points(text: &str) -> Vec<u32> {
        text.chars().map(u32::from).collect()
    }

    #[test]
    fn escaping_matches_elementtree() {
        assert_eq!(
            escaped_text(&points("a & b < c > \"d\" 'e'\n\tf\x01")),
            b"a &amp; b &lt; c &gt; \"d\" 'e'\n\tf\x01"
        );
        assert_eq!(
            escaped_attribute(&points("A & B < C > \"D\" 'E'\r\n\t\x01")),
            b"A &amp; B &lt; C &gt; &quot;D&quot; 'E'&#13;&#10;&#09;\x01"
        );
        // サロゲートは 10 進の文字参照(xmlcharrefreplace)。&# はエスケープしない。
        assert_eq!(
            escaped_attribute(&[0x72, 0xdce9, 0x26, 0xd800, 0x1f600]),
            "r&#56553;&amp;&#55296;\u{1f600}".as_bytes()
        );
        assert_eq!(escaped_text(&points("]]>")), b"]]&gt;");
    }

    #[test]
    fn column_letters_follow_openpyxl() {
        for (index, expected) in [
            (1, Some("A")),
            (26, Some("Z")),
            (27, Some("AA")),
            (702, Some("ZZ")),
            (703, Some("AAA")),
            (16384, Some("XFD")),
            (16385, Some("XFE")),
            (18278, Some("ZZZ")),
            (18279, None),
            (0, None),
        ] {
            assert_eq!(column_letter(index).as_deref(), expected, "{index}");
        }
    }

    #[test]
    fn times_follow_isoformat() {
        assert_eq!(w3cdtf(0), "1970-01-01T00:00:00Z");
        assert_eq!(w3cdtf(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(w3cdtf(1_794_441_599), "2026-11-11T23:59:59Z");
        assert_eq!(w3cdtf(-1), "1969-12-31T23:59:59Z");
        assert_eq!(w3cdtf(253_402_300_799), "9999-12-31T23:59:59Z");
    }

    #[test]
    fn fixed_parts_have_the_legacy_sizes() {
        assert_eq!(THEME_XML.len(), 10140);
        assert_eq!(APP_XML.len(), 205);
        assert_eq!(STYLES_XML.len(), 2752);
        let core = core_xml(0, 1);
        let core = String::from_utf8(core).unwrap();
        assert!(core.contains(">1970-01-01T00:00:00Z</dcterms:created>"));
        assert!(core.contains(">1970-01-01T00:00:01Z</dcterms:modified>"));
    }

    #[test]
    fn preserve_follows_openpyxl_whitespace() {
        assert!(needs_preserve(&points(" a")));
        assert!(needs_preserve(&points("x\n")));
        assert!(needs_preserve(&points("\u{3000}a")));
        assert!(!needs_preserve(&points("a b")));
        assert!(!needs_preserve(&points("\n")));
        assert!(!needs_preserve(&points("  ")));
    }
}
