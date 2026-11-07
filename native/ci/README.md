# ビルドと CI の入口

このフォルダは Issue #442 の Task 2.2 を受け持ちます。
配るプラグインは変更しません。最後に作る `native-dist.tar` は入口を含まない 5 ファイルです。

## コマンド

リポジトリ直下から実行します。Python 3.10 以上と標準ライブラリだけを使います。
`CARGO_TARGET_DIR` にはリポジトリの外のフォルダを指定してください。
CI では `RUNNER_TEMP/native-ci` が既定です。`CARGO_HOME` は呼び出し側の値を保ちます。

```sh
python3 -B native/ci/check_workflow.py
python3 -B native/ci/source_input_hash.py --root .
python3 -B native/ci/build.py test
python3 -B native/ci/build.py prepare --platform linux-x86_64
python3 -B native/ci/build.py build --platform linux-x86_64
```

- `prepare`: Rust `1.99.1`・`rustfmt`・`clippy`・対象の標準ライブラリを用意します。
- `build`: 書式・clippy・Rust テストの後に 2 回ビルドします。今回作った 2 本を `check_probe.py` で検査します。
- `reproduce`: 2 回のビルドと比較だけを行います。`probe` とその補助が未結合の段階でも使えます。
- `combine`: Mac の 2 回分をそれぞれ `lipo` で結合します。固定識別子で簡易署名し、署名を検査してから `cmp` で比べます。
- `verify-universal`: 同じ最終 Mac ファイルを受け取り、署名の検査と `check_probe.py` を行います。署名し直しません。
- `package`: Linux 2 本と最終 Mac 1 本を集め、`make_dist.py` の検査を通して tar に包みます。

Linux で指定する名前は `linux-x86_64`・`linux-arm64` です。
Mac は `macos-x86_64`・`macos-arm64` です。対象と同じ OS・CPU でビルドします。
転送の材料は `--downloads <フォルダ>` で指定できます。既定は出力先の `downloads/` です。

2 回のビルドは別々の空のフォルダを使います。
最適化とパスの置き換えは `native/Cargo.toml`・`native/.cargo/` の設定を使います。
`RUSTFLAGS` などの外部上書きは拒否します。
Mac の両方で `MACOSX_DEPLOYMENT_TARGET=11.0`・`ZERO_AR_DATE=1` を指定します。
時刻は `SOURCE_DATE_EPOCH=0` に固定します。実行番号や commit は実行ファイルへ入れません。

Linux では Rust に付属する musl を使います。`musl-gcc` や C の追加依存は導入しません。
ELF の CPU、動的ローダー指定の不在、共有ライブラリ依存の不在を実行前に検査します。
musl の版は Rust `1.99.1` の [musl-toolchain.sh](https://github.com/rust-lang/rust/blob/1.99.1/src/ci/docker/scripts/musl-toolchain.sh) と
[musl.sh](https://github.com/rust-lang/rust/blob/1.99.1/src/ci/docker/scripts/musl.sh) の `1.2.5` です。
Rust 側の修正を含む実体を識別できるように、付属する各ファイルの SHA256 も記録します。

## 成果物と他 Task との結合

`build-info.json` の必須項目は次の形です。
`artifacts` と `SHA256SUMS` はコピー後の 3 本から同時に作ります。

```json
{
  "schema_version": 1,
  "source_commit": "40 桁の commit SHA",
  "source_input_sha256": "64 桁の SHA256",
  "cargo_lock_sha256": "64 桁の SHA256",
  "rustc": "rustc --version の出力",
  "targets": {
    "x86_64-unknown-linux-musl": {"linker": "版", "sdk": "付属ライブラリの説明"},
    "aarch64-unknown-linux-musl": {"linker": "版", "sdk": "付属ライブラリの説明"},
    "x86_64-apple-darwin": {"linker": "版", "sdk": "版"},
    "aarch64-apple-darwin": {"linker": "版", "sdk": "版"}
  },
  "ci": {"run_id": "実行番号", "run_url": "実行の URL"},
  "artifacts": [
    {"path": "linux-x86_64/dev-workflow", "sha256": "64 桁の SHA256"},
    {"path": "linux-arm64/dev-workflow", "sha256": "64 桁の SHA256"},
    {"path": "macos-universal/dev-workflow", "sha256": "64 桁の SHA256"}
  ]
}
```

対象ごとの記録には Rust・Cargo・リンカー・musl・Xcode・SDK・設定を加えます。
`signing` には固定識別子、署名時刻を付けない設定、署名に使った道具の SHA256 と OS・Xcode の版を置きます。
対象間でソースの commit・入力・Cargo.lock・Rust の版が違う材料は組み合わせません。
tar の取り出しではファイル名と数を限定します。リンクや権限違い、容量超過を拒否します。

- `make_dist.py` はリポジトリ直下の `scripts/validate.py` を `importlib` で読みます。
  `check_native_bundle(path, "artifact")` が返す問題の一覧が空のときだけ出力先を作ります。
  入力の実行権限を自動修復しません。実行ファイルを検査のために起動しません。
- `check_probe.py` は `scripts/support_native.py` の `require_native_binary()`・`run_command()`・`snapshot_tree()` を使います。
  実行ファイルの指定が無い場合は失敗します。
  ソース入力の独立した計算値、実行 CPU、自己ハッシュ、出力、終了コード、作業フォルダの無変更を確かめます。
- この 2 つの依存が未結合の作業ツリーでは、単体テストが補助をモックに置き換えます。
  実物を使う結合確認を済ませたとは扱いません。

単体テストは担当範囲の中に置いています。次のどちらでも実行できます。
リポジトリ直下の `scripts/test_*.py` への自動収集とは別に、CI の最初の job が必ず実行します。

```sh
python3 -B native/ci/build.py test
(cd native/ci && python3 -B -m unittest discover -s scripts -p 'test_native_ci.py')
```

## ワークフローの検査

`.github/workflows/` の全 `*.yml`・`*.yaml` を検査します。
標準ライブラリで曖昧さなく読むため、YAML のうち JSON と共通の書き方だけを使います。
通常の YAML のブロック表記・アンカーなどは検査不能として失敗させます。
JSON の重複キーも拒否します。新しい CI も同じ形式で追加してください。

- イベントは `pull_request`・`push`・`workflow_dispatch` の 3 つです。
- 権限は `contents: read` だけです。秘密情報・書き込み権限・キャッシュを渡しません。
- checkout は PR のソース SHA とリポジトリを明示します。`persist-credentials: false` を必須にします。
- 各 job は checkout の直後に `check_workflow.py` を呼びます。
- `run` は `native/ci/` のスクリプト 1 本を呼ぶだけです。ビルド設定は書きません。
- Mac の 2 種類の実行確認が成功するまで、配布用 tar を作りません。

外部 Action は 2026-11-08 に `gh api` でタグを解決しました。
採用した SHA の commit API と、その SHA の `action.yml` が存在することも確認しました。

| Action | タグ | 確認した commit SHA |
|---|---|---|
| `actions/checkout` | `v4` | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/upload-artifact` | `v4` | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/download-artifact` | `v4` | `d3f86a106a0bac45b974a628896c90dbdf5c8093` |

確認に使う API は `repos/actions/<名前>/commits/v4`、`repos/actions/<名前>/commits/<SHA>`、
`repos/actions/<名前>/contents/action.yml?ref=<SHA>` です。
更新するときは 3 つを確認してから `check_workflow.py` の記録とワークフローを同時に変えます。
