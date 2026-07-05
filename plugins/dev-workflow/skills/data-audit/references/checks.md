# data-audit 検査カタログ(層 × 観点 × 検出方法)

data-audit の Phase 1(境界の列挙)・Phase 2(観点別スキャン)・Phase 3(深刻度確定)が参照する。
シグネチャ例は**網羅ではなくヒント**。実際のスタックは Phase 0 のマニフェスト検出で判定し、未知のスタックでは「HTTP ルーティング定義」「シリアライズ点」「スキーマ定義」に相当する構造を探す。

## 機密フィールドの既定辞書(フォールバック)

profile の `audit.sensitive_fields` が無い場合の既定。カラム名・プロパティ名との**部分一致(大文字小文字・snake/camel 無視)**で判定する。

| クラス | パターン |
|---|---|
| 認証情報 | password, passwd, secret, token, credential, privateKey, apiKey, accessKey, refreshToken, sessionId, otp, hash, salt |
| 個人識別 | ssn, myNumber, passport, licenseNumber, birthDate, gender |
| 連絡先 | phone, address, postalCode(email は文脈依存 → 深刻度は 1 段下げ) |
| 決済 | cardNumber, cvv, cvc, bankAccount, iban |
| 内部管理 | isAdmin, role, internalNote, stripeCustomerId 等の外部サービス ID(露出時は「高」扱い) |

スキーマ実測でこの辞書に一致したカラムは「実在機密」として昇格し、露出系観点(B1 / F1 / D1)の主対象にする。

## 境界の検出(層の存在判定と列挙)

### バックエンド境界
| 種類 | 検出シグネチャ例 |
|---|---|
| server actions | `"use server"` ディレクティブ(ファイル先頭 or 関数先頭) |
| API ルート | `app/**/route.{ts,js}` / `pages/api/**`(Next.js)、`routes/*.php`(Laravel)、`urls.py`(Django)、`.get(` `.post(` 等のルーター呼び出し(Hono / Express / Fastify)、tRPC の `router({`、GraphQL の resolvers / typeDefs |
| RPC・他 | gRPC の .proto、WebSocket ハンドラ、キュー・cron ハンドラ(外部入力を受けるもの) |

### DB 境界
| 種類 | 検出シグネチャ例 |
|---|---|
| スキーマ | `schema.prisma`、drizzle のテーブル定義、`schema.zmodel`(ZenStack)、`migrations/`、`supabase/`、Eloquent / Django の Model クラス |
| クエリ呼び出し点 | ORM クライアントのメソッド呼び出し(findMany / findUnique / select / query / raw SQL) |

### フロント境界
| 種類 | 検出シグネチャ例 |
|---|---|
| クライアントコード | `"use client"`、components/ 配下、SPA のエントリポイント |
| 公開環境変数 | `NEXT_PUBLIC_` / `VITE_` / `REACT_APP_` / `EXPO_PUBLIC_` プレフィックス(**値は読まない**。変数名と参照箇所のみ) |
| server → client 受け渡し | server component から client component への props、`getServerSideProps` の戻り値、HTML への埋め込み(`__NEXT_DATA__` 相当) |

## 観点カタログ

### B1 レスポンス形状(露出)— 重大
- **何を探すか**: ORM の取得結果を**加工なしで**レスポンス(`Response.json` / `res.json` / server action の return / API の戻り値)に渡している経路。select / omit / serializer(Resource・Serializer・DTO・zod の `.pick()`)を通らずに返る形
- **判定**: 返却されるモデルが機密辞書のフィールドを含む → **重大**。含まない → 過剰露出として「中」(B5 と統合可)
- シグネチャ例: `findMany()`(select 無し)→ そのまま return、Eloquent `->get()` → `->toJson()`、Django Model → `JsonResponse(model.__dict__)`

### B2 認証・認可の欠如 — 重大
- **何を探すか**: Phase 1 の境界一覧の**各エントリ**について、認証チェック(セッション / トークン検証、ミドルウェア適用、server action 冒頭の認可関数)が通るかを突合する。ミドルウェアの除外パス(matcher / except)設定も確認
- **判定**: 認可が見つからない境界のうち、profile の `audit.public_boundaries` に載っていないもの → **重大**(公開が意図なら profile への追記を提案)
- 注意: server actions は「フォームから呼ばれるだけ」でも公開エンドポイントである(直接呼び出し可能)

### B3 IDOR(不適切な直接オブジェクト参照)— 重大
- **何を探すか**: params / body の `id` をそのまま where 条件に使い、**所有者・テナント条件が付いていない**取得・更新・削除
- **判定**: 認証済みでも他人のリソースに触れる → **重大**。マルチテナントでテナント条件欠如 → **重大**

### B4 入力検証・mass assignment — 高
- **何を探すか**: リクエスト body を**丸ごと** create / update に渡す形(`...body` スプレッド、`$request->all()`)。スキーマ検証(zod / valibot / FormRequest / serializer)無しで DB クエリに到達する経路
- **判定**: 機密・権限カラム(role, isAdmin 等)が書き込み可能 → **重大**に昇格。それ以外 → 高

### B5 過剰取得 — 中〜高
- **何を探すか**: ①ループ内 await クエリ(N+1)②`take` / `limit` / pagination の無い一覧取得 ③不要に深い include / join 連鎖 ④一覧系 API での全カラム取得
- **判定**: 一覧・無限スクロール等の高頻度経路 → 高。管理画面等の低頻度経路 → 中

### B6 エラー・ログ露出 — 高
- **何を探すか**: ①catch した error オブジェクトをそのままレスポンスへ(スタックトレース・SQL・内部パス)②リクエスト body・機密フィールドの console.log / logger 出力
- **判定**: 本番経路でスタックトレースが外部に出る → 高。ログへの機密出力 → 高(ログ基盤経由の二次漏えい)

### F1 機密のクライアント混入 — 重大
- **何を探すか**: ①公開プレフィックス環境変数の**用途**(変数名から機密が疑われるもの。値は読まない)②クライアントコード内のシークレット文字列リテラル ③server component / loader から client component へ**モデル丸ごと props 渡し**(シリアライズされて HTML / ペイロードに乗る)
- **判定**: 機密辞書該当フィールドがクライアントに到達 → **重大**

### F2 ストレージ保存 — 高
- **何を探すか**: localStorage / sessionStorage / 平文 cookie(`httpOnly` 無し)への token・個人情報の保存
- **判定**: 認証トークンの localStorage 保存 → 高(XSS 時の持ち出し)。表示用キャッシュの個人情報 → 中

### F3 UI だけの認可 — 高
- **何を探すか**: フロントの role / 権限分岐で**隠しているだけ**の操作。対応するバックエンド境界(B2 の結果)に同等の認可が無いか突合する
- **判定**: バックエンド側に認可が無い → B2 の重大として計上(F3 は突合の入口)

### F4 過剰保持 — 中
- **何を探すか**: API レスポンス全体を state / store に保存(必要フィールドの選択なし)。DevTools から全データが見える+メモリ浪費
- **判定**: 機密辞書該当を含む → 高。含まない → 中

### D1 機密カラムの保護 — 重大
- **何を探すか**: スキーマ内の機密辞書一致カラムについて、**書き込み経路**がハッシュ化(bcrypt / argon2)・暗号化を通るか。平文保存の痕跡
- **判定**: パスワード・トークンの平文保存 → **重大**

### D2 行レベルのアクセス制御 — 重大
- **何を探すか**: RLS(migrations 内の `enable row level security` / `create policy`)の有効化状況、ZenStack の `@@allow` / `@@deny` 定義の無いモデル、マルチテナントでの tenant 条件の一貫性
- **判定**: クライアントから直接 DB に触れる構成(BaaS)で RLS 無効テーブル → **重大**。サーバー経由のみなら B2 / B3 に還元して評価

### D3 論理削除の漏れ — 高
- **何を探すか**: `deletedAt` / `isDeleted` カラムがあるのに、フィルタを通らないクエリ経路(ORM のグローバルフィルタ・default scope の有無も確認)
- **判定**: 削除済みデータが一覧・詳細 API から見える → 高

### D4 インデックス欠如 — 中
- **何を探すか**: コード中の where / orderBy / 検索で頻出するカラムと、スキーマの index 定義(`@@index` / `CREATE INDEX`)の突合
- **判定**: 高頻度経路の全件スキャン相当 → 中(データ量見込みを添えて報告)

### D5 巨大カラムの常時取得 — 中
- **何を探すか**: text / blob / json 型の大きいカラムが、一覧系クエリでも select されている(select 指定が無く全カラム取得になっている場合を含む)
- **判定**: 一覧で本文・バイナリを毎回取得 → 中(B5 と統合可)

## 深刻度・確度の基準

| 深刻度 | 基準 |
|---|---|
| 重大 | 機密が実際に外部へ出る / 認可なしで他人のデータに触れる(即時対応を推奨) |
| 高 | 条件付きで漏える・攻撃の足がかりになる・運用規模で確実に顕在化する性能問題 |
| 中 | 無駄・将来リスク(即時被害なし。改善提案として提示) |

| 確度 | 基準 |
|---|---|
| 確実 | 実コードで経路を追い切り、問題の成立を確認した |
| 要確認 | 実行時条件・インフラ設定・仕様意図に依存する(ユーザーへの質問として提示し、断定しない) |

## --quick の対象観点

B1 / B2 / B3 / F1 / D1 / D2(重大クラスのみ)。B4 / B5 / B6 / F2 / F3 / F4 / D3 / D4 / D5 は省略し「未監査」に明記する。
