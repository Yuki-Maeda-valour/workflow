#![forbid(unsafe_code)]
use dev_workflow_core::loop_state::{
    self, CodepointString, LegacyIntLimit, PatternInput, PatternItem,
};
use dev_workflow_core::{CommandError, CommandOutput, ExitCode, execute};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    path::PathBuf,
    process::{Child, Command},
    time::{Duration, Instant},
};
// The graph is observable only here. Ordinary fixtures call the public library.
#[allow(dead_code)]
#[path = "../src/loop_state/mod.rs"]
mod observation;
fn text(value: &Value) -> CodepointString {
    CodepointString::try_from_codepoints(
        value
            .as_array()
            .unwrap()
            .iter()
            .map(|v| u32::try_from(v.as_u64().unwrap()).unwrap())
            .collect(),
    )
    .unwrap()
}
fn graph(raw: &[u8], limit: i64) -> Value {
    use observation::graph::{Data, compose};
    let Ok(g) = compose(
        raw,
        observation::LegacyIntLimit::from_python_value(limit).unwrap(),
    ) else {
        return json!({"accepted":false});
    };
    let mut nodes = Vec::new();
    let mut ids = HashMap::new();
    let mut pending = VecDeque::new();
    if !g.nodes.is_empty() {
        ids.insert(0, 0);
        pending.push_back(0);
    }
    while let Some(id) = pending.pop_front() {
        let mut reference = |id: usize| -> usize {
            if let Some(&id) = ids.get(&id) {
                id
            } else {
                let next = ids.len();
                ids.insert(id, next);
                pending.push_back(id);
                next
            }
        };
        let node = &g.nodes[id];
        nodes.push(match &node.data{
            Data::Scalar(v)=>json!({"kind":"scalar","tag":node.tag,"value":v}),
            Data::Sequence(v)=>json!({"kind":"sequence","tag":node.tag,"value":v.iter().map(|&id|reference(id)).collect::<Vec<_>>()}),
            Data::Mapping(v)=>json!({"kind":"mapping","tag":node.tag,"value":v.borrow().iter().map(|&(k,v)|[reference(k),reference(v)]).collect::<Vec<_>>()}),
        });
    }
    json!({"accepted":true,"nodes":nodes})
}
fn result(value: Result<Vec<CodepointString>, loop_state::ProfileError>) -> Value {
    match value {
        Ok(v) => {
            json!({"accepted":true,"patterns":v.iter().map(CodepointString::as_codepoints).collect::<Vec<_>>()})
        }
        Err(e) => json!({"accepted":false,"reason":e.message()}),
    }
}
fn fixture(row: &Value) -> Value {
    let value = match row["purpose"].as_str().unwrap() {
        "loop-profile" | "node-graph" => {
            let hex = row["input_hex"].as_str().unwrap();
            assert!(hex.len().is_multiple_of(2));
            let raw: Vec<_> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            if row["purpose"] == "node-graph" {
                graph(
                    &raw,
                    row["context"]["integer_max_str_digits"].as_i64().unwrap(),
                )
            } else {
                result(loop_state::profile_patterns(
                    &raw,
                    LegacyIntLimit::from_python_value(
                        row["context"]["integer_max_str_digits"].as_i64().unwrap(),
                    )
                    .unwrap(),
                ))
            }
        }
        "canonical" => {
            let input = &row["input"];
            let input = if input["kind"] == "list" {
                PatternInput::List(
                    input["items"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|item| {
                            if item["kind"] == "text" {
                                PatternItem::Text(text(&item["codepoints"]))
                            } else {
                                PatternItem::Other
                            }
                        })
                        .collect(),
                )
            } else {
                PatternInput::NotList
            };
            result(loop_state::canonical_secret_patterns(&input))
        }
        "matcher" => {
            json!({"matched":loop_state::secret_path(&text(&row["input"]["rel"]),&row["input"]["patterns"].as_array().unwrap().iter().map(text).collect::<Vec<_>>())})
        }
        purpose => panic!("unknown fixture purpose: {purpose}"),
    };
    json!({"id":row["id"],"result":value})
}
fn fixture_main() -> ExitCode {
    execute(|io| {
        let mut input = Vec::new();
        let mut buffer = [0; 8192];
        loop {
            let count = io
                .read_input(&mut buffer)
                .map_err(|e| CommandError::runtime(e.to_string()))?;
            if count == 0 {
                break;
            }
            input.extend_from_slice(&buffer[..count]);
        }
        let rows: Value =
            serde_json::from_slice(&input).map_err(|e| CommandError::runtime(e.to_string()))?;
        let mut stdout = serde_json::to_vec(
            &rows
                .as_array()
                .unwrap()
                .iter()
                .map(fixture)
                .collect::<Vec<_>>(),
        )
        .unwrap();
        stdout.push(b'\n');
        Ok(CommandOutput {
            stdout,
            stderr: Vec::new(),
            code: ExitCode::SUCCESS,
        })
    })
}
struct CheckerChild(Child);
impl Drop for CheckerChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        self.0.wait().expect("比較器の子を回収できません");
    }
}
fn main() -> std::process::ExitCode {
    if std::env::args().nth(1).as_deref() == Some("--r4s-fixture") {
        return std::process::ExitCode::from(fixture_main().as_u8());
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let mut child = CheckerChild(
        Command::new("python3")
            .arg("-B")
            .arg(root.join("native/ci/check_r4s.py"))
            .arg("--bridge")
            .arg(std::env::current_exe().unwrap())
            .current_dir(root)
            .spawn()
            .expect("R4S checker could not start"),
    );
    let deadline = Instant::now() + Duration::from_secs(300);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(status.success(), "R4S checker failed: {status}");
            return std::process::ExitCode::SUCCESS;
        }
        if Instant::now() >= deadline {
            child.0.kill().unwrap();
            child.0.wait().unwrap();
            panic!("R4S checker timed out");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
