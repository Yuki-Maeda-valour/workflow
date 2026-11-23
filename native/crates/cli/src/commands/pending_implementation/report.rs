//! 結果の形(旧版の`result`の辞書)・確認値2種・判定(旧版の`list_digest`・`identity_digest`・
//! `decide_outcome`)。JSONは旧版の`canonical_json`と同じ正準の形で書く。

use super::support::json::{self, Value};
use super::support::text::{self, PyText};
use super::system::{Exc, System};
use super::{ROOT_IDS, ScanError};

/// 旧版の`STAT_FIELDS`の7項目。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StatValues {
    pub dev: i128,
    pub ino: i128,
    pub mode: i128,
    pub uid: i128,
    pub size: i128,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
}

impl StatValues {
    pub fn of(stat: &dev_workflow_platform::r4g::fs::Stat) -> Self {
        Self {
            dev: stat.dev,
            ino: stat.ino,
            mode: i128::from(stat.mode),
            uid: stat.uid,
            size: stat.size,
            mtime_ns: stat.mtime_ns,
            ctime_ns: stat.ctime_ns,
        }
    }

    /// 旧版の`stable`: 番号・種類・持ち主。
    pub fn stable(&self) -> (i128, i128, i128, i128) {
        (self.dev, self.ino, self.mode & S_IFMT, self.uid)
    }
}

pub const S_IFMT: i128 = 0o170000;

/// 置き場1つ。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Root {
    pub id: &'static str,
    pub input_path: Option<Vec<u8>>,
    pub resolved_path: Option<Vec<u8>>,
    pub status: &'static str,
    pub reason: Option<&'static str>,
    /// `dev`・`ino`・`mode`・`uid`。
    pub identity: Option<[i128; 4]>,
    pub canonical_id: Option<&'static str>,
}

impl Root {
    pub fn skipped(id: &'static str) -> Self {
        Self {
            id,
            input_path: None,
            resolved_path: None,
            status: "skipped",
            reason: Some("unset"),
            identity: None,
            canonical_id: None,
        }
    }
}

/// 候補1つ。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub root_id: &'static str,
    pub path: Vec<u8>,
    pub name: Vec<u8>,
    pub kind: &'static str,
    pub values: StatValues,
    pub relation: &'static str,
    pub reason: Option<&'static str>,
    /// 印の`top`・`common`・`taskdir`のパス。
    pub owner: Option<[Vec<u8>; 3]>,
}

/// 旧版の`result`。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Doc {
    pub result: &'static str,
    pub cwd: Option<Vec<u8>>,
    pub complete: bool,
    pub roots: Vec<Root>,
    pub candidates: Vec<Candidate>,
    pub issues: Vec<Issue>,
    pub digests: Option<(String, String)>,
}

/// 旧版の`issue`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Issue {
    pub code: &'static str,
    pub root_id: Option<&'static str>,
    pub path: Option<Vec<u8>>,
    pub message: String,
}

impl Issue {
    pub fn of(error: &ScanError) -> Self {
        Self {
            code: error.code,
            root_id: error.root_id,
            path: error.path.clone(),
            message: error.message.clone(),
        }
    }
}

fn key(name: &str) -> PyText {
    text::from_str(name)
}

fn hex(bytes: &[u8]) -> Value {
    Value::str(
        &bytes
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
}

fn optional_hex(bytes: &Option<Vec<u8>>) -> Value {
    bytes.as_deref().map_or(Value::Null, hex)
}

/// `os.fsdecode`(UTF-8と`surrogateescape`)の文字列。
fn decoded(bytes: &[u8]) -> Value {
    Value::Str(text::decode(bytes))
}

fn optional_str(value: Option<&str>) -> Value {
    value.map_or(Value::Null, Value::str)
}

fn object(entries: Vec<(&str, Value)>) -> Value {
    Value::Object(
        entries
            .into_iter()
            .map(|(name, value)| (key(name), value))
            .collect(),
    )
}

fn identity(values: &Option<[i128; 4]>, with_mode: bool) -> Value {
    match values {
        None => Value::Null,
        Some([dev, ino, mode, uid]) => {
            let mut entries = vec![("dev", Value::int(dev)), ("ino", Value::int(ino))];
            if with_mode {
                entries.push(("mode", Value::int(mode)));
            }
            entries.push(("uid", Value::int(uid)));
            object(entries)
        }
    }
}

impl Root {
    /// 確認値の入力の7項目(`ROOT_DIGEST_KEYS`)。`with_mode`が偽なら`identity`から`mode`を外す。
    fn digest_json(&self, with_mode: bool) -> Value {
        object(vec![
            ("id", Value::str(self.id)),
            ("input_path_bytes_hex", optional_hex(&self.input_path)),
            ("resolved_path_bytes_hex", optional_hex(&self.resolved_path)),
            ("status", Value::str(self.status)),
            ("reason", optional_str(self.reason)),
            ("identity", identity(&self.identity, with_mode)),
            ("canonical_id", optional_str(self.canonical_id)),
        ])
    }

    fn json(&self) -> Value {
        object(vec![
            ("id", Value::str(self.id)),
            (
                "input_path",
                self.input_path.as_deref().map_or(Value::Null, decoded),
            ),
            ("input_path_bytes_hex", optional_hex(&self.input_path)),
            (
                "resolved_path",
                self.resolved_path.as_deref().map_or(Value::Null, decoded),
            ),
            ("resolved_path_bytes_hex", optional_hex(&self.resolved_path)),
            ("status", Value::str(self.status)),
            ("reason", optional_str(self.reason)),
            ("identity", identity(&self.identity, true)),
            ("canonical_id", optional_str(self.canonical_id)),
        ])
    }
}

impl Candidate {
    fn common(&self) -> Vec<(&'static str, Value)> {
        vec![
            ("root_id", Value::str(self.root_id)),
            ("path_bytes_hex", hex(&self.path)),
            ("name_bytes_hex", hex(&self.name)),
            ("kind", Value::str(self.kind)),
            ("dev", Value::int(self.values.dev)),
            ("ino", Value::int(self.values.ino)),
        ]
    }

    /// `list_sha256`の入力の11項目。
    fn list_json(&self) -> Value {
        let mut entries = self.common();
        entries.extend([
            ("mode", Value::int(self.values.mode)),
            ("uid", Value::int(self.values.uid)),
            ("size", Value::int(self.values.size)),
            ("mtime_ns", Value::int(self.values.mtime_ns)),
            ("ctime_ns", Value::int(self.values.ctime_ns)),
        ]);
        object(entries)
    }

    /// `identity_sha256`の入力の8項目。
    fn identity_json(&self) -> Value {
        let mut entries = self.common();
        entries.extend([
            ("uid", Value::int(self.values.uid)),
            ("reason", optional_str(self.reason)),
        ]);
        object(entries)
    }

    fn json(&self) -> Value {
        let mut entries = self.common();
        entries.extend([
            ("path", decoded(&self.path)),
            ("mode", Value::int(self.values.mode)),
            ("uid", Value::int(self.values.uid)),
            ("size", Value::int(self.values.size)),
            ("mtime_ns", Value::int(self.values.mtime_ns)),
            ("ctime_ns", Value::int(self.values.ctime_ns)),
            ("relation", Value::str(self.relation)),
            ("reason", optional_str(self.reason)),
            (
                "owner",
                match &self.owner {
                    None => Value::Null,
                    Some(paths) => {
                        let mut owner = Vec::new();
                        for (name, path) in ["top", "common", "taskdir"].iter().zip(paths) {
                            owner.push((key(name), decoded(path)));
                            owner.push((key(&format!("{name}_bytes_hex")), hex(path)));
                        }
                        Value::Object(owner)
                    }
                },
            ),
        ]);
        object(entries)
    }
}

impl Issue {
    fn json(&self) -> Value {
        object(vec![
            ("code", Value::str(self.code)),
            ("root_id", optional_str(self.root_id)),
            ("path_bytes_hex", optional_hex(&self.path)),
            ("message", Value::str(&self.message)),
        ])
    }
}

/// 旧版の`confirmation_candidates`。
pub fn confirmation_candidates(doc: &Doc) -> Vec<&Candidate> {
    doc.candidates
        .iter()
        .filter(|item| item.relation == "confirmation-required")
        .collect()
}

/// 旧版の`list_digest`。
pub fn list_digest(doc: &Doc) -> String {
    let payload = object(vec![
        ("schema_version", Value::int(1)),
        ("cwd_bytes_hex", optional_hex(&doc.cwd)),
        (
            "roots",
            Value::Array(
                doc.roots
                    .iter()
                    .map(|root| root.digest_json(true))
                    .collect(),
            ),
        ),
        (
            "candidates",
            Value::Array(
                confirmation_candidates(doc)
                    .into_iter()
                    .map(Candidate::list_json)
                    .collect(),
            ),
        ),
    ]);
    json::sha256(&payload)
}

/// 旧版の`identity_digest`。
pub fn identity_digest(doc: &Doc) -> String {
    let payload = object(vec![
        ("schema_version", Value::int(1)),
        ("digest", Value::str("identity")),
        ("cwd_bytes_hex", optional_hex(&doc.cwd)),
        (
            "roots",
            Value::Array(
                doc.roots
                    .iter()
                    .map(|root| root.digest_json(false))
                    .collect(),
            ),
        ),
        (
            "candidates",
            Value::Array(
                confirmation_candidates(doc)
                    .into_iter()
                    .map(Candidate::identity_json)
                    .collect(),
            ),
        ),
    ]);
    json::sha256(&payload)
}

/// 結果の1行(正準JSON。改行は付けない)。
pub fn result_json(doc: &Doc) -> String {
    let mut entries = vec![
        ("schema_version", Value::int(2)),
        ("result", Value::str(doc.result)),
        ("cwd", doc.cwd.as_deref().map_or(Value::Null, decoded)),
        ("cwd_bytes_hex", optional_hex(&doc.cwd)),
        ("complete", Value::Bool(doc.complete)),
        (
            "roots",
            Value::Array(doc.roots.iter().map(Root::json).collect()),
        ),
        (
            "candidates",
            Value::Array(doc.candidates.iter().map(Candidate::json).collect()),
        ),
        (
            "issues",
            Value::Array(doc.issues.iter().map(Issue::json).collect()),
        ),
    ];
    if let Some((list, identity)) = &doc.digests {
        entries.push(("list_sha256", Value::str(list)));
        entries.push(("identity_sha256", Value::str(identity)));
    }
    json::dumps(&object(entries))
}

/// 候補の並べ替え(置き場の順、名前のバイト列の順)。旧版の`sort`は安定。
pub fn sort_candidates(doc: &mut Doc) {
    doc.candidates.sort_by(|left, right| {
        let index = |id: &str| ROOT_IDS.iter().position(|root| *root == id);
        (index(left.root_id), &left.name).cmp(&(index(right.root_id), &right.name))
    });
}

/// 問題の並べ替え(`code`・置き場の順・パスの16進・文)。
pub fn sort_issues(doc: &mut Doc) {
    doc.issues.sort_by(|left, right| {
        let order = |issue: &Issue| {
            (
                issue.code,
                issue.root_id.map_or(-1, |id| {
                    ROOT_IDS
                        .iter()
                        .position(|root| *root == id)
                        .map_or(-1, |i| i as i64)
                }),
                issue
                    .path
                    .as_deref()
                    .map(|path| {
                        path.iter()
                            .map(|byte| format!("{byte:02x}"))
                            .collect::<String>()
                    })
                    .unwrap_or_default(),
                text::from_str(&issue.message),
            )
        };
        order(left).cmp(&order(right))
    });
}

/// 渡された確認値(2つは同時に渡せない)。値は小文字の64桁。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Acknowledged {
    None,
    List(String),
    Identity(String),
}

/// 重なりの注記(標準エラーの1行)。
pub const OVERLAP_LINE: &str = "重なりのある候補があるため、identity の確認値では続行できません。list_sha256 で確認してください。";

/// 旧版の`decide_outcome`。入出力をせず、`doc`を変えない。判定の`in OVERLAP_REASONS`だけを`sys`に問う。
pub fn decide_outcome(
    doc: &Doc,
    acknowledged: &Acknowledged,
    digests: &(String, String),
    sys: &mut dyn System,
) -> Result<(u8, &'static str, Vec<String>), Exc> {
    let mut notes = Vec::new();
    let confirm = confirmation_candidates(doc);
    let outcome = match acknowledged {
        Acknowledged::Identity(value) => {
            let mut overlapping = false;
            for item in &confirm {
                if sys.is_overlap_reason(item.reason)? {
                    overlapping = true;
                    break;
                }
            }
            if overlapping {
                notes.push(OVERLAP_LINE.to_string());
                "list-changed"
            } else if *value == digests.1 {
                "acknowledged"
            } else {
                "list-changed"
            }
        }
        Acknowledged::List(value) => {
            if *value == digests.0 {
                "acknowledged"
            } else {
                "list-changed"
            }
        }
        Acknowledged::None if !confirm.is_empty() => "confirmation-required",
        Acknowledged::None if !doc.candidates.is_empty() => "other-projects-only",
        Acknowledged::None => "none",
    };
    let others = doc.candidates.len() - confirm.len();
    if others > 0 {
        notes.insert(
            0,
            format!(
                "別プロジェクトの保護記録: {others} 件。所属の印で別のプロジェクトと確かめたため、止めません(名前とパスはJSONにだけ出します)。"
            ),
        );
    }
    let code = if matches!(outcome, "none" | "other-projects-only" | "acknowledged") {
        0
    } else {
        10
    };
    Ok((code, outcome, notes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_platform::r4g::OFlags;
    use dev_workflow_platform::r4g::fs::Stat;
    use serde_json::Value as Json;

    /// 旧版の関数の値を固定した表(check_r4g.py が今の旧版と照らす)。
    fn cases() -> Json {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../ci/r4g_fixtures/pending-function-cases.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    fn leak(text: &str) -> &'static str {
        Box::leak(text.to_string().into_boxed_str())
    }

    fn unhex(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
            .collect()
    }

    fn number(value: &Json) -> i128 {
        i128::from(value.as_i64().unwrap())
    }

    /// 旧版の`result`の辞書から、確認値と判定が読む項目だけを写す(ほかの項目は読まない)。
    fn doc_from(value: &Json) -> Doc {
        let roots = value["roots"]
            .as_array()
            .unwrap()
            .iter()
            .map(|root| Root {
                id: leak(root["id"].as_str().unwrap()),
                input_path: root["input_path_bytes_hex"].as_str().map(unhex),
                resolved_path: root["resolved_path_bytes_hex"].as_str().map(unhex),
                status: leak(root["status"].as_str().unwrap()),
                reason: root["reason"].as_str().map(leak),
                identity: root["identity"].as_object().map(|identity| {
                    ["dev", "ino", "mode", "uid"].map(|key| number(&identity[key]))
                }),
                canonical_id: root["canonical_id"].as_str().map(leak),
            })
            .collect();
        let candidates = value["candidates"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| Candidate {
                root_id: leak(item["root_id"].as_str().unwrap()),
                path: unhex(item["path_bytes_hex"].as_str().unwrap()),
                name: unhex(item["name_bytes_hex"].as_str().unwrap()),
                kind: leak(item["kind"].as_str().unwrap()),
                values: StatValues {
                    dev: number(&item["dev"]),
                    ino: number(&item["ino"]),
                    mode: number(&item["mode"]),
                    uid: number(&item["uid"]),
                    size: number(&item["size"]),
                    mtime_ns: number(&item["mtime_ns"]),
                    ctime_ns: number(&item["ctime_ns"]),
                },
                relation: leak(item["relation"].as_str().unwrap()),
                reason: item["reason"].as_str().map(leak),
                owner: None,
            })
            .collect();
        Doc {
            result: "internal",
            cwd: value["cwd_bytes_hex"].as_str().map(unhex),
            roots,
            candidates,
            ..Doc::default()
        }
    }

    #[test]
    fn digests_match_the_old_functions() {
        let cases = cases();
        let digests = cases["digests"].as_array().unwrap();
        assert_eq!(digests.len(), 21);
        for case in digests {
            let doc = doc_from(&case["result"]);
            assert_eq!(
                list_digest(&doc),
                case["list"].as_str().unwrap(),
                "{}",
                case["label"]
            );
            assert_eq!(
                identity_digest(&doc),
                case["identity"].as_str().unwrap(),
                "{}",
                case["label"]
            );
        }
        assert_eq!(
            digests[0]["list"].as_str().unwrap(),
            "c6846d4850a3703d5365dc73c53c70bb351d19a06aa118773f3816c1a929a990"
        );
    }

    /// ファイルにも時計にも触れない System(触れたら止める)。
    struct NoIo;

    impl System for NoIo {
        fn stat(&mut self, _: Option<i32>, _: &[u8]) -> Result<Stat, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn fstat(&mut self, _: i32) -> Result<Stat, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn open(&mut self, _: Option<i32>, _: &[u8], _: OFlags) -> Result<i32, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn close(&mut self, _: i32) -> Result<(), Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn read(&mut self, _: i32, _: usize) -> Result<Vec<u8>, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn readlink(&mut self, _: &[u8]) -> Result<Vec<u8>, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn scandir(&mut self, _: i32, _: usize) -> Result<(Vec<Vec<u8>>, Option<Exc>), Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn getcwd(&mut self) -> Result<Vec<u8>, Exc> {
            unreachable!("判定はファイルに触れない")
        }
        fn monotonic(&mut self) -> Result<f64, Exc> {
            unreachable!("判定は時計に触れない")
        }
        fn environ(&self, _: &[u8]) -> Option<Vec<u8>> {
            unreachable!("判定は環境を読まない")
        }
        fn check_interrupt(&mut self) -> Result<(), Exc> {
            unreachable!("判定は信号を確かめない")
        }
    }

    #[test]
    fn decisions_match_the_old_function_without_io() {
        let cases = cases();
        let decisions = cases["decisions"].as_array().unwrap();
        assert_eq!(decisions.len(), 15);
        for case in decisions {
            let doc = doc_from(&case["result"]);
            let before = doc.clone();
            let acknowledged = match case["acknowledged"].as_object().unwrap().iter().next() {
                None => Acknowledged::None,
                Some((kind, value)) if kind == "list" => {
                    Acknowledged::List(value.as_str().unwrap().to_string())
                }
                Some((_, value)) => Acknowledged::Identity(value.as_str().unwrap().to_string()),
            };
            let digests = (
                case["digests"]["list"].as_str().unwrap().to_string(),
                case["digests"]["identity"].as_str().unwrap().to_string(),
            );
            let (code, outcome, notes) =
                decide_outcome(&doc, &acknowledged, &digests, &mut NoIo).unwrap();
            let expected = &case["expected"];
            assert_eq!(i64::from(code), expected[0].as_i64().unwrap(), "{case}");
            assert_eq!(outcome, expected[1].as_str().unwrap(), "{case}");
            let expected_notes: Vec<&str> = expected[2]
                .as_array()
                .unwrap()
                .iter()
                .map(|note| note.as_str().unwrap())
                .collect();
            assert_eq!(notes, expected_notes, "{case}");
            assert_eq!(doc, before, "判定は結果を書き換えない");
        }
    }
}
