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
- **判定**: 返却されるモデルが機密辞書のフィールドを含む → **重大**。含まない → 過剰露出として「中」(B5 と統合可。候補モードでは行わない — 下の「候補モードの識別子と観点群」)
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

## 候補モードの識別子と観点群(指摘キー)

候補モード(`--candidates`。手順・正規化・既知の判定の正本は [../../create-task/references/candidate-mode.md](../../create-task/references/candidate-mode.md))で、候補の指摘キー `data-audit:{観点群}:{種類}:{識別子}` を決める定義。同じ指摘の同一性を、行番号に頼らずに表す。

### 観点群と候補の単位

- 観点群は Phase 2 の 4 群: `exposure`(`scan-exposure` の担当)・`authz`(`scan-authz`)・`db`(`scan-db`)・`efficiency`(`scan-efficiency`)。観点 ID から群への対応は SKILL.md の Phase 2 の表が正本
- **1 候補 = 1 つの(観点群・識別子)**。同じ識別子で同じ群の観点(例 B2 と B3)は 1 候補にまとめ、観点 ID の列は候補の「出典」に書く
- 識別子をまたいで統合しない(同じ根本原因の候補どうしは、本文で互いを挙げる)
- 観点群をまたぐ統合(B1 の「B5 と統合可」)は行わない。見つけた観点の群でキーにする
- D2 の「サーバー経由のみなら B2 / B3 に還元」は判定の規則なので、そのまま使う(サーバー経由だけの構成では D2 を候補にせず、認可の問題は B2・B3 の候補として出る)

### 識別子の場所

- 境界を越える入口(route・handler・action・rpc・component)があれば、それにする
- 入口の無い定義(model・column・index・env・storage)は、その定義にする
- どちらでもないときだけ file にする
- 補助の関数・サービス層の中の指摘も、それを呼ぶ入口の識別子にする。呼ぶ入口が複数なら、入口ごとに 1 候補にして、本文で互いを挙げる
- 振り分けが入れ子のとき(前置きの mount・ルーターへの受け渡し・連ねた呼び出し)は、欠陥のある関数(か、それを呼ぶ関数)を直接登録・振り分けた、いちばん内側の段を入口にする。外側の段(ルーターへ渡すだけの振り分け・mount)は入口にしない

### 種類と識別子

| 種類 | 識別子 | 使うとき |
|---|---|---|
| route | `{宣言のあるファイル}#{登録先}:{METHOD} {宣言の字面}` | パスの字面と関数を、1 つの登録にするとき(下の「route の形」) |
| handler | `{path}#{関数名}`(振り分けた先の関数) | それ以外の振り分け(`if` の比較・正規表現・辞書の引き・呼び出しを含まない表) |
| action | `{path}#{export 名}` | server action |
| rpc | `{ルーター}.{手続き}` | RPC の手続き |
| model・column | `{モデル}[.{カラム}]` | モデル・カラムの定義 |
| index | `{テーブル}({カラムを辞書順に,区切り})` | インデックス |
| component | `{path}#{名}` | コンポーネント |
| env | `{変数名}` | 環境変数 |
| storage | `{種別}:{キー}` | ブラウザのストレージ・cookie |
| file | `{path}` | ほかに当たらないとき |

- クラスのメソッドは、`{関数名}`・`{export 名}` の所を `{Class.method}` と書く
- パスは管理ルート相対(正規化は candidate-mode.md)

**route の形**(API をどこで定義したか〈フレームワークか、リポジトリの中の自作か〉は問わない)

- `<対象>.<メソッド>(<パスの字面>, <関数>)` の呼び出し(`app.get('/x', fn)`・`self.router.get('/:id', self.show)` など)
- `@<対象>.route(<パスの字面>)` のデコレータ
- 各要素がパスの字面と関数を受け取る呼び出しである宣言(`urlpatterns = [path(…), re_path(…)]` など)
- 連ねた呼び出し(`app.route('/x').get(fn)`)。パスの字面と関数を 1 つの登録とみなす(登録先は `app`、METHOD は `GET`)
- ファイルの置き場によるルーティング(`app/**/route.ts`・`pages/api/**` など)

呼び出しを含まない表(`ROUTES = [('/x', fn), …]` のようなタプル・辞書のリテラルを、ループと比較で振り分けるもの)は route ではなく handler にする。

**route の各部**

- 登録先: ルーティングの API を呼んだ対象・デコレータの対象の字面(`app`・`usersRouter`・`bp` など)を、定義のスコープで修飾したもの
  - 定義のスコープは、囲むクラス・関数の名を `.` でつないだもの(モジュールの直下なら無し)。例 `Users.register.self.router`
  - 名の無い関数の段は、代入先の変数名があればそれにする。無ければ(default export・`module.exports`・引数の callback)、その段を省く
  - 呼び出しの対象を持たない宣言(`urlpatterns = [path(…)]`)では、代入先の変数名
  - ファイルの置き場によるルーティングでは `file`。宣言のあるファイル = そのファイル、字面 = 置き場から決まるパス
- METHOD: メソッドを絞らない宣言(`path()`・`app.use`・`app.all`)なら `ANY`。複数なら、大文字を辞書順に `,` でつなぐ
- 字面は書かれたとおりにする。パラメータの書き方(`:id`・`[id]`・`{id}`)も揃えない

例: `data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id` / `data-audit:exposure:handler:app.py#login`

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

**エージェント単位で省く**。`--quick` では `scan-efficiency`(取得効率スキャン)を起動せず、起動した 3 体(`scan-exposure` / `scan-authz` / `scan-db`)は**担当観点を全部見る**。起動しなかったエージェントの担当観点は「未監査」に明記する。**どの観点をどのエージェントが担当するかは SKILL.md の表が正本**(ここで再掲しない)。
