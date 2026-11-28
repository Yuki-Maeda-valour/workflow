# ビルドと CI の入口

このフォルダは Rust のビルドと検査を受け持ちます。段階 1a(#448)からタスク保存先の旧版との比較も行います。
配るプラグインは変更しません。最後に作る `native-dist.tar` は入口を含まない 5 ファイルです。

## コマンド

リポジトリ直下から実行します。CIの補助はPython 3.10以上と標準ライブラリを使います。R4Sの比較には`requirements-dev.txt`の開発依存も必要です。
旧版との比較を含む `build`・`reproduce`・`verify-universal` は、Python 3.10 系と root 以外の通常利用者での実行が必要です。
`r4d1` も、Python 3.10 系と root 以外の通常利用者での実行が必要です。
`r4d1` には、reftable(Git が参照を表の形のファイルに保存する形式)を作れる git 2.45 以上も必要です。欠ければ失敗にします。
`r4d3` も、Python 3.10 系(整数の桁の上限 4300)と root 以外の通常利用者での実行が必要です。Linux では、本物の `take` のための bash 4.0 以上と git 2.45 以上も必要です。欠ければ失敗にします。
`r4a-rest`・`r4a-rest-universal` も、Python 3.10 系と root 以外の通常利用者での実行が必要です。
`CARGO_TARGET_DIR` にはリポジトリの外のフォルダを指定してください。
CIの`build.py`呼出しは、コマンド名を先に置き、その後にオプションを指定します。オプション先行形と`--`を先に置く形はCI定義の検査で拒否します。
CI では `RUNNER_TEMP/native-ci` が既定です。`CARGO_HOME` は呼び出し側の値を保ちます。

```sh
python3 -B native/ci/check_workflow.py
python3 -B native/ci/source_input_hash.py --root .
python3 -B native/ci/build.py test
python3 -B native/ci/build.py prepare --platform linux-x86_64
python3 -B native/ci/build.py build --platform linux-x86_64
python3 -B native/ci/build.py r4d1 --platform linux-x86_64
python3 -B native/ci/build.py r4d3 --platform linux-x86_64
```

- `prepare`: 実行中のPythonで`pip install -r requirements-dev.txt`を行い、Rust `1.99.1`・`rustfmt`・`clippy`・対象の標準ライブラリを用意します。
  - CIの4機種(環境変数 `GITHUB_ACTIONS` が `true`)では、入れる前に、残っている 1.99.1 を消します(Linux arm64 は #509、ほかの3機種は #523)。機械に入れかけの 1.99.1 が残っていると、`rustup toolchain install` が `detected conflict` で失敗するためです。
  - そのとき、`rustup --version` と `rustup toolchain list` を、自動導入を止めて(`RUSTUP_AUTO_INSTALL=0`)リポジトリ直下で流します。`native/` の中では、`native/rust-toolchain.toml` の指定で、rustup が入れかけの道具を入れ直そうとするためです。
  - 1.99.1 が一覧にあれば、`rustup show home` の下の状態を 1 行で標準エラーへ出してから、`rustup toolchain uninstall 1.99.1` を流します。状態の行は、入れかけになる理由を後で調べる材料です。
  - 手元の機械(`GITHUB_ACTIONS` が `true` でない)では消しません。rustup の失敗は、どの工程でも止めます。
- `build`: 書式・clippy・Rust テストの後に 2 回ビルドします。RustテストにはR4Sとtask-digestの専用比較も含まれます。各回の製品用実行物を `check_probe.py` と `check_resolve_task_dir.py` で検査します。R4BとR4Rのexample単体検査、R4Rの対応表の照合、各回の `r4b_contract`・`r4r_contract` の専用比較も必須です。S2Lの単体と変異の検査(`check_loop_state_units.py`・`check_loop_state_mutations.py`)と、各回の `loop_state_fixture` の比較(`check_loop_state.py`・`test_loop_state.py`)も必須です。
- `reproduce`: 書式・clippy・Rust テストを省き、2 回のビルドと比較、各回の製品用実行物とR4B・R4R検査用実行物の上記の検査を行います。各回の `loop_state_fixture` の比較(`check_loop_state.py`・`test_loop_state.py`)も行います。
- `combine`: Mac の 2 回分をそれぞれ `lipo` で結合します。固定識別子で簡易署名し、署名を検査してから `cmp` で比べます。
- `verify-universal`: 同じ最終 Mac ファイルを受け取り、署名の検査と `check_probe.py`・`check_resolve_task_dir.py` を行います。R4B・R4R検査用実行物の署名検査と専用比較も行います。Intel と Apple Silicon の両方で実行し、署名し直しません。S2Lの `loop_state_fixture` も、署名検査と比較(`check_loop_state.py`・`test_loop_state.py`)を行います。
- CIの工程全体の上限は`build`が120分、`verify-universal`が60分です(S2L #468の前は60分・30分)。Mac IntelでR4Bの1回の比較に838.626秒かかり、2回比較する`build`が従来の40分で打ち切られたため延ばしました。署名後の工程も製品用比較とR4B比較を各1回行うため、従来の15分から延ばしました。[実測と根拠](../../docs/testing.md#4-種類の-ci-と成果物)を参照してください。Macの`build`は、run 43085911008で当時の上限60分の中で終わりました(x86_64が44分46秒、arm64が28分35秒)。`verify-universal`は、run 43085911008でMac arm64が13分9秒、Mac x86_64が17分8秒で成功しました(上限30分)。同じcommitのpush用のrun 43085900763では、Mac x86_64が26分39秒で、上限まで残り約3分でした。2つのrunとも`distribution`の工程は待ちのまま始まらず、GitHub側の実行機の割り当ての問題と推測しています。推測の根拠(HTTP 502の記録と、mainの過去のrunの`distribution`の成功)は [testing.md](../../docs/testing.md#pr用ciの記録run-43085911008) にあります。
- `r4a-rest`: `build`の後に、同じ機種の2回のビルド(`1/`・`2/`)の`r4a_rest_contract`が同じことを`cmp`で確かめてから、`1/`を専用比較します(R4A残り5本。2回目の比較は#516でやめました)。Linux 2種・Mac 2種です。
- `r4a-rest-universal`: `combine`の後に、署名後のMac共通の`r4a_rest_contract`を、Intel/Apple Siliconの両CPUで専用比較します(署名し直しません)。
- `r4c`: `build`の後に、同じ機種の2回のビルドの`adopt_candidate_contract`を、同じ番号の`diff_snapshot_contract`と組にして専用比較します(R4C)。Linux 2種・Mac 2種です。
- `r4c-universal`: `combine`の後に、署名後のMac共通の`adopt_candidate_contract`を、Intel/Apple Siliconの両CPUで専用比較します(署名し直しません)。
- `package`: Linux 2 本と最終 Mac 1 本を集め、`make_dist.py` の検査を通して tar に包みます。
- `r4d1`: `build` の後の必須ジョブです。上限は 30 分です。次の比較のコマンドを `quality()` と同じ環境で流し、比較のテストがちょうど 1 件合格したことを確かめます。比較のテストには `#[ignore]` の印があり、通常の `cargo test` では流れません(結果は `ignored`)。
  - `cargo test --locked -p dev-workflow-cli --test diagnostic_reftable -- --ignored --exact rust_matches_the_legacy_reftable_reader_on_every_input`
- `r4d3`: `build` の後の必須ジョブです(R4D-3 #485)。上限は 30 分です。`r4d1` と同じく、次の比較のコマンドを `quality()` と同じ環境で流し、比較のテストがちょうど 1 件合格したことを確かめます。比較のテストには `#[ignore]` の印があり、通常の `cargo test` では流れません(結果は `ignored`)。
  - `cargo test --locked -p dev-workflow-cli --test diagnostic_state -- --ignored --exact rust_matches_the_legacy_state_and_ref_checks_on_every_input`

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
- Linux2種・Mac2種の`build`/`reproduce`は、別々の空の出力先から4本(製品用`dev-workflow`と、検査用の`r4b_contract`・`r4r_contract`・`loop_state_fixture`)を各2回作ります。4本の一覧は`build.py`の`CI_BINARIES`で、R4RとS2Lも同じ一覧で作って転送します。
- 各回に`check_r4b.py --fixture <今回作ったr4b_contractの絶対パス>`を必須実行します。通常の製品CLIへR4Bを登録して代用しません。
- Linuxでは4本のELFを検査し、各2回のバイト列を比較します。Macも、CPUごとの各2回のバイト列を比較します(#516)。さらに4本を各2回結合・署名し、署名後のバイト列を比較します。
- `verify-universal`は同じ署名後の4本を受け取り、Intel/Apple Siliconの両CPUで`r4b_contract`の専用比較を実行します。専用比較の失敗は工程全体の失敗です。
- 転送用の固定一覧(`BUILD_FILES`)は`1/dev-workflow`・`1/r4b_contract`・`1/r4r_contract`・`1/loop_state_fixture`・`2/dev-workflow`・`2/r4b_contract`・`2/r4r_contract`・`2/loop_state_fixture`と`record.json`です。
- Mac結合後の転送一覧(`UNIVERSAL_FILES`)は`dev-workflow`・`r4b_contract`・`r4r_contract`・`loop_state_fixture`と`record.json`です。欠落・余分なファイル・ハッシュや権限の違いを拒否します。
- R4A残り5本(#471)の`r4a_rest_contract`も、同じ一覧(`CI_BINARIES`・`BUILD_FILES`・`UNIVERSAL_FILES`)で作って転送します。
- R4C(#478)の`adopt_candidate_contract`も、同じ一覧で作って転送します。配布一覧(`make_dist.BINARY_PATHS`)には入れません。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`r4b_contract`は`native-dist.tar`の5ファイルへ入りません。配布済みのプラグインも変更しません。
- 比較はPython3.10系・非rootで行います。自己検査16件・既存46件・追加23件を必須実行し、追加比較だけで約9分かかります。短い自己検査だけで置き換えません。

R4Bの上記経路はコード上で必須にしました。今回の手元Linux品質検証と、未確認の4機種CI・Mac実機・最低OS・最終全回帰テストは区別します。

## R4Rの必須比較と検査用実行物

#473のタスク本文の復元の部品は未登録・未配布です。[契約](../../docs/design.md#タスク本文の復元の内部部品r4r)と [実行手順・手元実測](../../docs/testing.md#r4rの比較)を参照してください。

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example r4r_contract`を実行します。
- 続けて `r4r_map()` が、同じexampleの `-- --list` と `-- --list --ignored` の出力を取り、`check_r4r.py --map-list <一覧> --map-ignored <一覧>` で対応表と照らします。46件の全件・その機械で流す対応先の存在・`#[ignore]` の不在を確かめます。
- Linux2種・Mac2種の`build`/`reproduce`は、`r4r_contract`を4本の1つとして各2回作ります。ELFの検査・バイト列の比較・Macの結合と署名・転送の一覧は、[R4Bの節](#r4bの必須比較と検査用実行物)にまとめてあります。
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

## S2Lの必須比較と検査用実行物

#468の`loop-state`の7コマンドは未登録・未配布です。[契約](../../docs/design.md#loop-stateの状態観測7コマンドs2l)と [実行手順・件数・時間](../../docs/testing.md#s2lの比較)を参照してください。

- `quality()`は、各OSで1回、`check_loop_state_units.py`(Rustテスト57件)と`check_loop_state_mutations.py`(誤りを入れた21種類)を流します。
- 安全の観測のRustテスト(`safety_tests.rs` の `Fixture`)が試験のrepoを作る git には、後ろの自動の手入れを止める指定を付けます(#517)。手入れが作って消す`.git/objects/maintenance.lock`が、テスト用の監視を作る途中に消えて落ちたためです。記録は[testing.md](../../docs/testing.md#s2lの比較)にあります。
- Linux2種・Mac2種の`build`/`reproduce`は、製品用`dev-workflow`・検査用`r4b_contract`・`r4r_contract`と同じ2回のビルドで、検査用`loop_state_fixture`も作ります。
- 各回に`check_loop_state.py --fixture <今回作ったloop_state_fixtureの絶対パス>`と`test_loop_state.py --fixture <同じパス>`を必須実行します。
- Linuxは4本のELFを検査し、各2回のバイト列を比べます。Macも、CPUごとの各2回のバイト列を比べます(#516)。さらに4本を各2回結合・署名し、署名後のバイト列を比べます。
- `verify-universal`は、署名後の同じ`loop_state_fixture`を、Intel・Apple Siliconの両CPUで比べます。専用比較の失敗は工程全体の失敗です。
- 転送用の固定一覧は、`CI_BINARIES`の4本(`dev-workflow`・`r4b_contract`・`r4r_contract`・`loop_state_fixture`)の各2回と`record.json`です。Mac結合後は4本と`record.json`です。
- `package`は製品用だけを配布用へ渡します。`loop_state_fixture`は`native-dist.tar`の5ファイルへ入りません。
- 比較は本家CPython 3.10.11・PyYAML 6.0.3・非rootで行います。件数の欠け・skip・時間切れを成功にしません。
- 自己検査`test_loop_state.py`の26件は、比較器が不一致・外す環境変数の残り・固定名の誤った対応づけを拒否することを確かめます。
- 手元の比較は1回176〜235秒でした。CIの工程の上限(`build`120分・`verify-universal`60分)の中で完走するかは未確認です。
- 上限はS2Lで延ばしました。R4Rのpush用のrun 43085900763で、Mac Intelの`build`が48.2分(上限60分)、`verify-universal`が26.6分(上限30分)を使っており、S2Lの比較を足すと超えるおそれが高いためです。[根拠](../../docs/testing.md#4-種類の-ci-と成果物)を参照してください。

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

`check_r4u.py` の自己検査です。86件です。0件・skip・失敗のどれかがあれば終了1です。
固定名の対応づけ、`Current thread` の形式、部分書き込みの期待、ケースの一覧、未実施の扱い、子の環境の組み立て、旧 Python の確かめを単体で確かめます。
送った時点の確かめ(最後のフレーム)と、固定期待に無いケースの扱い(旧版で測って不合格にすること・既存の項目を変えずに足すこと)も確かめます。
実際の子でも、合格するはずの組が合格し、余計な出力・入力の変更・両側の同じ誤り・時間切れ・記録に現れない検査対象・許さない変数を拒むことを確かめます。
疑似端末の出力を、子が終わった後に書く目印まで読み切ること(目印が出ないか書けなければ不合格)と、捕まえた出力と書き込みの記録が合わないときの失敗の文に、旧版か Rust か・2つの長さ・最初に違う位置を出すことも確かめます(#519)。

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

## R4D-1の必須比較

#481 の診断の reftable の比較は、build の後の専用ジョブ `r4d1` で流します(#488)。4 機種(Linux 2 種・Mac 2 種)で、`build.py r4d1 --platform <機械>` を流します。
- 通常の `cargo test --locked` では、比較のテストは流れません。`quality()` も比較を流しません。
- `r4d1()` は、`quality()` と同じ環境(`build_environment()`・Python 3.10.11・root 以外)で比較のコマンドを 1 回流します。標準出力は 1 行ずつ CI のログに出します。
- 比較のテストがちょうど 1 件合格したことを確かめます。0 件・飛ばし・別のテストの合格・時間切れは、合格にしません。
- `distribution` の `needs` には入れません。必須 CI の全ジョブの成功を確かめてから merge する決まり([testing.md](../../docs/testing.md#共通の終了値と入出力の検査c0a))があるので、必須です。
  - この決まりは、CI が自動で止めるものではありません。merge の前に、管理担当が `r4d1` を含む全ジョブの成功を確かめます。
- 件数と時間は [testing.md](../../docs/testing.md#r4d-1-の-reftable-の比較) にあります。

## R4D-3の必須比較

#485 の保護記録の形式の検査と参照の解決の比較は、build の後の専用ジョブ `r4d3` で流します。4 機種(Linux 2 種・Mac 2 種)で、`build.py r4d3 --platform <機械>` を流します。
- 通常の `cargo test --locked` では、比較のテストは流れません。`quality()` も比較を流しません。
- `r4d3()` は、`quality()` と同じ環境(`build_environment()`・Python 3.10.11・root 以外)で比較のコマンドを 1 回流します。1 回の上限は 1,800 秒(`R4D3_TIMEOUT`)で、ジョブの上限 30 分が先に効きます。標準出力は 1 行ずつ CI のログに出します。
- 比較のテストがちょうど 1 件合格したことを確かめます。0 件・飛ばし・別のテストの合格・時間切れは、合格にしません。
- `build.py` が控えるのは cargo test の標準出力だけです。比較器の自己検査の行(標準エラー)は、比較のテスト(`tests/diagnostic_state.rs`)が確かめます。
- 比較器は `native/ci/check_diagnostic_state.py`、その自己検査は `native/ci/test_diagnostic_state.py` です。自己検査は、印の環境変数 `R4D3_NO_SELFTEST` が無いときの `--compare` の中で流れます。
- Mac で、本物の `take` の組(T)に要る bash・git の版と GNU の `stat`・`find`・`grep` が足りなければ、`R4D-3 作れなかった入力: T(darwin・<足りない物>)` の行が出ます。その行の名前は PR の本文に書きます。
- `distribution` の `needs` には入れません。merge の前に、管理担当が `r4d3` を含む全ジョブの成功を確かめます。
- `check_workflow.py` は、`build.py r4d3` のコマンド・必須のジョブ・上限 30 分(`R4D3_TIMEOUTS`)・比較の前の Python 3.10.11 を確かめます。`test_native_ci.py` の `R4D3Tests` は、ジョブの形・上限・要約の行の確かめ・`main()` の呼び分けを確かめます。
- 件数と時間は [testing.md](../../docs/testing.md#r4d-3-の保護記録と参照の比較) にあります。

## R4Gの必須比較と検査用実行物

#479の3コマンド(pending-implementation・reviews-dir・implementation-git)は未登録・未配布です。契約と決定済みの差は #479 と `native/ci/r4g_fixtures/decided-differences.json` にあります。
比較(`check_r4g.py`)は、`build`と`verify-universal`では流しません。別の2つのジョブ`r4g`・`r4g-universal`で流します(2026-11-12 12:59の決定)。

```sh
python3 -B native/ci/build.py r4g --platform linux-x86_64
python3 -B native/ci/build.py r4g-universal --platform macos-arm64
```

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example r4g_contract`を実行します。`build`に残るR4Gの分は、この単体検査と`r4g_contract`のビルドだけです。
- Linux2種・Mac2種の`build`/`reproduce`は、`r4g_contract`を`CI_BINARIES`の1つとして各2回作り、tarに入れます。転送の一覧(`BUILD_FILES`・`UNIVERSAL_FILES`)にも入ります。ELFの検査・バイト列の比較・Macの結合と署名は、[R4Bの節](#r4bの必須比較と検査用実行物)にまとめてあります。
- `r4g`: `build`の後に4機種で流します。`build`のtarを受け取り、2回のビルドの`r4g_contract`が同じことを`cmp`で確かめてから、`1/`で`check_r4g.py --fixture <受け取ったr4g_contractの絶対パス>`を流します(`r4g_probe()`。2回目の比較は#516でやめました)。
- `r4g-universal`: `combine`の後にMac2機種で流します。署名後のtarを受け取り、`r4g_contract`の署名と2種類のCPUを確かめてから比較します。Linuxの名前を渡すと失敗します。
- 2つのジョブは、`prepare`でRustと開発依存を用意し(受け取ったtarの記録を照らすときにRustの版を読むため)、tarを受け取ってから比較します。比較の失敗は工程全体の失敗です。
- `distribution`の`needs`の末尾に`r4g`と`r4g-universal`を足しました。R4Gの比較が落ちたら配布のtarを作りません(R4B・R4Rと同じ)。
- `check_workflow.py`は、`native.yml`に必須のジョブ(`REQUIRED_NATIVE_JOBS`)と、2つのジョブの`build.py`の工程と機種(`R4G_JOBS`。`r4g`は4機種・`r4g-universal`はMac2機種)を確かめます。
- Linuxの`r4g`は、始めに(`prepare_r4g_linux()`)straceが無ければ`apt-get`で入れ、GitHubのUbuntu 24.04がAppArmorで止めている利用者の名前空間を`sysctl`で許します(/proc を隠す組が使う)。`prepare`・`build`・`reproduce`・`distribution`では行いません。Linuxの`r4g_probe()`は`--require-strace --require-userns`を付け、straceの記録の比較と名前空間を必須にします。手元の名前空間を作れない機械では`--require-userns`を付けずに流し、その組を「未実施」と記録します(合格の件数に数えません)。
- `r4g_probe()`は、短い一時フォルダ(`/tmp/r4g…`)を`TMPDIR`に渡します。既存テストが一時フォルダの下に作るAF_UNIXのソケットのパスは、Macで103バイトまでです(Linuxは107)。Macの既定の`TMPDIR`では超えるので、比較器が始めに止まります。1回の上限は3600秒です(`R4G_TIMEOUT`)。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`r4g_contract`は`native-dist.tar`の5ファイルへ入りません。
- Linuxだけの組: `/dev/full`の出力先・pipeの大きさの指定・/procを読む観測・/procを隠す組。Macではこれらを「未実施」と記録し、最後の行に`未実施 N 件`として出します。
- reviews-dirの4節は、道具の版の基準(bash 5.2.21・coreutils 9.4・findutils 4.9.0・sed 4.9。2026-11-12 00:35の決定)がある機械だけで流します。Ubuntu 24.04の実行機は基準と同じ版です。Macは基準の版が無いので、4節を「未実施」と記録します。Linuxで版が違えば失敗にします。
- reviews-dirの信号の比較は、Linuxでは`/proc/<pid>/wchan`でシェルと群の全員が待つ呼出しの中にいるのを確かめてから信号を送ります。Mac(とLinux以外)は読めないので0.5秒待つだけです。Macではreviews-dirの節は未実施ですが、implementation-gitの信号の組でも使い、0.5秒待つだけです。基準の道具を用意したMacでreviews-dirを流して揺れたときは、落ちた組の群の状態(比較器が添える)を記録し、Linuxの結果を正とします。
- reviews-dirは、呼出元が旧スクリプトの変数と同じ名前をexportしたときに旧bashが子へ渡す値も再現し、比較します(2026-11-12 14:24の決定4。名前の一覧は`check_r4g.py`の`REVIEWS_SCRIPT_NAMES`)。
- 比較はPython3.10.11・非rootで行います。

ジョブの上限(`check_workflow.py`の`R4G_TIMEOUTS`)は、`r4g`が60分、`r4g-universal`が30分です。理由は次の実測と見込みです。

- 手元の測定(Linux x86_64の容器、2026-11-12): 16 CPUで1回あたり約620〜700秒でした。CPUを2つに絞った回と4つに絞った回を同時に流すと、976秒と983秒でした(どちらも合格190件)。
- 1回のうち約181秒は時計で決まります(本物の期限 Git 30秒・事前検査120秒を旧版とRustの両方で待つ分と、本物の時計の上限の節)。
- `r4g`(2回): Linuxは1回約17〜20分と見込みます(GitHubの実行機を手元の2〜4 CPUの1〜1.2倍と見る。未確認の推測)。2回で約34〜40分、`prepare`とstraceの用意に約3分を足して約37〜43分です。上限の2割を残すと約54分になるので、60分にしました。比較は#516で1回になりましたが、上限は2回の時の値のままです。
- Macはreviews-dirの4節とLinuxだけの組が無く、1回あたり約11〜15分と見込みます(Mac Intelは時計で決まらない分を手元の1.55倍と見る。R4BのMac Intelの実測838秒と手元の約9分から)。2回で約22〜30分、`prepare`に約5分を足して約27〜35分で、60分に収まります(比較は#516で1回になりました)。
- `r4g-universal`(1回): Mac Intelで約11〜15分と`prepare`の約5分で約16〜20分です。2割を残すと約25分になるので、30分にしました。
- 4機種と署名後のMacのCIでの時間と成功は未確認です。最初のCIで実測して記録します。

`policy`の`build.py test`は、`quality()`の単体検査・`r4g_probe()`(Linuxの`--require-strace --require-userns`・短い`TMPDIR`・上限)・`r4g`と`r4g-universal`の流れ(4機種・2本の一致の確かめと1回目の比較〈#516〉・署名とCPUの確かめ・Linuxの準備はこの2つだけ)・`build`と`verify-universal`が比較を流さないこと・ワークフローの2つのジョブの形と上限・`distribution`の`needs`・必須のジョブの一覧と機種の確かめ・検査用実行物が配布物へ入らないことを確かめます。

R4Gの上記経路はコード上で必須にしました。今回の手元Linux品質検証と、未確認の4機種CI・Mac実機・最低OS・最終全回帰テストは区別します。

## ワークフローの検査

`.github/workflows/` の全 `*.yml`・`*.yaml` を検査します。
標準ライブラリで曖昧さなく読むため、YAML のうち JSON と共通の書き方だけを使います。
通常の YAML のブロック表記・アンカーなどは検査不能として失敗させます。
JSON の重複キーも拒否します。新しい CI も同じ形式で追加してください。

- イベントは `pull_request`・`push`・`workflow_dispatch` の 3 つです。
  - push 用は main だけで流れます。作業ブランチへ送っても流れません。
  - pull_request 用は PR ごとに流れます。同じ PR に新しい SHA が来ると、古い SHA の流れを打ち切ります(`concurrency`。同じ組の流れを 1 本に絞る設定)。
  - main の push 用と、手で起動した流れは、打ち切りません。
  - `check_workflow.py` は、`push` と `concurrency` を決めた 1 つの形だけ許します。どのワークフローも、この `concurrency` を必ず持ちます(無いと拒みます)。
    - `push` は `branches: ["main"]` だけです。
    - `concurrency` は `check_workflow.py` の `CONCURRENCY` と同じ字面だけです。組の名前は、ワークフローの名前・イベントの種類・PR の番号か run の番号です。
  - 作業ブランチでは push 用の CI が流れません。PR の本文に書く CI の値は、PR の先頭の SHA の pull_request 用の run から取ります。
  - 打ち切られた run の結果は `cancelled` になります。merge の条件には、PR の先頭の SHA の、打ち切られていない run の最新の attempt(やり直しの回)だけを数えます。打ち切られた run は、SHA に関係なく数えません(同じ SHA のまま PR を閉じて開き直すと、先頭の SHA にも打ち切られた run が残ります)。
  - 古い SHA の pull_request 用の run は、やり直し(re-run)しません。やり直すと同じ組に入り、動いている先頭の SHA の run を打ち切るためです。やり直すのは、PR の先頭の SHA の run だけです。
  - ワークフローの名前を重ねません。組の名前に入るため、重なると同じ PR の 2 本が互いを打ち切ります。`check_workflow.py` が拒みます。
  - `name` は、書くなら空でない文字列だけです。
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

## R4Eの必須比較と検査用実行物

#482の部品(Markdownの表をXLSXにする旧版のスクリプトの移植)は未登録・未配布です。検査用の入口は`r4e_contract`(cargoのexample)です。
比較(`check_r4e.py`)は、`build`と`verify-universal`では流しません。別の2つのジョブ`r4e`・`r4e-universal`で流します。

```sh
python3 -B native/ci/build.py r4e --platform linux-x86_64
python3 -B native/ci/build.py r4e-universal --platform macos-arm64
```

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example r4e_contract`を実行します。
- Linux2種・Mac2種の`build`/`reproduce`は、`r4e_contract`を`CI_BINARIES`の1つとして各2回作ります。
  - Linuxでは静的なELFかを検査し、2本のバイト列を比べます。
  - Macでは`reproduce`がCPUごとの2本のバイト列を比べ(#516)、`combine`が2回分を結合・署名し、署名後のバイト列を比べます。`verify-universal`は署名と2種類のCPUを確かめます。
  - 転送の一覧(`BUILD_FILES`・`UNIVERSAL_FILES`)にも入ります。欠落・余分なファイル・ハッシュや権限の違いを拒否します。
- `r4e`: `build`の後に4機種で流します。`build`のtarを受け取り、2回のビルドの`r4e_contract`が同じことを`cmp`で確かめてから、`1/`で比較します(2回目の比較は#516でやめました)。
- `r4e-universal`: `combine`の後にMac2機種で流します。署名後のtarを受け取り、`r4e_contract`の署名と2種類のCPUを確かめてから比較します。Linuxの名前を渡すと失敗します。
- 2つのジョブの工程全体の上限は、どちらも30分です。
- 2つのジョブは、次の順に進めます。
  1. `prepare`でRustと開発依存を用意します。受け取ったtarの記録を照らすときに、Rustの版を読むためです。
  2. Linuxでは`sudo apt-get update`と`sudo apt-get install -y strace`の後、`strace -f -e trace=execve -o <一時のファイル> /bin/true`を流します。`execve`の行がちょうど1行でなければ止めます。比較器もLinuxでは`strace`を必須にしています。
  3. 実行中のPythonがCPython 3.10.11かを確かめます。違えば止めます。
  4. 作業用の一時フォルダに、旧版用のvenv(Pythonの部品を分ける環境)を作ります。部品は`pip install --no-deps --require-hashes -r native/ci/r4e-requirements.txt`で入れます(openpyxl 3.1.5・et_xmlfile 2.0.0。lxmlは入れません)。
  5. `check_r4e.py --fixture <受け取ったr4e_contractの絶対パス> --legacy-python <venvのpython> --temp-parent <作業用の一時フォルダ>`を流します。最後の行(`合格: <件数> 件(未実施 <件数> 件)/ <秒> 秒`)がCIの記録に残ります。比較の失敗は工程全体の失敗です。
- 比較はPython3.10系・非rootで行います。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`r4e_contract`は`native-dist.tar`の5ファイルへ入りません。
- `distribution`の`needs`の末尾に`r4e`と`r4e-universal`を足しました。R4Eの比較が落ちたら配布物を作りません(R4B・R4Rと同じ)。
- `check_workflow.py`は、`native.yml`だけに必須のジョブの検査(`REQUIRED_NATIVE_JOBS`の7つ)を当てます。ジョブを足すときは、この一覧の末尾へ足します。
- `check_r4e.py`は、Linuxだけのケース24件(`LINUX_ONLY_CASES`)と、Linuxだけの固定期待(`LINUX_ONLY_FIXED`)を固定します。`cases.json`の`linux_only`がこれと違えば、LinuxでもMacでも失敗です。

別のジョブにした理由は、時間の予算です(2026-11-12の管理担当の決定)。

- main 942a4deのpushのCI(run 43114316373)で、Mac Intelの`build`は47分10秒(上限60分)でした。`verify-universal`は16分43秒(上限30分)でした。
- `build`の内訳は、`check_r4b.py`が1回約13分50秒(2回で約27分40秒)、`check_r4r.py`が1回約54秒、品質の検査が約10分でした。
- 上限の2割を残す予算では、`build`にR4Eの比較を足せる時間は約50秒しかありません。
- R4Eの比較は、手元のLinux x86_64で1回約40〜70秒です。`build`で2回・`verify-universal`で1回流す形は収まりません。
- `build`に残る分(`r4e_contract`のreleaseビルド2回とexampleの単体検査)は、手元のLinux x86_64で約25秒増えました(releaseビルド1回あたり約8秒、exampleの単体検査約9秒)。Mac Intelでの増え方は未確認です。

4機種と署名後のMacのCIでの成功は未確認です。

## S3I-Aの必須比較と検査専用入口

- 比較器`check_implement_guard.py`は、新しい2つのジョブ`implement-guard`(`build`の後・4機種)と`implement-guard-universal`(`combine`の後・Mac2機種)で流します。`build`と`verify-universal`では流しません(2026-11-12 12:59の決定)。
- 4機種と署名後のMacのCIでの成功は未確認です。

比較はPython3.10系・root以外の利用者で行い、Cコンパイラ(`cc`)を要ります。#474の部品(`implement-guard.sh`のGitを使わない部分の移植)は未登録・未配布です。検査専用入口は`implement_guard_contract`(cargoのexample)です。[契約](../../docs/design.md#implement-guardのgitを呼ばない3コマンドと記録の読み取りs3i-a)と [実行手順・件数](../../docs/testing.md#s3i-aの比較)を参照してください。

- `quality()`は通常のCargo検査に加え、`cargo test --locked -p dev-workflow-cli --example implement_guard_contract`を実行します。Macでは、この単体テストがerrnoの文を全番号と表の外の1000で`os.strerror`と照らします。
- Linux2種・Mac2種の`build`/`reproduce`は、`implement_guard_contract`を`CI_BINARIES`の1つとして各2回作ります。
  - Linuxでは、静的にリンクした実行ファイル(ELF。共有ライブラリを読まない形)かを検査し、2本のバイト列を比べます。
  - Macでは`reproduce`がCPUごとの2本のバイト列を比べ(#516)、`combine`が2回分を結合・署名し、署名後のバイト列を比べます。`verify-universal`は署名と2種類のCPUを確かめます。
  - 転送の一覧(`BUILD_FILES`・`UNIVERSAL_FILES`)にも入ります。
- `implement-guard`: `build`のtarを受け取り、1本目の`implement_guard_contract`で比較器を流します。2本が同じバイト列であることは、比較の前にこのジョブが`cmp`で確かめます(#516。`reproduce`も4機種で確かめます)。範囲はLinuxが`full`、Macが`mac`です(比較器が機械で決めます)。
- `implement-guard-universal`: 署名と2種類のCPUを確かめてから、範囲`signed`で比較器を流します。Linuxの名前を渡すと失敗します。
- 起動の記録の確かめ: Linuxでは`strace`(プロセスの起動〈`execve`〉を記録する道具。ptraceという仕組みを使う)で、Rustの入口が`cp`のほかを起動しないことを確かめます。CIでは`strace`を入れません。実行機に無いかptraceが使えなければ、比較器はその確かめを未実施と記録します。
- `/dev/null`を開けない起動: 利用者の名前空間(`unshare -U -m`。権限なしで、そのプロセスだけのファイルの見え方を作る仕組み)を作れる機械だけで流します。作れなければ、比較器が未実施と記録します。
- 詰めたFIFOの信号: 段の始めに1回だけ、書き込みで止まったことを`/proc`から見分けられるかを試します。
  - 見分け方は、状態が`S`・syscallの番号が機械のwrite(x86_64は1、aarch64は64)・そのfdが詰めたFIFOを指す・`wchan`がパイプへの書き込みで待つ関数の名前(`pipe_write`・`anon_pipe_write`・`fifo_pipe_write`)のどれか、のすべてです。後の2つの名前は、新しいカーネルの名前への読み替えです(推測)。
  - `wchan`は、コンパイラが付ける`.`から後ろの接尾辞(例: `pipe_write.isra.0`)を落として比べます。名前の集合と接尾辞の扱いは、2026-11-19 03:44の管理担当の判断(Issue #474の追加修正記録)によります。
  - 書かせてから10秒の中で見分けられなければ失敗にし、段のケースを流しません。問題の文に、最後に読んだ`stat`・`syscall`・`wchan`の値を出します。
  - writeの番号を持たないLinux(CIに無い機械)と、Linuxでない機械だけは、未実施と記録します。
  - aarch64の見分け方は、単体の自己検査でだけ確かめました。
- strace と名前空間の試しは、20秒で終わらないか、起動できなければ未実施と記録します。使えない機械で長く止まらないようにするためです。
- 比較器は、範囲ごとのケースの一覧(群ごとの件数とラベルの一覧のsha256。`implement_guard_fixtures/case_lists.json`)と、Linuxではerrnoの文の表(`gen_strerror.py --check`)も照らします。
- 上限は`implement-guard`が45分、`implement-guard-universal`が20分です。手元の所要時間の2倍以上にしました。`build`と`verify-universal`の上限は変えていません。
  - 手元(基準の容器・Linux x86_64・2026-11-12)の範囲`full`は、実装担当の回で700秒と727秒、管理担当の検証の回(commit `c4ded694`)で532秒でした(比較器の自己検査と旧selftestの2通りを含みます。実装担当の回は、ほかの検査と同時に流しました)。
  - CIの直しの後(2026-11-13・実装担当の回)の範囲`full`は、554秒でした(比較器の自己検査23件と、Pythonで書く記録の段を含みます)。
  - 詰めたFIFOの段の見分け方の直しの後(2026-11-19・実装担当の回)の範囲`full`は、650.6秒でした(比較器の自己検査38件・31.0秒と、詰めたFIFOの段の48件・49.1秒を含みます)。
  - 45分(2700秒)は、最も長い727秒の約3.7倍で、2倍以上の目安を満たします。
  - 範囲`signed`は、実装担当の回(2026-11-13)で25秒でした(比較器の自己検査23件を含みます)。20分は、その2倍以上です。
  - CIの4機種とMacでの時間は未確認です。
- `package`は製品用のLinux2本とMac1本だけを配布用へ渡します。`implement_guard_contract`は`native-dist.tar`の5ファイルへ入りません。
- `distribution`の`needs`に2つのジョブを足しました。全ジョブの成功がマージの条件なので、2つのジョブも必須です。`check_workflow.py`の`REQUIRED_NATIVE_JOBS`・`COMPARISON_JOBS`(R4Eと共通の比較のジョブの一覧)が、ジョブ・工程・機種・`needs`を確かめます。
- 取り込みの後(main d63703f。R4E #482 を含む)は、`REQUIRED_NATIVE_JOBS`が11個です。`COMPARISON_JOBS`は、`r4e`・`r4e-universal`の`needs`(`build`・`combine`)も確かめます。

CIのジョブの手元での再現(リポジトリ直下):

```sh
python3 -B native/ci/build.py implement-guard --platform linux-x86_64
python3 -B native/ci/build.py implement-guard-universal --platform macos-arm64
```

次の2つはCIでは流しません。旧版か道具の版が変わったときに、手元の基準の容器で流します。

```sh
python3 -B native/ci/implement_guard_measure.py --check   # 旧版の測定の照らし(値を省くと implement_guard_fixtures/expected_old.json)
python3 -B native/ci/implement_guard_mutations.py         # Rust のソースへの変異の確かめ
```

## R4A残り5本の必須比較と検査用実行物

- 5本(`resolve-task-store`・`candidate-keys`・`host-argv`・`loop-task-text`・`loop-child-output`)の移植は、未登録・未配布です。
- 実行物は開発用の `r4a_rest_contract` です。
- 契約は [設計のR4A節](../../docs/design.md#残り5本の解析補助r4a)、APIは [native/README.md](../README.md#残り5本の解析補助r4a)、手順と件数は [testing.md](../../docs/testing.md#残り5本の解析補助の比較r4a)にあります。
- 必須caseは2034件です。比較器の自己検査(`test_r4a_rest.py`)は117件です。境界の組は21組です。

- `quality()`は、`cargo test --locked -p dev-workflow-cli --example r4a_rest_contract`と、`-p dev-workflow-platform --example r4a_rest_platform`を実行します。
- 専用比較(`check_r4a_rest.py --strict-pending`)は`build`・`verify-universal`には入れず、必須の2ジョブで流します。
  - `r4a-rest`: `build`の後。4機種で、同じ機種の`1/`と`2/`の`r4a_rest_contract`が同じことを`cmp`で確かめてから、`1/`を比べます(#516)。比較器の自己検査は、その1回の比較で流します。
  - `r4a-rest-universal`: `combine`の後。Mac 2機種で、署名後のMac共通の実行物を各CPUで比べます。
  - どちらも、`actions/setup-python`の3.10.11の後に`build.py prepare`を流します。`build.py`は、そのPythonで比較器を起動します。比較器は、自分を動かしたPythonで旧版を動かします(環境変数`R4A_REST_PYTHON`は、比較器は読みません)。
- 比較器は、比較の前に自己検査117件を流します。落ちても、本体の比較は流し、終了コードは1のままです。最後の行で自己検査の失敗を知らせます。
- 自己検査は、検査専用の絞り込み`--case <ID>`で短くなりました。絞り込んだ走りは、`--only`と同じ部分比較です。CIの`build.py`は、この引数を渡しません。
- 作業機での実測は、自己検査が約45秒(絞り込みの前は約1319秒)、比較が約95秒です(2026-11-23、自己検査114件の時点。作業機の負荷でばらつきます。以前の比較は約100〜123秒)。
- 比較器が起動するすべての子は、グループで記録し、走りごとの印`R4A_REST_RUN`で終わりに確かめます。残っていれば、止めて終了コード1にします。
  - 時間切れと例外ではグループごとSIGTERM、少し待ってからSIGKILLです。外からSIGTERMを受けたときは、作業フォルダも消してから終わります。LinuxはPDEATHSIGも使います。
  - Macの`r4a-rest`で、終わりに`r4a_rest_contract`が各12個残りました。commit 33d51ecとab0b9abの、どちらのpush用CIも、この仕組みの前の版です。
  - 確かめた事実: commit 33d51ecのCIのMacでは、本体の比較が流れず、自己検査だけが流れました。その中で、各12個が残りました。
  - ab0b9abでも、Mac 2機種で各12個でした。pidの範囲は、33d51ecがMac x86_64で10661〜14602・Mac arm64で7489〜13422、ab0b9abがMac x86_64で9640〜15088・Mac arm64で7867〜14664です。
  - ab0b9abでは自己検査と本体の比較の両方が流れました。本体の比較でも残ったかは未確認です(pidの範囲は、自己検査と比較に分けてログに出ません)。
  - 自己検査が起動した子が止められたとき、その下のRustが残った経路と推測します(未確認)。次のCIで、0個になるかを確かめます。
- 上限は、比較1回の上限(900秒)×回数 + 自己検査の見込み(900秒)+ 準備と余裕で決めました。`r4a-rest`は60分、`r4a-rest-universal`は45分です(`r4a-rest`は2回の時の値のままです。比較は#516で1回になりました)。
  - CIの実測(commit ab0b9ab、自己検査80件、絞り込みの前、[Native run 43250661920](https://github.com/Yuki-Maeda-valour/workflow/actions/runs/43250661920))は、自己検査がLinux x86_64で1055.9秒、Linux arm64で1026.5秒、Mac x86_64で1286.1秒、Mac arm64で850.1秒でした。
  - 比較1回は、Linux x86_64が約117秒、Linux arm64が約99秒、Mac x86_64が298.8秒、Mac arm64が140.2秒でした。
  - 絞り込み後の自己検査は、作業機の約42秒(commit e74acea の時点、105件)に、Mac x86_64の遅さ(同じ80件で作業機の約1.2倍)をあてはめると約50秒になります(見込み。未確認)。
  - 見込みの合計(Mac x86_64)は、`r4a-rest`が約11分(比較2回の時の見込み。#516の後は1回)、`r4a-rest-universal`が約6分です。準備とダウンロードは含みません(Linuxでは約30秒、Mac x86_64では約74秒でした。ab0b9abのジョブの始めから、`r4a-rest`の手順までの時間です)。
  - 最悪の場合(比較が上限900秒を使い切る)でも、約31分(`r4a-rest`。比較2回の時の見込み)と約16分(`r4a-rest-universal`)で、上限の内です。上限は、絞り込み後のCIの実測が出てから見直します。今は変えません。
- `r4a_rest_contract`は転送用の成果物に入りますが、配布物には入りません(`make_dist.BINARY_PATHS`に無いことを検査します)。
- Macの`strerror`は、Apple Libc `Libc-1725.40.4`の表(`errno_macos.rs`)を使い、OSの`strerror`は使いません。比較器が、旧版の`os.strerror`と表を照らします(違えば失敗)。
  - 一致しました。Linux x86_64・Linux arm64・Mac x86_64・Mac arm64のすべてです(commit ab0b9ab の push 用 CI、[Native run 43250661920](https://github.com/Yuki-Maeda-valour/workflow/actions/runs/43250661920))。
- Macの故障の差込みは、`fault_shim.c`を`cc -dynamiclib`で作り、`DYLD_INSERT_LIBRARIES`で旧版に当てます。差込みが効くことを、OSごとの固定の期待にしています(Linux・Macとも「効く」)。commit ab0b9ab の push 用 CI では、Linux x86_64・Linux arm64・Mac x86_64・Mac arm64のすべてで、旧版に効きました。`cc`が無い・ビルドの失敗・効きの期待違いは、どのOSでも失敗です。CIで効かないと分かったときに、期待を固定し直します。
- `/dev/full`を使う満杯の出力と、待ちの状態を`/proc`で確かめる信号のcase(`requires_linux`、86件)は、Linuxだけで流します。
- Macの未実施は、固定の一覧のとおりのときだけ`結果: 未完(…)`で終了コード0にします。件数を出し、成功には数えません。一覧と違えば失敗です。
  - `requires_raw_name`(2件): UTF-8でないファイル名に頼るcaseです。macOS(APFS)は、この名前を`EILSEQ`で拒みます。拒まれる名前の集合も、OSごとに固定します。
  - locale(7件、caseのIDで固定): 旧版の`ja_JP.eucJP`の固定期待の照らしだけを未実施にします。Rustと、設定なしの旧版の比較は必ず流します。
- Macの本体の比較は、commit ab0b9ab の push 用 CI で、初めて流れました(Mac x86_64・Mac arm64とも1935/2034、未実施95件。固定の一覧のとおり)。
  - 不一致は4件で、どれも旧版の固定期待の照らしでした。OSごとの期待にして直しました(commit e74acea)。
  - 故障のcase 3件は、Macの旧版の標準エラーが`Exception ignored in: <_io.TextIOWrapper name='<stdout>' …>`の2行になるためです。Tracebackの期待は、Macでは無しです。原因のbufferの大きさは推測です(未確認)。
  - permlogのsymlinkの固定期待は、ELOOPのerrnoがMacで62です(Linuxは40)。
  - Macの自己検査は、Linuxでまねる検査3件が落ちました。Linuxだけで流すようにしました。
- commit e74acea の push 用 CI([Native run 43296607476](https://github.com/Yuki-Maeda-valour/workflow/actions/runs/43296607476))の結果です。
  - `r4a-rest`は、Linux 2機種とMac x86_64が成功しました。Macは`結果: 未完`の終了コード0で、未実施95件は固定の一覧のとおりでした。
  - 自己検査(105件)は、Linux x86_64が38.0秒、Linux arm64が43.6秒、Mac x86_64が67.9秒、Mac arm64が43.5秒でした。比較1回は、Linuxが約93〜141秒、Mac x86_64が240.9〜343.8秒、Mac arm64が208.3〜396.7秒でした。
  - 残った子は、どのログにも無く、0個でした。
  - Macの自己検査105件は、2機種で「OK (skipped=7)」で合格しました。skipした7件は、Linuxだけで意味のある検査です。Macで初めて流れた`ps -E`で印を探す検査と`pgrep`を使う検査も、通りました。
  - Mac arm64の`r4a-rest`と、`r4a-rest-universal`のMac x86_64は、比較器の不安定さで失敗しました。Rustと旧版の差ではありません。
    - 疑似端末の読み残し: 親が子の起動の直後にslaveを閉じると、Macではmasterの読み残しが消えることがあると推測します。親は、子が終わって読み切るまでslaveを開けておくようにしました。読み切りの判定は時間に頼らず、子が終わった後にslaveへ一意の目印を書き、masterから目印が出るまで読みます(出なければ失敗)。
    - 子1つの上限: 旧版が5 MBのJSONを標準入力で読むcaseが、子1つの上限15秒に当たりました。当時は遅い機械のためと考えて、上限を60秒にしました。commit 63e111aのCIと手元の確かめで、入力の渡し方の不具合が見つかりました(次の項)。e74acea の15秒の時間切れも、同じ原因と推測します(未確認。同じ渡し方の誤りが、e74acea の比較器にもありました)。上限60秒はそのまま残し、失敗の文にかかった秒数を出します。比較全体の上限(900秒)は変えません。
  - `r4a-rest-universal`のMac arm64は、機械が割り当たらず、取り消されました。
- commit 63e111a の push 用 CI([Native run 45097810895](https://github.com/Yuki-Maeda-valour/workflow/actions/runs/45097810895))では、`build` 4機種・`r4a-rest` 4機種・`r4a-rest-universal`のMac arm64が成功しました。`r4a-rest-universal`のMac x86_64は1件(`child/legacy/origin-json: reason-huge`の時間切れ)が落ちました。
  - 原因は、比較器の入力の渡し方でした。`communicate`の1回目の0.2秒で書き切れなかった大きな入力の残りが、2回目から送られず、標準入力も閉じられませんでした。遅さではありません(手元で同じ入力を旧版へ直接渡すと0.04秒でした)。
  - 直し: 入力は、書き込み専用のthreadが最後まで書いて標準入力を閉じます。8 MBの入力を、眠ってから読む子へ渡す自己検査を足しました。
- 次は未確認です。CIの結果を見てから書きます。
  - 入力の渡し方を直した後の、`r4a-rest` 4機種と`r4a-rest-universal` 2機種の結果。
  - `r4a-rest-universal`のMac x86_64で、`child/legacy/origin-json: reason-huge`が通るか。
  - 必須case 2034件・自己検査117件での、各機種の時間。
- 自己検査で、印で子を探す確かめが、子の始まる前に探して落ちることがありました(#513)。子の合図を受けてから探す形に直しました。記録は[testing.md](../../docs/testing.md#残り5本の解析補助の比較r4a)にあります。
- 疑似端末の読み切りで、端末に子の出力が残ったまま終わりの目印を書き、書き込みが止まりうる順番がありました(#522)。子の終わりを `select` の前に読む形に直しました(#519 の R4U と同じ形)。記録は[testing.md](../../docs/testing.md#残り5本の解析補助の比較r4a)にあります。
- 詳しい仕組み(境界の組・待ちの確かめ・子の回収・`--case`・OSごとの期待・差込みの診断`R4A_CALLS`・Macの条件のまね)は、[testing.md](../../docs/testing.md#残り5本の解析補助の比較r4a)にあります。

## R4Cの必須比較と検査用実行物

- 候補の採用と改名(旧`adopt-candidate.py`。#478)の移植は、未登録・未配布です。
- 実行物は開発用の`adopt_candidate_contract`です。事前検査のために、同じ成果物の`diff_snapshot_contract`(S3Dの検査用実行物)も比較の入力にします。
- 契約は [設計のR4C節](../../docs/design.md#候補の採用と改名r4c)、APIは [native/README.md](../README.md#候補の採用と改名r4c)、手順と件数は [testing.md](../../docs/testing.md#r4cの比較)にあります。
- 旧版との比較は、固定期待の照合(旧版411件・基準16件)・Rust432件・承認済みの差(旧版112件・Rust40件。E24の36ケースを含む)・未接続0件でした(2026-11-24・作業機)。
  - 作業機(Ubuntu 22.04・bash 5.1.16)の回は、版に依る4ケース(E20cと、E21のうち3ケース)が失敗し、終了1でした。基準の版の容器(Ubuntu 24.04)では通りました。
  - 作業機で比較1回は約228秒です。件数と内訳は [testing.md](../../docs/testing.md#r4cの比較)にあります。

- `quality()`は、`cargo test --locked -p dev-workflow-cli --example adopt_candidate_contract`を、取り込んだS3DとR4Aの部品の単体検査を外して実行します。
  - 外す名前は、`build.py`の`ADOPT_IMPORTED_SKIPS`の5つ(`diff_snapshot::`・`candidate_keys::`・`r4a_rest_support::`・`resolve_task_dir_definition::`・`task_digest_definition::`)です。S3DとR4Aの部品の単体検査は、それぞれのexampleの単体検査(`build`の`quality()`。S3DのMacは`s3d`ジョブの型検査)に残します。
  - `--skip`は名前の一部に一致したものを外すので、R4Cの検査の名前に、この5つを含めません。
- 専用比較(`check_adopt_candidate.py`)は`build`・`verify-universal`には入れず、必須の2ジョブで流します(2026-11-12 12:59の決定)。
  - `r4c`: `build`の後。4機種で、同じ機種の`1/`と`2/`の`adopt_candidate_contract`を、同じ番号の`diff_snapshot_contract`と組にして比べます。Linuxでは`--require-userns`を付けます。
  - `r4c-universal`: `combine`の後。Mac 2機種で、署名後のMac共通の実行物を各CPUで比べます(署名し直しません)。
  - どちらも、`actions/setup-python`の3.10.11の後に`build.py prepare`を流します。比較器は、自分を動かしたPythonで旧版を動かします(`--old-python`の既定)。
  - どちらも、比較の前に`prepare_r4c`が道具を用意します。旧版の事前検査が`bash`で旧`diff-snapshot.sh`を動かすためです。
    - S3Dの`s3d`と同じ道具の準備(`s3d_ci.prepare_tools`。Macはbrewのbash・GNUの道具・git-lfs)と、GNUの道具だけのPATH(`s3d_ci.environment`)です。
    - Linuxでは、`/proc`を隠す利用者の名前空間(`unshare -rm`)を、GitHubのUbuntu 24.04のAppArmorの制限から外します(R4Gと同じ。`kernel.apparmor_restrict_unprivileged_userns=0`)。
  - 比較は`adopt_probe`が流します。`--evidence`を付け、機種で変わる旧版の診断の生出力を、その機種のログへ残します。
- 比較器は、旧版の前提(bash 4.0以上とGNUの`grep`・`find`・`mktemp`・`sort`)を確かめて記録します。前提が無い組は、基準の機械のLinuxでは失敗、Macで用意できなければ名前と理由を付けて未実施にします。成功にもskipにもしません。
- 版に依るケース(E20cと、E21のうち3ケース)は、基準(CIのUbuntu 24.04の版)と違う機械で、Linuxは失敗、Macは未実施にします。版に依る欄の無いケースは、どの機械でも比べます。
- 上限は、`r4c`・`r4c-universal`とも30分です(`check_workflow.py`が確かめます)。見積もりは約25分で、余裕が小さいです。
  - 作業機の比較1回は約228秒です。`r4c`は1機種で比較2回、`r4c-universal`は1回です。
  - CIの機種ごとの時間は未測定です。PRのCIで記録してから、上限を見直します。
- `adopt_candidate_contract`と`diff_snapshot_contract`は、転送用の成果物に入りますが、配布物には入りません(`make_dist.BINARY_PATHS`に無いこと)。`adopt_candidate_contract`は`test_native_ci.py`が、`diff_snapshot_contract`は`test_s3d_ci.py`が確かめます。
- errnoの文は、Linuxだけが基準の表(glibc。131件)をRustが持ちます。MacはOSの`strerror`(旧Pythonと同じ関数)を使います。
  - 比較器は、比較の前に、旧Pythonの`os.strerror`を基準の表(Linux 131件・Mac 12件)と照らし、違えば失敗にします(`verify_errno_baseline`)。
  - Rustの表(`diagnostics.rs`)と比較器の基準の表の一致は、全回帰テストの`scripts/test_native_adopt_candidate.py`が確かめます。
  - CIのglibc 2.39・macOSとの照合は未確認です(PRのCIで確かめます)。
- 詳しい仕組み(件数・版に依る欄・順序を決めないSIGINT・事前検査の観測の包み・子のモード)は、[testing.md](../../docs/testing.md#r4cの比較)にあります。

## S2Eの必須比較と検査用実行物

#470の環境照合9コマンドと内部readerは未登録・未配布です。検査用の入口は`s2e_contract`(cargoのexample)で、製品のCLIには登録しません。比較器は`check_s2e.py`(部品は`s2e_commands.py`。自己検査は`test_s2e.py`。固定期待は`s2e_fixtures/`)です。

- 契約は [設計のS2E節](../../docs/design.md#environment-guardの環境照合と内部readers2e)、APIは [native/README.md](../README.md#environment-guardの環境照合s2e)、手順と件数は [testing.md](../../docs/testing.md#s2eの比較)にあります。

```bash
python3 -B native/ci/build.py s2e --platform linux-x86_64
python3 -B native/ci/build.py s2e-universal --platform macos-arm64
```

- `quality()`は、`cargo test --locked -p dev-workflow-cli --example s2e_contract`を実行します。`build`に残るS2Eの分は、この単体検査と`s2e_contract`のビルドだけです。
- Linux2種・Mac2種の`build`/`reproduce`は、`s2e_contract`を`CI_BINARIES`の1つとして各2回作り、tarに入れます(`BUILD_FILES`・`UNIVERSAL_FILES`にも入ります)。Macの結合と署名は[R4Bの節](#r4bの必須比較と検査用実行物)と同じです。
- 比較(`check_s2e.py`)は`build`・`verify-universal`には入れず、必須の2ジョブで流します(2026-11-12 12:59の決定)。
  - `s2e`: `build`の後に4機種で流します。`build`のtarを受け取り、2回のビルドの`s2e_contract`のそれぞれで`check_s2e.py --fixture <絶対パス>`を流します(`s2e_probe()`)。
  - `s2e-universal`: `combine`の後にMac2機種で流します。署名後のtarを受け取り、署名と2種類のCPUを確かめてから比較します。Linuxの名前を渡すと失敗します。
  - どちらも`actions/setup-python`の3.10.11の後に`build.py prepare`を流します(`requirements-dev.txt`を入れます)。`prepare`はRustと開発依存を用意します。受け取ったtarの記録を照らす`read_record`→`identity()`が`rustc 1.99.1`を要るためです。比較器はPython3.10系・非rootでなければ止まります。
  - `s2e_probe()`は環境変数`S2E_SIMULATE_MAC`を外して流します。比較器は、模擬の合格を終了コード3(Linuxの合格ではない)で返すので、CIでは合格にしません。
- `distribution`の`needs`には足しません(S3D・R4Cと同じ。末尾2つを`r4g`・`r4g-universal`に固定する既存の検査を変えないため)。必須のジョブの一覧(`REQUIRED_NATIVE_JOBS`)には足したので、ジョブが無いと`check_workflow.py`が失敗します。
- `check_workflow.py`は、`s2e`(4機種・`build`の後)と`s2e-universal`(Mac2機種・`combine`の後)の工程・機種・`needs`(`S2E_JOBS`)と、上限(`S2E_TIMEOUTS`)を確かめます。`test_native_ci.py`の`S2EConnectionTests`が、`build.py`の流れ・署名とCPUの確かめ・ワークフローの形を確かめます。
- Linuxだけの項目の扱い: 比較器が各項目に付けた「Linuxだけ」の印(`EXPECTED_LINUX_ONLY`の36項目)は、Macでは流しません(流さなかった名前と理由は出力に出ます)。理由は、Rustの`bootstrap`が要ること・`PATH_MAX`・`~user`・localeの名前です([設計](../../docs/design.md#linuxだけにした比較の項目)に表があります)。`/dev/full`は、36項目の印とは別に`has_dev_full()`で切り替え、Macの経路では`pipe-closed`だけで比べます。機種ごとの項目数は完全一致で確かめます。
  - 手元のLinuxからMacの経路を模擬するには`S2E_SIMULATE_MAC=1`を付けます(比較器の検査用。Mac実機の確認ではありません)。
- 比較器自身の検査(`test_s2e.py`。秒は、各回の全体から比較と9コマンドを引いた「残り」で、[testing.md](../../docs/testing.md#s2eの比較)の「時間」の表にある)は、`s2e`の1回目だけ流します。2回目は`s2e_probe(fixture, selftest=False)`が`check_s2e.py --no-selftest`を付けて省き、比較器が「比較器自身の検査は省いた」と出力します(件数の行の自己検査の欄は「省略(--no-selftest)」。最後の行は`S2E: 合格`です)。`s2e-universal`は1回なので必ず流します。
- 件数(手元のLinux x86_64で`check_s2e.py`を流した値):
  - 比較: 710件(通常の一致691・認めた差19)。比較器自身の検査: 84件。
  - 9コマンドの旧新比較: Linux 233項目(全機種で必須197・Linuxだけ36)、Macの経路197項目。機種ごとの項目数は完全一致で確かめ、どの項目もRustを起動した比較が1件以上なければ不合格です(Rustを起動した比較は計1751件)。
  - 既存テストの対応表: 117件。対応表が指す比較のケースとRustのテストの名前: 256件。
- `gen_cases.py`(`s2e_fixtures/`)はCIで流しません。固定期待を作り直す人がLinuxで流します(`--check`もLinuxで流します)。Linux以外では`main2`の領域が理由を出して止まります。Macの疑似端末の挙動は未確認です。
- 注意(機種を問わない): コンテナのPID 1が子を回収しない機械では、`left`が旧新とも真になる誤判定が出ないことを確かめます。
- 1回の比較の上限(内側の上限`S2E_TIMEOUT`)は3000秒(50分)です。

ジョブの上限(`check_workflow.py`の`S2E_TIMEOUTS`)は、`s2e`が120分、`s2e-universal`が60分です。`build`(120分)・`verify-universal`(60分)と同じ枠にしました。S3Dと同じ形で、内側の上限×比較の回数+準備(約5分)が、ジョブの上限より小さくなるように決めました(内側の上限が先に効きます)。
- `s2e`(2回): 50分×2+5分=105分<120分。`s2e-universal`(1回): 50分+5分=55分<60分。
- 測った値(手元の作業機)は、回ごとの表にまとめました([testing.md](../../docs/testing.md#s2eの比較)の「時間」)。CIの見積りに使ったMacの経路の模擬の値は、次の3つです。
  - 自己検査つきのMacの経路の模擬: 310.1秒(2026-11-26)。
  - 前の見積りの元: 326.1秒(2026-11-25。コマンドに`--no-selftest`が無い回)。
  - `--no-selftest`付きのMacの経路の模擬: 177.0秒。
- Linuxの見積りには、表のLinuxの全体の447.5〜477.4秒の回と、`--no-selftest`の269.9〜315.8秒の回を使いました(1114.1秒・563.6秒の回は使っていません)。
- 倍率の出典([testing.md](../../docs/testing.md#s2eの比較)の「CIの時間の見積り」に値と節があります): R4Eは手元の40〜60秒に対しCIのMac x86_64が315秒(約5.3〜7.9倍)で、CIのLinux x86_64の98秒の3.2倍でした。R4Aの残りはMac x86_64が298.8秒、Linux x86_64が117.1秒で、2.55倍でした。
- 見積り(推測。CIでは未測定): 元の値は、自己検査を含むMacの経路の模擬310.1秒(2026-11-26)です。`s2e-universal`の1回と`s2e`の1回目は自己検査を流すので、この条件に当たります。
  - 手元からCIのMac x86_64への倍率(R4Eの5.3〜7.9倍)を当てると、1回は約1,640〜2,450秒(約27〜41分)です。この倍率はMac x86_64だけに当たる値で、Mac arm64の倍率は確かめていません。以前の元の326.1秒(コマンドに`--no-selftest`が無い回)なら約29〜43分です。
  - CIのLinuxからMac x86_64への倍率(2.55〜3.2倍)を当てると約13〜17分です(手元とCIのLinuxが同じ速さという仮定。未確認)。
  - 内側の上限50分は、時間の順に決めました。先に、326.1秒の見積りの上端(約43分)が当初の35分を超えたので50分にしました。後で測った自己検査つきの310.1秒でも、上端は約2,450秒(約41分)で35分を超えます。50分には約550秒(約9分。326.1秒なら約7分)の余裕があり、倍率が上端を超えるかCIが混むと足りないおそれがあります。上限の値は変えていません。
  - 177.0秒(自己検査を含まない)に同じ倍率を当てると約16〜23分で、Mac x86_64(Mac Intel)の`s2e`の2回目の見積り(推測)になります。Mac arm64とLinuxの機種には、この倍率は当たりません。どちらの見積りも、PRのCIの実測で確かめます。Linuxは、手元の値で1回約7.5〜8分(表の447.5〜477.4秒の回)、2回目は約4.5〜5.3分(表の269.9〜315.8秒の回)です。CIのLinuxが手元と同じ速さという仮定です(未確認)。
- 既存のジョブ(`build`の120分・`verify-universal`の60分など)の上限は変えていません。`build`に足した時間は`s2e_contract`のビルドと単体検査だけです(測定は未実施)。
- PRのCIで機種ごとの時間を記録してから、上限を見直します。50分を超えるときは、内側の上限・ジョブの上限・`check_workflow.py`を同じPRで直します。

Macの実機のCIで確かめることは次のとおりです(Linuxの手元では確かめられていません)。
- `/bin/bash`3.2で、環境の全体を書き出す手順(`compgen -e`・`${!v}`・`printf '%s=%s\0'`)がLinuxと同じ形になること。
- 旧Pythonが足すPEP 538の`LC_CTYPE`が、Rustの`coerce_c_locale`と一致すること。比較器の`no-lcctype`の4手順が通ること。
- 偽のRustの`sigint-leave-group`・`sigint-hold-pipes`を使う自己検査が、Macでも通ること。
- `other_outputs`が`bootstrap`のたびに`out/`の下を2回読むので、Macでの所要時間が延びていないこと。
- 既定の監視先(`/etc`は`/private/etc`へのリンク)で、旧版とRustの`bootstrap`がどちらも失敗すること(`run_mac_default`。推測が外れるとMacの`s2e`・`s2e-universal`が落ちます)。
- 旧版の`bootstrap`を置き換えた経路(`patched`)で作った出力一式での、`verify`以降の旧新比較が通ること(`run_patched_sample`)。
- Macで使うlocaleの名前(`LANG_NAME`=`en_US.UTF-8`)で比較が通ること。U1は、CIのLinuxに`en_US.UTF-8`が無いと「比較から外した物」として合格になるので、実行ログの「比較から外した物」の行で確かめます。
- 子の環境の`__CF_USER_TEXT_ENCODING`を比較から外す扱い(2026-11-12 02:41の決定。Macの旧Pythonが子の環境に足す変数)で、ほかの変数・値・並びが旧版と一致すること。
- `go`の待ちの上限`GO_TIMEOUT_MAC`の境目: 100000000以下と100000002以上を使います。100000001は旧版でも揺れると推測しています(未確認)。
- 機種ごとの項目数(Mac 197項目。今の値は`check_s2e.py`の`EXPECTED_ITEMS`)が完全一致し、項目ごとにRustを起動した比較が1件以上あること。
- 木の深さの端の比較は、Macでは流しません(`DEEP_REASON`でLinuxだけの印)。Macの`PATH_MAX`(1024)では約990段の絶対パスを作れないという推測で、Macでは未確認のままです。
