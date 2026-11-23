# S3D の profile 純粋部品

このディレクトリは、`secret-profiles.py` の snapshot 用の受理条件を保持する。
製品の実行中には Python を起動しない。
OS の操作、環境変数、Git、source の表示、既定値と和集合は CLI 側が受け持つ。

## 参照元とライセンス

- YAML の呼出段数の観測は [PyYAML 6.0.3](https://github.com/yaml/pyyaml/tree/6.0.3/lib/yaml) の `scanner.py`、`parser.py`、`composer.py`、`constructor.py`、`reader.py` に基づく。
  `legacy_scanner.rs` と `legacy_parser.rs` は、その token 取得順と状態遷移を再現する。
  値の構築と文法判定は既存の R4S の解析部品が受け持つ。
  PyYAML の MIT ライセンスを `LICENSE-PyYAML` に置く。
- 旧 helper の文字コード設定(`PYTHONIOENCODING`・`PYTHONUTF8`・`PYTHONCOERCECLOCALE`・locale)は、2026-11-11 22:24 の利用者決定で UTF-8 と `surrogateescape` に固定した。codec の表と、その生成用の Python は置かない。
  `codec.rs` は、和集合を `item.encode("utf-8")` で作るときの厳密な UTF-8 と、孤立 surrogate の失敗位置と文だけを持つ。
- CLI 側の旧 helper の起動時の失敗の文、`argparse` の使い方と誤りの文の書式は、[CPython 3.10.11](https://github.com/python/cpython/tree/v3.10.11) の `Python/initconfig.c`・`Lib/argparse.py` に基づく。
  CPython のライセンス全文を `LICENSE-CPython` に置く。
- 原版に対する変更は、Rust の反復処理と、旧 Python の呼出段数の明示計算である。
  元のファイルを実行時に読み込む方式ではない。

## 再帰の固定値

基準は CPython 3.10.11、PyYAML 6.0.3、通常の再帰上限 1000 である。
旧 helper 全体を別プロセスで起動して測る。
テスト関数から旧 Python の関数を直接呼ぶと残りの呼出段数が変わるため、その結果は使わない。

- `parser_budget.rs` は compose、merge、scalar の予算を持つ。`recursion.rs` は同じ予算型を再公開し、重複探索を数える。
- `loop_state/mod.rs` も同じ専用ファイルを相対参照する。既存の観測用テストで型を別実装へ置き換えない。
- compose の子は 2 段ずつ増える。map の value には追加の 1 段がある。
- scanner は token を要求された時点の段数を使う。先読み済みの token を後から要求しても、深い位置で再走査したとは数えない。
- simple key の候補、1024 文字の期限、flow と block の状態、未解決の key による先読みを保持する。
- Python の class 呼出しで増える C 側の段数も数える。`Mark` を作る際の値はその一例である。
- scalar 構築は root・seq・map/set・pairs 等の呼出元を区別する。
- alias の訪問済み集合を保つ。未使用値も旧版の順序で構築する。

`recursion_cases.json` は 421 入力を固定する。
初期 70 入力と、混在・先読み・scalar の呼出元を組み合わせた 351 入力である。
各入力は短い生成指定、生成結果の SHA-256、旧 helper の判定を持つ。
Rust の検査は同じ入力を独立に生成し、SHA-256 と判定を照合する。
Python の再観測は次の形で行う。

```bash
python3 -B native/crates/core/src/s3d_profile/generate_recursion_cases.py \
  --helper plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py --check
```

Python は上記の固定版でなければ停止する。
helper のソースの SHA-256 も照合する。
通常の開発テストは固定データを使うため Python を必要としない。

## block scalar ヘッダの固定値

`block_scalar_cases.json` は126入力を持つ。
`|` と `>`、ヘッダ21種、本文の字下げ3種を組み合わせる。
ヘッダコメント内の数字を、本文の字下げ指定として解釈しない。
次の入口は旧 helper 全体で判定を取り直す。

```bash
python3 -B native/crates/core/src/s3d_profile/generate_block_scalar_cases.py \
  --helper plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py --check
```
