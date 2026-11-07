#![forbid(unsafe_code)]

mod source_input;

use std::env;
use std::path::Path;
use std::process::{Command, Stdio};

fn main() {
    if let Err(error) = build() {
        eprintln!("dev-workflow のビルド情報を作れません: {error}");
        std::process::exit(1);
    }
}

fn build() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=source_input.rs");
    for name in [
        "GIT_INDEX_FILE",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }

    let manifest_dir =
        env::var_os("CARGO_MANIFEST_DIR").ok_or("CARGO_MANIFEST_DIR がありません")?;
    let input = source_input::SourceInput::discover(Path::new(&manifest_dir))?;
    println!("cargo:rerun-if-changed={}", input.index.display());
    for entry in &input.entries {
        println!(
            "cargo:rerun-if-changed={}",
            input.root.join(&entry.path).display()
        );
    }
    let hash = input.sha256()?;

    let rustc = env::var_os("RUSTC").ok_or("RUSTC がありません")?;
    let output = Command::new(rustc)
        .arg("--version")
        .stdin(Stdio::null())
        .output()?;
    if !output.status.success() {
        return Err("rustc --version が失敗しました".into());
    }
    let version = std::str::from_utf8(&output.stdout)?.trim_end();
    if !version.starts_with("rustc 1.99.1 ") || version.contains(['\r', '\n']) {
        return Err("Rust 1.99.1 が必要です".into());
    }
    println!("cargo:rustc-env=DEV_WORKFLOW_SOURCE_INPUT_SHA256={hash}");
    println!("cargo:rustc-env=DEV_WORKFLOW_RUSTC={version}");
    Ok(())
}
