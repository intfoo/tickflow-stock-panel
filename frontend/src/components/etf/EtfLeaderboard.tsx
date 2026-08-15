import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { keepPreviousData } from '@tanstack/react-query'
import { etfFundApi, type EtfLeaderboardRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface SortCol {
  key: string
  label: string
  type: 'pct' | 'num'
}

const COLUMNS: SortCol[] = [
  { key: 'change_pct', label: '今日', type: 'pct' },
  { key: 'change_pct_5d', label: '5日', type: 'pct' },
  { key: 'change_pct_20d', label: '20日', type: 'pct' },
  { key: 'change_pct_60d', label: '60日', type: 'pct' },
  { key: 'share', label: '份额(亿)', type: 'num' },
  { key: 'inflow_1d', label: '今日流入', type: 'num' },
  { key: 'inflow_5d', label: '5日流入', type: 'num' },
  { key: 'inflow_20d', label: '20日流入', type: 'num' },
  { key: 'inflow_60d', label: '60日流入', type: 'num' },
  { key: 'amount', label: '成交额(亿)', type: 'num' },
  { key: 'market_cap', label: '市值(亿)', type: 'num' },
]

const PAGE_SIZE = 20

function fmtPct(v: number | null): string {
  if (v == null) return '-'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`
}

function fmtNum(v: number | null): string {
  if (v == null) return '-'
  return v.toFixed(2)
}

function pctClass(v: number | null): string {
  if (v == null) return 'text-muted'
  return v >= 0 ? 'text-bull' : 'text-bear'
}

function numClass(v: number | null): string {
  if (v == null) return 'text-muted'
  return v >= 0 ? 'text-bull' : 'text-bear'
}

export function EtfLeaderboard() {
  const [sort, setSort] = useState('amount')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const [broadOnly, setBroadOnly] = useState(false)

  // 拉 instruments 建 symbol→name map
  const instrumentsQuery = useQuery({
    queryKey: QK.etfFundInstruments,
    queryFn: etfFundApi.getInstruments,
    staleTime: 10 * 60_000,
  })

  const nameMap = useMemo(() => {
    const m = new Map<string, string>()
    for (const item of instrumentsQuery.data?.items ?? []) {
      m.set(item.symbol, item.name)
    }
    return m
  }, [instrumentsQuery.data?.items])

  const query = useQuery({
    queryKey: QK.etfFundLeaderboard({ sort, order, page, size: PAGE_SIZE, broad_only: broadOnly }),
    queryFn: () => etfFundApi.getLeaderboard({ sort, order, page, size: PAGE_SIZE, broad_only: broadOnly }),
    placeholderData: keepPreviousData,
  })

  const rows = query.data?.rows ?? []
  const total = query.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const handleSort = (col: string) => {
    if (col === sort) {
      setOrder(o => (o === 'desc' ? 'asc' : 'desc'))
    } else {
      setSort(col)
      setOrder('desc')
      setPage(1)
    }
  }

  const fmtCell = (row: EtfLeaderboardRow, col: SortCol) => {
    const val = row[col.key as keyof EtfLeaderboardRow] as number | null
    if (col.type === 'pct') {
      return (
        <span className={`font-mono ${pctClass(val)}`}>{fmtPct(val)}</span>
      )
    }
    // inflow 列用红绿着色，其他数字列中性
    const isInflow = col.key.startsWith('inflow_')
    const cls = isInflow ? numClass(val) : ''
    return (
      <span className={`font-mono ${cls}`}>{fmtNum(val)}</span>
    )
  }

  return (
    <div className="w-full">
      {/* 工具栏 */}
      <div className="mb-2 flex items-center justify-between gap-3">
        <label className="flex items-center gap-1.5 text-xs text-secondary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={broadOnly}
            onChange={e => { setBroadOnly(e.target.checked); setPage(1) }}
            className="accent-accent"
          />
          只看宽基
        </label>
        {query.data?.data_date && (
          <span className="text-[10px] text-muted">数据日期: {query.data.data_date}</span>
        )}
      </div>

      {/* 表格 */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted">
              <th className="px-2 py-1.5 text-left font-medium whitespace-nowrap">代码</th>
              <th className="px-2 py-1.5 text-left font-medium whitespace-nowrap">名称</th>
              <th className="px-2 py-1.5 text-right font-medium whitespace-nowrap">最新价</th>
              {COLUMNS.map(col => (
                <th
                  key={col.key}
                  onClick={() => handleSort(col.key)}
                  className={`px-2 py-1.5 text-right font-medium whitespace-nowrap cursor-pointer hover:text-foreground transition-colors ${sort === col.key ? 'text-accent' : ''}`}
                >
                  {col.label}
                  {sort === col.key && <span className="ml-0.5">{order === 'desc' ? '▼' : '▲'}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {query.isLoading && (
              <tr><td colSpan={COLUMNS.length + 3} className="py-8 text-center text-muted">加载中…</td></tr>
            )}
            {!query.isLoading && rows.length === 0 && (
              <tr><td colSpan={COLUMNS.length + 3} className="py-8 text-center text-muted">暂无数据</td></tr>
            )}
            {rows.map(row => (
              <tr key={row.symbol} className="border-b border-border/50 hover:bg-elevated/40 transition-colors">
                <td className="px-2 py-1.5 font-mono text-secondary whitespace-nowrap">
                  {row.is_broad && <span className="mr-1 inline-block rounded bg-accent/15 px-1 text-[9px] text-accent">宽</span>}
                  {row.symbol}
                </td>
                <td className="px-2 py-1.5 text-secondary whitespace-nowrap">{nameMap.get(row.symbol) ?? '-'}</td>
                <td className="px-2 py-1.5 text-right font-mono text-foreground">{row.price != null ? row.price.toFixed(3) : '-'}</td>
                {COLUMNS.map(col => (
                  <td key={col.key} className="px-2 py-1.5 text-right whitespace-nowrap">
                    {fmtCell(row, col)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 分页 */}
      <div className="mt-2 flex items-center justify-between gap-3 text-xs text-muted">
        <span>共 {total} 只</span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded border border-border px-2 py-0.5 text-secondary hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
          >
            上一页
          </button>
          <span className="font-mono">{page} / {totalPages}</span>
          <button
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="rounded border border-border px-2 py-0.5 text-secondary hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  )
}
