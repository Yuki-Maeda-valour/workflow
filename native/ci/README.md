# ビルドと CI の入口

このフォルダは Rust のビルドと検査を受け持ちます。段階 1a(#448)からタスク保存先の旧版との比較も行います。
配るプラグインは変更しません。最後に作る `native-dist.tar` は入口を含まない 5 ファイルです。

## コマンド

リポジトリ直下から実行します。CIの補助はPython 3.10以上と標準ライブラリを使います。R4Sの比較には`requirements-dev.txt`の開発依存も必要です。
旧版との比較を含む `build`・`reproduce`・`verify-universal` は、Python 3.10 系と root 以外の通常利用者での実行が必要です。
`CARGO_TARGET_DIR` にはリポジトリの外のフォルダを指定してください。
CIの`build.py`呼出しは、コマンド名を先に置き、その後にオプションを指定します。オプション先行形と`--`を先に置く形はCI定義の検査で拒否します。
CI では `RUNNER_TEMP/native-ci` が既定です。`CARGO_HOME` は呼び出し側の値を保ちます。

```sh
python3 -B native/ci/check_workflow.py
python3 -B native/ci/source_input_hash.py --root .
python3 -B native/ci/build.py test
python3 -B native/ci/build.py prepare --platform linux-x86_64
python3 -B native/ci/build.py build --platform linux-x86_64
```

- `prepare`: 実行中のPythonで`pip install -r requirements-dev.txt`を行い、Rust `1.99.1`・`rustfmt`・`clippy`・対象の標準ライブラリを用意します。
- `build`: 書式・clippy・Rust テストの後に 2 回ビルドします。RustテストにはR4Sとtask-digestの専用比較も含まれます。各回の製品用実行物を `check_probe.py` と `check_resolve_task_dir.py` で検査します。R4BとR4Rのexample単体検査、R4Rの対応表の照合、各回の `r4b_contract`・`r4r_contract` の専用比較も必須です。
- `reproduce`: 書式・clippy・Rust テストを省き、2 回のビルドと比較、各回の製品用実行物とR4B・R4R検査用実行物の上記の検査を行います。
- `combine`: Mac の 2 回分をそれぞれ `lipo` で結合します。固定識別子で簡易署名し、署名を検査してから `cmp` で比べます。
- `verify-universal`: 同じ最終 Mac ファイルを受け取り、署名の検査と `check_probe.py`・`check_resolve_task_dir.py` を行います。R4B・R4R検査用実行物の署名検査と専用比較も行います。Intel と Apple Silicon の両方で実行し、署名し直しません。
- CIの工程全体の上限は`build`が60分、`verify-universal`が30分です。Mac IntelでR4Bの1回の比較に838.626秒かかり、2回比較する`build`が従来の40分で打ち切られたため延ばしました。署名後の工程も製品用比較とR4B比較を各1回行うため、従来の15分から延ばしました。[実測と根拠](../../docs/testing.md#4-種類の-ci-と成果物)を参照してください。Macの`build`は、run 43085911008で新上限の中で終わりました(x86_64が44分46秒、arm64が28分35秒)。`verify-universal`は、run 43085911008でMac arm64が13分9秒、Mac x86_64が17分8秒で成功しました(上限30分)。同じcommitのpush用のrun 43085900763では、Mac x86_64が26分39秒で、上限まで残り約3分でした。2つのrunとも`distribution`の工程は待ちのまま始まらず、GitHub側の実行機の割り当ての問題と推測しています。推測の根拠(HTTP 502の記録と、mainの過去のrunの`distribution`の成功)は [testing.md](../../docs/testing.md#pr用ciの記録run-43085911008) にあります。
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

## R4Sの必須比較

- #454の調査を基に、#462でloop-state用の純粋部品を実装しました。`core`の`loop_state_profile`テストが、標準の`cargo test --locked`から`check_r4s.py --bridge <今回のテスト実行物>`を必ず起動します。
- Linux2種・Mac2種の`build`はPython 3.10.11を用意し、`prepare`でPyYAML 6.0.3を含む開発依存を入れます。比較はUnicode 13.0.0と非rootも要求します。依存不在や失敗を省略しません。
- 比較対象は固定634件、追加218件と56件、JSON境界4件、旧用途94件、Unicode全域と公開matcherの5716方向です。自己検査20件には実装故障10種を含みます。内部例外が旧版の既知の拒否へ変わって見える場合も不合格にします。
- 旧用途94件の確認はRustへの移植ではありません。Pythonの再帰上限だけの承認済み差を、固定した入力と別期待で検査します。[比較の契約と手順](../../docs/testing.md#loop-state用の純粋部品の検査r4s)に従ってください。
- `build.py test`はCI補助の検査です。R4Sの実比較はCargoの経路で行います。`reproduce`はRustテストを省くため、その代わりにはなりません。公開CLIや配布物に比較用の入口はありません。
- 2026-11-10のLinux実装検証は成功しました。この時点でR4SのPR用必須CIは未実行です。4環境へ検査を必須化したことと、その成功を分けて記録します。Mac実機・最低OSも未確認です。

## task-digestの必須比較(R4A)

- #460の専用入口は[専用テスト](../crates/cli/tests/task_digest.rs)です。`harness = false`の通常起動で`check_task_digest.py --fixture <今回のテスト実行物>`と比較器の自己検査を必ず実行します。製品CLIへは登録していません。
- Linux2種・Mac2種の`build`は、Python3.10.11・非rootで`quality()`の`cargo test --locked`からこの比較を実行します。0件、時間切れ、入力欠落、比較器の失敗やskipを成功にしません。
- 旧Pythonと固定期待に対し、本文・値・生の入出力・終了値・入力無変更を比較します。承認済みの特殊入出力3条件と対照、環境設定の境界は通常件数と分けます。
- `COLUMNS`の4300/4301桁と、有効な`PYTHONINTMAXSTRDIGITS`の無制限・最小値・上書き値も実PTYで比較します。
- `build.py test`と`reproduce`、配布物のprobe/保存先比較は、この専用比較の代わりになりません。
- 2026-11-10の手元Linux x86_64・Python3.10.12では専用比較と自己検査33件が成功し、未実施0件でした。Mac等の成功はこの実測から判断しません。[分類別件数・限定差・未確認条件](../../docs/testing.md#task-digestの比較r4a)を参照してください。

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
- `check_resolve_task_dir.py` は、旧 Python 版と今回作った版の出力・終了値・ファイルの無変更を比較します。実行ファイルの指定が無い・不正な場合や、Python 3.10 系でない場合は終了 1 です。
  通常利用者での権限検査と、承認済みの `0400`・非 UTF-8 出力の差は [検査手順](../../docs/testing.md#タスク保存先の比較段階-1a)に従います。
  非 UTF-8 は `utf-8:strict` と `utf-8:surrogateescape` の専用ケースで検査し、通常の比較の条件を維持します。
- 配布物のテストは、偽の ELF・Mach-O を材料に組み立て、実際の `validate.py` の関数で検査します。
  実行補助との結合テストは、テスト用の実行ファイルを実際の `support_native.py` で起動します。
  Rust の実行ファイルの確認は、各ビルドの後に `check_probe.py` と `check_resolve_task_dir.py` で行います。

単体テストは担当範囲の中に置いています。次のどちらでも実行できます。
リポジトリ直下の `scripts/test_*.py` への自動収集とは別に、CI の最初の job が必ず実行します。

```sh
python3 -B native/ci/build.py test
(cd native/ci && python3 -B -m unittest discover -s scripts -p 'test_native_ci.py')
```

非 UTF-8 の専用ケースをまとめて確かめるテスト内だけで、`PYTHONNODEBUGRANGES=1` を指定します。
Python 3.11 以降の子プロセスでも、例外表示の位置の印を抑え、Python 3.10 と同じ表示を検査するためです。
設定値もテストで確かめます。実バイナリの比較を Python 3.10 系に限る条件は変えません。

## R4Bの必須比較と検査用実行物

#461の2コマンドは未登録・未配布です。[契約](../../docs/design.md#origin判定とgit設定の確認値r4b)と [実行手順・手元実測](../../docs/testing.md#r4bの比較)を参照してください。

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example r4b_contract`を実行します。
- Linux2種・Mac2種の`build`/`reproduce`は、別々の空の出力先から3本(製品用`dev-workflow`と、検査用の`r4b_contract`・`r4r_contract`)を各2回作ります。3本の一覧は`build.py`の`CI_BINARIES`で、R4Rも同じ一覧で作って転送します。
- 各回に`check_r4b.py --fixture <今回作ったr4b_contractの絶対パス>`を必須実行します。通常の製品CLIへR4Bを登録して代用しません。
- Linuxでは3本のELFを検査し、各2回のバイト列を比較します。Macは3本を各2回結合・署名し、署名後のバイト列を比較します。
- `verify-universal`は同じ署名後の3本を受け取り、Intel/Apple Siliconの両CPUで`r4b_contract`の専用比較を実行します。専用比較の失敗は工程全体の失敗です。
- 転送用の固定一覧(`BUILD_FILES`)は`1/dev-workflow`・`1/r4b_contract`・`1/r4r_contract`・`2/dev-workflow`・`2/r4b_contract`・`2/r4r_contract`と`record.json`です。
- Mac結合後の転送一覧(`UNIVERSAL_FILES`)は`dev-workflow`・`r4b_contract`・`r4r_contract`と`record.json`です。欠落・余分なファイル・ハッシュや権限の違いを拒否します。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`r4b_contract`は`native-dist.tar`の5ファイルへ入りません。配布済みのプラグインも変更しません。
- 比較はPython3.10系・非rootで行います。自己検査16件・既存46件・追加23件を必須実行し、追加比較だけで約9分かかります。短い自己検査だけで置き換えません。

R4Bの上記経路はコード上で必須にしました。今回の手元Linux品質検証と、未確認の4機種CI・Mac実機・最低OS・最終全回帰テストは区別します。

## R4Rの必須比較と検査用実行物

#473のタスク本文の復元の部品は未登録・未配布です。[契約](../../docs/design.md#タスク本文の復元の内部部品r4r)と [実行手順・手元実測](../../docs/testing.md#r4rの比較)を参照してください。

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example r4r_contract`を実行します。
- 続けて `r4r_map()` が、同じexampleの `-- --list` と `-- --list --ignored` の出力を取り、`check_r4r.py --map-list <一覧> --map-ignored <一覧>` で対応表と照らします。46件の全件・その機械で流す対応先の存在・`#[ignore]` の不在を確かめます。
- Linux2種・Mac2種の`build`/`reproduce`は、`r4r_contract`を3本の1つとして各2回作ります。ELFの検査・バイト列の比較・Macの結合と署名・転送の一覧は、[R4Bの節](#r4bの必須比較と検査用実行物)にまとめてあります。
- 各回に`check_r4r.py --fixture <今回作ったr4r_contractの絶対パス>`を必須実行します(`r4r_probe()`)。通常の製品CLIへR4Rを登録して代用しません。
- `verify-universal`は同じ署名後の`r4r_contract`を受け取り、Intel/Apple Siliconの両CPUで専用比較を実行します。専用比較の失敗は工程全体の失敗です。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`r4r_contract`は`native-dist.tar`の5ファイルへ入りません。配布済みのプラグインも変更しません。
- 比較はPython3.10系・非rootで行い、Cコンパイラ(`cc`)を要ります。`cc` が無ければ省略せずに失敗します。
  - 4機種の実行機に`cc`があることは、[PR #483のCI](https://github.com/Yuki-Maeda-valour/workflow/actions/runs/43040473054)(attempt 1)で確かめました。Linux 2機種は比較が成功し、Mac 2機種は観測器を組む自己検査が成功しました。
- Linuxでは自己検査19件・既存33件の2回・追加25件を必須実行します。手元のLinux x86_64では、2026-11-11に約100秒(自己検査16件の時点)、2026-11-12に約122秒でした。Macでは自己検査19件・既存18件の2回・追加9件を流します。Macの`build`の工程全体は、run 43085911008でx86_64が44分46秒、arm64が28分35秒でした(上限60分)。R4Rの比較だけの時間は、Mac arm64で1回あたり約69秒でした。Mac x86_64の比較だけの時間は未確認です。
- Macでは、旧helperの子にだけ付く`__CF_USER_TEXT_ENCODING`の1項目を、条件をすべて満たすときだけ旧の側から除いて比べます(2026-11-12 02:41の決定)。Rustの側は除きません。条件と記録の行は [testing.md](../../docs/testing.md#r4rの比較) にあります。
- 比べる配置は、一時フォルダの実体のパスに置きます。Macの`/var`は`/private/var`へのリンクで、リンクを含むパスでは旧helperとRustが退避本文を開けずに止まるからです。
- Macの出力先を差し替える実験3件のうち、`test_partial_direct_write_then_two_failures_is_33`だけが、`--version`だけに答える観測器をcpに使います。MacのcpはBSD版で`--version`に答えないからです。ほかの2件は変えていません(1件は本物のcp、1件はcpを使わない)。
- 自己検査が落ちても、後の段(既存の2回・追加の境界・`EAGAIN`の記録)を省かずに流し、最後に終了値1で失敗します。落ちた件の名前と例外の行は、`R4R: 不合格(<段>)`の行に出ます。
- `policy`の`build.py test`は、`quality()`・`r4r_probe()`・`verify_universal()`の接続と、検査用実行物が配布物へ入らないことを確かめます。

R4Rの上記経路はコード上で必須にしました。今回の手元Linux品質検証と、未確認のMac実機・最低OS・最終全回帰テストは区別します。
上のPR用CI(run 43040473054)では、Mac 2機種が比較器の自己検査で落ちました。直した後のrun 43067201954ではMacの比較の本体が流れ、追加の境界の7件が`__CF_USER_TEXT_ENCODING`の差で落ちました。この1項目を除くようにした後のrun 43085911008では、Mac 2機種の`build`が成功しました。記録は [testing.md](../../docs/testing.md#pr用ciの記録run-43040473054) と、その後の2回の節にあります。

## check_links の必須比較(R4U)

#480の文書のリンク検査の部品は未登録・未配布です。[契約](../../docs/design.md#r4ucheck_linksの文書のリンク検査)と [流し方・件数・時間](../../docs/testing.md#check_links-の比較r4u)を参照してください。

- 比較は、2026-11-12 12:59 の決定のとおり、`build` と `verify-universal` に入れず、必須ジョブ `r4u` で流します(Task 3.1c)。
- 入口は [専用テスト](../crates/cli/tests/check_links.rs)(`harness = false`)です。
  - テストの引数 `--r4u-compare` を受けたときだけ、`check_r4u.py --fixture <今回のテスト実行物>` と `test_r4u.py` を起動します。どちらかの失敗・未実行・時間切れ(全体900秒)でテストが落ちます。
  - 引数が無いとき(`build` の `quality()` が流す、`--r4u-compare` を付けない `cargo test --locked`)は、`R4U の比較は流していない: ` で始まる1行を出して終了0にします。比較の合格には数えません。比較器の結果の行の頭 `R4U の比較: ` とは別の文字列です。
- `build` に残るのは、単体テスト(`check_links_units`)と `cargo clippy --all-targets` の検査だけです。`reproduce` と `verify-universal` は R4U を流しません。
- R4U は検査用の実行物を別に作りません。`CI_BINARIES`・`BUILD_FILES`・`UNIVERSAL_FILES` と、配布用の `native-dist.tar` の5ファイルは変わりません。

### ジョブ `r4u` と `build.py r4u`

```sh
python3 -B native/ci/build.py prepare --platform linux-x86_64
python3 -B native/ci/build.py r4u --platform linux-x86_64
```

- `.github/workflows/native.yml` の末尾のジョブ `r4u` は、`build` の後に、`build` と同じ4機種の matrix(`fail-fast` なし)で流れます。上限は30分です。最初の実測(run 43157591744)では、最も長い機械でも3分21秒でした。
- 手順は5つです。checkout・`check_workflow.py`・`actions/setup-python` の 3.10.11 の3つは、`build` と同じです。続けて `build.py prepare --platform <機械>` と `build.py r4u --platform <機械>` を流します。
- `build` の成果物は受け取りません。ジョブの中で、検査対象のテストの実行物を作ります。Mac 共通の実行物を作らないので、`combine` の後のジョブはありません。
- `distribution` の `needs` には入れません(R4E #482 の `r4e` と同じ形)。main に保護の設定は無く、PR 用の CI の全ジョブの成功がマージの条件なので、`r4u` の失敗はマージを止めます。
- `build.py r4u` は、`--platform` が必須です。Rust が `1.99.1` で、実行中の Python が CPython 3.10.11 であることを確かめます。どちらかが違えば、比較の前に止まります。
- 確かめた後、その Python を `R4U_PYTHON` に渡して、`cargo test --locked -p dev-workflow-cli --test check_links -- --r4u-compare` を流します。出力先は、作業フォルダの中の一時フォルダです。比較の失敗は工程全体の失敗です。

### `check_workflow.py` と `test_native_ci.py`

- `check_workflow.py` は、次の3つの集合に `r4u` を入れました。
  - `native_build_command` が受け付ける操作の名前。ここから欠けると、ジョブ `r4u` の手順を正しい形の `build.py` の呼び出しとして読めず、`native.yml` の検査で不合格になります。
  - `native_timeouts` のジョブ `r4u` の上限(30分)。上限の違うジョブ `r4u` を拒みます。
  - `comparison_indices` が数える比較の操作。比較の前に、`actions/setup-python` の手順がちょうど1つあることを確かめます。Python 3.10.11 を用意しないジョブ `r4u` を拒みます。
- `native_timeouts` と `comparison_indices` から `r4u` が欠けても、`check_workflow.py` は今の `native.yml` を合格にします。上限の違うジョブ `r4u` や、Python 3.10.11 を用意しないジョブ `r4u` も通してしまいます。この欠けは、`build.py test`(`test_native_ci.py` の `R4UConnectionTests`)が見つけます。
- `build.py` では、操作の選択肢と、`--platform` が要る操作の集合に `r4u` を入れました。
- `scripts/test_native_ci.py` の `R4UConnectionTests` の10件が、次を確かめます。
  - `build.py r4u` が、Rust と Python を確かめてから、その Python を `R4U_PYTHON` に渡して比較を流すこと。違う Rust や Python では比較の前に止まること。比較の失敗が工程の失敗になること。
  - `build.py` の `main` が `r4u` に `--platform` を求め、`r4u` だけを呼ぶこと。`build` が、`--r4u-compare` を付けない `cargo test --locked` を流すこと。
  - `native.yml` のジョブ `r4u` が、`build` の後に4機種で、上限30分・5手順の形で流れ、`distribution` の `needs` に入らないこと。
  - `check_workflow.py` の3つの集合の `r4u` が、どれも欠かせないこと。1つずつ除いた版で確かめます。操作の名前を除いた版は、今の `native.yml` を不合格にします。上限と比較の操作を除いた版は、壊れた形(上限の違うジョブ `r4u`・Python 3.10.11 を用意しないジョブ `r4u`)を通し、今の版はそれを拒みます。
- `build.py test` は80件です(2026-11-12 の手元の実測。commit 841eb03)。

### `check_r4u.py`

旧版(本家 CPython 3.10.11)・固定期待・Rust の検査用の実行物を比べる比較器です。

| 使い方 | 中身 |
|---|---|
| `--fixture <実行物>` | 必須のケース508件を比べ、ケースの ID ごとの結果と、組ごとの件数を出す |
| `--fixture <実行物> --only <ID>` | 調べるための部分実行。結果を表示し、終了2で終わる(合格にしない) |
| `--measure-cases [--out <ファイル>]` | その OS の旧版で、ケースごとの固定期待(`cases-linux.json` か `cases-macos.json`)を作る |
| `--measure-step2 [--out <ファイル>]` | 呼出履歴を出している間の失敗と SIGINT の部品ごとの表(`step2.json`)を作る |
| `--extract-fixture <ログ…> [--out <ファイル>]` | 比較器が CI のログへ出した旧版の値を取り出す。既定の書き先は `cases-macos.json`。全体の形は新しく書き(既にあれば上書きしない)、足す形は既存の項目を変えずに足りないケースだけを足す |

- 旧版を動かす Python は `R4U_PYTHON`(無ければ PATH の `python3`)です。版と、`pathlib.py`・`posixpath.py`・`genericpath.py` の SHA-256 を出します。本家と違うファイルの欄を含むケースは失敗にします。
- Mac の framework の Python は、起動用のプログラムを通さず、本体を直接起動します。
- 子の環境は、許すものを並べて一から作ります。子の中の環境変数の名前は、共有ライブラリが記録します。余分な変数と欠けた変数は失敗です。
- root では失敗します。開ける fd の上限(soft)が64より小さいときも失敗します。
- その OS の固定期待が無いときと、固定期待に無いケースがあるときは、ほかが全部合格でも不合格です。
  - 固定期待が無いときは全部のケースを、無いケースがあるときはそのケースだけを、旧版で測ります。測った値は区切りの行の間に出すので、`--extract-fixture` で取り出せます。
  - 同じ OS の複数の機械のログを渡すと、機械の欄のほかが同じことを確かめて1つにします。ログは行頭が時刻だけの形で渡します(`gh run view --log` の形は読めません)。
- Mac で行わなくてよいのは、閉じた一覧(`/dev/full` が無い・UTF-8 でない名前を作れない・旧版の試しの書き込みが記録に現れない)のケースだけです。件数と理由を出し、成功に数えません。
- 信号は、共有ライブラリが決まった write の直後に送るか、子の FIFO の待ちを確かめてから送ります(Linux は `/proc/<pid>/wchan`、Mac は `proc_pidinfo` で読むスレッドの状態)。
- FIFO の待ちの途中の SIGINT の7件では、旧版と Rust の呼出履歴の最後のフレームが `pathlib.py` の open の行(`File "pathlib.py", line 1119, in open`)であることも確かめます(Task 3.1b)。

### `test_r4u.py`

`check_r4u.py` の自己検査です。79件です。0件・skip・失敗のどれかがあれば終了1です。
固定名の対応づけ、`Current thread` の形式、部分書き込みの期待、ケースの一覧、未実施の扱い、子の環境の組み立て、旧 Python の確かめを単体で確かめます。
送った時点の確かめ(最後のフレーム)と、固定期待に無いケースの扱い(旧版で測って不合格にすること・既存の項目を変えずに足すこと)も確かめます。
実際の子でも、合格するはずの組が合格し、余計な出力・入力の変更・両側の同じ誤り・時間切れ・記録に現れない検査対象・許さない変数を拒むことを確かめます。

### `r4u_write_shim.c`

write(2) の呼び出しを1回ずつ記録し、決めた故障と SIGINT を当てる共有ライブラリです。比較の専用で、製品には入りません。

- 比較器と自己検査が、一時フォルダで `cc -O1 -Wall -Wextra -Werror -fPIC` で組みます。Linux は `-shared`(と `-ldl`)、Mac は `-dynamiclib` です。組めなければ比較全体が失敗です。
- Linux は `LD_PRELOAD`、Mac は `DYLD_INSERT_LIBRARIES` と `__DATA,__interpose` で `write` を置き換えます。旧 Python と Rust の両方へ、同じ形で差し込みます。
- 設定は環境変数で渡します。
  - `R4U_SHIM_LOG`: 記録の書き先です。`fd:<数>` なら受け継いだ fd(パイプ)、ほかはファイルのパス(追記)です。比較器はパイプを使うので、`RLIMIT_FSIZE` の影響を受けません。
  - `R4U_SHIM_RULES`: `;` で区切った規則です。当てる write は、fd ごとの回数(`nth`)か、中身の頭(`prefix`)と何回目か(`occ`)で選びます。
  - 動作は `fail`(失敗)・`short`(途中まで書く)・`sigint`(書いた後に自分へ SIGINT)・`quota`(合計の上限)・`cut`(その位置で切り、その後を失敗にする)・`budget`(上限をまたぐ write だけを失敗にする。`RLIMIT_FSIZE` がこう動く OS のための模擬で、CI の4種はどれも当たらなかった)です。`persist=1` で当て続けます。
- 記録の1行は、1回の write の fd・要求の長さ・戻り値・errno・当てた動作・中身(16進)です。
- プログラムの始まりに、受け継いだ環境変数の名前を1行記録します。値は `__CF_USER_TEXT_ENCODING`・`__PYVENV_LAUNCHER__` だけを記録します。
- exec の連鎖でも記録を続けます(Mac の framework の起動用のプログラムが本体を起動し直すときなど)。
- ファイルへ記録するときは、記録用の fd を200以上へ移します。起動の前に閉じていた fd 0〜2 を使わないためです。

### CI の記録(run 43157591744)

push 用の CI の Native(commit 841eb03)の attempt 2 のジョブ `r4u` の結果です。
attempt 1 は、build (macos-x86_64) の成果物の保存が GitHub 側の通信の失敗(`ENOTFOUND`)で止まりました。失敗したジョブだけを再実行した attempt 2 で、全13ジョブが成功しました。

- Linux 2種は、508件がすべて合格しました(固定期待と比べた507件)。
- Mac 2種は、合格492件・未実施16件・不合格0件でした(固定期待と比べた491件)。未実施は閉じた一覧の2条件だけです(`/dev/full` が無い11件・UTF-8 でない名前を作れない5件)。
- 4種とも、旧版と Rust の試しの書き込みが記録に現れました。自己検査は79件がすべて合格しました。
- ジョブ `build` では比較が流れませんでした(`R4U の比較は流していない: ` の行があり、`R4U の比較: ` の行はありません)。
- ジョブ `r4u` の時間は、最も長い Mac x86_64 でも3分21秒でした(上限30分)。機械ごとの時間は [testing.md](../../docs/testing.md#check_links-の比較r4u) の「R4U の時間の記録」にあります。
- Task 3.1c の前(run 43118591090・commit 5b142a1)は比較が `build` の中で流れ、Mac x86_64 の `build` は43分29秒でした(上限60分)。

## ワークフローの検査

`.github/workflows/` の全 `*.yml`・`*.yaml` を検査します。
標準ライブラリで曖昧さなく読むため、YAML のうち JSON と共通の書き方だけを使います。
通常の YAML のブロック表記・アンカーなどは検査不能として失敗させます。
JSON の重複キーも拒否します。新しい CI も同じ形式で追加してください。

- イベントは `pull_request`・`push`・`workflow_dispatch` の 3 つです。
- 権限は `contents: read` だけです。秘密情報・書き込み権限・キャッシュを渡しません。
- checkout は PR のソース SHA とリポジトリを明示します。`persist-credentials: false` を必須にします。
- 各 job は checkout の直後に `check_workflow.py` を呼びます。
- 旧版との比較を行う工程は、その検査の後、比較の前に `actions/setup-python` で Python 3.10.11 を用意します。指定は `python-version` だけとし、キャッシュは追加しません。
- `run` は `native/ci/` のスクリプト 1 本を呼ぶだけです。ビルド設定は書きません。
  - 例外は、段階 0b(#443)の配布の確かめの 1 行 `python3 -B scripts/check_dist_launch.py --root .` だけです。配布 commit の浅い clone で、配った入口を起動します。`native/` の外に置くのは、0b で `native/`(ソース入力)を変えないためです。字面が少しでも違えば拒みます。
- Mac の 2 種類の実行確認が成功するまで、配布用 tar を作りません。

外部 Action は 2026-11-08 に `gh api` でタグを解決しました。
採用した SHA の commit API と、その SHA の `action.yml` が存在することも確認しました。

| Action | タグ | 確認した commit SHA |
|---|---|---|
| `actions/checkout` | `v4` | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/upload-artifact` | `v4` | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/download-artifact` | `v4` | `d3f86a106a0bac45b974a628896c90dbdf5c8093` |
| `actions/setup-python` | `v7.1.0` | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` |

Python 3.10.11 は [公式の配布物](https://github.com/actions/python-versions/releases/tag/3.10.11-11114431484)に Linux 24.04 の x64・arm64 と Darwin の x64・arm64 がそろいます。
製品の Python 3.10 以上という下限は変えません。

確認に使う API は `repos/actions/<名前>/commits/<タグ>`、`repos/actions/<名前>/commits/<SHA>`、
`repos/actions/<名前>/contents/action.yml?ref=<SHA>` です。
更新するときは 3 つを確認してから `check_workflow.py` の記録とワークフローを同時に変えます。
