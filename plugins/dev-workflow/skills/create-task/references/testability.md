# テスタビリティ改善フレームワーク(詳細)

SKILL.md「テスタビリティ改善(このスキルの核)」の詳細版。思想は「テストを書く」のではなく「**テストが書きやすいコードにする**」。テスト未導入のプロジェクトでも、将来テストを足せる構造へ寄せることが目的。

before/after は代表 3 手法(純粋関数抽出・副作用分離・Container/Presentational)を示す。コードは説明用の最小例であり、命名・配置・import 規約は各プロジェクトの実コードに合わせること。

## 3 層分離原則(React 系)

| 層 | 責務 | 置き場所の例 |
|---|---|---|
| Component | 何を表示するか(JSX + props 受け渡し) | components/ |
| Hook | 状態と副作用(useState / useEffect / データ取得) | hooks/ |
| lib | 計算と変換(純粋関数) | lib/ |

150 行超のコンポーネントで JSX の前に 50 行以上のロジックがある、データ取得 + ロジック + UI が 1 ファイルに混在している、といった箇所が 3 層分離の主対象。

## 1. 純粋関数抽出(優先度: 高)

コンポーネントやフックに埋め込まれた計算・変換・判定を、引数と戻り値だけで動く純粋関数として lib へ出す。最もテストが書きやすく、効果が大きい。

**抽出対象の検出**

- 文字列フォーマット・ラベル変換(ステータス → 表示名、日付、金額 等)
- 数値計算(進捗率、集計、割合、閾値判定)
- バリデーション(入力値の検証)
- データ変換(API レスポンス → 表示用データ)
- フィルタ・ソート条件(配列操作ロジック)
- 複合条件(3 条件以上の &&/|| チェーン、三項連鎖、switch)

**Before**

```tsx
function ContractCard({ contract }) {
  const statusLabel = contract.status === "active" ? "稼働中"
    : contract.status === "paused" ? "一時停止"
    : contract.status === "draft" ? "下書き" : "不明"
  const progress = Math.round((contract.minted / contract.maxSupply) * 100)
  return <div>{statusLabel} {progress}%</div>
}
```

**After**

```tsx
// lib/contracts.ts(純粋関数 → 単体テスト容易)
export function getStatusLabel(status: ContractStatus): string {
  const labels: Record<ContractStatus, string> = {
    active: "稼働中", paused: "一時停止", draft: "下書き",
  }
  return labels[status] ?? "不明"
}
export function calcProgress(minted: number, maxSupply: number): number {
  if (maxSupply === 0) return 0
  return Math.round((minted / maxSupply) * 100)
}

// components/contract-card.tsx(表示に専念)
function ContractCard({ contract }) {
  return (
    <div>
      {getStatusLabel(contract.status)} {calcProgress(contract.minted, contract.maxSupply)}%
    </div>
  )
}
```

## 2. 副作用分離(優先度: 中)

コンポーネントが直接 API を呼ぶ・useEffect 内で複雑な処理をしている場合、副作用を Hook に切り出し、コンポーネントは「何を表示するか」に集中させる。純粋なデータ処理は lib へ。

**Before**

```tsx
function DashboardContent() {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    (async () => {
      setLoading(true)
      const res = await fetch("/api/stats")
      setStats(await res.json())
      setLoading(false)
    })()
  }, [])
  // フィルタ・ソートもここに...
  return <div>...</div>
}
```

**After**

```tsx
// hooks/use-dashboard-stats.ts(副作用を隔離)
export function useDashboardStats() {
  return useQuery({ queryKey: ["dashboard-stats"], queryFn: fetchDashboardStats })
}
// lib/dashboard.ts(純粋なデータ処理)
export function filterStats(stats: Stats, f: Filter): Stats { /* ... */ }

// components/dashboard-content.tsx(表示のみ)
function DashboardContent() {
  const { data, isLoading } = useDashboardStats()
  return <div>...</div>
}
```

## 3. Container / Presentational 分離(優先度: 低)

表示用コンポーネントは必要データを props で受け取り、内部でデータ取得しない。任意のデータでレンダリングでき、スナップショット/Storybook 検証がしやすくなる。全コンポーネントに強制せず、複雑なデータ取得を持つ・同じ表示を別データソースで再利用したい場合に適用する。

**Before / After**

```tsx
// Before: 内部でフック呼び出し → テスト時にモックが必要
function ContractList() {
  const { contracts, isLoading } = useContracts()
  return <div>{contracts.map(c => <ContractCard key={c.id} contract={c} />)}</div>
}

// After
// Presentational: props で受け取る → テスト容易
function ContractListView({ contracts, isLoading }: ContractListViewProps) {
  return <div>{contracts.map(c => <ContractCard key={c.id} contract={c} />)}</div>
}
// Container: データ取得を担当
function ContractList() {
  const { contracts, isLoading } = useContracts()
  return <ContractListView contracts={contracts} isLoading={isLoading} />
}
```

## 4. 型の厳密化(優先度: 高)

any や曖昧な型は、テスト時にどんな値を渡すべきか不明確になる。厳密な型はテストケースの設計ガイドになる。

- `any` → 具体型に置換
- enum 的に使う `string` → ユニオン型
- `object` → 具体的なインターフェース
- optional の乱用 → 必須/任意を明確化

```ts
// Before
type ContractStatus = string
function getLabel(status: any): string { /* ... */ }
// After
type ContractStatus = "active" | "paused" | "draft" | "expired"
function getLabel(status: ContractStatus): string { /* ... */ }
// → 全ステータスを網羅するテストケースが型から自明になる
```

動的型付け言語では、型注釈の代わりに「入力バリデーション境界の明確化」に読み替える。境界で検証し、内側は正規化済みの値だけを扱う。

## 5. 定数の外部化(優先度: 中)

マジックナンバー・文字列を定数へ抽出する。テスト時に期待値として import でき、値変更も 1 箇所で済む。

```ts
// Before(ハードコード)
if (tokens.length >= 100) { /* ... */ }
const PAGE_SIZE = 20
// After: lib/constants/tokens.ts
export const TOKENS_MAX_DISPLAY = 100
export const TOKENS_PAGE_SIZE = 20
```

## 6. 優先度(着手順)

| 優先度 | 対象 | 理由 |
|---|---|---|
| 高 | ドメインロジック(計算・変換・バリデーション)の純粋関数化 | テストが最も書きやすく効果が大きい |
| 高 | 緩い型(any)の排除 | 型安全性がテストケース設計の土台になる |
| 中 | 副作用の分離(Hook 境界の明確化) | Hook 単体テストが可能になる |
| 中 | 定数の外部化 | 変更影響が局所化し、期待値に使える |
| 低 | Container / Presentational 分離 | Storybook 等の導入時に効果を発揮 |

## バックエンド・非 React での読み替え

3 層分離は React の型。バックエンドや非 React では「純粋なビジネスロジック(計算・判定・変換)」を I/O(DB・HTTP・時刻・乱数)から切り離すことが本質。ドメインロジックを副作用のない関数に寄せ、DB アクセスや外部呼び出しはリポジトリ/ゲートウェイに隔離すると、ドメインロジックを単体テストできる。定数の外部化・型の厳密化は言語・レイヤーを問わず有効。
