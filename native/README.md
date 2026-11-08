# Rust の土台と共有の約束

このフォルダは段階 0a(#442)の土台と、段階 1a(#448)の開発用ソースです。
`resolve-task-dir` は段階 1a で追加しました。段階 0b の実行ファイルには含まれません。
現在の配布状況は [README.md](../README.md#e-rust-版を試す段階-0b) を参照してください。
内部の版は `0.1.0` です。配布プラグインの版とは別に管理します。
`probe --json` は実行環境とビルド情報を JSON で返します。
`probe --self-sha256` は自己の実行ファイル全体の SHA256 を返します。
どちらも末尾改行付きの標準出力と終了 0 を返します。
不正な引数は終了 2、自己のファイルを読めない場合などの実行時エラーは終了 1 です。
実行時に外部コマンド・ネットワーク・設定ファイルを使わず、状態ファイルを作りません。

## タスク保存先の解決(段階 1a)

`dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]` で保存先を解決します。

- `--project-root` は必須です。`--task-dir` の省略と空文字は区別します。
- 成功は JSON の標準出力と終了 0、解決失敗は JSON の標準出力と終了 1、候補複数は JSON の標準出力と終了 2 です。これらの標準エラーは空です。
- 引数の検査は Python 3.10 の旧版に合わせます。ファイルの作成・変更、タスク本文の読み取りは行いません。
- 一覧を読めても子の情報を取得できない権限条件では、旧版の未処理の `PermissionError` と Rust の JSON エラーの差を認めます。[設計の記録](../docs/design.md#タスク保存先の解決段階-1a)に条件と出力先を記しています。
- 旧版との比較の条件と、通常利用者での権限検査は [検査手順](../docs/testing.md#タスク保存先の比較段階-1a)にあります。

## 部品とコマンドの登録

- `crates/cli`: 引数を `OsString` のまま受け取る入口です。
- `crates/core`: 終了コード・出力処理・ビルド情報を持ちます。
- `crates/platform`: OS 固有の処理を置く境界です。段階 0 では処理を持ちません。
- `platform` 以外の自作 crate(Rust の部品単位)は `#![forbid(unsafe_code)]` を使います。
- `platform` は `#![deny(unsafe_op_in_unsafe_fn)]` を使います。公開する関数は安全な関数に限ります。

`crates/cli/src/commands/mod.rs` の `COMMANDS: &[Command]` が名前と関数の固定表です。
`Command` は `name: &'static str` と `run: fn(&[OsString]) -> CommandResult` を持ちます。
関数へ渡す引数にはコマンド名を含めません。`probe` と `resolve-task-dir` は登録済みです。
`resolve-task-dir` は `commands/resolve_task_dir/` に置きます。今後の機能も `commands/<名前>/mod.rs` に追加します。
`probe.rs` は段階 0 の試しのコマンドとして単独ファイルに置きます。

`--version` と `--help` は `main.rs` が受け持ちます。
引数なし、未知のコマンド、ほかの引数を伴う全体のオプションは終了 2 です。
コマンド固有の引数検査は各関数が受け持ちます。

## 終了コードと出力

`dev_workflow_core` は次の型と関数を公開します。

| 名前 | 約束 |
|---|---|
| `ExitCode` | `Success = 0`、`Runtime = 1`、`Usage = 2` |
| `CommandOutput` | 完成した `stdout: Vec<u8>`・`stderr: Vec<u8>` と `code: ExitCode`。`stdout(...)` は標準出力と終了 0 を作る |
| `CommandResult` | `Result<CommandOutput, CommandError>`。`Ok` でも終了 1・2 と完成した出力を返せる |
| `CommandError` | `Runtime(String)` と `Usage(String)`。`runtime(...)`・`usage(...)` で作れる |
| `emit(result)` | 標準出力・標準エラーへ書き、`ExitCode` を返す |
| `emit_to(result, stdout, stderr)` | 書き込み先を指定する同じ処理。テストにも使う |

- 各関数は出力前に処理を終えます。`CommandOutput` のバイト列には接頭辞や改行を追加しません。
- `CommandError` は標準エラーへ `dev-workflow: <理由>\n` の形で書きます。
- `resolve-task-dir` の解決失敗は `CommandOutput` で JSON の標準出力を返します。既存の `probe` の出力と終了値は変えません。
- 書き込みと `flush` はどちらも確認します。`BrokenPipe` も終了 1 です。
- 標準出力の書き込み失敗は標準エラーへ理由を出します。既に書けたバイト列は取り消せません。
- 標準エラーにも書けないときは終了 1 だけを返します。追加の出力や panic は起こしません。

`build_info` の定数は `NATIVE_VERSION`・`SOURCE_INPUT_SHA256`・`RUSTC` です。
`RUSTC` は `rustc --version` の末尾改行を除いた値です。
`--version` は `dev-workflow 0.1.0 (source_input_sha256: <小文字64桁>)\n` を返します。
`--help` は `--version`・`--help`・`probe --json`・`probe --self-sha256`・`resolve-task-dir` の 5 つの使い方を表示します。

## ソース入力の SHA256

Task 2.2 の Python の計算も、次のバイト列をそのまま使います。
実装は `crates/core/source_input.rs` です。実行ファイルからは呼びません。

1. `CARGO_MANIFEST_DIR` から `git rev-parse --show-toplevel` でリポジトリ直下を求めます。
2. その直下で `git ls-files -s -z -- native` を実行します。`-z` はパスの区切りを NUL にします。
3. stage が `0` の項目だけを認めます。Git のモードと相対パスを取り出します。
4. 相対パスは `native/` を含むリポジトリ直下からのパスです。UTF-8 のバイト列の昇順で並べます。
5. 本文は作業ツリーから読みます。index(ステージした内容の置き場)の本文とオブジェクト名はハッシュに入れません。
6. 以下の順で連結したバイト列の SHA256 を取り、小文字の16進数64桁にします。

```text
ASCII("dev-workflow-source-input-v1") || 0x00 || U64BE(ファイル数)
各ファイルをパス順に:
    U64BE(パスのバイト数) || UTF8(相対パス)
    U64BE(モードのバイト数) || ASCII(Git のモード)
    U64BE(本文のバイト数) || 本文
```

`U64BE(n)` は符号なし64ビット整数を8バイトのビッグエンディアン(上位のバイトから先に並べる方式)で表した値です。
`||` はバイト列の連結です。追加の改行や区切りはありません。モードは6文字なので長さは常に6です。
空の本文も長さ0を入れます。本文の改行や文字コードは変えません。

照合用の例は1ファイルだけの `native/a`・モード `100644`・本文 `b"hello\0\n"` です。
期待値は `64c7696a322f08896afd9e2f6e7d824dc1c780f16282b89d40fdacfbd79c3f06` です。

- モード `100644`・`100755` は通常ファイルの本文を読みます。
- モード `120000` はリンク先の名前のバイト列を読みます。リンク先の本文は読みません。
- Linux と macOS のパスを対象にします。パスが UTF-8 でない場合と制御文字を含む場合は拒否します。
- 重複パス、空要素、`.`・`..`、競合中の項目、上記以外のモードは拒否します。
- 親フォルダがリンクの場合と、Git のモードと実体の種類が違う場合も拒否します。
- git の不在、コマンドの失敗、空の一覧、本文の読み取り失敗ではビルドを止めます。
- 追跡していないファイルと `native/` の外のファイルは含めません。生成物は追跡しないでください。
- commit の SHA・時刻・CI の実行番号は含めません。ビルド情報の定数にも置きません。

Git の全呼び出しに `base-commit.md` の安全用のオプションを同じ順で付けます。
`GIT_NO_LAZY_FETCH=1` と `GIT_TERMINAL_PROMPT=0` も付けます。
`build.rs` は追跡ファイル全部と `git rev-parse --git-path index` が示すパスを `cargo:rerun-if-changed` に渡します。
相対パスはすべて同じリポジトリ直下を基準に解決します。

## ビルドと検証

Rust は `rust-toolchain.toml` で `1.99.1` に固定しています。
`Cargo.lock` は共有します。直接依存は `sha2`・`serde`・`serde_json` だけです。
共有の `Cargo.toml`・`Cargo.lock`・登録の表・core の公開する型を変えるときは Task 1.1 の担当へ返してください。

次のコマンドは `native/` で実行します。
新しいソースは先に Git の追跡へ加える必要があります。空の一覧ではビルドしません。

```sh
export CARGO_TARGET_DIR="$(mktemp -d)"
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --release --locked --target x86_64-unknown-linux-musl
```

release の設定は `codegen-units=1`・`opt-level=3`・`lto="fat"`・`strip="symbols"`・`debug=0`・`incremental=false` です。
panic の設定は既定のままです。Linux のリンカーは Rust の musl 向けの既定を使います。
`.cargo/config.toml` と `.cargo/rustc-wrapper.sh` がソース・依存・出力先の絶対パスを固定名へ置き換えます。
wrapper は bash 3.2 以上で動きます。Cargo が渡す `CARGO_BIN_EXE_dev-workflow` もコンパイラへ引き継ぎます。
CI(変更ごとの自動検査)から `RUSTFLAGS` や最適化設定を渡す必要はありません。
外部の設定や環境変数でビルドの設定を上書きしないでください。
再現性の比較は同じ道具と設定を使い、別々の空の `CARGO_TARGET_DIR` で作った最終実行ファイルに `cmp` を使います。

テストでは、一時リポジトリの `native/` の外のファイルを変更し、計算値が不変であることを確認します。
追跡した本文・パス・Git のモードの変更では計算値が変わることも確認します。
