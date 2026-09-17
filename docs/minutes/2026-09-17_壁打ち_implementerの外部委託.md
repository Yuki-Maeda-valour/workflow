# 壁打ち記録: implementer の外部委託(Codex)— 設計は Claude / 実装は Codex

- **日付**: 2026-09-17
- **形式**: /discuss-spec による対話(差分モード)
- **反映先**: `docs/design.md` §7-2(方針)/ `do-task/references/external-runners.md`(契約)/ `delegation-map.md`(解決)
- **関連 Issue**: #51
- **扱った論点**: 1 方針の見直し / 2 起動経路 / 3 機密ガード / 4 信頼モデル / 5 フォールバック / 6 契約の置き場所 / 7 検証の維持(全 7 論点)

## 前提(実測で確認した事実)

| 事実 | 確認方法 |
|---|---|
| **§7-2 の「implementer の外部化は行わない(スコープ外)」は検討の結果ではない** — 出典の決定録(2026-09-09)に該当する決定は 1 件も無く、`features.implementer: internal \| cursor` のスキーマだけが残っている | `docs/minutes/2026-09-09_壁打ち_AI非依存の開発基盤.md` を grep(0 件) |
| `codex-cli 0.153.4` / ChatGPT ログイン済 / `review-agent.sh --runner codex --dry-run` が `codex exec --sandbox read-only` を出力し exit 0 | ローカル実測(2026-09-17) |
| `codex exec` は `-C, --cd <DIR>` と `-s, --sandbox`(`read-only` / `workspace-write` / `danger-full-access`)を持つ | `codex exec --help` の実測(2026-09-17)。不正値 `-s bogus` で `invalid value ... [possible values: ...]` が返ることも確認 |
| **`codex exec resume` は `-s/--sandbox` も `-C/--cd` も受け付けない**(`error: unexpected argument '-s' found` / 同 `'-C'`)。ヘルプにも両フラグと `workspace-write` の語が存在しない | ローカル実測(2026-09-17)。`codex exec resume -s bogus --last` / `-C /nonexistent --last` の双方でパース段階で失敗 |
| OpenAI 公式プラグイン `openai/codex-plugin-cc` v1.0.6 は skills(3)+commands(8)+agent(1)+hooks(3)、常時 ~449 tok。`codex:codex-rescue` は `Agent` の `subagent_type` で呼べる subagent | `claude plugin details` + プラグイン本体の読解(2026-09-17) |
| **plugin は codex CLI の薄いラッパー** — agent 定義自身が "thin forwarding wrapper" と名乗り、`--write` は内部で `sandbox: "workspace-write"` に解決され、resume は app-server の `thread/resume` を叩く | `agents/codex-rescue.md` / `scripts/codex-companion.mjs:491` / `scripts/lib/codex.mjs:750` の実測 |
| plugin の Stop hook(review gate)は既定 `stopReviewGate: false` | `scripts/lib/state.mjs:23` の実測 |
| `external-runners.md` §9 は「一時ツリーは**取り違えの防止**であって機密の封じ込めではない」と自ら明記している | 同ファイル §9 の限界④ |

## 決定

1. **implementer の外部委託を開く。オプトイン(宣言)は維持し、宣言された環境では implementer の既定を外部にする。**
   - 理由: §7-2 の「行わない」は明示的な検討の結果ではなく範囲設定だった。毎回の指定を不要にしないと「設計は Claude / 実装は Codex」という運用が成立しない
   - 却下: 「`codex` が PATH にあって認証済みなら宣言なしで外部へ」(コードが宣言なしに外部ベンダーへ渡り課金も自動発生する。design §7-2 の「宣言が無ければ外部 CLI を探しに行かない」と正面衝突)/「開けない(現状維持)」(Codex 併用の利点を捨てる)

2. **起動経路は ③ CLI を自前実装する**(`codex exec --sandbox workspace-write`)。
   - 理由: 柔軟さ = 制御の細かさでは CLI が上。`secret_paths` の除外・ログ規約(何を渡したかの記録)・cwd の限定を自前で持てる。配布物が自己完結する(外部プラグイン・MCP 設定に依存しない)
   - 却下: 「① plugin(`codex:codex-rescue`)に乗る」— **実測で「plugin のほうが柔軟」という前提が成立しなかった**。plugin の機能はすべて codex CLI 由来で、plugin 固有の優位は実装済みのジョブ管理(status / result / cancel)だけ。その代わりに `secret_paths` 除外とログ規約を放棄し、plugin 未導入環境では動かなくなる /「② MCP」— 据え置き(解決表 §5 の「有効化手段が未定・未検証」を動かさない)/「③ を正・① を任意の高速路」— 契約が 2 本になり維持コストが倍
   - 派生: ジョブ管理(進捗・中止)は自前実装が要る
   - 派生(**実測で確定**): **差し戻しは毎回 `codex exec` の新規起動で行い、resume 経路は採らない。** `codex exec resume` が `--sandbox` と `--cd` を受け付けないため、判定 3(フラグの確立)と §9(cwd の限定)をどちらも満たせない — `codex review` を既定表から外した理由(design §7-3)とまったく同じ構造。文脈は渡すプロンプト(前回の diff・レビュー指摘)で補う。これは既存のレビュー経路の扱い(`external-runners.md` §8「外部ランナーは会話継続ができない。再レビューは毎回新規起動」)と一致する

3. **実装は作業ツリーで直接行わせる。** 保護は「実行前に clean を要求 + HEAD を記録 + 実行後に diff を提示」。
   - 理由: 依存・ビルド環境がそのまま使えるため implementer 自身がテストを回せる。`external-runners.md` §9 自身が一時ツリーを「機密の封じ込めではない」と認めており、実装で失うのは越境読み取りの防止だけ
   - 却下: 「git worktree で実装 → diff を適用」(worktree 内に依存〈node_modules 等〉が無いと implementer がテストを回せない)/「実行前に自動 WIP commit」(ユーザーの git 状態を書き換える。§7-2 の原則と衝突)

4. **宣言は profile に許す(既定表の名前のみ)。ただし実行の是非はセッション初回に明示承認を取る。**
   - 理由: 決定 1 の「既定外部」は profile 宣言なしでは成立しない。一方で書き込み委託は「リポジトリを書ける者が、外部に書かせる設定を仕込める」状態を作る。**MCP 宣言(design §7-6)が既に持つ「profile に書いてある = 承認済みとは扱わず、実行前に明示承認を取る」形**を踏襲する
   - 却下: 「profile 可・追加承認なし」(`runners` と同じ扱いにすると書き込み委託で信頼モデルの穴が残る)/「profile 不可・セッション引数のみ」(決定 1 が実質成立しない)

5. **走行中に外部 implementer が落ちたときは、内蔵 implementer が自動で引き継ぐ。**
   - 起動前の失敗(未検出・認証切れ・リミット・読み取り専用〈書き込み範囲〉未確立・無応答)は既存契約どおり **エラーとして報告 + 内蔵編成へ縮退して続行**
   - 走行中の失敗では**残った変更を巻き戻さず**、内蔵 implementer が diff を読んで続きを実装する。**引き継いだ事実と残っていた変更は必ず報告する**(サイレント禁止)
   - 理由: 完走を優先する。巻き戻しはユーザーの git 状態を書き換えるため採らない
   - 却下: 「停止して diff を提示し、続行 / 巻き戻し / 引き継ぎをユーザーが選ぶ」(自動で完走しない)/「巻き戻して内蔵が最初から」(外部が使ったトークンが無駄になり、git 状態も書き換える)
   - 残るリスク: 他者の中途半端なコードを引き継ぐ品質リスク。**決定 7 の diff 突合・チェックリスト照合がスコープ縮小を検出する**ことで担保する

6. **契約は `external-runners.md` を拡張して置く。**
   - 判定順序・終了コード・既定表・ログ規約・機密ガードの手順は**レビューと実装で共有**し、差分(サンドボックスモード・機密ガードの適用範囲・失敗時の扱い)だけ節を分ける
   - 理由: 同じ事実を 2 箇所に書かない(§7-2)。既定表と判定順序の二重管理を作らない
   - 却下: 「新規 `external-implementer.md`」(既定表・判定順序・ログ規約が二重管理になる)/「解決表 + スクリプトだけに持たせる」(散文の義務が守られないことは §7-2 が既に指摘している)
   - 派生: ファイルの副題「(レビュアーのホスト外委託)」は実態に合わせて改める

7. **team-lead 側の検証は現行のまま。** diff 突合・チェックリスト照合・品質ゲート再実行・全 reviewer の APPROVED まで反復を、implementer が誰であっても同じに適用する。
   - 理由: implementer の所在は team-lead の検証構造を変えない
   - 却下: 「外部が書いたときだけゲートを厚くする」(構造が変わらないため不要。決定 3 の diff 提示と決定 7 の突合で足りる)

## 暫定

なし。

## 未決(宿題)

> 壁打ち時点の未決 4 件のうち 1 件(`codex exec resume` の可否)は**同日の実測で解決**した。決定 2 の派生に移した。

| 未決 | 何が分かれば決まるか |
|---|---|
| 走行中にリミットへ到達したときの codex の終了コードと出力形式 | 実測。判定 4(疎通プローブ)は起動前の話であり、走行中の到達は未確認。決定 5 の「自動引き継ぎ」の発火条件を書くのに必要 |
| 実装用エントリ(`codex exec --sandbox workspace-write`)のヘルプ照合をどう課すか | 既定表の判定 3 は「読み取り専用フラグの確立」を求める。書き込み用では「書き込み範囲の限定の確立」に読み替える必要があり、照合対象の語を決める必要がある |
| ② MCP バックエンド(`codex mcp-server`)の評価 | 解決表 §5 の「有効化手段が未定」を動かすかどうか。今回は据え置き |

## スコープ外に出た論点

- **レビュワーへの Codex 追加**: v4.0.0 の `--runners=codex` で既に動くため、この壁打ちの対象外(実装不要)
- **review gate(plugin の Stop hook)の採用**: do-task の APPROVED 反復と二重になり使用量を食うため**採らない**。既定 `stopReviewGate: false` のまま使う
- **`codex-plugin-cc` 自体の扱い**: ワークフローには組み込まず、詰まったときの手動の脱出ハッチとして置く(2026-09-17 に user scope で導入済み)
