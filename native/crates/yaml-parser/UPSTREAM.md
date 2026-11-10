# YAML解析部品の出典

`workflow-yaml-parser 0.1.0`は、`saphyr-parser 0.1.2`をloop-stateの旧YAML規則に合わせたライブラリである。

- [上流commit](https://github.com/saphyr-rs/saphyr/tree/478b9f76addf0687478113e0dd3a1dba86e4d274/parser): `478b9f76addf0687478113e0dd3a1dba86e4d274`。
- [公開crate](https://crates.io/crates/saphyr-parser/0.1.2)のSHA-256: `f54c7c96aa2abeb55dbdcda75287722d5e454f020aaa817895d4d62b9785073a`。
- ライセンスは`MIT OR Apache-2.0`である。`LICENSE`と`.licenses/`内の4原文を保持する。
- 直接依存は`arraydeque =0.5.1`と`thiserror =2.0.21`である。両方の既定機能を無効にする。間接依存はworkspaceの`Cargo.lock`で固定する。
- 上流の実行ツール、外部テスト用依存、環境を読む`debug_prints`は含めない。

## 局所修正

- `scanner.rs`: scalarを`Vec<u32>`で保持する。escape内のsurrogateを保持する。Unicode改行、アンカー文字、directive、tag URIのUTF-8復号を旧版に合わせる。flowの深さ255の制限を除く。TAB、暗黙キーの長さと行、block末尾、flowの空白条件を旧版に合わせる。版の数字には呼出元から渡された整数変換上限を適用する。
- `parser.rs`: scalarイベントを整数列へ変更する。重複アンカー、宣言の保持、空キーと文書終了記号を旧版に合わせる。ドキュメント内のcrate名をローカル名へ変える。
- `input.rs`・`input/str.rs`: 閉じたtokenの直後のコメントを受け付ける。EOFで文字走査を止める。
- `char_traits.rs`: NEL・LS・PSを改行へ含める。EOFのNULを文字走査から除き、URIの文字条件を旧版に合わせる。不要になった上流のアンカー文字判定を除く。
- `lib.rs`・`debug.rs`: ローカル用途を記す。診断出力と環境の読取りを除く。unsafeを禁止する。
- 元の著作権表示を保持する。Rustの書式とlintに合わせた変更も同じファイルに含む。

coreは`Parser::new_from_iter`を使う。UTF-8とReaderの許容文字を先に検査し、先頭BOMを1個だけ取り除く。
最適化された`StrInput`はUnicode改行の互換経路には使用しない。
このcrateはイベントだけを作る。YAML 1.1の型解決、全ノードの明示重複検査、SafeLoader相当の構築はcoreが行う。

## 更新方法

1. 更新の目的と旧版からの差を正式Issueへ記す。依存を自動更新しない。
2. 元crateの版・commit・SHA-256・ライセンス原文を確認する。
3. 元版との差分を調べ、上記の修正を新しい版へ移す。
4. 固定634件、JSON境界、Unicode全域、比較器の誤合格拒否、通常のRust検査を全て通す。
5. 新しい動作差と上限を無断で許可しない。独立レビュー後に出典と固定依存を更新する。
